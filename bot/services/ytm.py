from __future__ import annotations
import logging
import time
import threading
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot import Bot

from bot.config.models import YtmModel
from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service as _Service
from bot.services.youtube_bridge import YouTubeBridge
from bot import errors


class YtmService(_Service):
    def __init__(self, bot: Bot, config: YtmModel):
        self.bot = bot
        self.config = config
        self.name = "ytm"
        self.hostnames = ["music.youtube.com", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"]
        self.is_enabled = self.config.enabled
        self.error_message = ""
        self.warning_message = ""
        self.help = ""
        self.hidden = False
        self.yt_config = bot.config.services.yt
        self._warm_lock = threading.Lock()
        self._is_warmed = False
        
    def _fetch_and_queue_autoplay(self, video_id: str, original_url: str):
        """Background task to fetch Watch Playlist and add to queue."""
        try:
            logging.info(f"[YTM] Starting background Autoplay fetch for video_id={video_id}")
            start_time = time.perf_counter()
            
            new_tracks = self._get_recommendation_tracks(video_id, 50)
            
            if new_tracks:
                # Add to bot queue safely
                self.bot.player.track_list.extend(new_tracks)
                
                duration = (time.perf_counter() - start_time) * 1000
                logging.info(f"[YTM] Background Autoplay fetch added {len(new_tracks)} tracks in {duration:.2f}ms")
            else:
                logging.info("[YTM] Background Autoplay fetch found no new tracks.")
                
        except Exception as e:
            logging.error(f"[YTM] Background Autoplay fetch failed: {e}")

    def initialize(self):
        # Shares the YouTube account: the bridge keys credentials on bot_id, so
        # signing in once with yl covers both yt and ytm for this bot.
        self._bridge = YouTubeBridge(client="YTMUSIC")

        # Run pre-warming in a background thread so the bot connects to TeamTalk immediately
        threading.Thread(target=self._pre_warm, daemon=True, name="YTM_PreWarm").start()

    def _pre_warm(self):
        if self._is_warmed:
            return
        with self._warm_lock:
            if self._is_warmed:
                return
            self._bridge.wait_ready(timeout=5.0)
            for attempt in range(1, 4):
                try:
                    logging.info(f"YTM Service pre-warming (attempt {attempt}/3)...")
                    self._bridge.search("music", 1, mode="music")
                    self._is_warmed = True
                    logging.info("YTM Service pre-warming finished successfully.")
                    return
                except Exception as e:
                    if attempt < 3:
                        logging.warning(f"YTM Pre-warming attempt {attempt} failed: {e}. Retrying in 0.5 seconds...")
                        time.sleep(0.5)
                    else:
                        logging.error(f"YTM Pre-warming failed after 3 attempts: {e}")

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        start_time = time.perf_counter()
        info = track.extra_info or {}
        video_id = info.get("videoId") or info.get("id")
        source_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else track.url
        self._bridge.download(source_url, file_path, video=video)
        duration = (time.perf_counter() - start_time) * 1000
        logging.info(f"YTM Download finished in {duration:.2f}ms for {track.name}")

    def get(
        self,
        url: str,
        extra_info: Optional[Dict[str, Any]] = None,
        process: bool = False,
    ) -> List[Track]:
        start_time = time.perf_counter()
        if not (url or extra_info):
            raise errors.InvalidArgumentError()

        # Stream resolution is handled by the persistent YouTube.js bridge.
        if process:
            info = dict(extra_info or {})
            video_id = info.get("videoId") or info.get("id")
            resolved = self._bridge.resolve(url=url if not video_id else "", video_id=video_id or "")
            stream = {**info, **resolved}
            stream_url = resolved.get("url")
            if not stream_url:
                raise errors.ServiceError("YouTube.js returned no stream URL")

            title = resolved.get("title") or info.get("title") or self.bot.translator.translate("Unknown")
            uploader = resolved.get("uploader")
            if uploader:
                title += f" - {uploader}"

            current_video_id = resolved.get("id") or video_id
            if current_video_id and not getattr(self.bot.player, "is_playlist", False):
                try:
                    remaining = len(self.bot.player.track_list) - 1 - self.bot.player.track_index
                    if remaining <= 4:
                        self._fetch_autoplay_async(current_video_id)
                except Exception as e:
                    logging.debug(f"[YTM] Trace bot player state error: {e}")

            duration = (time.perf_counter() - start_time) * 1000
            logging.info(f"YTM Get (Process/YouTube.js) finished in {duration:.2f}ms for {title}")
            return [
                Track(
                    service=self.name,
                    name=title,
                    url=stream_url,
                    type=TrackType.Live if resolved.get("is_live") else TrackType.Default,
                    format="mp3",
                    extra_info=stream,
                    extracted_at=time.perf_counter(),
                )
            ]

        # If process=False, we are adding to queue (The "Radio" logic)
        if extra_info and not url:
             t_title = extra_info.get("title", "")
             t_vid = extra_info.get("videoId") or extra_info.get("id")
             t_url = f"https://www.youtube.com/watch?v={t_vid}" if t_vid else ""
             return [Track(service=self.name, url=t_url, name=t_title, type=TrackType.Dynamic, extra_info=extra_info)]

        lower_url = url.lower() if url else ""
        if (
            "list=" in lower_url
            or "/channel/" in lower_url
            or "/@" in lower_url
            or "/c/" in lower_url
            or "/user/" in lower_url
            or "/playlist" in lower_url
            or (url and url.startswith("UC"))
        ):
            playlist = self._bridge.playlist(url)
            tracks: List[Track] = []
            for entry in playlist.get("entries", []):
                entry["playlist_title"] = playlist.get("title")
                entry["playlist_uploader"] = playlist.get("uploader")
                tracks.append(
                    Track(
                        service=self.name,
                        url=entry.get("webpage_url", ""),
                        name=entry.get("title", ""),
                        extra_info=entry,
                        type=TrackType.Dynamic,
                    )
                )
            duration = (time.perf_counter() - start_time) * 1000
            logging.info(f"YTM Get (Playlist/Channel) finished in {duration:.2f}ms for {url}")
            return tracks

        video_id = None
        if extra_info and "videoId" in extra_info:
             video_id = extra_info["videoId"]
        elif url:
             if "v=" in url:
                  video_id = url.split("v=")[1].split("&")[0]
             elif "youtu.be" in url:
                  video_id = url.split("/")[-1]
        
        track_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
        track_name = (extra_info.get("title") if extra_info else "") or ""
        track = Track(
            service=self.name,
            url=track_url,
            name=track_name,
            type=TrackType.Dynamic,
            extra_info=extra_info or ({"videoId": video_id} if video_id else None),
        )
        if video_id and not getattr(self.bot.player, "is_playlist", False):
            self._fetch_autoplay_async(video_id)

        duration = (time.perf_counter() - start_time) * 1000
        logging.info(f"YTM Get (Fast Dynamic) finished in {duration:.2f}ms for {track_url}")
        return [track]

    def _fetch_autoplay_async(self, video_id: str) -> None:
         threading.Thread(target=self._fetch_autoplay_sync, args=(video_id,), daemon=True, name=f"Autoplay_{video_id}").start()

    def _fetch_autoplay_sync(self, video_id: str) -> bool:
         try:
              logging.info(f"[YTM] Fetching continuous recommendations for {video_id}")
              recommendation_tracks = self._get_recommendation_tracks(video_id, 50)

              if recommendation_tracks:
                   current_idx = self.bot.player.track_index
                   recent_tracks = self.bot.player.track_list[max(0, current_idx - 15):]
                   existing_ids = set()
                   for t in recent_tracks:
                        t_info = getattr(t, "extra_info", None) or {}
                        vid = t_info.get("videoId") or t_info.get("id") or t_info.get("contentId")
                        if vid:
                             existing_ids.add(vid)
                   existing_ids.add(video_id)

                   new_tracks = []
                   for track in recommendation_tracks:
                        t_info = track.extra_info or {}
                        v_id = t_info.get('videoId')
                        if not v_id or v_id in existing_ids:
                             continue
                        existing_ids.add(v_id)
                        new_tracks.append(track)
                        if len(new_tracks) >= 15:
                             break
                   
                   if new_tracks:
                        logging.info(f"[YTM] Adding {len(new_tracks)} continuous recommendations to track list (total: {len(self.bot.player.track_list) + len(new_tracks)})")
                        self.bot.player.track_list.extend(new_tracks)
                        if hasattr(self.bot.player, "_schedule_prefetch"):
                             self.bot.player._schedule_prefetch()
                        return True
                   else:
                        logging.info(f"[YTM] No new unique recommendations found for video_id {video_id}")
         except Exception as e:
              logging.error(f"[YTM] Autoplay fetch failed: {e}")
         return False

    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        if limit is None:
            limit = self.config.search_results
        start_time = time.perf_counter()
        results = self._bridge.search(query, limit, mode="music").get("entries", [])
        if not results:
             raise errors.NothingFoundError("")
        
        results = results[:limit]
        
        duration = (time.perf_counter() - start_time) * 1000
        logging.info(f"YTM Search (Fast) finished in {duration:.2f}ms for query: {query}")
        tracks = self._create_tracks_from_results(results)
        player = getattr(self.bot, "player", None)
        if len(tracks) == 1 and not getattr(player, "is_playlist", False):
             vid = results[0].get("videoId") or results[0].get("id")
             if vid:
                  self._fetch_autoplay_async(vid)
        return tracks

    def _create_tracks_from_results(self, results: List[Dict[str, Any]]) -> List[Track]:
        tracks: List[Track] = []
        for item in results:
             t_title = item.get("title")
             artists = item.get("artists") or []
             t_artist = ", ".join(
                 artist.get("name", "")
                 for artist in artists
                 if isinstance(artist, dict) and artist.get("name")
             ) or item.get("uploader", "")
             
             full_title = f"{t_title} - {t_artist}" if t_artist else t_title
             t_video_id = item.get("videoId")
             t_url = f"https://www.youtube.com/watch?v={t_video_id}"
             stream_url = item.get("stream_url")
             
             if stream_url:
                 track = Track(
                     service=self.name,
                     url=stream_url,
                     name=full_title,
                     format="mp3",
                     type=TrackType.Live if item.get("is_live") else TrackType.Default,
                     extra_info=item,
                     extracted_at=time.perf_counter(),
                 )
                 track._is_fetched = True
             else:
                 track = Track(
                     service=self.name,
                     url=t_url,
                     name=full_title,
                     type=TrackType.Dynamic,
                     extra_info=item,
                 )
             tracks.append(track)
        return tracks

    def _get_recommendation_tracks(self, video_id: str, limit: int) -> List[Track]:
        entries = self._bridge.recommendations(video_id, limit).get("entries", [])
        return self._create_tracks_from_results(entries)
