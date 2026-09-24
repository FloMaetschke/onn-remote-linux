#!/usr/bin/env python3
"""Assistant button on the remote -> speech-to-text dictation on the PC.

This is the version that runs day to day (Omarchy / Hyprland / PipeWire / voxtype).
It shows the whole pattern and can be adapted through environment variables:

  ONN_ADDR        Bluetooth address of the remote (required)
  ONN_ADAPTER     Bluetooth adapter, default hci0
  ONN_START_CMD   command that starts recording/recognition (default: omarchy-dictation start)
  ONN_STOP_CMD    command that stops it and types the text (default: omarchy-dictation stop)
  ONN_HYPRLAND    1 = arrow keys move focus, Channel +/- switches workspace (default 0)

Flow
  Assistant   set the virtual audio source "onn_remote" as default microphone, run START_CMD,
              send MIC_OPEN
  audio       decode ADPCM and play it into the virtual sink with pacat; the dictation tool
              records from the default microphone (= the sink's monitor)
  OK          MIC_CLOSE, run STOP_CMD, restore the original microphone
  ~15 s       the remote ends the stream by itself; the service reopens the microphone
              immediately so that dictation can run for up to MAX_RECORD_S in one go
"""
import asyncio
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from bleak import BleakClient

from atvv import (ATVV_CTL, ATVV_RX, ATVV_TX, CMD_GET_CAPS, CMD_MIC_CLOSE, CMD_MIC_OPEN,
                  CTL_AUDIO_START, CTL_AUDIO_STOP, CTL_START_SEARCH, FrameAssembler,
                  decode_key, device)

ADDR = os.environ["ONN_ADDR"]
ADAPTER = os.environ.get("ONN_ADAPTER", "hci0")
START_CMD = shlex.split(os.environ.get("ONN_START_CMD", "omarchy-dictation start"))
STOP_CMD = shlex.split(os.environ.get("ONN_STOP_CMD", "omarchy-dictation stop"))
HYPRLAND = os.environ.get("ONN_HYPRLAND", "0") == "1"

SINK = "onn_remote"
PREBUFFER_S = 0.35      # head start so that the dictation tool does not miss the first words
MAX_RECORD_S = 55       # keep below the recording limit of the dictation tool
STATE = Path.home() / ".cache/onn-voice/real_source"

