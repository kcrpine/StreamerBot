"""Input device selection in SoundDeviceManager.

The default must land on PulseAudio's native monitor of the container sink, not
on index 0 (ALSA "pulse"), because the SDK's capture thread busy-waits on the
ALSA route. Explicit choices must still win. See CHANGELOG [058].
"""
import unittest
from types import SimpleNamespace

from bot.sound_devices import (
    NATIVE_MONITOR_INPUT,
    SoundDevice,
    SoundDeviceManager,
    SoundDeviceType,
)


def _device(name, id):
    return SoundDevice(name, id, SoundDeviceType.Input)


# The list the SDK returns inside the image, in its order (ids as observed).
CONTAINER_INPUTS = [
    _device("pulse", 0),
    _device("default", 1),
    _device("Default Sink", 2),
    _device("Default Source", 3),
    _device("StreamerBotSink", 4),
    _device(NATIVE_MONITOR_INPUT, 5),
    _device("TeamTalk Virtual Sound Device", 1978),
]


class _FakeTT:
    def __init__(self, inputs):
        self._inputs = inputs
        self.selected = None

    def get_input_devices(self):
        return self._inputs

    def set_input_device(self, id):
        self.selected = id


class _FakePlayer:
    def get_output_devices(self):
        return [SoundDevice("default", "auto", SoundDeviceType.Output)]

    def set_output_device(self, id):
        pass


def _select(inputs, input_device_name="", input_device=0):
    tt = _FakeTT(inputs)
    config = SimpleNamespace(
        sound_devices=SimpleNamespace(
            output_device_name="",
            output_device=0,
            input_device_name=input_device_name,
            input_device=input_device,
        )
    )
    bot = SimpleNamespace(config=config, player=_FakePlayer(), ttclient=tt)
    SoundDeviceManager(bot).initialize()
    return tt.selected


class InputDeviceSelectionTests(unittest.TestCase):
    def test_default_config_uses_the_native_pulse_monitor(self):
        self.assertEqual(_select(CONTAINER_INPUTS), 5)

    def test_a_configured_name_still_wins(self):
        self.assertEqual(_select(CONTAINER_INPUTS, input_device_name="default"), 1)

    def test_a_non_default_index_is_honoured(self):
        self.assertEqual(_select(CONTAINER_INPUTS, input_device=1), 1)

    def test_without_the_monitor_it_falls_back_to_index_zero(self):
        bare_metal = [_device("pulse", 0), _device("default", 1)]
        self.assertEqual(_select(bare_metal), 0)


if __name__ == "__main__":
    unittest.main()
