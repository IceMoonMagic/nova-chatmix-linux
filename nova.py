#!/usr/bin/python3
# Licensed under the 0BSD
import array
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from signal import SIGINT, SIGTERM, signal
from subprocess import Popen, check_output
from typing import Callable, TypeVar

from usb.core import Device, USBError, USBTimeoutError, find


USB_MSG: type = array.array
# dict[Code, Action(USB_MSB) -> ...]
OPT_CODES: type = dict[int, Callable[[USB_MSG], ...]]


@dataclass(frozen=True)
class HeadsetEndpoint:
    # bInterfaceNumber
    INTERFACE: int

    # bEndpointAddress
    ENDPOINT_TX: int | None
    ENDPOINT_RX: int

    # Total USB packet is 128 bytes, data is last 64 bytes.
    MSG_LEN: int = field(default=64)

    # First byte controls data direction.
    TX: int = field(default=0x6)  # To base station.
    RX: int = field(default=0x7)  # From base station.

    def __post_init__(self):
        if self.ENDPOINT_TX is None:
            object.__setattr__(self, "TX", None)
            # setattr(self, "TX", None)


@dataclass
class HeadsetFeature(ABC):
    endpoint: HeadsetEndpoint

    @property
    def has_action(self) -> bool:
        return self.endpoint.ENDPOINT_TX is not None

    @property
    def opt_codes(self) -> OPT_CODES:
        return {}

    def ignore(self, *_args, **_kwargs):
        return

    # Takes a tuple of ints and turns it into bytes
    # with the correct length padded with zeroes
    def _create_msg_data(self, *data: int) -> bytes:
        return bytes(data).ljust(self.endpoint.MSG_LEN, b"0")

    def _write(self, dev: Device, *data: int) -> bool:
        if self.endpoint.ENDPOINT_TX is None:
            return False
        dev.write(
            self.endpoint.ENDPOINT_TX,
            self._create_msg_data(self.endpoint.TX, *data),
        )
        return True


HF = TypeVar("HF", bound=HeadsetFeature)


class Headset(ABC):
    # USB IDs
    VID: int = NotImplemented
    PID: int = NotImplemented

    # Selects correct device, and makes sure we can control it
    def __init__(self):
        self.dev = find(idVendor=self.VID, idProduct=self.PID)
        if self.dev is None:
            raise ValueError("Device not found")

        self.actions: dict[type[HF], HF] = {}
        self.listeners: dict[HeadsetEndpoint, OPT_CODES] = {}
        self.on_open: list[Callable[["Headset"], ...]] = []
        self.on_close: list[Callable[["Headset"], ...]] = []
        # Stops processes when program exits
        self.closing = False

    def attempt_action(
        self, cls: type[HeadsetFeature], method, *args, **kwargs
    ) -> bool:
        if cls not in self.actions:
            return False
        method(self.actions[cls], self.dev, *args, **kwargs)
        return True

    def _add_features(self, *features: HeadsetFeature):
        for feat in features:
            self._add_feature(feat)

    def _add_feature(self, feature: HeadsetFeature):
        interface_features = self.listeners.get(feature.endpoint, {})
        interface_features.update(feature.opt_codes)
        self.listeners[feature.endpoint] = interface_features

        if self.dev.is_kernel_driver_active(feature.endpoint.INTERFACE):
            self.dev.detach_kernel_driver(feature.endpoint.INTERFACE)

        if feature.has_action:
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

    def listen(self, endpoint: HeadsetEndpoint):
        while not self.closing:
            try:
                msg = self.dev.read(endpoint.ENDPOINT_RX, endpoint.MSG_LEN, -1)
                action = self.listeners[endpoint].get(msg[0])
                if __debug__:
                    print(msg)
                    print(action)
                if action is not None:
                    action(msg)

            except USBTimeoutError:
                continue
            except USBError as _e:
                raise
        self.close(..., ...)

    def open(self):
        for on_open in self.on_open:
            on_open(self)

    # Terminates processes and disables features
    def close(self, _signum, _frame):
        self.closing = True
        for on_close in self.on_close:
            on_close(self)


@dataclass
class SonarIcon(HeadsetFeature):
    endpoint: HeadsetEndpoint
    # As far as I know, this only controls the icon.
    opt_sonar_icon: int = field(default=141)

    # Keeps track of enabled features for when close() is called
    sonar_icon_enabled: bool = field(default=False, init=False)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {}

    # Enables/Disables Sonar Icon
    def set_sonar_icon(self, dev: Device, state: bool):
        if self._write(dev, self.opt_sonar_icon, int(state)):
            self.sonar_icon_enabled = state


