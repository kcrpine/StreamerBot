from __future__ import annotations
import http.cookiejar
import logging
import time
import os
import threading
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot import Bot

from bot.config.models import YtModel

from bot.player.enums import TrackType
from bot.player.track import Track
from bot.services import Service as _Service
from bot.services.youtube_bridge import YouTubeBridge
from bot import errors


class YtService(_Service):
    def __init__(self, bot: Bot, config: YtModel):
        self.bot = bot
        self.config = config
        self.name = "yt"
        self.hostnames = ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"]
        self.is_enabled = self.config.enabled
        self.error_message = ""
        self.warning_message = ""
        self.help = ""
        self.hidden = False
        self._cookie_lock = threading.Lock()
        self._warm_lock = threading.Lock()
        self._is_warmed = False
        self._max_retries = 2

    def initialize(self):
        self._bridge = YouTubeBridge(client="YTMUSIC")

        # Sign-in is optional. Anonymous search and playback still work, exactly
        # as the cookie-less path always did; a signed-in session additionally
        # unlocks age-restricted videos and the account's own recommendations.
        if self._bridge.is_signed_in():
            logging.info("YT Service: signed in to YouTube.")
        else:
            logging.info(
                "YT Service: not signed in to YouTube. Public videos still play; "
                "use the yl command to sign in for age-restricted content."
            )

        # Run pre-warming in a background thread so the bot connects to TeamTalk immediately
        threading.Thread(target=self._pre_warm, daemon=True, name="YT_PreWarm").start()

    def _pre_warm(self):
        if self._is_warmed:
            return
        with self._warm_lock:
            if self._is_warmed:
                return
            self._bridge.wait_ready(timeout=5.0)
            for attempt in range(1, 4):
                try:
                    logging.info(f"YT Service pre-warming (attempt {attempt}/3)...")
                    self.search("test", limit=1)
                    self._bridge.resolve(video_id="48Lrud3Bxpc")
                    self._is_warmed = True
                    logging.info("YT Service pre-warming finished successfully.")
                    return
                except Exception as e:
                    if attempt < 3:
                        logging.warning(f"YT Pre-warming attempt {attempt} failed: {e}. Retrying in 0.5 seconds...")
                        time.sleep(0.5)
                    else:
                        logging.error(f"YT Pre-warming failed after 3 attempts: {e}")

    def download(self, track: Track, file_path: str, video: bool = False) -> None:
        start_time = time.perf_counter()
        info = track.extra_info or {}
        video_id = info.get("videoId") or info.get("id") or info.get("contentId")
        source_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else track.url
        self._bridge.download(source_url, file_path, video=video)
        duration = (time.perf_counter() - start_time) * 1000
        logging.info(f"YT Download finished in {duration:.2f}ms for {track.name}")

    def get(
        self,
        url: str,
        extra_info: Optional[Dict[str, Any]] = None,
        process: bool = False,
    ) -> List[Track]:
        start_time = time.perf_counter()
        if not (url or extra_info):
            raise errors.InvalidArgumentError()
        
        last_error = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                wait_time = 2 ** attempt
                logging.warning(f"YT Get: Retry {attempt}/{self._max_retries} for '{url}' after {wait_time}s delay")
                time.sleep(wait_time)

            try:
                return self._get_inner(url, extra_info, process, start_time)
            except errors.ServiceError as e:
                last_error = e
                error_msg = str(e)
                is_auth_error = "Sign in to confirm" in error_msg or "cookies" in error_msg.lower()
                if not is_auth_error or attempt >= self._max_retries:
                    raise
                logging.warning(f"YT Get: Auth-related error, will retry: {error_msg[:100]}")
        
        raise last_error or errors.ServiceError("Max retries exceeded")

    def _get_inner(
        self,
        url: str,
        extra_info: Optional[Dict[str, Any]],
        process: bool,
        start_time: float,
    ) -> List[Track]:
        info = dict(extra_info or {})
        video_id = info.get("videoId") or info.get("contentId") or info.get("id")

        if not process:
            if extra_info:
                if video_id and not info.get("webpage_url"):
                    info["webpage_url"] = f"https://www.youtube.com/watch?v={video_id}"
                return [Track(service=self.name, url=info.get("webpage_url", url), extra_info=info, type=TrackType.Dynamic)]

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
                logging.info(f"YT Get (Playlist/Channel) finished in {duration:.2f}ms for {url}")
                return tracks

            info = self._bridge.info(url=url)
            video_id = info.get("id") or info.get("videoId")
            title = info.get("title", self.bot.translator.translate("Unknown Title"))
            if info.get("uploader"):
                title += f" - {info['uploader']}"
            original_track = Track(
                service=self.name,
                url=info.get("webpage_url", url),
                name=title,
                type=TrackType.Dynamic,
                extra_info=info,
            )
            if video_id and not getattr(self.bot.player, "is_playlist", False):
                self._fetch_autoplay_async(video_id)
            duration = (time.perf_counter() - start_time) * 1000
            logging.info(f"YT Get (Fast Dynamic) finished in {duration:.2f}ms for {title}")
            return [original_track]

        if not video_id:
            if not url:
                raise errors.ServiceError("No YouTube video ID available for stream resolution")
            resolved = self._bridge.resolve(url=url)
        else:
            resolved = self._bridge.resolve(video_id=video_id)

        stream = {**info, **resolved}
        stream_url = resolved.get("url")
        if not stream_url:
            raise errors.ServiceError("YouTube.js returned no stream URL")
        title = resolved.get("title") or info.get("title") or self.bot.translator.translate("Unknown")
        uploader = resolved.get("uploader") or info.get("uploader")
        if uploader:
            title += f" - {uploader}"
        track_type = TrackType.Live if resolved.get("is_live") else TrackType.Default
        current_video_id = resolved.get("id") or video_id

        if current_video_id and not getattr(self.bot.player, "is_playlist", False):
            try:
                remaining = len(self.bot.player.track_list) - 1 - self.bot.player.track_index
                if remaining <= 4:
                    self._fetch_autoplay_async(current_video_id)
            except Exception as e:
                logging.debug(f"[YT] Trace bot player state error: {e}")

        duration = (time.perf_counter() - start_time) * 1000
        logging.info(f"YT Get (Process/YouTube.js) finished in {duration:.2f}ms for {title}")
        return [
            Track(
                service=self.name,
                url=stream_url,
                name=title,
                format="mp3",
                type=track_type,
                extra_info=stream,
                extracted_at=time.perf_counter(),
            )
        ]

    def _get_recommendations(self, video_id: str, limit: int = 15) -> List[Track]:
        try:
             logging.info(f"[YT] Fetching recommendations for {video_id}")
             url = f"https://www.youtube.com/watch?v={video_id}"
             headers = {
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
                 "Accept-Language": "en-US,en;q=0.9"
             }
             
             import httpx
             import re
             import json
             import http.cookiejar
             
             jar = None
             if self.config.cookiefile_path and os.path.isfile(self.config.cookiefile_path):
                 try:
                     jar = http.cookiejar.MozillaCookieJar(self.config.cookiefile_path)
                     jar.load(ignore_discard=True, ignore_expires=True)
                     logging.info(f"[YT] Recommendations: Loaded cookies from {self.config.cookiefile_path}")
                 except Exception as e:
                     logging.warning(f"[YT] Recommendations: Could not load cookies from {self.config.cookiefile_path}: {e}")
                     jar = None

             with httpx.Client(http2=True, follow_redirects=True, timeout=10.0, cookies=jar) as client:
                 response = client.get(url, headers=headers)
             if response.status_code != 200:
                 logging.error(f"[YT] Recommendations fetch failed: HTTP {response.status_code}")
                 return []
                 
             pattern = r"var ytInitialData = ({.*?});"
             match = re.search(pattern, response.text)
             if not match:
                 pattern = r"window\[['\"]ytInitialData['\"].*? = ({.*?});"
                 match = re.search(pattern, response.text)
                 
             if not match:
                 logging.error("[YT] Recommendations fetch failed: Could not find ytInitialData")
                 return []
                 
             data = json.loads(match.group(1))
             
             # Extract both compactVideoRenderer and lockupViewModel items
             items = []
             def find_videos_and_lockups(obj):
                 if isinstance(obj, dict):
                     if 'compactVideoRenderer' in obj:
                         items.append(('video', obj['compactVideoRenderer']))
                     elif 'lockupViewModel' in obj:
                         items.append(('lockup', obj['lockupViewModel']))
                     else:
                         for v in obj.values():
                             find_videos_and_lockups(v)
                 elif isinstance(obj, list):
                     for item in obj:
                         find_videos_and_lockups(item)
             
             try:
                 find_videos_and_lockups(data)
             except Exception as ex:
                 logging.debug(f"[YT] Recursive search error: {ex}")
             
             new_tracks = []
             count = 0
             for kind, item in items:
                  if count >= limit:
                      break
                  if not item or not isinstance(item, dict):
                      continue
                      
                  v_id = None
                  title = ""
                  channel = ""
                  
                  if kind == 'video':
                      v_id = item.get('videoId')
                      if not v_id:
                          continue
                      title_obj = item.get('title', {})
                      if 'simpleText' in title_obj:
                          title = title_obj['simpleText']
                      elif 'runs' in title_obj and isinstance(title_obj['runs'], list) and len(title_obj['runs']) > 0:
                          title = title_obj['runs'][0].get('text', '')
                          
                      channel_obj = item.get('longBylineText', {}) or item.get('shortBylineText', {})
                      if 'runs' in channel_obj and isinstance(channel_obj['runs'], list) and len(channel_obj['runs']) > 0:
                          channel = channel_obj['runs'][0].get('text', '')
                  
                  elif kind == 'lockup':
                      v_id = item.get('contentId')
                      content_type = item.get('contentType')
                      if content_type != 'LOCKUP_CONTENT_TYPE_VIDEO':
                          continue
                      if not v_id:
                          continue
                      
                      metadata = item.get('metadata', {}).get('lockupMetadataViewModel', {})
                      title = metadata.get('title', {}).get('content', '')
                      
                      rows = metadata.get('metadata', {}).get('contentMetadataViewModel', {}).get('metadataRows', [])
                      if len(rows) > 0:
                          parts = rows[0].get('metadataParts', [])
                          if len(parts) > 0:
                              txt_obj = parts[0].get('text', {})
                              if isinstance(txt_obj, dict):
                                  channel = txt_obj.get('content', '')
                              elif isinstance(txt_obj, str):
                                  channel = txt_obj
                  
                  if not v_id:
                      continue
                      
                  full_title = f"{title} - {channel}" if channel else title
                  
                  track = Track(
                       service=self.name,
                       name=full_title,
                       url=f"https://www.youtube.com/watch?v={v_id}",
                       type=TrackType.Dynamic,
                       extra_info=item
                  )
                  new_tracks.append(track)
                  count += 1
             
             return new_tracks
        except Exception as e:
             logging.error(f"[YT] Recommendations fetch error: {e}")
             return []

    def _fetch_autoplay_async(self, video_id: str) -> None:
         threading.Thread(target=self._fetch_autoplay_sync, args=(video_id,), daemon=True, name=f"Autoplay_{video_id}").start()

    def _fetch_autoplay_sync(self, video_id: str) -> bool:
         try:
              logging.info(f"[YT] Fetching continuous recommendations for {video_id}")
              recs: List[Track] = []
              try:
                  entries = self._bridge.recommendations(video_id, 50).get("entries", [])
                  for item in entries:
                      t_vid = item.get("videoId")
                      if not t_vid:
                          continue
                      title = item.get("title", "")
                      uploader = item.get("uploader", "")
                      full_title = f"{title} - {uploader}" if uploader else title
                      recs.append(
                          Track(
                              service=self.name,
                              name=full_title,
                              url=item.get("webpage_url") or f"https://www.youtube.com/watch?v={t_vid}",
                              type=TrackType.Dynamic,
                              extra_info=item,
                          )
                      )
              except Exception as ex:
                  logging.debug(f"[YT] Bridge recommendations query failed: {ex}")

              if len(recs) < 5:
                  try:
                      fallback_recs = self._get_recommendations(video_id, limit=20)
                      if fallback_recs:
                          recs.extend(fallback_recs)
                  except Exception as ex:
                      logging.debug(f"[YT] Web scraping fallback error: {ex}")

              if recs:
                   # Deduplica apenas contra as faixas à frente no buffer e as últimas 15 tocadas
                   current_idx = self.bot.player.track_index
                   recent_tracks = self.bot.player.track_list[max(0, current_idx - 15):]
                   existing_ids = set()
                   for t in recent_tracks:
                        t_info = getattr(t, "extra_info", None) or {}
                        vid = t_info.get("id") or t_info.get("videoId") or t_info.get("contentId")
                        if vid:
                             existing_ids.add(vid)
                   existing_ids.add(video_id)

                   new_tracks = []
                   for t in recs:
                        t_info = getattr(t, "extra_info", None) or {}
                        t_vid = t_info.get("id") or t_info.get("videoId") or t_info.get("contentId")
                        if not t_vid and hasattr(t, "_url") and t._url and "v=" in t._url:
                             t_vid = t._url.split("v=")[1].split("&")[0].split("?")[0]
                        if not t_vid or t_vid in existing_ids:
                             continue
                        existing_ids.add(t_vid)
                        new_tracks.append(t)
                        if len(new_tracks) >= 15:
                             break

                   if new_tracks:
                        logging.info(f"[YT] Adding {len(new_tracks)} continuous recommendations to track list (total: {len(self.bot.player.track_list) + len(new_tracks)})")
                        self.bot.player.track_list.extend(new_tracks)
                        if hasattr(self.bot.player, "_schedule_prefetch"):
                             self.bot.player._schedule_prefetch()
                        return True
                   else:
                        logging.info(f"[YT] No new unique recommendations found for video_id {video_id}")
         except Exception as e:
              logging.error(f"[YT] Autoplay fetch failed: {e}")
         return False


    def search(self, query: str, limit: Optional[int] = None) -> List[Track]:
        if limit is None:
            limit = self.config.search_results
        start_time = time.perf_counter()
        try:
            entries = self._bridge.search(query, limit).get("entries", [])
            tracks = []
            for video in entries:
                if not video.get("webpage_url") and not video.get("stream_url"):
                    continue
                title = video.get("title") or self.bot.translator.translate("Unknown Title")
                uploader = video.get("uploader")
                if uploader and uploader not in title:
                    title += f" - {uploader}"
                stream_url = video.get("stream_url")
                if stream_url:
                    track = Track(
                        service=self.name,
                        url=stream_url,
                        name=title,
                        format="mp3",
                        type=TrackType.Live if video.get("is_live") else TrackType.Default,
                        extra_info=video,
                        extracted_at=time.perf_counter(),
                    )
                    track._is_fetched = True
                else:
                    track = Track(
                        service=self.name,
                        url=video.get("webpage_url", ""),
                        name=title,
                        type=TrackType.Dynamic,
                        extra_info=video,
                    )
                tracks.append(track)
            if not tracks:
                raise errors.NothingFoundError("")
            player = getattr(self.bot, "player", None)
            if len(tracks) == 1 and not getattr(player, "is_playlist", False):
                vid = entries[0].get("id") or entries[0].get("videoId")
                if vid:
                    self._fetch_autoplay_async(vid)
            duration = (time.perf_counter() - start_time) * 1000
            logging.info(f"YT Search (YouTube.js) finished in {duration:.2f}ms for query: {query}")
            return tracks
        except Exception as e:
            logging.error(f"YT Search failed: {e}")
            raise errors.NothingFoundError("")