# keys -> Hyprland (Lua dispatchers, Hyprland with Lua configuration)
FOCUS = {"Up": "u", "Down": "d", "Left": "l", "Right": "r"}
WORKSPACE = {"Channel +": "e+1", "Channel -": "e-1"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def hypr(lua):
    """Run hyprctl from a systemd service: look up the instance signature ourselves."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    locks = sorted(Path(runtime, "hypr").glob("*/hyprland.lock"), key=lambda p: p.stat().st_mtime)
    if locks:
        env = dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE=locks[-1].parent.name)
        subprocess.Popen(["hyprctl", "dispatch", lua], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restore_source():
    """In case a crash left the default microphone on the virtual source."""
    cur = sh("pactl", "get-default-source").stdout.strip()
    if cur.startswith(SINK) and STATE.exists():
        sh("pactl", "set-default-source", STATE.read_text().strip())
        log("default microphone restored")


class Dictation:
    def __init__(self):
        self.active = False
        self.frames = FrameAssembler()
        self.send = None
        self.saved_source = None
        self.player = None
        self.queue = None
        self.pump = None
        self.started_at = 0.0
        self.last_press = 0.0

    async def start(self):
        if self.active:
            return
        self.active = True
        self.started_at = time.time()
        self.frames.reset()
        cur = sh("pactl", "get-default-source").stdout.strip()
        if cur.startswith(SINK):                    # never remember the virtual source as "original"
            cur = STATE.read_text().strip() if STATE.exists() else cur
        else:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(cur)
        self.saved_source = cur
        sh("pactl", "set-default-source", f"{SINK}.monitor")
        sh(*START_CMD)
        log(f"dictation started (microphone was: {cur})")
        self.player = subprocess.Popen(
            ["pacat", "-p", "-d", SINK, "--format=s16le", "--rate=8000", "--channels=1",
             "--latency-msec=40"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.queue = asyncio.Queue()
        self.pump = asyncio.create_task(self._pump())

    async def _pump(self):
        await asyncio.sleep(PREBUFFER_S)
        while (chunk := await self.queue.get()) is not None:
            try:
                self.player.stdin.write(chunk)
                self.player.stdin.flush()
            except (BrokenPipeError, ValueError):
                return

    def feed(self, packet):
        if self.active:
            for pcm in self.frames.push(packet):
                self.queue.put_nowait(pcm)

    async def finish(self):
        if self.send:
            self.send(CMD_MIC_CLOSE)
        await self.stop()

    async def stop(self):
        if not self.active:
            return
        self.active = False
        await asyncio.sleep(0.4)                    # let the last audio packets drain
        self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(self.pump, 3)
        except asyncio.TimeoutError:
            pass
        sh(*STOP_CMD)
        log(f"dictation stopped ({time.time() - self.started_at:.1f} s)")
        await asyncio.sleep(0.5)
        try:
            self.player.stdin.close()
            self.player.terminate()
        except Exception:
            pass
        if self.saved_source and not self.saved_source.startswith(SINK):
            sh("pactl", "set-default-source", self.saved_source)


async def session(dic):
    loop = asyncio.get_running_loop()
    async with BleakClient(device(ADDR, ADAPTER), timeout=6) as client:
        log("remote connected")

        async def _write(cmd):
            try:
                await client.write_gatt_char(ATVV_TX, bytes(cmd), response=False)
            except Exception:
                pass

        dic.send = lambda cmd: loop.create_task(_write(cmd))

        def on_ctl(_, data):
            code = data[0]
            if code == CTL_START_SEARCH:
                if time.time() - dic.last_press < 1.0:   # the message sometimes arrives twice
                    return
                dic.last_press = time.time()
                if dic.active:
                    loop.create_task(dic.finish())
                else:
                    async def go():
                        await dic.start()
                        dic.send(CMD_MIC_OPEN)
                    loop.create_task(go())
            elif code == CTL_AUDIO_START:
                dic.frames.reset()
            elif code == CTL_AUDIO_STOP:
                if dic.active and time.time() - dic.started_at < MAX_RECORD_S:
                    dic.send(CMD_MIC_OPEN)               # work around the remote's 15 s limit
                else:
                    loop.create_task(dic.stop())

        def on_report(_, data):
            key = decode_key(bytes(data))
            if key == "OK" and dic.active:
                loop.create_task(dic.finish())
            elif HYPRLAND and key in FOCUS:
                hypr(f'hl.dsp.focus({{ direction = "{FOCUS[key]}" }})')
            elif HYPRLAND and key in WORKSPACE:
                hypr(f'hl.dsp.focus({{ workspace = "{WORKSPACE[key]}" }})')

        await client.start_notify(ATVV_CTL, on_ctl)
        await client.start_notify(ATVV_RX, lambda _, d: dic.feed(bytes(d)))
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties and char.uuid not in (ATVV_CTL, ATVV_RX):
                    try:
                        await client.start_notify(char, on_report)
                    except Exception:
                        pass

        dic.send(CMD_MIC_CLOSE)                          # clears a possibly stuck state
        await asyncio.sleep(0.3)
        dic.send(CMD_GET_CAPS)
        while client.is_connected:
            if dic.active and time.time() - dic.started_at > MAX_RECORD_S:
                log("time limit reached")
                await dic.finish()
            await asyncio.sleep(0.3)
        log("remote disconnected")
        await dic.stop()


async def shutdown(dic):
    await dic.stop()
    restore_source()
    os._exit(0)


async def main():
    dic = Dictation()
    if SINK not in sh("pactl", "list", "short", "sinks").stdout:
        sh("pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
           "sink_properties=device.description=Onn-Remote-Mic")
    restore_source()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: loop.create_task(shutdown(dic)))
    while True:
        try:
            await session(dic)
        except Exception:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
