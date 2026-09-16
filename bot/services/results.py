"""How a search result list is ordered and read out, for every service that has
more than one kind of result.

This lived in `browser_service.py` while the four browser services were the only
ones that needed it. Netflix borrowed it first, then Spotify, and a third
borrower is the point at which shared vocabulary stops being a detail of one
module. Nothing here touches a browser, a page or an engine: it takes result
dicts and a translator and returns text.

The rules it encodes are from the plan's "Search result ordering" section, and
both of them exist for a listener rather than a reader:

- **Containers before individual tracks.** A sighted user skims a mixed list and
  picks out the album. A screen reader user hears all twenty-five entries in
  sequence, so "three albums, then the tracks" is navigable where an interleaved
  list is not.
- **Kind first on every line.** That is the word being listened for, and naming
  it first lets someone stop reading once they hear the one they want.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Which kinds are shown before which. `top` leads because it is the service's own
# answer to the query; the containers follow because choosing a whole thing is
# usually what someone wants from a list they are hearing rather than skimming.
KIND_ORDER = ("top", "artist", "album", "playlist", "series", "title", "track")

KIND_LABELS = {
    "artist": "Artist",
    "album": "Album",
    "playlist": "Playlist",
    "series": "Series",
    "title": "Title",
    "track": "Track",
    "top": "Top result",
}

# The summary line counts things, and "24 Track" read aloud is wrong in a way
# that a written list gets away with. Kept as its own table rather than adding
# an "s", because a translator needs both forms and several shipped languages
# do not pluralise by suffix at all.
KIND_LABELS_PLURAL = {
    "artist": "Artists",
    "album": "Albums",
    "playlist": "Playlists",
    "series": "Series",
    "title": "Titles",
    "track": "Tracks",
    "top": "Top results",
}

# Kinds that are not a single thing to play. Selecting one expands it into its
# tracks through the service's own get(), which is the same path a pasted link
# takes, so there is one expansion per service and not two.
CONTAINER_KINDS = frozenset({"artist", "album", "playlist"})


def order_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group by kind without disturbing the service's own ranking.

    The service's ordering is its answer to the query, and re-sorting it by title
    or duration produces worse results, so within each kind the original order is
    preserved exactly. Only the groups move.
    """
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for item in results:
        by_kind.setdefault(item.get("kind", "title"), []).append(item)

    ordered: List[Dict[str, Any]] = []
    for kind in KIND_ORDER:
        ordered.extend(by_kind.pop(kind, []))
    for remaining in by_kind.values():  # anything the site invented
        ordered.extend(remaining)
    return ordered


def promote_first_playable(
    results: List[Dict[str, Any]], playable_kind: str = "track"
) -> List[Dict[str, Any]]:
    """Mark the best single thing to play as the top result.

    Without this, ordering containers first means a bare `p QUERY` plays an
    artist or an album rather than the song that was asked for — and an artist is
    not a thing that can be played at all. Apple Music gets a `top` from its own
    search page; the services that do not are given one here, from the first
    result that is actually a single playable item.

    Mutates nothing: the promoted entry is a copy, so a cached result list is not
    rewritten by being read.
    """
    for index, item in enumerate(results):
        if item.get("kind") == playable_kind:
            promoted = dict(item, kind="top", top_kind=playable_kind)
            return [promoted] + results[:index] + results[index + 1 :]
    return results


def spoken_title(item: Dict[str, Any]) -> str:
    """The title as it is read out in a result list.

    A top result says what it is, because its group label only says "Top result":
    "Song: Adventure of a Lifetime, by Coldplay". Every other line already has its
    kind as the group label, so it only adds the artist.
    """
    title = (item.get("title") or "").strip()
    artist = (item.get("artist") or "").strip()
    by = f", by {artist}" if artist and artist != title else ""
    if item.get("kind") == "top":
        kind_word = {
            "track": "Song",
            "album": "Album",
            "artist": "Artist",
            "playlist": "Playlist",
        }.get(item.get("top_kind", ""), "")
        if item.get("top_kind") == "artist":
            return f"{kind_word}: {title}" if kind_word else title
        return f"{kind_word}: {title}{by}" if kind_word else f"{title}{by}"
    return f"{title}{by}"


def describe_results(translator, results: List[Dict[str, Any]]) -> str:
    """A summary of what was found, then a numbered line per result."""
    counts: Dict[str, int] = {}
    for item in results:
        counts[item.get("kind", "title")] = counts.get(item.get("kind", "title"), 0) + 1

    summary_parts = [
        translator.translate("%(count)s %(kind)s")
        % {
            "count": count,
            "kind": translator.translate(
                (KIND_LABELS if count == 1 else KIND_LABELS_PLURAL).get(kind, kind)
            ),
        }
        for kind, count in counts.items()
    ]
    lines = [", ".join(summary_parts) + "."]
    for index, item in enumerate(results, 1):
        label = translator.translate(KIND_LABELS.get(item.get("kind", "title"), "Title"))
        lines.append(f"{index}. {label}: {item.get('title', '')}")
    return chr(10).join(lines)


def describe_tracks(translator, tracks: List[Any]) -> str:
    """The same list, once the results have become Tracks.

    Each service keeps its result's kind in `extra_info`, so the numbered list the
    user hears can name it. The command used to print bare titles, which made an
    album, an artist and the song on that album three identical-looking lines.
    """
    return describe_results(
        translator,
        [
            {
                "kind": (getattr(track, "extra_info", None) or {}).get("kind", "title"),
                "title": track.name,
            }
            for track in tracks
        ],
    )


def is_container(track: Any) -> bool:
    """Whether selecting this result should expand it rather than play it."""
    kind = (getattr(track, "extra_info", None) or {}).get("kind", "")
    if kind == "top":
        kind = (getattr(track, "extra_info", None) or {}).get("top_kind", "")
    return kind in CONTAINER_KINDS


__all__ = [
    "CONTAINER_KINDS",
    "KIND_LABELS",
    "KIND_LABELS_PLURAL",
    "KIND_ORDER",
    "describe_results",
    "describe_tracks",
    "is_container",
    "order_results",
    "promote_first_playable",
    "spoken_title",
]
