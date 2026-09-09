"""Account connection: encrypted credential storage and sign-in jobs.

Everything here is per bot. The store lives under the bot's own data directory,
so two bots on one host never share a Netflix or Spotify account, and deleting
one bot cannot sign another out.
"""

# The six services the portal can connect, in the order the status page lists
# them. Kept here rather than in the portal so the commands, the store and the
# portal all agree on the identifiers.
SERVICES = ("yt", "sp", "nf", "dp", "am", "az")

SERVICE_NAMES = {
    "yt": "YouTube",
    "sp": "Spotify",
    "nf": "Netflix",
    "dp": "Disney Plus",
    "am": "Apple Music",
    "az": "Amazon Music",
}


def service_name(service: str) -> str:
    return SERVICE_NAMES.get(service, service)
