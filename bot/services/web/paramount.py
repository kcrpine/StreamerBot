"""Paramount Plus, driven through a browser page.

Only what is Paramount's own is here; the sign-in, profile, search, playback
and audio track logic is VideoSiteAdapter's. Not yet measured against the live
site: see bot/services/web/video_site.py for what that means when it fails.
"""

from __future__ import annotations

from bot.services.web.video_site import VideoSiteAdapter

HOME = "https://www.paramountplus.com"


class ParamountAdapter(VideoSiteAdapter):
    name = "pp"
    SITE = "Paramount Plus"
    SCHEME = "paramount://"
    HOME = HOME
    home_url = f"{HOME}/home/"
    LOGIN_PATH = "/account/signin/"
    PROFILES_PATH = "/profiles/"
    SEARCH_PATH = "/search/?q={query}"
    TITLE_PATHS = ("/shows/", "/movies/")
    MOVIE_PATHS = ("/movies/",)
    SIGNED_OUT_MARKS = ("/signin", "/signup")
    # Paramount's own links end in a slash, and its pages redirect without one.
    TRAILING_SLASH = True
    LOGGED_IN_SELECTORS = (
        '[data-testid*="profile"]',
        'a[href*="/account/"]',
        'nav a[href*="/shows/"]',
    )


__all__ = ["ParamountAdapter"]