@dataclass
class ChatMix(HeadsetFeature):
    device_name: str
    endpoint: HeadsetEndpoint
    # ChatMix controls, 2 bytes show and control game and chat volume.
    opt_chatmix: int = field(default=69)
    # Enabling this options enables
    # the ability to switch between volume and ChatMix.
    opt_chatmix_enable: int = field(default=73)

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
    def set_chatmix_controls(self, dev: Device, state: bool):
        if self._write(dev, self.opt_chatmix_enable, int(state)):
            self.chatmix_controls_enabled = state

    def on_open(self, _dev):
        self._start_virtual_sinks()

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
            check_output(["pactl", "list", "sinks", "short"])
            .decode()
            .split("\n")
        )
        for sink in sinks:
            print(sink)
            name = sink.split("\t")[1]
            if self.device_name in name:
                self.pw_original_sink = name
                break

    # Creates virtual pipewire loopback sinks,
    # and redirects them to the real headset sink
    def _start_virtual_sinks(self):
        self._detect_original_sink()
        cmd = [
            "pw-loopback",
            "-P",
            self.pw_original_sink,
            "--capture-props=media.class=Audio/Sink",
            "-n",
        ]
        self.pw_loopback_game_process = Popen(cmd + [self.pw_game_sink])
        self.pw_loopback_chat_process = Popen(cmd + [self.pw_chat_sink])

    def _remove_virtual_sinks(self):
        if self.pw_loopback_game_process is not None:
            self.pw_loopback_game_process.terminate()
        if self.pw_loopback_chat_process is not None:
            self.pw_loopback_chat_process.terminate()

    def chatmix(self, msg: USB_MSG):
        # 4th and 5th byte contain ChatMix data
        # print(msg[1:3])
        game_vol = msg[1]
        chat_vol = msg[2]

        # Set Volume using PulseAudio tools.
        # Can be done with pure pipewire tools, but I didn't feel like it
        cmd = ["pactl", "set-sink-volume"]

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
    endpoint: HeadsetEndpoint
    # Volume controls, 1 byte
    opt_volume: int = field(default=37)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {self.opt_volume: self.ignore}

    # Sets Volume
    def set_volume(self, dev: Device, attenuation: int):
        self._write(dev, self.opt_volume, attenuation)


@dataclass
class EQ(HeadsetFeature):
    endpoint: HeadsetEndpoint

    # EQ controls, 2 bytes show and control which band and what value.
    opt_eq: int = field(default=49)
    # EQ preset controls, 1 byte sets and shows enabled preset.
    # Preset 4 is the custom preset required for OPT_EQ.
    opt_eq_preset: int = field(default=46)

    @property
    def opt_codes(self) -> OPT_CODES:
        return {self.opt_eq: self.ignore}

    # Sets EQ preset
    def set_eq_preset(self, dev: Device, preset: int):
        self._write(dev, self.opt_eq_preset, preset)


class NovaProWireless(Headset):
    # USB IDs
    VID = 0x1038
    PID = 0x12E0

    ENDPOINT_4 = HeadsetEndpoint(0x4, 0x4, 0x84)

    def __init__(self):
        super().__init__()
        self._add_features(
            SonarIcon(self.ENDPOINT_4, 141),
            ChatMix(
                "SteelSeries_Arctis_Nova_Pro_Wireless", self.ENDPOINT_4, 69, 73
            ),
            Volume(self.ENDPOINT_4, 37),
            EQ(self.ENDPOINT_4, 49, 46),
        )
        self.open()


class Nova5X(Headset):
    # USB IDs
    VID = 0x1038
    PID = 0x2253

    ENDPOINT_4 = HeadsetEndpoint(0x5, None, 0x84)

    def __init__(self):
        super().__init__()
        self._add_feature(
            ChatMix(self.ENDPOINT_4, "SteelSeries_Arctis_Nova_5X", 69),
        )
        self.open()


# When run directly, just start the ChatMix implementation.
# (And activate the icon, just for fun)
if __name__ == "__main__":
    nova = Nova5X()
    signal(SIGINT, nova.close)
    signal(SIGTERM, nova.close)
    try:
        nova.attempt_action(SonarIcon, SonarIcon.set_sonar_icon, True)
        nova.attempt_action(ChatMix, ChatMix.set_chatmix_controls, True)
        nova.listen(nova.ENDPOINT_4)
    finally:
        nova.close(..., ...)
