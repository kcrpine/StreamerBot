"""config / cf: the settings menu, saving one setting at a time, and its routing.

What these pin is the contract a screen reader user relies on: 0 is always
back, 99 is always restart and only appears when a restart is pending, a value
is saved the moment it is given, and a non-number closes the menu and runs as
an ordinary command so nobody is stuck in it.
"""

import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot import errors
from bot.commands import config_menu
from bot.commands.config_menu import RESTART, ConfigMenu, build_groups, read_setting, write_in_memory
from bot.config import ConfigManager
from bot.config.models import ConfigModel


def translate(text):
    return text


USER = SimpleNamespace(id=7, username="admin")


class FakeCommand:
    def __init__(self, config):
        self.config = config
        self.ttclient = Mock()
        self.translator = SimpleNamespace(
            translate=translate,
            get_locales=lambda: ["en", "ru"],
            set_locale=Mock(),
        )


def make_menu(config=None, write=None, clock=None):
    config = config or ConfigModel()
    command = FakeCommand(config)
    saved = []

    def default_write(setting, value):
        saved.append((setting.path, value))
        write_in_memory(config, setting.path, value)
        if setting.apply:
            setting.apply(value)

    restart = Mock()
    menu = ConfigMenu(
        build_groups=lambda: build_groups(command),
        translate=translate,
        read=lambda path: read_setting(config, path),
        write=write or default_write,
        restart=restart,
        **({"clock": clock} if clock else {}),
    )
    return menu, config, command, saved, restart


def group_number(menu, label):
    for number, group in enumerate(menu.build_groups(), 1):
        if group.label == label:
            return str(number)
    raise AssertionError(label)


def setting_number(menu, group_label, setting_label):
    for group in menu.build_groups():
        if group.label == group_label:
            for number, setting in enumerate(group.settings, 1):
                if setting.label == setting_label:
                    return str(number)
    raise AssertionError(setting_label)


