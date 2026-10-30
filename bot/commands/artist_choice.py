"""`dla`: which of an artist's releases to download, asked once.

    What do you want to download by Coldplay?
    For main albums only, send: 1
    For everything, including singles, EPs, live albums and compilations, send: 2
    For singles and EPs only, send: 3
    To cancel, send: 0

The question names the artist, so a search that matched the wrong one is heard
and cancelled before anything downloads. Each command is last on its line, after
a colon, so the review cursor finds it at the end.

Anything that is not a number cancels the question and runs as an ordinary
command (CommandProcessor._run), the same rule as dlp's playlist question, and a
question left unanswered for half an hour is dropped.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from bot.services.gamdl_downloader import ARTIST_SELECTIONS

CANCEL = 0
EXPIRES_AFTER_SECONDS = 30 * 60

# (reply to send, whether the question stays open). None means "not an answer".
Outcome = Optional[Tuple[str, bool]]


class ArtistChoice:
    def __init__(
        self,
        name: str,
        translate: Callable[[str], str],
        start: Callable[[str], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.translate = translate
        self.start = start
        self._clock = clock
        self._touched = clock()

    def expired(self) -> bool:
        return self._clock() - self._touched > EXPIRES_AFTER_SECONDS

    def question(self) -> str:
        return "\n".join([
            self.translate("What do you want to download by {name}?").format(name=self.name),
            self.translate("For main albums only, send: 1"),
            self.translate(
                "For everything, including singles, EPs, live albums and compilations, send: 2"
            ),
            self.translate("For singles and EPs only, send: 3"),
            self.translate("To cancel, send: 0"),
        ])

    def answer(self, text: str) -> Outcome:
        try:
            number = int(text.strip())
        except ValueError:
            return None
        self._touched = self._clock()

        if number == CANCEL:
            return self.translate("Cancelled."), False
        if number in ARTIST_SELECTIONS:
            return self.start(ARTIST_SELECTIONS[number]), False
        return "\n".join([
            self.translate("That is not one of the choices."), self.question()
        ]), True


__all__ = ["ArtistChoice", "CANCEL", "EXPIRES_AFTER_SECONDS"]
