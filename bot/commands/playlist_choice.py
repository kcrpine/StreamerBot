"""`dlp` on a YouTube playlist: the whole playlist, or one video picked from a list.

First one question: 1 for the whole playlist as a zip, 2 for a numbered list.
The list comes 20 videos at a time, and every choice in it is a number:

    Page 2 of 7 of Sunday Masses, 137 videos. Send a number:
    1. ...
    20. ...
    21. Next videos, 41 to 60
    22. Download the whole playlist
    0. Cancel

1 to 20 are always the videos on the page shown, and 21, 22 and 0 never move,
so someone listening rather than looking learns them once. The heading names
the page rather than a video range, so "21" is never heard meaning two things.
On the last page 21 goes back to the start instead, so it is never a dead
number. A playlist that fits on one page has no 21 at all.

Anything that is not an answer cancels the question and runs as an ordinary
command (CommandProcessor._run), the same rule as the audio description prompt,
so nobody is stuck inside it. A question left unanswered for half an hour is
dropped for the same reason.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

PAGE_SIZE = 20
NEXT_PAGE = PAGE_SIZE + 1
WHOLE_PLAYLIST = PAGE_SIZE + 2
CANCEL = 0
EXPIRES_AFTER_SECONDS = 30 * 60

# (reply to send, whether the question stays open). None means "not an answer".
Outcome = Optional[Tuple[str, bool]]


def video_label(track) -> str:
    title = (track.name or "").strip()
    uploader = ((track.extra_info or {}).get("uploader") or "").strip()
    if uploader and uploader not in title:
        # "by", not " - ": many titles already contain a dash.
        return f"{title}, by {uploader}"
    return title


class PlaylistChoice:
    def __init__(
        self,
        tracks: List,
        name: str,
        translate: Callable[[str], str],
        download_whole: Callable[[], None],
        download_one: Callable[[object], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tracks = tracks
        self.name = name
        self.translate = translate
        self.download_whole = download_whole
        self.download_one = download_one
        self.listing = False
        self.page = 0
        self._clock = clock
        self._touched = clock()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.tracks) // PAGE_SIZE))

    def expired(self) -> bool:
        return self._clock() - self._touched > EXPIRES_AFTER_SECONDS

    def question(self) -> str:
        # Each command last on its line, after a colon, so the review cursor
        # finds it at the end. The same rule as youtube_signed_out_message.
        return "\n".join([
            self.translate("The playlist {name} has {count} videos.").format(
                name=self.name, count=len(self.tracks)
            ),
            self.translate("For the whole playlist as one zip file, send: 1"),
            self.translate("To pick one video from a list, send: 2"),
            self.translate("To cancel, send: 0"),
        ])

    def page_text(self) -> str:
        first = self.page * PAGE_SIZE
        shown = self.tracks[first:first + PAGE_SIZE]
        lines = [
            self.translate("Page {page} of {pages} of {name}, {total} videos. Send a number:").format(
                page=self.page + 1, pages=self.pages, name=self.name, total=len(self.tracks)
            )
        ]
        lines += [f"{number}. {video_label(track)}" for number, track in enumerate(shown, 1)]
        if self.pages > 1:
            if self.page + 1 < self.pages:
                following = first + PAGE_SIZE
                lines.append(f"{NEXT_PAGE}. " + self.translate("Next videos, {first} to {last}").format(
                    first=following + 1, last=min(following + PAGE_SIZE, len(self.tracks))
                ))
            else:
                lines.append(f"{NEXT_PAGE}. " + self.translate("Back to videos 1 to {last}").format(
                    last=min(PAGE_SIZE, len(self.tracks))
                ))
        lines.append(f"{WHOLE_PLAYLIST}. " + self.translate("Download the whole playlist"))
        lines.append(f"{CANCEL}. " + self.translate("Cancel"))
        return "\n".join(lines)

    def answer(self, text: str) -> Outcome:
        try:
            number = int(text.strip())
        except ValueError:
            return None
        self._touched = self._clock()

        if number == CANCEL:
            return self.translate("Cancelled."), False

        if not self.listing:
            if number == 1:
                self.download_whole()
                return "", False
            if number == 2:
                self.listing = True
                self.page = 0
                return self.page_text(), True
            return self.question(), True

        if number == NEXT_PAGE and self.pages > 1:
            self.page = (self.page + 1) % self.pages
            return self.page_text(), True
        if number == WHOLE_PLAYLIST:
            self.download_whole()
            return "", False
        shown = self.tracks[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        if 1 <= number <= len(shown):
            return self.download_one(shown[number - 1]), False
        return self.translate(
            "There is no {number} on this page. Send a number from 1 to {last}, or to cancel, send: 0"
        ).format(number=number, last=len(shown)), True


__all__ = [
    "CANCEL",
    "NEXT_PAGE",
    "PAGE_SIZE",
    "PlaylistChoice",
    "WHOLE_PLAYLIST",
    "video_label",
]