class MenuTests(TestCase):
    def test_top_menu_has_no_restart_until_one_is_needed(self):
        menu, *_ = make_menu()
        self.assertNotIn("99.", menu.top_text())
        self.assertTrue(menu.top_text().endswith("0. Close settings"))

    def test_text_setting_is_saved_and_needs_a_restart(self):
        menu, config, _, saved, restart = make_menu()
        menu.answer(group_number(menu, "Server connection"))
        menu.answer(setting_number(menu, "Server connection", "Server address"))
        reply, still_open = menu.answer("tt.example.org")

        self.assertTrue(still_open)
        self.assertEqual(config.teamtalk.hostname, "tt.example.org")
        self.assertEqual(saved, [(("teamtalk", "hostname"), "tt.example.org")])
        self.assertTrue(reply.startswith("Saved. Server address is now tt.example.org. It takes effect after a restart."))
        self.assertIn(f"{RESTART}. Restart now to apply changes\n0. Back", reply)
        restart.assert_not_called()

    def test_a_port_out_of_range_is_refused_and_the_question_stays(self):
        menu, config, _, saved, _ = make_menu()
        menu.answer(group_number(menu, "Server connection"))
        menu.answer(setting_number(menu, "Server connection", "TCP port"))
        reply, still_open = menu.answer("70000")

        self.assertTrue(still_open)
        self.assertEqual(saved, [])
        self.assertTrue(reply.startswith("70000 is not a number from 1 to 65535."))
        self.assertTrue(reply.endswith("To keep it, send: 0"))

        reply, _ = menu.answer("10334")
        self.assertEqual(config.teamtalk.tcp_port, 10334)

    def test_zero_keeps_a_text_value(self):
        menu, config, _, saved, _ = make_menu()
        menu.answer(group_number(menu, "Bot identity"))
        menu.answer(setting_number(menu, "Bot identity", "Nickname"))
        reply, still_open = menu.answer("0")
        self.assertTrue(still_open)
        self.assertEqual(saved, [])
        self.assertTrue(reply.startswith("Bot identity."))

    def test_a_dash_clears_a_clearable_value(self):
        config = ConfigModel()
        config.teamtalk.channel = "/Music/"
        menu, config, _, _, _ = make_menu(config)
        menu.answer(group_number(menu, "Server connection"))
        prompt, _ = menu.answer(setting_number(menu, "Server connection", "Channel"))
        self.assertIn("To use the root channel, send a single dash: -", prompt)
        menu.answer("-")
        self.assertEqual(config.teamtalk.channel, "/")

    def test_a_number_channel_is_stored_as_an_id(self):
        menu, config, *_ = make_menu()
        menu.answer(group_number(menu, "Server connection"))
        menu.answer(setting_number(menu, "Server connection", "Channel"))
        menu.answer("12")
        self.assertEqual(config.teamtalk.channel, 12)

    def test_live_setting_is_applied_and_needs_no_restart(self):
        menu, config, command, _, _ = make_menu()
        menu.answer(group_number(menu, "Bot identity"))
        menu.answer(setting_number(menu, "Bot identity", "Nickname"))
        reply, _ = menu.answer("Radio Bot")

        command.ttclient.change_nickname.assert_called_once_with("Radio Bot")
        self.assertNotIn("restart", reply.lower())
        self.assertFalse(menu.restart_pending)

    def test_toggle_flips_with_one(self):
        menu, config, *_ = make_menu()
        menu.answer(group_number(menu, "Server connection"))
        prompt, _ = menu.answer(setting_number(menu, "Server connection", "Encrypted connection"))
        self.assertIn("To turn it on, send: 1", prompt)
        self.assertIn("To keep it off, send: 0", prompt)
        menu.answer("1")
        self.assertTrue(config.teamtalk.encrypted)

    def test_choice_marks_the_current_value(self):
        menu, config, command, _, _ = make_menu()
        menu.answer(group_number(menu, "Bot identity"))
        prompt, _ = menu.answer(setting_number(menu, "Bot identity", "Gender"))
        self.assertIn("1. Neutral, current", prompt)
        self.assertTrue(prompt.endswith("0. Keep Neutral"))
        menu.answer("3")
        self.assertEqual(config.teamtalk.gender, "f")
        command.ttclient.change_gender.assert_called_once_with("f")

    def test_language_change_applies_at_once(self):
        menu, config, command, _, _ = make_menu()
        menu.answer(group_number(menu, "Bot identity"))
        menu.answer(setting_number(menu, "Bot identity", "Language"))
        menu.answer("2")
        self.assertEqual(config.general.language, "ru")
        command.translator.set_locale.assert_called_once_with("ru")

    def test_sending_the_same_value_says_no_change(self):
        menu, config, _, saved, _ = make_menu()
        menu.answer(group_number(menu, "Bot identity"))
        menu.answer(setting_number(menu, "Bot identity", "Nickname"))
        reply, _ = menu.answer(config.teamtalk.nickname)
        self.assertTrue(reply.startswith("No change."))
        self.assertEqual(saved, [])

    def test_services_group_is_longer_than_nine_so_restart_is_ninety_nine(self):
        menu, *_ = make_menu()
        services = [g for g in menu.build_groups() if g.label == "Services"][0]
        self.assertGreater(len(services.settings), 9)
        menu.answer(group_number(menu, "Services"))
        menu.answer("9")
        # 9 is a service, not restart.
        self.assertEqual(menu.level, "setting")

    def test_ninety_nine_restarts_once_a_restart_is_pending(self):
        menu, config, _, _, restart = make_menu()
        menu.answer(group_number(menu, "Playback"))
        menu.answer(setting_number(menu, "Playback", "Volume when the bot starts"))
        menu.answer("40")
        reply, still_open = menu.answer(str(RESTART))
        self.assertEqual((reply, still_open), ("", False))
        restart.assert_called_once_with()

    def test_ninety_nine_with_nothing_pending_says_so(self):
        menu, _, _, _, restart = make_menu()
        reply, still_open = menu.answer(str(RESTART))
        self.assertTrue(still_open)
        self.assertTrue(reply.startswith("Nothing is waiting for a restart."))
        restart.assert_not_called()

    def test_restart_after_a_server_change_asks_first(self):
        menu, _, _, _, restart = make_menu()
        menu.answer(group_number(menu, "Server connection"))
        menu.answer(setting_number(menu, "Server connection", "Server address"))
        menu.answer("tt.example.org")
        reply, _ = menu.answer(str(RESTART))
        self.assertIn("streamerbot.sh", reply)
        restart.assert_not_called()
        reply, still_open = menu.answer("0")
        self.assertTrue(still_open)
        self.assertTrue(reply.startswith("Server connection."))
        menu.answer(str(RESTART))
        menu.answer("1")
        restart.assert_called_once_with()

    def test_closing_with_a_restart_pending_asks_once_more(self):
        menu, *_ = make_menu()
        menu.answer(group_number(menu, "Playback"))
        menu.answer(setting_number(menu, "Playback", "Default service"))
        menu.answer("2")
        menu.answer("0")
        reply, still_open = menu.answer("0")
        self.assertTrue(still_open)
        self.assertIn("To restart now, send: 99", reply)
        reply, still_open = menu.answer("0")
        self.assertFalse(still_open)
        self.assertIn("To restart later, send: rs", reply)

    def test_closing_with_nothing_pending(self):
        menu, *_ = make_menu()
        self.assertEqual(menu.answer("0"), ("Settings closed.", False))

    def test_a_non_number_is_not_an_answer(self):
        menu, *_ = make_menu()
        self.assertIsNone(menu.answer("v 50"))
        menu.answer(group_number(menu, "Playback"))
        self.assertIsNone(menu.answer("p hello"))

    def test_menu_expires(self):
        now = [0.0]
        menu, *_ = make_menu(clock=lambda: now[0])
        now[0] = 31 * 60
        self.assertTrue(menu.expired())

    def test_a_failed_save_is_reported_and_nothing_changes(self):
        def failing(setting, value):
            raise OSError("disk full")

        menu, config, *_ = make_menu(write=failing)
        menu.answer(group_number(menu, "Bot identity"))
        menu.answer(setting_number(menu, "Bot identity", "Nickname"))
        reply, _ = menu.answer("Other")
        self.assertTrue(reply.startswith("That could not be saved: disk full"))
        self.assertEqual(config.teamtalk.nickname, "StreamerBot")
        self.assertFalse(menu.restart_pending)

    def test_long_values_are_shortened_in_the_menu(self):
        config = ConfigModel()
        config.teamtalk.status = "x" * 200
        menu, *_ = make_menu(config)
        reply, _ = menu.answer(group_number(menu, "Bot identity"))
        line = [l for l in reply.split("\n") if l.startswith("2. Status message: ")][0]
        self.assertTrue(line.endswith("…"))
        self.assertLess(len(line), 100)


