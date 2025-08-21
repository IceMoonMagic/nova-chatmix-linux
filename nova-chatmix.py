#!/usr/bin/python3

# Licensed under the 0BSD

import array
from abc import ABC
from dataclasses import dataclass, field
from signal import SIGINT, SIGTERM, signal
from subprocess import Popen, check_output
from typing import Callable, TypeVar

from hid import device
from hid import enumerate as hidenumerate

CMD_PACTL = "pactl"
CMD_PWLOOPBACK = "pw-loopback"

USB_MSG: type = array.array
# dict[Code, Action(USB_MSB) -> ...]
ACTION = Callable[[USB_MSG], ...]
OPT_CODES: type = dict[int, ACTION]


@dataclass
class HeadsetFeature(ABC):
    tx: int | None
    rx: int

    @property
    def opt_codes(self) -> OPT_CODES:
        return {}

    def ignore(self, *_args, **_kwargs):
        return

    # Takes a tuple of ints and turns it into bytes
    # with the correct length padded with zeroes
    def _create_msg_data(self, *data: int, msg_len) -> bytes:
        return bytes(data).ljust(msg_len, b"0")

    def _write(self, dev: device, *data: int) -> bool:
        if self.tx in {NotImplemented, None}:
            return False
        dev.write(
            self._create_msg_data(self.tx, *data, msg_len=dev.PACKET_SIZE),
        )
        return True


HF = TypeVar("HF", bound=HeadsetFeature)


class Headset(ABC, device):
    # USB IDs
    VID: int = NotImplemented
    PID: int = NotImplemented

    # bInterfaceNumber
    INTERFACE: int = NotImplemented

    # wMaxPacketSize
    PACKET_SIZE: int = NotImplemented

    # First byte controls data direction.
    TX: int | None = NotImplemented  # To base station.
    RX: int = NotImplemented  # From base station.

    # Selects correct device, and makes sure we can control it
    def __init__(self):
        devpath = None
        for hiddev in hidenumerate(self.VID, self.PID):
            if hiddev["interface_number"] == self.INTERFACE:
                devpath = hiddev["path"]
                break
        if not devpath:
            raise DeviceNotFoundException

        super().__init__()
        self.open_path(devpath)

        self.actions: dict[type[HF], HF] = {}
        self.listeners: OPT_CODES = {}
        self.on_open: list[Callable[["Headset"], ...]] = []
        self.on_close: list[Callable[["Headset"], ...]] = []
        # Stops processes when program exits
        self.closing = False

    def attempt_action(
        self, cls: type[HeadsetFeature], method, *args, **kwargs
    ) -> bool:
        if cls not in self.actions:
            return False
        method(self.actions[cls], self, *args, **kwargs)
        return True

    def _add_features(self, *features: HeadsetFeature):
        for feat in features:
            self._add_feature(feat)

    def _add_feature(self, feature: HeadsetFeature):
        self.listeners.update(feature.opt_codes)
        self.actions[feature.__class__] = feature

        if hasattr(feature, "on_open"):
            self.on_open.append(feature.on_open)

        if hasattr(feature, "on_close"):
            self.on_close.append(feature.on_close)

    # Prints output from base station. `debug` argument enables raw output.
    # def print_output(self, debug: bool = False):
    #     while not self.CLOSE:
    #         try:
    #             msg = self.dev.read(self.ENDPOINT_RX, self.MSGLEN)
    #             if debug:
    #                 print(msg)
    #             match msg[1]:
    #                 case self.OPT_VOLUME:
    #                     print(f"Volume: -{msg[2]}")
    #                 case self.OPT_CHATMIX:
    #                     print(f"Game Volume: {msg[2]} - Chat Volume: {msg[3]}")
    #                 case self.OPT_EQ:
    #                     print(f"EQ: Bar: {msg[2]} - Value: {(msg[3] - 20) / 2}")
    #                 case self.OPT_EQ_PRESET:
    #                     print(f"EQ Preset: {msg[2]}")
    #                 case _:
    #                     print("Unknown Message")
    #         except USBTimeoutError:
    #             continue

    def listen(self):
        while not self.closing:
            # Note: Blocks closing
            msg = self.read(self.PACKET_SIZE, 1000)
            if not msg:
                continue
            action = self.listeners.get(msg[0])
            if __debug__:
                print(msg)
                print(action)
            if action is not None:
                action(msg)
        self.close(..., ...)

    # Note: Overwrites hidapi.device.open
    def open(self):
        for on_open in self.on_open:
            on_open(self)

    # Terminates processes and disables features
    # Note: Overwrites hidapi.device.close
    def close(self, _signum, _frame):
        self.closing = True
        for on_close in self.on_close:
            on_close(self)


