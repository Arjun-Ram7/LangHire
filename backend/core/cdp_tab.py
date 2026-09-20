"""A minimal Chrome DevTools Protocol client for one page, with a timeout on every call.

The assist watcher used a full Playwright connection for this. When that connection stalled it
hung the watcher for good (the driver process sat idle for hours and no tab got the Pause/Resume
control), and a hung call cannot be cancelled cleanly. Here every call has a deadline, a stalled
tab is dropped on its own, and nothing else depends on it.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import websockets


class CdpError(RuntimeError):
    """The page rejected a command, or its connection is gone."""


class CdpTab:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self.alive = True

    @classmethod
    async def open(cls, url: str, timeout: float = 5.0) -> "CdpTab":
        # max_size=None: a page's evaluate result can be large; pings off: the browser does not need them.
        ws = await asyncio.wait_for(
            websockets.connect(url, max_size=None, ping_interval=None, open_timeout=timeout), timeout + 1
        )
        return cls(ws)

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
        if not self.alive:
            raise CdpError("connection is closed")
        async with self._lock:
            message_id = next(self._ids)
            try:
                await asyncio.wait_for(
                    self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}})), timeout
                )
                return await asyncio.wait_for(self._await_reply(message_id), timeout)
            except asyncio.TimeoutError:
                # A late reply could otherwise be taken for the next call's: give the socket up.
                self.alive = False
                raise
            except websockets.ConnectionClosed as exc:
                self.alive = False
                raise CdpError("connection closed") from exc

    async def _await_reply(self, message_id: int) -> Any:
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") != message_id:
                continue  # an event, or the answer to an earlier call
            if "error" in message:
                raise CdpError(str(message["error"].get("message", message["error"])))
            return message.get("result")

    async def close(self) -> None:
        self.alive = False
        try:
            await asyncio.wait_for(self._ws.close(), 2)
        except Exception:
            pass