class StartupCommandTests(TestCase):
    def open_commands(self, commands=()):
        config = ConfigModel()
        config.general.start_commands = list(commands)
        menu, config, _, saved, _ = make_menu(config)
        reply, _ = menu.answer(group_number(menu, "Startup commands"))
        return menu, config, saved, reply

    def test_commands_are_not_numbered_beside_the_actions(self):
        _, _, _, reply = self.open_commands(["u http://a", "v 40"])
        self.assertIn("Command 1: u http://a\nCommand 2: v 40\nSend a number:\n1. Add a command", reply)

    def test_empty_list(self):
        _, _, _, reply = self.open_commands()
        self.assertIn("No commands yet.", reply)
        self.assertNotIn("2. Remove", reply)

    def test_add_saves_and_needs_a_restart(self):
        menu, config, saved, _ = self.open_commands()
        menu.answer("1")
        reply, _ = menu.answer("u http://example.org:8000/live.mp3")
        self.assertEqual(config.general.start_commands, ["u http://example.org:8000/live.mp3"])
        self.assertEqual(saved[-1], (("general", "start_commands"), ["u http://example.org:8000/live.mp3"]))
        self.assertTrue(reply.startswith("Saved. Added the startup command: u http://example.org:8000/live.mp3 It takes effect after a restart."))
        self.assertIn("99. Restart now to apply changes", reply)

    def test_zero_while_adding_keeps_the_list(self):
        menu, config, saved, _ = self.open_commands()
        menu.answer("1")
        menu.answer("0")
        self.assertEqual(saved, [])
        self.assertEqual(menu.level, "commands")

    def test_remove_one(self):
        menu, config, _, _ = self.open_commands(["a", "b"])
        prompt, _ = menu.answer("2")
        self.assertIn("1. a\n2. b", prompt)
        menu.answer("1")
        self.assertEqual(config.general.start_commands, ["b"])

    def test_remove_all_asks_first(self):
        menu, config, saved, _ = self.open_commands(["a", "b"])
        prompt, _ = menu.answer("3")
        self.assertIn("This removes all 2 startup commands.", prompt)
        menu.answer("0")
        self.assertEqual(config.general.start_commands, ["a", "b"])
        menu.answer("3")
        menu.answer("1")
        self.assertEqual(config.general.start_commands, [])