@dataclass
class SonarIcon(HeadsetFeature):
    # As far as I know, this only controls the icon.
    opt_sonar_icon: int = field(default=0x8D)

    # Keeps track of enabled features for when close() is called
    sonar_icon_enabled: bool = field(default=False, init=False)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {}

    # Enables/Disables Sonar Icon
    def set_sonar_icon(self, dev: device, state: bool):
        if self._write(dev, self.opt_sonar_icon, int(state)):
            self.sonar_icon_enabled = state


@dataclass
class ChatMix(HeadsetFeature):
    device_name: str
    # ChatMix controls, 2 bytes show and control game and chat volume.
    opt_chatmix: int = field(default=0x45)
    # Enabling this options enables
    # the ability to switch between volume and ChatMix.
    opt_chatmix_enable: int = field(default=0x49)

    # PipeWire Names
    # This is automatically detected,
    # can be set manually by overriding this variable
    pw_original_sink: str | None = field(default=None, kw_only=True)
    # Names of virtual sound devices
    pw_game_sink: str = field(default="NovaGame")
    pw_chat_sink: str = field(default="NovaChat")

    # PipeWire virtual sink processes
    pw_loopback_game_process: Popen = field(default=None, init=False)
    pw_loopback_chat_process: Popen = field(default=None, init=False)

    # Keeps track of enabled features for when close() is called
    chatmix_controls_enabled: bool = field(default=False, init=False)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {self.opt_chatmix: self.chatmix}

    # Enables/Disables chatmix controls
    def set_chatmix_controls(self, dev: device, state: bool):
        if self._write(dev, self.opt_chatmix_enable, int(state)):
            self.chatmix_controls_enabled = state

    # def on_open(self, _dev):
    #     self._start_virtual_sinks()

    def on_close(self, dev):
        if self.chatmix_controls_enabled:
            self.set_chatmix_controls(dev, False)
            self._remove_virtual_sinks()

    # Checks available sinks and select headset
    def _detect_original_sink(self):
        # If sink is set manually, skip auto-detect
        if self.pw_original_sink:
            return
        sinks = (
            check_output([CMD_PACTL, "list", "sinks", "short"])
            .decode()
            .split("\n")
        )
        for sink in sinks:
            name = sink.split("\t")[1]
            if self.device_name in name:
                print(name)
                self.pw_original_sink = name
                break
        else:
            raise RuntimeError("Original Sink not found")

    def _are_sinks_open(self) -> (bool, bool):
        """Checks if the sinks' processes exists and haven't been terminated.

        :return: (game sink open, chat sink open)
        """
        return (
            self.pw_loopback_game_process is not None
            and self.pw_loopback_game_process.poll() is None,
            self.pw_loopback_chat_process is not None
            and self.pw_loopback_chat_process.poll() is None,
        )

    # Creates virtual pipewire loopback sinks,
    # and redirects them to the real headset sink
    def _start_virtual_sinks(self):
        self._detect_original_sink()
        cmd = [
            CMD_PWLOOPBACK,
            "-P",
            self.pw_original_sink,
            "--capture-props=media.class=Audio/Sink",
            "-n",
        ]

        game, chat = self._are_sinks_open()
        if not game:
            self.pw_loopback_game_process = Popen(cmd + [self.pw_game_sink])
        if not chat:
            self.pw_loopback_chat_process = Popen(cmd + [self.pw_chat_sink])

    def _remove_virtual_sinks(self):
        if self.pw_loopback_game_process is not None:
            self.pw_loopback_game_process.terminate()
        if self.pw_loopback_chat_process is not None:
            self.pw_loopback_chat_process.terminate()

    def chatmix(self, msg: USB_MSG):
        if False in self._are_sinks_open():
            # One or both sinks are closed
            self._start_virtual_sinks()

        # 4th and 5th byte contain ChatMix data
        # print(msg[1:3])
        game_vol = msg[1]
        chat_vol = msg[2]

        # Set Volume using PulseAudio tools.
        # Can be done with pure pipewire tools, but I didn't feel like it
        cmd = [CMD_PACTL, "set-sink-volume"]

        # Actually change volume.
        # Everytime you turn the dial,
        # both volumes are set to the correct level
        Popen(cmd + [f"input.{self.pw_game_sink}", f"{game_vol}%"])
        Popen(cmd + [f"input.{self.pw_chat_sink}", f"{chat_vol}%"])

    # ChatMix implementation
    # Continuously read from base station
    # and ignore everything but ChatMix messages (OPT_CHATMIX)
    # The .read method times out and returns an error.
    # This error is caught and basically ignored.
    # Timeout can be configured, but not turned off (I think).


