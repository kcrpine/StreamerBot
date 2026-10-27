"""`config` (also `cf`): the bot's settings as a numbered menu in a private message.

An admin changes a setting by sending numbers, and each change is written to
config.json the moment it is made, so there is no `sc` to forget. When a change
only takes effect after a restart, every menu from then on ends with

    99. Restart now to apply changes
    0. Back

99 rather than 9, because the Services group has more than nine entries and a
number must never mean two things. It appears only while a restart is pending,
always in the same place, just before 0.

The rules are the ones `dlp`'s playlist question follows (playlist_choice.py):
the command a person has to send is last on its line after a colon, so the
review cursor finds it at the end; anything that is not a number, sent while
the menu wants a number, closes the menu and runs as an ordinary command, so
nobody is stuck inside it; a menu left alone for half an hour is dropped.

Where the menu asks for typed text, such as a server address, everything sent
is the answer, because a host name or a nickname can be any word at all. 0
keeps the old value there too. Clearing a value is a single dash, named in
words: a lone full stop is silent to a screen reader at its usual punctuation
level.

A confirmation and the menu that follows it go in one message, confirmation
first. Two messages can be announced out of order, and the second interrupts
the first.

Reviewed by the accessibility lead before it was written; the decisions above
are from that review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union

TEXT = "text"
NUMBER = "number"
TOGGLE = "toggle"
CHOICE = "choice"
COMMANDS = "commands"

BACK = 0
RESTART = 99
CLEAR = "-"
EXPIRES_AFTER_SECONDS = 30 * 60
# Long values (a status message, a URL) are cut in the menu and shown whole in
# the setting's own question.
MENU_VALUE_LENGTH = 60
ECHO_LENGTH = 30

# (reply to send, whether the menu stays open). None means "not an answer".
Outcome = Optional[Tuple[str, bool]]


@dataclass
class Setting:
    path: Tuple[str, ...]
    label: str
    kind: str
    restart: bool = False
    # Server address, ports, login and channel: a wrong value there means the
    # bot cannot come back to say so, and restarting asks first.
    connection: bool = False
    minimum: int = 0
    maximum: Union[int, Callable[[], int]] = 0
    # What to ask for, for a text setting: "Send the new address."
    ask: str = ""
    # Text settings that may be empty, the value they clear to, and what the
    # prompt calls clearing ("To use the root channel").
    clearable: bool = False
    clear_value: Any = ""
    clear_label: str = ""
    # CHOICE: (stored value, spoken label) pairs.
    choices: Callable[[], List[Tuple[Any, str]]] = lambda: []
    # Called with the new value once it is saved, for settings the bot can
    # take up at once (nickname, status, gender, language).
    apply: Optional[Callable[[Any], None]] = None
    # Shown in full, as the user chose, and registered for log redaction.
    secret: bool = False


@dataclass
class Group:
    label: str
    settings: List[Setting] = field(default_factory=list)
    # An extra sentence under the group's heading.
    note: str = ""


def shorten(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


class ConfigMenu:
    """One admin's walk through the settings.

    build_groups is called on every render rather than once, so the menu
    follows a language change made in it and always shows current values.
    """

    def __init__(
        self,
        build_groups: Callable[[], List[Group]],
        translate: Callable[[str], str],
        read: Callable[[Sequence[str]], Any],
        write: Callable[[Setting, Any], None],
        restart: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.build_groups = build_groups
        self.translate = translate
        self.read = read
        self.write = write
        self.restart = restart
        self._clock = clock
        self._touched = clock()
        self.restart_pending = False
        self.connection_changed = False
        # Where the menu is: top, group, setting, commands, commands_add,
        # commands_remove, commands_clear, confirm_restart or closing.
        self.level = "top"
        self.group_index = 0
        self.setting_index = 0
        # Where to go back to from confirm_restart.
        self._before_confirm = "top"

    # -- state -------------------------------------------------------------

    def expired(self) -> bool:
        return self._clock() - self._touched > EXPIRES_AFTER_SECONDS

    def _group(self) -> Group:
        return self.build_groups()[self.group_index]

    def _setting(self) -> Setting:
        return self._group().settings[self.setting_index]

    @staticmethod
    def _is_commands_group(group: Group) -> bool:
        return len(group.settings) == 1 and group.settings[0].kind == COMMANDS

    def _maximum(self, setting: Setting) -> int:
        return setting.maximum() if callable(setting.maximum) else setting.maximum

    # -- what a value sounds like -------------------------------------------

    def value_text(self, setting: Setting, value: Any = None, read: bool = True) -> str:
        if read and value is None:
            value = self.read(setting.path)
        if setting.kind == TOGGLE:
            return self.translate("on") if value else self.translate("off")
        if setting.kind == CHOICE:
            for stored, label in setting.choices():
                if stored == value:
                    return label
            return str(value)
        if value is None or value == "":
            return self.translate("not set")
        return str(value)

    # -- menus ---------------------------------------------------------------

    def _menu_end(self, back_label: str) -> List[str]:
        lines = []
        if self.restart_pending:
            lines.append(f"{RESTART}. " + self.translate("Restart now to apply changes"))
        lines.append(f"{BACK}. {back_label}")
        return lines

    def top_text(self) -> str:
        lines = [self.translate("Settings. Each change is saved as soon as you make it. Send a number:")]
        for number, group in enumerate(self.build_groups(), 1):
            lines.append(f"{number}. {group.label}")
        lines += self._menu_end(self.translate("Close settings"))
        return "\n".join(lines)

    def group_text(self) -> str:
        group = self._group()
        if self._is_commands_group(group):
            return self.commands_text()
        restarts = [setting.restart for setting in group.settings]
        heading = [group.label + "."]
        if group.note:
            heading.append(group.note)
        if all(restarts):
            heading.append(self.translate("Changes here take effect after a restart."))
        elif any(restarts):
            heading.append(self.translate("Some changes here take effect after a restart."))
        heading.append(self.translate("Send a number:"))
        lines = [" ".join(heading)]
        for number, setting in enumerate(group.settings, 1):
            value = shorten(self.value_text(setting), MENU_VALUE_LENGTH)
            lines.append(f"{number}. {setting.label}: {value}")
        lines += self._menu_end(self.translate("Back"))
        return "\n".join(lines)

    def commands_text(self) -> str:
        commands = self._commands()
        # The commands are not numbered here: "1. u http://..." beside
        # "1. Add a command" would be two different ones in one message.
        if commands:
            lines = [self.translate("Startup commands, run each time the bot starts:")]
            lines += [
                self.translate("Command {number}: {command}").format(number=number, command=command)
                for number, command in enumerate(commands, 1)
            ]
        else:
            lines = [self.translate("Startup commands, run each time the bot starts. No commands yet.")]
        lines.append(self.translate("Send a number:"))
        lines.append("1. " + self.translate("Add a command"))
        if commands:
            lines.append("2. " + self.translate("Remove a command"))
            lines.append("3. " + self.translate("Remove all commands"))
        lines += self._menu_end(self.translate("Back"))
        return "\n".join(lines)

    def setting_prompt(self) -> str:
        setting = self._setting()
        current = self.value_text(setting)
        if setting.kind == TOGGLE:
            on = bool(self.read(setting.path))
            return "\n".join([
                self.translate("{setting} is {value}.").format(setting=setting.label, value=current),
                self.translate("To turn it off, send: 1") if on else self.translate("To turn it on, send: 1"),
                self.translate("To keep it on, send: 0") if on else self.translate("To keep it off, send: 0"),
            ])
        if setting.kind == CHOICE:
            value = self.read(setting.path)
            lines = [
                self.translate("{setting} is {value}. Send a number:").format(
                    setting=setting.label, value=current
                )
            ]
            for number, (stored, label) in enumerate(setting.choices(), 1):
                if stored == value:
                    label = self.translate("{choice}, current").format(choice=label)
                lines.append(f"{number}. {label}")
            lines.append(f"{BACK}. " + self.translate("Keep {value}").format(value=current))
            return "\n".join(lines)
        lines = [
            self.translate("{setting} is {value}.").format(setting=setting.label, value=current)
            + " " + (self.range_text(setting) if setting.kind == NUMBER else setting.ask)
        ]
        if setting.clearable:
            lines.append(
                (setting.clear_label or self.translate("To clear it"))
                + ", " + self.translate("send a single dash: {dash}").format(dash=CLEAR)
            )
        lines.append(self.translate("To keep it, send: 0"))
        return "\n".join(lines)

    def range_text(self, setting: Setting) -> str:
        return self.translate("Send a number from {minimum} to {maximum}.").format(
            minimum=setting.minimum, maximum=self._maximum(setting)
        )

    def closing_text(self) -> str:
        return "\n".join([
            self.translate("Some changes take effect only after a restart."),
            self.translate("To restart now, send: 99"),
            self.translate("To close without restarting, send: 0"),
        ])

    def confirm_restart_text(self) -> str:
        return "\n".join([
            self.translate(
                "The bot will restart with the new server details. If they are wrong, it cannot "
                "reconnect, and they can then only be fixed on the server with streamerbot.sh."
            ),
            self.translate("To restart, send: 1"),
            self.translate("To go back, send: 0"),
        ])

    def closed_text(self) -> str:
        if self.restart_pending:
            return "\n".join([
                self.translate("Settings closed. Some changes still need a restart."),
                self.translate("To restart later, send: rs"),
            ])
        return self.translate("Settings closed.")

    # -- after a change -------------------------------------------------------

    def _commit(self, setting: Setting, value: Any) -> str:
        if value == self.read(setting.path):
            return self.translate("No change. {setting} is still {value}.").format(
                setting=setting.label, value=self.value_text(setting, value, read=False)
            )
        try:
            self.write(setting, value)
        except Exception as error:
            return self.translate("That could not be saved: {error}").format(error=error)
        said = self.translate("Saved. {setting} is now {value}.").format(
            setting=setting.label, value=self.value_text(setting, value, read=False)
        )
        return self._after_saving(setting, said)

    def _after_saving(self, setting: Setting, said: str) -> str:
        if not setting.restart:
            return said
        self.restart_pending = True
        if setting.connection:
            self.connection_changed = True
        return said + " " + self.translate("It takes effect after a restart.")

    # -- answers -------------------------------------------------------------

    def answer(self, text: str) -> Outcome:
        self._touched = self._clock()
        if self._wants_text():
            return self._answer_text(text.strip())
        try:
            number = int(text.strip())
        except ValueError:
            return None
        if number == RESTART and self.level not in ("closing", "confirm_restart"):
            return self._restart_asked()
        return getattr(self, "_answer_" + self.level)(number)

    def _wants_text(self) -> bool:
        if self.level == "commands_add":
            return True
        return self.level == "setting" and self._setting().kind in (TEXT, NUMBER)

    def _current_menu(self) -> str:
        if self.level == "top":
            return self.top_text()
        if self.level in ("commands", "commands_add", "commands_remove", "commands_clear"):
            return self.commands_text()
        if self.level == "setting":
            return self.setting_prompt()
        return self.group_text()

    def _restart_asked(self) -> Tuple[str, bool]:
        if not self.restart_pending:
            return "\n\n".join([
                self.translate("Nothing is waiting for a restart."), self._current_menu()
            ]), True
        if self.connection_changed:
            self._before_confirm = self.level
            self.level = "confirm_restart"
            return self.confirm_restart_text(), True
        return self._restart_now()

    def _restart_now(self) -> Tuple[str, bool]:
        self.restart()
        return "", False

    def _answer_confirm_restart(self, number: int) -> Tuple[str, bool]:
        if number == 1:
            return self._restart_now()
        if number == BACK:
            self.level = self._before_confirm
            if self.level == "setting":
                self.level = "group"
            return self._current_menu(), True
        return self.confirm_restart_text(), True

    def _back_to_group(self, reply: str = "") -> Tuple[str, bool]:
        self.level = "group"
        return "\n".join(part for part in (reply, self.group_text()) if part), True

    def _answer_top(self, number: int) -> Tuple[str, bool]:
        groups = self.build_groups()
        if number == BACK:
            if self.restart_pending:
                self.level = "closing"
                return self.closing_text(), True
            return self.closed_text(), False
        if 1 <= number <= len(groups):
            self.group_index = number - 1
            self.level = "commands" if self._is_commands_group(groups[self.group_index]) else "group"
            return self.group_text(), True
        return self._no_such_number(number, len(groups), self.top_text()), True

    def _answer_closing(self, number: int) -> Tuple[str, bool]:
        if number == RESTART:
            if self.connection_changed:
                self._before_confirm = "top"
                self.level = "confirm_restart"
                return self.confirm_restart_text(), True
            return self._restart_now()
        if number == BACK:
            return self.closed_text(), False
        return self.closing_text(), True

    def _answer_group(self, number: int) -> Tuple[str, bool]:
        group = self._group()
        if number == BACK:
            self.level = "top"
            return self.top_text(), True
        if 1 <= number <= len(group.settings):
            self.setting_index = number - 1
            self.level = "setting"
            return self.setting_prompt(), True
        return self._no_such_number(number, len(group.settings), self.group_text()), True

    def _answer_setting(self, number: int) -> Tuple[str, bool]:
        setting = self._setting()
        if number == BACK:
            return self._back_to_group()
        if setting.kind == TOGGLE:
            if number != 1:
                return self.setting_prompt(), True
            return self._back_to_group(self._commit(setting, not bool(self.read(setting.path))))
        choices = setting.choices()
        if 1 <= number <= len(choices):
            return self._back_to_group(self._commit(setting, choices[number - 1][0]))
        return self._no_such_number(number, len(choices), self.setting_prompt()), True

    def _answer_text(self, value: str) -> Tuple[str, bool]:
        if self.level == "commands_add":
            return self._add_command(value)
        setting = self._setting()
        if value == str(BACK):
            return self._back_to_group()
        if value == CLEAR and setting.clearable:
            return self._back_to_group(self._commit(setting, setting.clear_value))
        if setting.kind == NUMBER:
            try:
                number: Optional[int] = int(value)
            except ValueError:
                number = None
            if number is None or not setting.minimum <= number <= self._maximum(setting):
                return "\n".join([
                    self.translate("{value} is not a number from {minimum} to {maximum}.").format(
                        value=shorten(value, ECHO_LENGTH), minimum=setting.minimum,
                        maximum=self._maximum(setting),
                    ),
                    self.range_text(setting),
                    self.translate("To keep it, send: 0"),
                ]), True
            return self._back_to_group(self._commit(setting, number))
        if not value:
            return self.setting_prompt(), True
        if setting.path[-1] == "channel" and value.isdigit():
            # A number is a channel id, as config.json has always allowed.
            return self._back_to_group(self._commit(setting, int(value)))
        return self._back_to_group(self._commit(setting, value))

    # -- startup commands ------------------------------------------------------

    def _commands_setting(self) -> Setting:
        return self._group().settings[0]

    def _commands(self) -> List[str]:
        return list(self.read(self._commands_setting().path) or [])

    def _commands_back(self, reply: str = "") -> Tuple[str, bool]:
        self.level = "commands"
        return "\n".join(part for part in (reply, self.commands_text()) if part), True

    def _save_commands(self, commands: List[str], said: str) -> Tuple[str, bool]:
        setting = self._commands_setting()
        try:
            self.write(setting, commands)
        except Exception as error:
            return self._commands_back(
                self.translate("That could not be saved: {error}").format(error=error)
            )
        return self._commands_back(self._after_saving(setting, said))

    def _answer_commands(self, number: int) -> Tuple[str, bool]:
        commands = self._commands()
        if number == BACK:
            self.level = "top"
            return self.top_text(), True
        if number == 1:
            self.level = "commands_add"
            return "\n".join([
                self.translate(
                    "Send the command to run when the bot starts, for example: u http://example.org:8000/live.mp3"
                ),
                self.translate("To keep the list as it is, send: 0"),
            ]), True
        if number == 2 and commands:
            self.level = "commands_remove"
            lines = [self.translate("Send the number of the command to remove:")]
            lines += [f"{n}. {command}" for n, command in enumerate(commands, 1)]
            lines.append(f"{BACK}. " + self.translate("Keep the list as it is"))
            return "\n".join(lines), True
        if number == 3 and commands:
            self.level = "commands_clear"
            return "\n".join([
                self.translate("This removes all {count} startup commands.").format(count=len(commands))
                if len(commands) > 1 else self.translate("This removes the only startup command."),
                self.translate("To remove them, send: 1") if len(commands) > 1
                else self.translate("To remove it, send: 1"),
                self.translate("To keep the list as it is, send: 0"),
            ]), True
        return self.commands_text(), True

    def _answer_commands_clear(self, number: int) -> Tuple[str, bool]:
        if number == 1:
            return self._save_commands([], self.translate("Saved. All startup commands were removed."))
        return self._commands_back()

    def _add_command(self, value: str) -> Tuple[str, bool]:
        if value == str(BACK) or not value:
            return self._commands_back()
        return self._save_commands(
            self._commands() + [value],
            self.translate("Saved. Added the startup command: {command}").format(command=value),
        )

    def _answer_commands_remove(self, number: int) -> Tuple[str, bool]:
        commands = self._commands()
        if number == BACK:
            return self._commands_back()
        if 1 <= number <= len(commands):
            removed = commands.pop(number - 1)
            return self._save_commands(
                commands,
                self.translate("Saved. Removed the startup command: {command}").format(command=removed),
            )
        return self.translate(
            "There is no {number} here. Send a number from 1 to {last}, or to go back, send: 0"
        ).format(number=number, last=len(commands)), True

    # -- helpers -------------------------------------------------------------

    def _no_such_number(self, number: int, last: int, menu: str) -> str:
        said = self.translate(
            "There is no {number} here. Send a number from 1 to {last}, or to go back, send: 0"
        ).format(number=number, last=last)
        return "\n\n".join(part for part in (said, menu) if part)


def build_groups(command: Any) -> List[Group]:
    """The settings the menu offers, with labels in the bot's current language.

    A chosen list rather than every key in config.json: ports the bot allocates
    itself, log formats and the like stay in streamerbot.sh, where a wrong value
    is less likely to take the bot off the server.
    """
    from bot import auth
    from bot.config.inspection import SERVICE_NAMES

    # Called translate, not t: Babel extracts translate(...) calls only.
    translate = command.translator.translate
    ttclient = command.ttclient
    translator = command.translator
    config = command.config

    def change_language(value: str) -> None:
        translator.set_locale(value)
        ttclient.change_status_text("")

    def port(name: str, label: str) -> Setting:
        return Setting(("teamtalk", name), label, NUMBER, restart=True, connection=True,
                       minimum=1, maximum=65535)

    return [
        Group(translate("Server connection"), [
            Setting(("teamtalk", "hostname"), translate("Server address"), TEXT, restart=True,
                    connection=True, ask=translate("Send the new address.")),
            port("tcp_port", translate("TCP port")),
            port("udp_port", translate("UDP port")),
            Setting(("teamtalk", "encrypted"), translate("Encrypted connection"), TOGGLE,
                    restart=True, connection=True),
            Setting(("teamtalk", "username"), translate("Username"), TEXT, restart=True,
                    connection=True, ask=translate("Send the new username."), clearable=True),
            Setting(("teamtalk", "password"), translate("Password"), TEXT, restart=True,
                    connection=True, ask=translate("Send the new password."), clearable=True,
                    secret=True),
            Setting(("teamtalk", "channel"), translate("Channel"), TEXT, restart=True,
                    connection=True,
                    ask=translate("Send the channel's path, such as /Music/, or its number."),
                    clearable=True, clear_value="/",
                    clear_label=translate("To use the root channel")),
            Setting(("teamtalk", "channel_password"), translate("Channel password"), TEXT,
                    restart=True, connection=True,
                    ask=translate("Send the new channel password."), clearable=True, secret=True),
        ], note=translate("Passwords are shown in this chat, and your TeamTalk client keeps them in its history.")),
        Group(translate("Bot identity"), [
            Setting(("teamtalk", "nickname"), translate("Nickname"), TEXT,
                    ask=translate("Send the new nickname."), apply=ttclient.change_nickname),
            Setting(("teamtalk", "status"), translate("Status message"), TEXT,
                    ask=translate("Send the new status message."), clearable=True,
                    apply=ttclient.change_status_text),
            Setting(("teamtalk", "gender"), translate("Gender"), CHOICE,
                    choices=lambda: [("n", translate("Neutral")), ("m", translate("Male")), ("f", translate("Female"))],
                    apply=ttclient.change_gender),
            Setting(("general", "language"), translate("Language"), CHOICE,
                    choices=lambda: [(code, language_name(code)) for code in translator.get_locales()],
                    apply=change_language),
        ]),
        Group(translate("Startup commands"), [
            Setting(("general", "start_commands"), translate("Startup commands"), COMMANDS, restart=True),
        ]),
        Group(translate("Playback"), [
            Setting(("player", "default_volume"), translate("Volume when the bot starts"), NUMBER,
                    restart=True, minimum=0, maximum=lambda: config.player.max_volume),
            Setting(("player", "max_volume"), translate("Highest volume allowed"), NUMBER,
                    minimum=1, maximum=200),
            Setting(("player", "seek_step"), translate("Seconds to skip when seeking"), NUMBER,
                    minimum=1, maximum=3600),
            Setting(("player", "stop_when_solo"), translate("Stop when nobody else is in the channel"),
                    TOGGLE),
            Setting(("general", "search_results_mode"), translate("Search results mode"), TOGGLE),
            Setting(("services", "default_service"), translate("Default service"), CHOICE,
                    restart=True,
                    choices=lambda: [(name, auth.service_name(name)) for name in SERVICE_NAMES]),
        ]),
        Group(translate("Files and downloads"), [
            Setting(("general", "delete_uploaded_files_after"),
                    translate("Seconds before uploaded files are deleted, 0 for never"), NUMBER,
                    minimum=0, maximum=31536000),
        ]),
        Group(translate("Messages"), [
            Setting(("general", "send_channel_messages"), translate("Channel messages"), TOGGLE),
            Setting(("audio_description", "default"), translate("Audio description"), CHOICE,
                    restart=True,
                    choices=lambda: [("ask", translate("Ask each time")), ("always", translate("Always")),
                                     ("never", translate("Never"))]),
        ]),
        Group(translate("Services"), [
            Setting(("services", name, "enabled"), auth.service_name(name), TOGGLE, restart=True)
            for name in SERVICE_NAMES
        ]),
        Group(translate("Account portal"), [
            Setting(("auth_portal", "enabled"), translate("Account portal"), TOGGLE, restart=True),
            Setting(("auth_portal", "host"), translate("Who can open the portal"), CHOICE, restart=True,
                    choices=lambda: [("127.0.0.1", translate("Only this computer")),
                                     ("0.0.0.0", translate("Any computer that can reach this one"))]),
            Setting(("auth_portal", "public_url"), translate("Public address"), TEXT,
                    ask=translate("Send the address people use to reach the portal, such as http://example.org:4419"),
                    clearable=True),
        ]),
    ]


def language_name(code: str) -> str:
    """The language's own name for itself, which is what someone looking for it knows."""
    try:
        from babel import Locale

        name = Locale.parse(code).get_display_name(code)
        if name:
            return name[0].upper() + name[1:]
    except Exception:
        pass
    return code


def read_setting(config: Any, path: Sequence[str]) -> Any:
    node = config
    for key in path:
        node = getattr(node, key)
    return node


def write_in_memory(config: Any, path: Sequence[str], value: Any) -> None:
    """Change the live model in place.

    TeamTalk, Player and ServiceManager hold references to sub-models, so the
    sub-model itself is changed and never replaced.
    """
    node = config
    for key in path[:-1]:
        node = getattr(node, key)
    setattr(node, path[-1], value)


__all__ = [
    "BACK",
    "CHOICE",
    "CLEAR",
    "COMMANDS",
    "ConfigMenu",
    "Group",
    "NUMBER",
    "RESTART",
    "Setting",
    "TEXT",
    "TOGGLE",
    "build_groups",
    "language_name",
    "read_setting",
    "shorten",
    "write_in_memory",
]