class SaveSettingTests(TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "config.json")
        with open(self.path, "w", encoding="UTF-8") as f:
            json.dump(ConfigModel(config_version=ConfigManager.version).model_dump(), f)
        self.manager = ConfigManager(self.path)

    def tearDown(self):
        self.manager.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def load(self):
        with open(self.path, encoding="UTF-8") as f:
            return json.load(f)

    def test_writes_only_the_one_key_and_keeps_edits_made_on_disk(self):
        # Someone edits the file with streamerbot.sh while the bot runs...
        self.manager.file_locker.release()
        data = self.load()
        data["teamtalk"]["nickname"] = "Changed on disk"
        with open(self.path, "w", encoding="UTF-8") as f:
            json.dump(data, f)
        self.manager.file_locker.acquire()
        # ...and something else is changed in memory without being saved.
        self.manager.config.general.language = "ru"

        self.manager.save_setting(("teamtalk", "hostname"), "tt.example.org")

        data = self.load()
        self.assertEqual(data["teamtalk"]["hostname"], "tt.example.org")
        self.assertEqual(data["teamtalk"]["nickname"], "Changed on disk")
        self.assertEqual(data["general"]["language"], "en")

    def test_leaves_no_temporary_file_behind(self):
        self.manager.save_setting(("general", "start_commands"), ["u http://a"])
        self.assertEqual(os.listdir(self.directory), ["config.json"])
        self.assertEqual(self.load()["general"]["start_commands"], ["u http://a"])

    def test_creates_a_missing_section(self):
        self.manager.file_locker.release()
        data = self.load()
        del data["auth_portal"]
        with open(self.path, "w", encoding="UTF-8") as f:
            json.dump(data, f)
        self.manager.file_locker.acquire()
        self.manager.save_setting(("auth_portal", "host"), "0.0.0.0")
        self.assertEqual(self.load()["auth_portal"], {"host": "0.0.0.0"})


