"""Account connection: encrypted credential storage and sign-in jobs.

Everything here is per bot. The store lives under the bot's own data directory,
so two bots on one host never share a Netflix or Spotify account, and deleting
one bot cannot sign another out.
"""

# The services the portal can connect, in the order the status page lists them.
# Kept here rather than in the portal so the commands, the store and the portal
# all agree on the identifiers.
SERVICES = ("yt", "sp", "nf", "dp", "am", "az", "pp", "hu", "tb")

# Services that play without an account. They can still be connected, but
# nothing tells a user they need to be, and `li` sends no link for them.
OPTIONAL_SIGN_IN = ("tb",)

SERVICE_NAMES = {
    "yt": "YouTube",
    # Not in SERVICES: it has no account of its own and plays through YouTube's
    # session. It is here so anything naming a service can name this one too.
    "ytm": "YouTube Music",
    "sp": "Spotify",
    "nf": "Netflix",
    "dp": "Disney Plus",
    "am": "Apple Music",
    "az": "Amazon Music",
    "pp": "Paramount Plus",
    "hu": "Hulu",
    "tb": "Tubi",
    # Not in SERVICES either: podcasts have no account.
    "pc": "Podcasts",
}


def service_name(service: str) -> str:
    return SERVICE_NAMES.get(service, service)