@dataclass
class Volume(HeadsetFeature):
    # Volume controls, 1 byte
    opt_volume: int = field(default=0x25)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {self.opt_volume: self.ignore}

    # Sets Volume
    def set_volume(self, dev: device, attenuation: int):
        self._write(dev, self.opt_volume, attenuation)


@dataclass
class EQ(HeadsetFeature):
    # EQ controls, 2 bytes show and control which band and what value.
    opt_eq: int = field(default=0x31)
    # EQ preset controls, 1 byte sets and shows enabled preset.
    # Preset 4 is the custom preset required for OPT_EQ.
    opt_eq_preset: int = field(default=0x2E)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {self.opt_eq: self.ignore}

    # Sets EQ preset
    def set_eq_preset(self, dev: device, preset: int):
        self._write(dev, self.opt_eq_preset, preset)


class NovaProWireless(Headset):
    # USB IDs
    VID: int = 0x1038
    PID: int = 0x12E0

    # bInterfaceNumber
    INTERFACE: int = 4

    # wMaxPacketSize
    PACKET_SIZE: int = 64

    # First byte controls data direction.
    TX: int | None = 0x6  # To base station.
    RX: int = 0x7  # From base station.

    def __init__(self):
        super().__init__()
        self._add_features(
            SonarIcon(self.TX, self.RX, 0x8D),
            ChatMix(
                self.TX,
                self.RX,
                "SteelSeries_Arctis_Nova_Pro_Wireless",
                0x45,
                0x49,
            ),
            Volume(self.TX, self.RX, 0x25),
            EQ(self.TX, self.RX, 0x31, 0x2E),
        )
        self.open()


class Nova5X(Headset):
    # USB IDs
    VID: int = 0x1038
    PID: int = 0x2253

    # bInterfaceNumber
    INTERFACE: int = 5

    # wMaxPacketSize
    PACKET_SIZE: int = 64

    # First byte controls data direction.
    TX: int | None = None  # To base station.
    RX: int = 0x7  # From base station.

    def __init__(self):
        super().__init__()
        self.chatmix = ChatMix(
            self.TX, self.RX, "SteelSeries_Arctis_Nova_5X", 0x45
        )
        self._add_feature(self.chatmix)
        self.listeners[0xB9] = self.on_power_change
        self.open()

    def on_power_change(self, msg: USB_MSG):
        match msg[1]:
            case 2:  # Power Off
                self.chatmix._remove_virtual_sinks()
            case 3:  # Power On
                self.chatmix._start_virtual_sinks()
            case _:
                pass


class DeviceNotFoundException(Exception):
    pass


# When run directly, just start the ChatMix implementation.
# (And activate the icon, just for fun)
if __name__ == "__main__":
    nova = Nova5X()
    signal(SIGINT, nova.close)
    signal(SIGTERM, nova.close)
    try:
        nova.attempt_action(SonarIcon, SonarIcon.set_sonar_icon, True)
        nova.attempt_action(ChatMix, ChatMix.set_chatmix_controls, True)
        nova.listen()
    finally:
        nova.close(..., ...)
