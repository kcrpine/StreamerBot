from __future__ import annotations
from enum import Enum
import logging
import sys
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from bot import Bot


class SoundDevice:
    def __init__(self, name: str, id: Union[int, str], type: SoundDeviceType) -> None:
        self.name = name
        self.id = id
        self.type = type


class SoundDeviceType(Enum):
    Output = 0
    Input = 1


# PulseAudio's own capture of the sink entrypoint.sh creates, as the TeamTalk SDK
# lists it under SOUNDSYSTEM_PULSEAUDIO. Index 0, the configured default, is the
# ALSA "pulse" device instead: the same audio routed through the ALSA plugin, and
# the SDK's capture thread busy-waits on that route, burning a quarter of a core
# or more per bot while nothing is even playing. See CHANGELOG [058].
NATIVE_MONITOR_INPUT = "StreamerBotSink.monitor"


class SoundDeviceManager:
    def __init__(self, bot: Bot) -> None:
        self.config = bot.config
        self.output_device_index = self.config.sound_devices.output_device
        self.input_device_index = self.config.sound_devices.input_device
        self.player = bot.player
        self.ttclient = bot.ttclient
        self.output_devices = self.player.get_output_devices()
        self.input_devices = self.ttclient.get_input_devices()

    @staticmethod
    def _find_by_name(devices: list, wanted: str) -> Union[SoundDevice, None]:
        """Case-insensitive substring match on the device description.

        Returns None when the name matches nothing, so the caller can fall back
        to the configured index rather than refusing to start.
        """
        if not wanted:
            return None
        needle = wanted.strip().lower()
        for device in devices:
            if needle in str(device.name).lower() or needle in str(device.id).lower():
                return device
        return None

    def initialize(self) -> None:
        logging.debug("Initializing sound devices")

        output = self._find_by_name(
            self.output_devices, self.config.sound_devices.output_device_name
        )
        if output is not None:
            self.player.set_output_device(str(output.id))
        else:
            if self.config.sound_devices.output_device_name:
                logging.warning(
                    "No output device matched "
                    f"{self.config.sound_devices.output_device_name!r}; "
                    f"falling back to index {self.output_device_index}"
                )
            try:
                self.player.set_output_device(
                    str(self.output_devices[self.output_device_index].id)
                )
            except IndexError:
                error = "Incorrect output device index: " + str(self.output_device_index)
                logging.error(error)
                sys.exit(error)

        input_device = self._find_by_name(
            self.input_devices, self.config.sound_devices.input_device_name
        )
        if (
            input_device is None
            and not self.config.sound_devices.input_device_name
            and self.input_device_index == 0
        ):
            # Only when nobody chose a device: a configured name or a non-default
            # index is a deliberate choice and is honoured as before. Outside the
            # container the monitor does not exist and this falls through to index 0.
            input_device = self._find_by_name(self.input_devices, NATIVE_MONITOR_INPUT)
            if input_device is not None:
                logging.info(f"Using input device {input_device.name!r}")
        if input_device is not None:
            self.ttclient.set_input_device(int(input_device.id))
        else:
            if self.config.sound_devices.input_device_name:
                logging.warning(
                    "No input device matched "
                    f"{self.config.sound_devices.input_device_name!r}; "
                    f"falling back to index {self.input_device_index}"
                )
            try:
                self.ttclient.set_input_device(
                    int(self.input_devices[self.input_device_index].id)
                )
            except IndexError:
                error = "Incorrect input device index: " + str(self.input_device_index)
                logging.error(error)
                sys.exit(error)

        logging.debug("Sound devices initialized")
