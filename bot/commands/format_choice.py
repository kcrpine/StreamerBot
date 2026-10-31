"""`dl`, `dlp`, `dla`: which file format to download Apple Music in, asked every time.

    Which file format do you want?
    For M4A, the original Apple Music file, send: 1
    For MP3, send: 2
    ...
    To cancel, send: 0

Asked after what to download has been settled, so the song or album is the one
that was playing when the command was sent, not whatever plays by the time the
user answers. Format names are in capitals because a synth reads "aac", "ogg"
and "flac" as words. Like dla's question, anything that is not a number drops
the question and runs as an ordinary command, and an unanswered question is
dropped after half an hour.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from bot.services.gamdl_downloader import FORMATS

CANCEL = 0
EXPIRES_AFTER_SECONDS = 30 * 60

# (reply to send, whether the question stays open). None means "not an answer".
Outcome = Optional[Tuple[str, bool]]


class FormatChoice:
    def __init__(
        self,
        command: str,
        translate: Callable[[str], str],
        start: Callable[[str], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # The command that asked, named when the question has expired.
        self.command = command
        self.translate = translate
        self.start = start
        self._clock = clock
        self._touched = clock()

    def expired(self) -> bool:
        return self._clock() - self._touched > EXPIRES_AFTER_SECONDS

    def expired_message(self) -> str:
        return self.translate(
            "The file format question has expired. To ask again, send: {command}"
        ).format(command=self.command)

    def question(self) -> str:
        return "\n".join([
            self.translate("Which file format do you want?"),
            self.translate("For M4A, the original Apple Music file, send: 1"),
            self.translate("For MP3, send: 2"),
            self.translate("For Opus, send: 3"),
            self.translate("For Ogg, send: 4"),
            self.translate("For FLAC, larger files with the same sound, send: 5"),
            self.translate("For WAV, the largest files with the same sound, send: 6"),
            self.translate("For AAC, send: 7"),
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
        if number in FORMATS:
            return self.start(FORMATS[number]), False
        return "\n".join([
            self.translate("That is not one of the choices."), self.question()
        ]), True


__all__ = ["CANCEL", "EXPIRES_AFTER_SECONDS", "FormatChoice"]
