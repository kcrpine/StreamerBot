"""Per-site browser adapters.

All the knowledge of how one streaming site's pages are laid out lives in one
file per site, behind this interface. That is deliberate: these sites redesign
without notice, and when Netflix moves its audio menu the fix should be one file
rather than a hunt through the engine.

**Every method here runs on the browser engine's worker thread.** Adapters
receive a Playwright page and use it synchronously. They must not spawn threads,
must not call back into Player, and must not block indefinitely — the engine
applies a timeout per job and a hung adapter stalls every other browser
operation behind it.

Selectors are the fragile part, so each adapter keeps a list of candidates per
element rather than one, and tries them in order. A site that changes one class
name should degrade to "could not find the audio menu" rather than to a
traceback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class WebServiceAdapter(ABC):
    """One streaming site, driven through a browser page."""

    #: Service id, matching the entry in ServiceManager.
    name: str = ""

    #: Where a signed-out visit lands.
    home_url: str = ""

    #: Substrings that mark an audio track as described, per locale. Matched
    #: case-insensitively against the track's own label. These are the site's
    #: words, never the bot's translated strings: the site labels tracks in the
    #: account's language, which has nothing to do with the bot's locale.
    audio_description_markers: tuple = (
        "audio description",
        "descriptive audio",
        "described",
        "audiodescripción",
        "audiodescrition",
        "audiodescrição",
        "audiodescription",
        "hörfilm",
        "audiodescrizione",
        "тифлокомментарий",
        "وصف صوتي",
    )

    # -- session -----------------------------------------------------------

    @abstractmethod
    def is_logged_in(self, page) -> bool: ...

    @abstractmethod
    def login(self, page, username: str, password: str, job) -> None:
        """Sign in, using job.request_otp() when the site asks for a code.

        job is an AuthJob. Call job.require_captcha(url) and return if a CAPTCHA
        appears: we do not solve those, and the portal offers session import
        instead.
        """

    # -- profiles ----------------------------------------------------------

    def list_profiles(self, page) -> List[Dict[str, Any]]:
        """[{id, name}]. Empty when the site has no profile concept."""
        return []

    def select_profile(self, page, profile_id: str) -> bool:
        return False

    # -- finding things ----------------------------------------------------

    @abstractmethod
    def search(self, page, query: str) -> List[Dict[str, Any]]:
        """[{id, title, kind, url}] with the site's own ordering preserved."""

    def watchlist(self, page) -> List[Dict[str, Any]]:
        return []

    def episodes(self, page, title_id: str) -> List[Dict[str, Any]]:
        return []

    # -- playback ----------------------------------------------------------

    @abstractmethod
    def play(self, page, track) -> None: ...

    @abstractmethod
    def pause(self, page) -> None: ...

    @abstractmethod
    def resume(self, page) -> None: ...

    def stop(self, page) -> None:
        """Leave the player entirely.

        Pausing is not enough: an advertisement or an autoplaying next episode
        starts on its own and would be heard over whichever engine took over.
        """
        page.goto(self.home_url, wait_until="domcontentloaded")

    def set_volume(self, page, volume: float) -> None:
        """volume is 0.0 to 1.0. Set on the media element, since the site's own
        slider is a different control on every site."""
        page.evaluate(
            "v => { const m = document.querySelector('video, audio');"
            " if (m) m.volume = Math.max(0, Math.min(1, v)); }",
            volume,
        )

    def seek(self, page, offset: float) -> None:
        page.evaluate(
            "o => { const m = document.querySelector('video, audio');"
            " if (m) m.currentTime = Math.max(0, m.currentTime + o); }",
            offset,
        )

    def get_position(self, page) -> Optional[float]:
        return page.evaluate(
            "() => { const m = document.querySelector('video, audio');"
            " return m ? m.currentTime : null; }"
        )

    def get_duration(self, page) -> Optional[float]:
        value = page.evaluate(
            "() => { const m = document.querySelector('video, audio');"
            " return m && isFinite(m.duration) ? m.duration : null; }"
        )
        return value

    # -- audio tracks ------------------------------------------------------

    @abstractmethod
    def list_audio_tracks(self, page) -> List[Dict[str, Any]]:
        """[{id, label}] from the player's own audio menu.

        Only meaningful once the player has loaded: the menu is not populated
        before then.
        """

    @abstractmethod
    def set_audio_track(self, page, track_id: str) -> bool: ...

    def find_described_track(self, tracks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The first track whose label looks like audio description."""
        for track in tracks:
            label = (track.get("label") or "").lower()
            if any(marker in label for marker in self.audio_description_markers):
                return track
        return None

    # -- helpers for subclasses -------------------------------------------

    @staticmethod
    def first_visible(page, selectors, timeout: float = 5000):
        """Try each selector in turn and return the first that appears.

        Sites change class names constantly, so every element is addressed by a
        list of candidates rather than one string. Returns None rather than
        raising, so a caller can report "could not find the sign-in button"
        instead of surfacing a Playwright timeout.
        """
        for selector in selectors:
            try:
                element = page.wait_for_selector(selector, timeout=timeout, state="visible")
                if element is not None:
                    return element
            except Exception:
                continue
        return None


__all__ = ["WebServiceAdapter"]
