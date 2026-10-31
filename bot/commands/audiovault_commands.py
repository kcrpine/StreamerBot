"""`av`: search AudioVault, choose a film or show, then stream or download it.

    av family guy
    AudioVault results for family guy: 2
    1: Family Guy, TV show, seasons: 27
    2: Family Guy Presents Blue Harvest (2007), movie
    To choose one, send its number.
    To cancel, send: 0

A film then asks 1 to stream, 2 to download. A show lists its seasons: a number
streams that season, "d" and a number downloads it, "all" downloads every
season as its own zip file. Each step is a numbered question like dla's: words
that are not an answer drop it and run as an ordinary command, and it is
dropped after half an hour. Counts come last after a colon, which also spares
the translators a plural form.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from bot import errors
from bot.commands.command import Command
from bot.services.audiovault import Title

if TYPE_CHECKING:
    from bot.TeamTalk.structs import User

EXPIRES_AFTER_SECONDS = 30 * 60
# Enough to find the right one, short enough to hear through.
MAX_RESULTS = 30

Outcome = Optional[Tuple[str, bool]]


class _Question:
    def __init__(self, translate: Callable[[str], str], clock: Callable[[], float] = time.monotonic):
        self.translate = translate
        self._clock = clock
        self._touched = clock()

    def expired(self) -> bool:
        return self._clock() - self._touched > EXPIRES_AFTER_SECONDS

    def expired_message(self) -> str:
        return self.translate(
            "That list has expired. To search again, send av and a title, for example: av spaceballs"
        )

    def _touch(self) -> None:
        self._touched = self._clock()

    def _not_a_choice(self) -> Outcome:
        return "\n".join([self.translate("That is not one of the choices."), self.question()]), True

    def question(self) -> str:
        raise NotImplementedError


class ResultsQuestion(_Question):
    def __init__(self, query: str, titles: List[Title], translate, pick: Callable[[Title], str], **kw):
        super().__init__(translate, **kw)
        self.query = query
        self.titles = titles
        self.pick = pick

    def question(self) -> str:
        lines = [
            self.translate("AudioVault results for {query}: {count}").format(
                query=self.query, count=len(self.titles)
            )
        ]
        for number, title in enumerate(self.titles, 1):
            if title.kind == "show":
                line = self.translate("{name}, TV show, seasons: {count}").format(
                    name=title.name, count=len(title.seasons)
                )
            else:
                line = self.translate("{name}, movie").format(name=title.name)
            lines.append(f"{number}: {line}")
        lines.append(self.translate("To choose one, send its number."))
        lines.append(self.translate("To cancel, send: 0"))
        return "\n".join(lines)

    def answer(self, text: str) -> Outcome:
        text = text.strip()
        if not text.isdigit():
            return None
        self._touch()
        number = int(text)
        if number == 0:
            return self.translate("Cancelled."), False
        if 1 <= number <= len(self.titles):
            return self.pick(self.titles[number - 1]), False
        return self._not_a_choice()


class MovieQuestion(_Question):
    def __init__(self, title: Title, translate, stream: Callable[[], str], download: Callable[[], str], **kw):
        super().__init__(translate, **kw)
        self.title = title
        self.stream = stream
        self.download = download

    def question(self) -> str:
        return "\n".join([
            self.title.name,
            self.translate("To stream it in the channel, send: 1"),
            self.translate("To download it to the channel's files as a zip file, send: 2"),
            self.translate("To cancel, send: 0"),
        ])

    def answer(self, text: str) -> Outcome:
        text = text.strip()
        if not text.isdigit():
            return None
        self._touch()
        if text == "0":
            return self.translate("Cancelled."), False
        if text == "1":
            return self.stream(), False
        if text == "2":
            return self.download(), False
        return self._not_a_choice()


_DOWNLOAD_ONE = re.compile(r"^d\s*(\d+)$", re.I)


class ShowQuestion(_Question):
    def __init__(self, title: Title, translate, stream: Callable[[int], str],
                 download: Callable[[int], str], download_all: Callable[[], str], **kw):
        super().__init__(translate, **kw)
        self.title = title
        self.stream = stream
        self.download = download
        self.download_all = download_all

    def question(self) -> str:
        lines = [
            self.translate("{name}. Seasons: {count}").format(
                name=self.title.name, count=len(self.title.seasons)
            )
        ]
        for number, season in enumerate(self.title.seasons, 1):
            lines.append(f"{number}: {season.label}")
        lines += [
            self.translate("To stream a season, send its number."),
            # translators: "d" is a command letter. Keep it exactly as it is.
            self.translate("To download one season, send d and its number, for example: d 1"),
            # translators: "all" is a command word. Keep it in English, exactly as it is.
            self.translate("To download every season, one zip file each, send: all"),
            self.translate("To cancel, send: 0"),
        ]
        return "\n".join(lines)

    def answer(self, text: str) -> Outcome:
        text = text.strip()
        count = len(self.title.seasons)
        if text.lower() == "all":
            self._touch()
            return self.download_all(), False
        match = _DOWNLOAD_ONE.match(text)
        if match:
            self._touch()
            number = int(match.group(1))
            if 1 <= number <= count:
                return self.download(number - 1), False
            return self._not_a_choice()
        if not text.isdigit():
            return None
        self._touch()
        number = int(text)
        if number == 0:
            return self.translate("Cancelled."), False
        if 1 <= number <= count:
            return self.stream(number - 1), False
        return self._not_a_choice()


class AudioVaultCommand(Command):
    @property
    def help(self) -> str:
        return self.translator.translate(
            "TITLE Searches AudioVault for audio-described films and TV shows. Choose one by its "
            "number, then stream it in the channel or download it to the channel's files"
        )

    @property
    def _jobs(self):
        return self.module_manager.audiovault_jobs

    def _register(self, user: User, question) -> str:
        self.command_processor.pending_audiovault[user.id] = question
        return question.question()

    def __call__(self, arg: str, user: User) -> Optional[str]:
        query = " ".join((arg or "").split())
        if not query:
            raise errors.InvalidArgumentError()
        service = self.service_manager.services.get("av")
        if service is None or not service.is_enabled:
            return self.translator.translate("AudioVault is not available on this bot.")
        self.run_async(self._search, query, user)
        return self.translator.translate("Searching AudioVault for {query}.").format(query=query)

    def _search(self, query: str, user: User) -> None:
        service = self.service_manager.services["av"]
        try:
            titles = service.client.search(query)
        except errors.ServiceError as error:
            self.ttclient.send_message(str(error), user)
            return
        except Exception as error:
            logging.error(f"[av] search for {query!r} failed: {error}", exc_info=True)
            self.ttclient.send_message(
                self.translator.translate("AudioVault could not be reached. Try again later."), user
            )
            return
        if not titles:
            self.ttclient.send_message(
                self.translator.translate(
                    "AudioVault found nothing for {query}. To search again, send av and a title, "
                    "for example: av spaceballs"
                ).format(query=query),
                user,
            )
            return
        question = ResultsQuestion(
            query, titles[:MAX_RESULTS], self.translator.translate,
            lambda title: self._pick(title, user),
        )
        self.ttclient.send_message(self._register(user, question), user)

    def _pick(self, title: Title, user: User) -> str:
        jobs = self._jobs
        refusal = jobs.refusal()
        if refusal:
            return refusal
        translate = self.translator.translate
        if title.kind == "movie":
            return self._register(user, MovieQuestion(
                title, translate,
                stream=lambda: jobs.stream(title.name, title.entry, user, episodes=False),
                download=lambda: jobs.download(title.name, title.entry, user),
            ))

        def season_name(index: int) -> str:
            return f"{title.name}, {title.seasons[index].label}"

        return self._register(user, ShowQuestion(
            title, translate,
            stream=lambda i: jobs.stream(season_name(i), title.seasons[i].entry, user, episodes=True),
            download=lambda i: jobs.download(season_name(i), title.seasons[i].entry, user),
            download_all=lambda: jobs.download_all(
                title.name, [(s.label, s.entry) for s in title.seasons], user
            ),
        ))


__all__ = ["AudioVaultCommand", "MovieQuestion", "ResultsQuestion", "ShowQuestion"]
