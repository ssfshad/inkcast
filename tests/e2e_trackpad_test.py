import asyncio
import aiohttp
import json
import unittest
from aiohttp import web
from unittest.mock import MagicMock

class TestE2ETrackpadAndAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import inkcast
        self.bridge = MagicMock()
        self.server = inkcast.Server(self.bridge, aspect=16/9)
        self.server.auth_manager.set_pin("654321")
        self.server.mouse_enabled = True
        self.server.loop = asyncio.get_running_loop()

        app = web.Application()
        app.router.add_get("/ws", self.server.ws_handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        # Get assigned dynamic port
        self.port = list(self.site._server.sockets)[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/ws"

    async def asyncTearDown(self):
        self.server.input_executor.shutdown()
        await self.runner.cleanup()

    async def test_local_session_unlocked_and_mouse_dispatch(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.url) as ws:
                hello = await ws.receive_json()
                self.assertEqual(hello.get("t"), "hello")
                self.assertFalse(hello.get("auth_required")) # LAN -> no PIN required
                self.assertTrue(hello.get("mouse_enabled"))

                # Send relative move
                await ws.send_str(json.dumps({"t": "mouse", "action": "move_rel", "dx": 0.01, "dy": 0.01}))
                await asyncio.sleep(0.05)

                # Send click
                await ws.send_str(json.dumps({"t": "mouse", "action": "click", "btn": "left"}))
                await asyncio.sleep(0.05)

    async def test_remote_session_locked_pin_auth_and_token(self):
        # Simulate Cloudflare Tunnel with CF-Connecting-IP header and external host
        headers = {"CF-Connecting-IP": "198.51.100.22", "Host": "inkcast-demo.trycloudflare.com"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.ws_connect(self.url) as ws:
                hello = await ws.receive_json()
                self.assertEqual(hello.get("t"), "hello")
                self.assertTrue(hello.get("auth_required")) # Remote -> PIN required

                # 1. Test wrong PIN
                await ws.send_str(json.dumps({"t": "auth", "pin": "000000"}))
                res = await ws.receive_json()
                self.assertEqual(res.get("t"), "auth_err")
                self.assertIn("incorrect", res.get("msg", "").lower())

                # 2. Test correct PIN
                await ws.send_str(json.dumps({"t": "auth", "pin": "654321"}))
                res_ok = await ws.receive_json()
                self.assertEqual(res_ok.get("t"), "auth_ok")
                token = res_ok.get("token")
                self.assertTrue(bool(token))

                # 3. Test re-authenticating with the issued token
                await ws.send_str(json.dumps({"t": "auth", "token": token}))
                res_tok = await ws.receive_json()
                self.assertEqual(res_tok.get("t"), "auth_ok")

                # 4. Now that remote is authenticated, send mouse move
                await ws.send_str(json.dumps({"t": "mouse", "action": "move_rel", "dx": -0.02, "dy": 0.03}))
                await asyncio.sleep(0.05)

if __name__ == "__main__":
    unittest.main()

