"""Small synchronous Chrome DevTools client backed by aiohttp on a private loop."""
import asyncio
import errno
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import aiohttp
import requests


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


class Chrome:
    def __init__(self, executable=CHROME):
        if executable is None:
            raise FileNotFoundError("Chrome is unavailable")
        self.profile = tempfile.TemporaryDirectory(prefix="seamless-chrome-")
        self.log = tempfile.TemporaryFile()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.process = subprocess.Popen([
            executable, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            # A temporary headless profile must not await a desktop keyring
            # unlock prompt before its first network navigation.
            "--password-store=basic",
            "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile.name}", "about:blank",
        ], stdout=self.log, stderr=self.log)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.sequence = 0
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    info = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1).json()
                    break
                except (requests.RequestException, ValueError):
                    if time.monotonic() > deadline or self.process.poll() is not None:
                        self.log.seek(0)
                        raise RuntimeError("Chrome startup failed: " + self.log.read().decode(errors="replace"))
                    time.sleep(0.05)
            self._await(self._connect(info["webSocketDebuggerUrl"]))
        except BaseException:
            self.close()
            raise

    def _await(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(15)

    async def _connect(self, url):
        self.session = aiohttp.ClientSession()
        self.websocket = await self.session.ws_connect(url)
        self.lock = asyncio.Lock()

    async def _command(self, method, params, session_id):
        async with self.lock:
            self.sequence += 1
            command = {"id": self.sequence, "method": method, "params": params}
            if session_id:
                command["sessionId"] = session_id
            await self.websocket.send_json(command)
            while True:
                message = await self.websocket.receive(timeout=10)
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError(f"Chrome disconnected: {message}")
                reply = json.loads(message.data)
                if reply.get("id") == command["id"]:
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    return reply.get("result", {})

    def call(self, method, params=None, session_id=None):
        return self._await(self._command(method, params or {}, session_id))

    def tab(self, url):
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable", session_id=session)
        self.call("Runtime.enable", session_id=session)
        tab = ChromeTab(self, target, session)
        if url != "about:blank":
            tab.wait("location.href !== 'about:blank'")
        return tab

    def close(self):
        async def disconnect():
            if hasattr(self, "websocket"):
                await self.websocket.close()
            if hasattr(self, "session"):
                await self.session.close()
        try:
            if hasattr(self, "thread") and self.thread.is_alive():
                try:
                    self._await(disconnect())
                finally:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    self.thread.join(5)
                    self.loop.close()
        finally:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
            finally:
                self.log.close()
                self._cleanup_profile()

    def _cleanup_profile(self):
        # Chrome children can briefly recreate files after the parent exits.
        # Retry only that race; permission errors and persistent failures surface.
        for attempt in range(21):
            try:
                self.profile.cleanup()
                return
            except OSError as exc:
                if exc.errno != errno.ENOTEMPTY or attempt == 20:
                    raise
                time.sleep(0.1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ChromeTab:
    def __init__(self, chrome, target, session):
        self.chrome, self.target, self.session = chrome, target, session

    def evaluate(self, expression):
        result = self.chrome.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, self.session)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait(self, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.025)
        raise AssertionError(f"Browser condition timed out: {expression}")

    def close(self):
        self.chrome.call("Target.closeTarget", {"targetId": self.target})

    def key(self, key, code, virtual_code):
        for event_type in ("keyDown", "keyUp"):
            self.chrome.call("Input.dispatchKeyEvent", {
                "type": event_type, "key": key, "code": code,
                "windowsVirtualKeyCode": virtual_code,
                "nativeVirtualKeyCode": virtual_code,
            }, self.session)
