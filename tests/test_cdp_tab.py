import asyncio
import json
import unittest

import websockets

from backend.core.cdp_tab import CdpError, CdpTab


async def serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = list(server.sockets)[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


class CdpTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_call_returns_its_own_result_and_skips_unrelated_events(self):
        async def handler(ws):
            async for raw in ws:
                message = json.loads(raw)
                await ws.send(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))
                await ws.send(json.dumps({"id": message["id"] + 99, "result": {"stray": True}}))
                await ws.send(json.dumps({"id": message["id"], "result": {"echo": message["method"], "p": message["params"]}}))

        server, url = await serve(handler)
        tab = await CdpTab.open(url)
        try:
            result = await tab.call("Runtime.evaluate", {"expression": "1"})
        finally:
            await tab.close()
            server.close()

        self.assertEqual(result, {"echo": "Runtime.evaluate", "p": {"expression": "1"}})

    async def test_an_error_reply_raises(self):
        async def handler(ws):
            async for raw in ws:
                await ws.send(json.dumps({"id": json.loads(raw)["id"], "error": {"code": -32000, "message": "No target"}}))

        server, url = await serve(handler)
        tab = await CdpTab.open(url)
        try:
            with self.assertRaises(CdpError):
                await tab.call("Runtime.evaluate", {})
        finally:
            await tab.close()
            server.close()

    async def test_a_silent_tab_times_out_and_is_reported_dead(self):
        async def handler(ws):
            async for _raw in ws:
                pass  # never answers

        server, url = await serve(handler)
        tab = await CdpTab.open(url)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await tab.call("Runtime.evaluate", {}, timeout=0.1)
            self.assertFalse(tab.alive)
            with self.assertRaises(CdpError):
                await tab.call("Runtime.evaluate", {}, timeout=0.1)
        finally:
            await tab.close()
            server.close()

    async def test_a_closed_connection_raises_instead_of_hanging(self):
        async def handler(ws):
            await ws.close()

        server, url = await serve(handler)
        tab = await CdpTab.open(url)
        try:
            await asyncio.sleep(0.05)
            with self.assertRaises(CdpError):
                await tab.call("Runtime.evaluate", {}, timeout=1)
            self.assertFalse(tab.alive)
        finally:
            await tab.close()
            server.close()
