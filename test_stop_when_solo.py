"""player.stop_when_solo: whether being left alone in a channel stops playback.

It used to stop unconditionally, which threw away whatever was streaming the
moment the last listener stepped out. Off by default; the move back to the
default channel is unaffected either way.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from bot.config.models import ConfigModel, PlayerModel
from bot.player.enums import State as PlayerState
from bot.TeamTalk.thread import TeamTalkThread


def make_thread(stop_when_solo, state=PlayerState.Playing):
    thread = TeamTalkThread.__new__(TeamTalkThread)
    player = Mock(state=state)
    thread.bot = SimpleNamespace(
        config=SimpleNamespace(player=PlayerModel(stop_when_solo=stop_when_solo)),
        player=player,
    )
    return thread, player


class StopWhenSoloTests(TestCase):
    def test_default_is_off(self):
        self.assertFalse(PlayerModel().stop_when_solo)
        self.assertFalse(ConfigModel().player.stop_when_solo)

    def test_a_config_without_the_key_keeps_playing(self):
        """Existing bots' config.json predates the key and must get the new default."""
        self.assertFalse(ConfigModel(player={"default_volume": 50}).player.stop_when_solo)

    def test_off_keeps_playing(self):
        thread, player = make_thread(stop_when_solo=False)
        thread.stop_if_solo_stop_enabled()
        player.stop.assert_not_called()

    def test_on_stops_playback(self):
        thread, player = make_thread(stop_when_solo=True)
        thread.stop_if_solo_stop_enabled()
        player.stop.assert_called_once_with()

    def test_on_does_not_stop_what_is_already_stopped(self):
        thread, player = make_thread(stop_when_solo=True, state=PlayerState.Stopped)
        thread.stop_if_solo_stop_enabled()
        player.stop.assert_not_called()
