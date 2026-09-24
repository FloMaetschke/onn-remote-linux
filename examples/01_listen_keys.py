#!/usr/bin/env python3
"""Live view of which button is pressed on the remote.

    python 01_listen_keys.py AA:BB:CC:DD:EE:FF

The remote has to be paired first (see README). After a longer idle period the
remote first has to wake up - that first key press is usually lost.
"""
import asyncio
import sys
import time

from bleak import BleakClient

from atvv import ATVV_CTL, ATVV_RX, CTL_START_SEARCH, decode_key, device

T0 = time.time()


def on_report(char, data):
    key = decode_key(bytes(data))
    if key:  # None = release
        print(f"{time.time() - T0:7.2f}s  {key:<12} (report {bytes(data).hex(' ')})", flush=True)


def on_ctl(_, data):
    if data[0] == CTL_START_SEARCH:
        print(f"{time.time() - T0:7.2f}s  Assistant button (ATVV START_SEARCH)", flush=True)


async def session(address):
    async with BleakClient(device(address), timeout=8) as client:
        for service in client.services:
            for char in service.characteristics:
                if "notify" not in char.properties or char.uuid == ATVV_RX:
                    continue
                cb = on_ctl if char.uuid == ATVV_CTL else on_report
                try:
                    await client.start_notify(char, cb)
                except Exception:
                    pass
        print("connected - press buttons (Ctrl+C quits)", flush=True)
        while client.is_connected:
            await asyncio.sleep(0.5)
        print("disconnected - waiting for reconnect (press any button)", flush=True)


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