class ConfigCommandTests(TestCase):
    def make(self):
        from bot.commands.admin_commands import ConfigCommand

        command = object.__new__(ConfigCommand)
        command.config = ConfigModel()
        command.config_manager = Mock()
        command.ttclient = Mock()
        command.translator = SimpleNamespace(translate=translate, get_locales=lambda: ["en"])
        command.command_processor = SimpleNamespace(pending_config_menu={})
        return command

    def test_opens_the_menu_for_that_user(self):
        command = self.make()
        reply = command("", USER)
        self.assertTrue(reply.startswith("Settings."))
        self.assertIsInstance(command.command_processor.pending_config_menu[USER.id], ConfigMenu)

    def test_a_password_is_registered_for_redaction_before_it_is_saved(self):
        from bot.auth import redaction

        command = self.make()
        order = []
        command.config_manager.save_setting.side_effect = lambda *a: order.append(
            "registered" if redaction.get_filter().scrub("secret-pass") != "secret-pass" else "not yet"
        )
        menu, *_ = make_menu()
        setting = [s for s in menu.build_groups()[0].settings if s.label == "Password"][0]
        command._write(setting, "secret-pass")
        self.assertEqual(order, ["registered"])
        self.assertEqual(command.config.teamtalk.password, "secret-pass")
        redaction.get_filter().clear()

    def test_admin_only_and_both_names(self):
        from bot.commands import CommandProcessor
        from bot.commands.admin_commands import ConfigCommand

        processor = object.__new__(CommandProcessor)
        processor.commands_dict = {}
        processor.admin_commands_dict = {"config": ConfigCommand, "cf": ConfigCommand}
        admin = SimpleNamespace(is_admin=True, type=None)
        someone = SimpleNamespace(is_admin=False, type=None)
        self.assertIs(CommandProcessor.get_command(processor, "cf", admin), ConfigCommand)
        self.assertIs(CommandProcessor.get_command(processor, "config", admin), ConfigCommand)
        with self.assertRaises(errors.UnknownCommandError):
            CommandProcessor.get_command(processor, "cf", someone)

    def test_slash_config_parses_as_config(self):
        from bot.commands import CommandProcessor

        self.assertEqual(CommandProcessor.parse_command(None, "/config"), ("config", ""))

    def test_registered_in_the_real_processor(self):
        import inspect
        from bot.commands import CommandProcessor

        source = inspect.getsource(CommandProcessor.__init__)
        self.assertIn('"config": admin_commands.ConfigCommand', source)
        self.assertIn('"cf": admin_commands.ConfigCommand', source)


class RoutingTests(TestCase):
    def setUp(self):
        from bot.commands import CommandProcessor

        self.sent = []
        self.processor = SimpleNamespace(
            pending_ad_prompt={},
            pending_playlist_choice={},
            pending_config_menu={},
            pending_playlist_download={},
            pending_ads_option={},
            pending_podcast_count={},
            ttclient=SimpleNamespace(send_message=lambda text, user=None: self.sent.append(text)),
            translator=SimpleNamespace(translate=translate),
            parse_command=Mock(side_effect=errors.ParseCommandError()),
        )
        self.run = lambda text: CommandProcessor._run(
            self.processor, SimpleNamespace(user=USER, text=text)
        )

    def test_an_answer_goes_to_the_menu_and_keeps_it_open(self):
        menu, *_ = make_menu()
        self.processor.pending_config_menu[USER.id] = menu
        self.run("1")
        self.assertIs(self.processor.pending_config_menu[USER.id], menu)
        self.assertTrue(self.sent[0].startswith("Server connection."))
        self.processor.parse_command.assert_not_called()

    def test_typed_text_is_the_value_not_a_command(self):
        menu, config, *_ = make_menu()
        self.processor.pending_config_menu[USER.id] = menu
        self.run("2")
        self.run("1")
        self.run("p hello")
        self.assertEqual(config.teamtalk.nickname, "p hello")
        self.processor.parse_command.assert_not_called()

    def test_a_non_number_closes_the_menu_and_runs_as_a_command(self):
        menu, *_ = make_menu()
        self.processor.pending_config_menu[USER.id] = menu
        self.run("v 50")
        self.assertNotIn(USER.id, self.processor.pending_config_menu)
        self.processor.parse_command.assert_called_once_with("v 50")

    def test_expired_menu_says_so(self):
        now = [0.0]
        menu, *_ = make_menu(clock=lambda: now[0])
        self.processor.pending_config_menu[USER.id] = menu
        now[0] = 31 * 60
        self.run("1")
        self.assertEqual(self.sent, ["That menu has expired. To open it again, send: cf"])
        self.assertNotIn(USER.id, self.processor.pending_config_menu)
