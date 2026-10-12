"""Hulu, driven through a browser page.

Only what is Hulu's own is here; everything else is VideoSiteAdapter's. Not yet
measured against the live site.

Two things worth knowing before debugging it. Hulu streams only to the United
States, so a bot whose host is elsewhere is refused by Hulu itself, whatever
this file does. And Disney has been moving Hulu into the Disney Plus app: if
hulu.com stops offering a player of its own, its titles are reached through
Disney Plus (`dp`) instead, and this adapter's failures will say so by the page
URL they log.
"""

from __future__ import annotations

from bot.services.web.video_site import VideoSiteAdapter

HOME = "https://www.hulu.com"


class HuluAdapter(VideoSiteAdapter):
    name = "hu"
    SITE = "Hulu"
    SCHEME = "hulu://"
    HOME = HOME
    home_url = f"{HOME}/hub/home"
    LOGIN_PATH = "/login"
    PROFILES_PATH = "/profiles"
    SEARCH_PATH = "/search?q={query}"
    # A film's page is /movie/<slug>, a series' /series/<slug>; /watch/<id>
    # goes straight to the player.
    TITLE_PATHS = ("/movie/", "/series/", "/watch/")
    MOVIE_PATHS = ("/movie/", "/watch/")
    SIGNED_OUT_MARKS = ("/login", "/welcome")
    LOGGED_IN_SELECTORS = (
        '[data-automationid*="profile"]',
        '[data-testid*="profile"]',
        'a[href*="/account"]',
    )
    PROFILE_CARDS = (
        '[data-automationid*="profile"] button, [data-testid*="profile"] button, '
        '[class*="ProfileItem"], [class*="profile-item"]'
    )


__all__ = ["HuluAdapter"]
