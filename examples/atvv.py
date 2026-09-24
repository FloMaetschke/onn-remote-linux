"""Helper library for the Onn / Google TV Bluetooth remote on Linux.

Contains
  * the GATT UUIDs and commands of the Google voice service (ATVV, "Android TV Voice"),
  * an IMA ADPCM decoder for the remote's audio frames,
  * the key table (HID reports -> key name),
  * a helper that connects via the BlueZ D-Bus object path.

Dependency: pip install bleak
"""
import struct
import wave

from bleak.backends.device import BLEDevice

# --- ATVV: the remote's voice service ---------------------------------------
ATVV_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664"   # host -> remote (write)
ATVV_RX = "ab5e0003-5a21-4f05-bc7d-af01f617b664"   # audio data (notify)
ATVV_CTL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"  # control messages (notify)

CMD_GET_CAPS = bytes([0x0A, 0x00, 0x04, 0x00, 0x01])  # protocol 0.4, codec 8 kHz ADPCM
CMD_MIC_OPEN = bytes([0x0C, 0x00, 0x01])              # open the microphone (8 kHz ADPCM)
CMD_MIC_CLOSE = bytes([0x0D])                         # close the microphone

CTL_AUDIO_STOP = 0x00     # audio stream ended (remote's ~15 s limit, or after MIC_CLOSE)
CTL_AUDIO_START = 0x04    # audio stream starts
CTL_START_SEARCH = 0x08   # Assistant / talk button pressed
CTL_AUDIO_SYNC = 0x0A     # timestamp, every ~350 ms while streaming
CTL_CAPS_RESP = 0x0B      # reply to GET_CAPS

FRAME_SIZE = 134          # 6-byte header + 128 bytes ADPCM = 256 samples @ 8 kHz
SAMPLE_RATE = 8000

# --- Keys -------------------------------------------------------------------
# The remote sends HID reports over GATT (characteristic 0x2A4D):
#  * short reports (2 or 4 bytes): 16-bit usage of the "Consumer Control" page,
#    little endian in the first two bytes
#  * 8-byte reports: standard keyboard report, key in byte 2 (volume keys only)
# A release is an all-zero report that the remote sends right after the press
# (tap semantics - there is no real "key held" information).
CONSUMER = {
    0x0030: "Power",
    0x01BB: "Input",
    0x022A: "Bookmark",
    0x0096: "Settings",
    0x0042: "Up",
    0x0043: "Down",
    0x0044: "Left",
    0x0045: "Right",
    0x0041: "OK",
    0x0224: "Back",
    0x0223: "Home",
    0x008D: "TV",
    0x00E2: "Mute",
    0x009C: "Channel +",
    0x009D: "Channel -",
    0x0077: "YouTube",
    0x0078: "Netflix",
    0x0079: "Disney+",
    0x007A: "HBO max",
    0x0221: "Assistant",
}
KEYBOARD = {0x80: "Volume +", 0x81: "Volume -"}


def decode_key(data: bytes):
    """Return the key name, or None for a release (all zeros)."""
    if not any(data):
        return None
    if len(data) == 8:
        return KEYBOARD.get(data[2], f"keyboard 0x{data[2]:02x}")
    usage = data[0] | (data[1] << 8 if len(data) > 1 else 0)
    return CONSUMER.get(usage, f"unknown 0x{usage:04x}")


# --- Connection -------------------------------------------------------------
def device(address: str, adapter: str = "hci0") -> BLEDevice:
    """Build a BLEDevice directly from the BlueZ D-Bus object path.

    Background: the remote sleeps most of the time and then does not show up in a scan.
    `BleakClient("AA:BB:...")` fails with "Device ... was not found" even though the
    paired device is known to BlueZ. With the object path bleak connects anyway.
    """
    path = f"/org/bluez/{adapter}/dev_{address.upper().replace(':', '_')}"
    return BLEDevice(address, "Onn-Remote", {"path": path, "props": {}})


# --- Audio ------------------------------------------------------------------
_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55,
    60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
    20350, 22385, 24623, 27086, 29794, 32767,
]
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def decode_frame(frame: bytes) -> bytes:
    """One 134-byte frame -> 256 samples of 16-bit PCM (little endian).

    Frame layout: [0:2] sequence number, [2] stream id, [3:5] predictor (int16 BE),
    [5] step index, [6:134] 128 bytes of IMA ADPCM, high nibble first.
    """
    pred = struct.unpack(">h", frame[3:5])[0]
    step = min(88, frame[5])
    out = []
    for byte in frame[6:]:
        for nibble in (byte >> 4, byte & 15):
            s = _STEP[step]
            diff = s >> 3
            if nibble & 1:
                diff += s >> 2
            if nibble & 2:
                diff += s >> 1
            if nibble & 4:
                diff += s
            if nibble & 8:
                diff = -diff
            pred = max(-32768, min(32767, pred + diff))
            step = max(0, min(88, step + _INDEX[nibble]))
            out.append(pred)
    return struct.pack("<%dh" % len(out), *out)


class FrameAssembler:
    """The remote sends every frame as 20-byte notifications (6 x 20 + 14 bytes)."""

    def __init__(self):
        self.buf = b""

    def reset(self):
        self.buf = b""

    def push(self, packet: bytes):
        """Return a list of decoded PCM frames (possibly empty)."""
        self.buf += packet
        frames = []
        while len(self.buf) >= FRAME_SIZE:
            frames.append(decode_frame(self.buf[:FRAME_SIZE]))
            self.buf = self.buf[FRAME_SIZE:]
        return frames


def write_wav(path, pcm: bytes, rate: int = SAMPLE_RATE):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
