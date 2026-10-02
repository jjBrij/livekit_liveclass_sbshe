"""
Simple WebSocket test client for the Live Class backend.

Usage:
    python ws_test.py <class_id> <token> [mode]

Modes:
    teacher  - connect as-is, send ping, send an unknown event, then disconnect
    student  - same as teacher, but for a student token

You can also edit the constants below and run without arguments.
"""

import asyncio
import json
import sys
import websockets


# --- edit these if you prefer to run without arguments ---
DEFAULT_CLASS_ID = 1
DEFAULT_TOKEN = "PASTE_YOUR_JWT_HERE"
# ---------------------------------------------------------


async def run(class_id: int, token: str):
    url = f"ws://127.0.0.1:8000/ws/classes/{class_id}/?token={token}"
    print(f"connecting to {url}")

    try:
        async with websockets.connect(url) as ws:
            print("connected, waiting for server.hello")

            # 1. server.hello
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            print("=> hello:", json.dumps(hello, indent=2))

            # 2. hands.snapshot (Block 11 sends this on connect)
            snapshot_raw = await ws.recv()
            snapshot = json.loads(snapshot_raw)
            print("=> snapshot:", json.dumps(snapshot, indent=2))

            # 3. ping/pong
            await ws.send(json.dumps({"type": "ping"}))
            pong_raw = await ws.recv()
            print("=> pong:", pong_raw)

            # 4. unknown event
            await ws.send(json.dumps({"type": "not.a.real.event"}))
            error_raw = await ws.recv()
            print("=> error:", error_raw)

            # 5. keep the connection briefly and then close
            await asyncio.sleep(2)
            print("closing")

    except websockets.exceptions.ConnectionClosed as exc:
        # This is how we learn the close code and reason when the server
        # rejects the connection (e.g. 4401 missing token, 4403 not owner).
        print(f"CONNECTION CLOSED code={exc.code} reason={exc.reason!r}")
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        cid = int(sys.argv[1])
        tok = sys.argv[2]
    else:
        cid = DEFAULT_CLASS_ID
        tok = DEFAULT_TOKEN

    asyncio.run(run(cid, tok))