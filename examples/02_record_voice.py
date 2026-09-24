#!/usr/bin/env python3
"""Record speech from the remote's microphone into a WAV file.

    python 02_record_voice.py AA:BB:CC:DD:EE:FF

Flow: press the Assistant button -> speak -> press OK (or the remote stops by itself
after ~15 s). The recording is written as recording-N.wav (8 kHz, mono) into the
current directory. The remote does not report the release of the Assistant button,
so recording is ended with the OK button.
"""
import asyncio
import sys
import time

from bleak import BleakClient

from atvv import (ATVV_CTL, ATVV_RX, ATVV_TX, CMD_GET_CAPS, CMD_MIC_CLOSE, CMD_MIC_OPEN,
                  CTL_AUDIO_START, CTL_AUDIO_STOP, CTL_START_SEARCH, FrameAssembler,
                  decode_key, device, write_wav)


async def session(address):
    frames = FrameAssembler()
    pcm = []
    state = {"recording": False, "count": 0, "last_press": 0.0}
    loop = asyncio.get_running_loop()

    async with BleakClient(device(address), timeout=8) as client:
        async def send(cmd):
            try:
                await client.write_gatt_char(ATVV_TX, cmd, response=False)
            except Exception:
                pass

        def finish():
            if not state["recording"]:
                return
            state["recording"] = False
            loop.create_task(send(CMD_MIC_CLOSE))
            state["count"] += 1
            name = f"recording-{state['count']}.wav"
            write_wav(name, b"".join(pcm))
            print(f"saved: {name} ({sum(map(len, pcm)) / 16000:.1f} s)", flush=True)

        def on_ctl(_, data):
            code = data[0]
            if code == CTL_START_SEARCH and time.time() - state["last_press"] > 1.0:
                state["last_press"] = time.time()
                if state["recording"]:
                    finish()  # a second press ends the recording as well
                else:
                    pcm.clear()
                    frames.reset()
                    state["recording"] = True
                    print("recording - press OK to finish", flush=True)
                    loop.create_task(send(CMD_MIC_OPEN))
            elif code == CTL_AUDIO_START:
                frames.reset()
            elif code == CTL_AUDIO_STOP and state["recording"]:
                finish()  # the remote's own limit (~15 s)

        def on_audio(_, data):
            if state["recording"]:
                pcm.extend(frames.push(bytes(data)))

        def on_report(_, data):
            if decode_key(bytes(data)) == "OK":
                finish()

        await client.start_notify(ATVV_CTL, on_ctl)
        await client.start_notify(ATVV_RX, on_audio)
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties and char.uuid not in (ATVV_CTL, ATVV_RX):
                    try:
                        await client.start_notify(char, on_report)
                    except Exception:
                        pass

        await send(CMD_MIC_CLOSE)  # clears a possibly stuck state
        await asyncio.sleep(0.3)
        await send(CMD_GET_CAPS)   # activates the voice service
        print("ready - press the Assistant button", flush=True)
        while client.is_connected:
            await asyncio.sleep(0.5)
        finish()


async def main(address):
    while True:
        try:
            await session(address)
        except Exception:
            await asyncio.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        asyncio.run(main(sys.argv[1]))
    except KeyboardInterrupt:
        pass
