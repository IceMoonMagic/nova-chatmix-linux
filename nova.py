#!/usr/bin/python3

# Licensed under the 0BSD

from subprocess import Popen, check_output
from signal import signal, SIGINT, SIGTERM
from usb.core import find, USBTimeoutError, USBError


class NovaProWireless:
    # USB IDs
    VID = 0x1038
    # PID = 0x12E0
    PID = 0x2253

    # bInterfaceNumber
    INTERFACE = 0x5

    # bEndpointAddress
    # ENDPOINT_TX = 0x4  # EP 4 OUT
    ENDPOINT_RX = 0x84  # EP 4 IN

    MSGLEN = 64  # Total USB packet is 128 bytes, data is last 64 bytes.

    # First byte controls data direction.
    # TX = 0x6  # To base station.
    RX = 0x7  # From base station.

    # Second Byte
    # This is a very limited list of options, you can control way more. I just haven't implemented those options (yet)
    ## As far as I know, this only controls the icon.
    OPT_SONAR_ICON = 141
    ## Enabling this options enables the ability to switch between volume and ChatMix.
    OPT_CHATMIX_ENABLE = 73
    ## Volume controls, 1 byte
    OPT_VOLUME = 37
    ## ChatMix controls, 2 bytes show and control game and chat volume.
    OPT_CHATMIX = 69
    ## EQ controls, 2 bytes show and control which band and what value.
    OPT_EQ = 49
    ## EQ preset controls, 1 byte sets and shows enabled preset. Preset 4 is the custom preset required for OPT_EQ.
    OPT_EQ_PRESET = 46

    # PipeWire Names
    ## This is automatically detected, can be set manually by overriding this variable
    PW_ORIGINAL_SINK = None
    ## Names of virtual sound devices
    PW_GAME_SINK = "NovaGame"
    PW_CHAT_SINK = "NovaChat"

    # PipeWire virtual sink processes
    PW_LOOPBACK_GAME_PROCESS = None
    PW_LOOPBACK_CHAT_PROCESS = None

    # Keeps track of enabled features for when close() is called
    CHATMIX_CONTROLS_ENABLED = False
    SONAR_ICON_ENABLED = False

    # Stops processes when program exits
    CLOSE = False

    # Selects correct device, and makes sure we can control it
    def __init__(self):
        self.dev = find(idVendor=self.VID, idProduct=self.PID)
        if self.dev is None:
            raise ValueError("Device not found")
        if self.dev.is_kernel_driver_active(self.INTERFACE):
            self.dev.detach_kernel_driver(self.INTERFACE)

    # Takes a tuple of ints and turns it into bytes with the correct length padded with zeroes
    def _create_msgdata(self, data: tuple[int]) -> bytes:
        return bytes(data).ljust(self.MSGLEN, b"0")

    # Enables/Disables chatmix controls
    # def set_chatmix_controls(self, state: bool):
    #     self.dev.write(
    #         self.ENDPOINT_TX,
    #         self._create_msgdata((self.TX, self.OPT_CHATMIX_ENABLE, int(state))),
    #     )
    #     self.CHATMIX_CONTROLS_ENABLED = state

    # Enables/Disables Sonar Icon
    # def set_sonar_icon(self, state: bool):
    #     self.dev.write(
    #         self.ENDPOINT_TX,
    #         self._create_msgdata((self.TX, self.OPT_SONAR_ICON, int(state))),
    #     )
    #     self.SONAR_ICON_ENABLED = state

    # Sets Volume
    # def set_volume(self, attenuation: int):
    #     self.dev.write(
    #         self.ENDPOINT_TX,
    #         self._create_msgdata((self.TX, self.OPT_VOLUME, attenuation)),
    #    )

    # Sets EQ preset
    # def set_eq_preset(self, preset: int):
    #     self.dev.write(
    #         self.ENDPOINT_TX,
    #         self._create_msgdata((self.TX, self.OPT_EQ_PRESET, preset)),
    #     )
    
    # Checks available sinks and select headset
    def _detect_original_sink(self):
        # If sink is set manually, skip auto detect
        if self.PW_ORIGINAL_SINK:
            return
        sinks = check_output(["pactl", "list", "sinks", "short"]).decode().split("\n")
        for sink in sinks:
            print(sink)
            name = sink.split("\t")[1]
            if "SteelSeries_Arctis_Nova_5X" in name:
                self.PW_ORIGINAL_SINK = name
                break

    # Creates virtual pipewire loopback sinks, and redirects them to the real headset sink
    def _start_virtual_sinks(self):
        self._detect_original_sink()
        cmd = [
            "pw-loopback",
            "-P",
            self.PW_ORIGINAL_SINK,
            "--capture-props=media.class=Audio/Sink",
            "-n",
        ]
        self.PW_LOOPBACK_GAME_PROCESS = Popen(cmd + [self.PW_GAME_SINK])
        self.PW_LOOPBACK_CHAT_PROCESS = Popen(cmd + [self.PW_CHAT_SINK])

    def _remove_virtual_sinks(self):
        self.PW_LOOPBACK_GAME_PROCESS.terminate()
        self.PW_LOOPBACK_CHAT_PROCESS.terminate()

    # ChatMix implementation
    # Continuously read from base station and ignore everything but ChatMix messages (OPT_CHATMIX)
    # The .read method times out and returns an error. This error is catched and basically ignored. Timeout can be configured, but not turned off (I think).
    def chatmix(self):
        self._start_virtual_sinks()
        while not self.CLOSE:
            try:
                msg = self.dev.read(self.ENDPOINT_RX, self.MSGLEN)
                if msg[0] != self.OPT_CHATMIX:
                    print(msg)
                    continue

                # 4th and 5th byte contain ChatMix data
                # print(msg[1:3])
                gamevol = msg[1]
                chatvol = msg[2]

                # Set Volume using PulseAudio tools. Can be done with pure pipewire tools, but I didn't feel like it
                cmd = ["pactl", "set-sink-volume"]

                # Actually change volume. Everytime you turn the dial, both volumes are set to the correct level
                Popen(cmd + [f"input.{self.PW_GAME_SINK}", f"{gamevol}%"])
                Popen(cmd + [f"input.{self.PW_CHAT_SINK}", f"{chatvol}%"])
            # Ignore timeout.
            except USBTimeoutError:
                continue
            except USBError as e:
                raise
                print("Device was probably disconnected, exiting..")
                self.CLOSE = True
                self._remove_virtual_sinks()
        # Remove virtual sinks on exit
        self._remove_virtual_sinks()

    # Prints output from base station. `debug` argument enables raw output.
    def print_output(self, debug: bool = False):
        while not self.CLOSE:
            try:
                msg = self.dev.read(self.ENDPOINT_RX, self.MSGLEN)
                if debug:
                    print(msg)
                match msg[1]:
                    case self.OPT_VOLUME:
                        print(f"Volume: -{msg[2]}")
                    case self.OPT_CHATMIX:
                        print(f"Game Volume: {msg[2]} - Chat Volume: {msg[3]}")
                    case self.OPT_EQ:
                        print(f"EQ: Bar: {msg[2]} - Value: {(msg[3] - 20) / 2}")
                    case self.OPT_EQ_PRESET:
                        print(f"EQ Preset: {msg[2]}")
                    case _:
                        print("Unknown Message")
            except USBTimeoutError:
                continue

    # Terminates processes and disables features
    def close(self, signum, frame):
        self.CLOSE = True
        if self.CHATMIX_CONTROLS_ENABLED:
            self.set_chatmix_controls(False)
        if self.SONAR_ICON_ENABLED:
            self.set_sonar_icon(False)


# When run directly, just start the ChatMix implementation. (And activate the icon, just for fun)
if __name__ == "__main__":
    nova = NovaProWireless()
    signal(SIGINT, nova.close)
    signal(SIGTERM, nova.close)
    # nova.set_sonar_icon(True)
    # nova.set_chatmix_controls(True)
    nova.chatmix()
