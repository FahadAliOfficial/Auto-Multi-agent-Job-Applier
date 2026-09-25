"""Playwright-shaped adapter for the companion Chrome extension.

The existing page objects intentionally remain unchanged.  This module maps the
small Playwright surface they use onto authenticated commands executed in a
dedicated tab of the user's normal Chrome profile.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import mimetypes
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.utils.logger import logger


PROTOCOL_VERSION = 1
TOKEN_PATH = Path("data/extension_pairing_token")


class ExtensionBridgeError(RuntimeError):
    """Raised when the companion extension cannot execute a command."""


class ExtensionBridge:
    def __init__(self, host: str, port: int, token: str, command_timeout: float = 30):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Extension bridge must bind to a loopback address")
        self.host = host
        self.port = port
        self.token = token
        self.command_timeout = command_timeout
        self._server = None
        self._socket = None
        self._connected = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._event_listeners: list = []

    async def start(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "Extension mode requires 'websockets'. Run: pip install -r requirements.txt"
            ) from exc

        async def handler(socket):
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=5)
                hello = json.loads(raw)
                valid = (
                    hello.get("type") == "hello"
                    and hello.get("protocol") == PROTOCOL_VERSION
                    and hmac.compare_digest(str(hello.get("token", "")), self.token)
                )
                if not valid:
                    await socket.close(code=4001, reason="authentication failed")
                    return
                if self._socket is not None:
                    await self._socket.close(code=4002, reason="replaced")
                self._socket = socket
                self._connected.set()
                await socket.send(json.dumps({"type": "hello_ok", "protocol": PROTOCOL_VERSION}))
                async for message in socket:
                    self._receive(json.loads(message))
            except Exception as exc:
                logger.debug("Extension bridge client ended: %s", exc)
            finally:
                if self._socket is socket:
                    self._socket = None
                    self._connected.clear()
                    self._fail_pending(ExtensionBridgeError("Chrome extension disconnected"))

        self._server = await websockets.serve(
            handler, self.host, self.port, max_size=20 * 1024 * 1024
        )

    async def wait_connected(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise ExtensionBridgeError(
                "Chrome extension did not connect. Load extension/chrome as an unpacked "
                "extension, open its Options page, and enter the pairing token from "
                f"{TOKEN_PATH}."
            ) from exc

    def add_event_listener(self, listener) -> None:
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener) -> None:
        with suppress(ValueError):
            self._event_listeners.remove(listener)

    def _receive(self, message: dict) -> None:
        if message.get("type") == "event":
            for listener in list(self._event_listeners):
                listener(message)
            return
        request_id = str(message.get("id", ""))
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if message.get("ok"):
            future.set_result(message.get("result"))
        else:
            future.set_exception(ExtensionBridgeError(message.get("error", "command failed")))

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def request(self, command: str, **payload):
        if self._socket is None:
            raise ExtensionBridgeError("Chrome extension is not connected")
        request_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._socket.send(json.dumps({
            "type": "command", "id": request_id, "command": command, **payload
        }))
        try:
            return await asyncio.wait_for(future, self.command_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ExtensionBridgeError(f"Extension command timed out: {command}") from exc

    async def stop(self) -> None:
        if self._socket is not None:
            with suppress(Exception):
                await self._socket.close()
        self._socket = None
        self._connected.clear()
        self._fail_pending(ExtensionBridgeError("Extension bridge stopped"))
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def _load_pairing_token(config: dict) -> str:
    env_name = str(config.get("token_env", "INDEED_EXTENSION_TOKEN"))
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


class ExtensionBrowserManager:
    """Browser manager that owns dedicated tabs, never the Chrome process."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        browser_cfg = self.config.get("bot", {}).get("browser", {})
        self.extension_cfg = browser_cfg.get("extension", {}) or {}
        self._bridge: ExtensionBridge | None = None
        self._context: RemoteContext | None = None
        self._page: RemotePage | None = None

    async def start(self):
        host = str(self.extension_cfg.get("host", "127.0.0.1"))
        port = int(self.extension_cfg.get("port", 8765))
        token = _load_pairing_token(self.extension_cfg)
        self._bridge = ExtensionBridge(
            host, port, token,
            float(self.extension_cfg.get("command_timeout_seconds", 30)),
        )
        await self._bridge.start()
        logger.info(
            "Extension bridge listening on ws://%s:%s (pairing token: %s)",
            host, port, TOKEN_PATH,
        )
        try:
            await self._bridge.wait_connected(
                float(self.extension_cfg.get("connect_timeout_seconds", 45))
            )
            result = await self._bridge.request(
                "ensure_tab",
                url=str(self.extension_cfg.get("start_url", "https://www.indeed.com/")),
                reuse=bool(self.extension_cfg.get("reuse_dedicated_tab", True)),
            )
        except Exception:
            await self._bridge.stop()
            self._bridge = None
            raise
        self._context = RemoteContext(self._bridge)
        self._page = self._context._page_from_data(result)
        self._page._recoverable = True
        return self._page

    async def stop(self) -> None:
        if self._bridge is not None:
            if self._page and self.extension_cfg.get("close_tab_on_exit", False):
                with suppress(Exception):
                    await self._page.close()
            await self._bridge.stop()
        self._bridge = None
        self._context = None
        self._page = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def save_session(self) -> None:
        return None

    async def has_saved_session(self) -> bool:
        return True

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        await self.page.goto(url, wait_until=wait_until)

    async def wait_for_navigation(self, timeout: int = 15000) -> None:
        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()


class _ExpectedPage:
    def __init__(self, context: "RemoteContext", opener_tab_id: int | None):
        self.context = context
        self.opener_tab_id = opener_tab_id
        self.value = asyncio.get_running_loop().create_future()

    async def __aenter__(self):
        self.context._page_watchers.append(self)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            with suppress(ValueError):
                self.context._page_watchers.remove(self)
            if not self.value.done():
                self.value.cancel()
        elif self.value.done():
            with suppress(ValueError):
                self.context._page_watchers.remove(self)
        else:
            self.value.add_done_callback(
                lambda _future: self.context._remove_page_watcher(self)
            )


class RemoteContext:
    def __init__(self, bridge: ExtensionBridge):
        self.bridge = bridge
        self.pages: list[RemotePage] = []
        self._page_watchers: list[_ExpectedPage] = []
        bridge.add_event_listener(self._on_event)

    def _remove_page_watcher(self, watcher: _ExpectedPage) -> None:
        with suppress(ValueError):
            self._page_watchers.remove(watcher)

    def _page_from_data(self, data: dict) -> "RemotePage":
        tab_id = int(data["tabId"])
        for page in self.pages:
            if page.tab_id == tab_id:
                page._update(data)
                return page
        page = RemotePage(self, tab_id, data.get("url", ""))
        self.pages.append(page)
        return page

    def _on_event(self, event: dict) -> None:
        if event.get("event") != "tab_created":
            return
        data = event.get("tab") or {}
        if "tabId" not in data:
            return
        page = self._page_from_data(data)
        opener = data.get("openerTabId")
        for watcher in reversed(self._page_watchers):
            if watcher.value.done():
                continue
            if watcher.opener_tab_id is None or watcher.opener_tab_id == opener:
                watcher.value.set_result(page)
                break

    async def new_page(self):
        return self._page_from_data(await self.bridge.request("new_tab", url="about:blank"))

    def expect_page(self, **_kwargs):
        # The extension reports only tabs opened by a dedicated automation tab,
        # so accepting the next such tab also supports nested external ATS flows.
        return _ExpectedPage(self, None)

    async def storage_state(self, **_kwargs):
        return {}


def _pattern(value: Any) -> dict | str:
    if isinstance(value, re.Pattern):
        flags = ""
        if value.flags & re.IGNORECASE:
            flags += "i"
        if value.flags & re.MULTILINE:
            flags += "m"
        return {"regex": value.pattern, "flags": flags}
    return str(value)


class RemoteKeyboard:
    def __init__(self, page: "RemotePage"):
        self.page = page

    async def press(self, key: str):
        return await self.page._dom({"kind": "page"}, "press", key=key)


class RemotePage:
    is_extension = True

    def __init__(self, context: RemoteContext, tab_id: int, url: str = ""):
        self.context = context
        self.bridge = context.bridge
        self.tab_id = tab_id
        self.url = url
        self.keyboard = RemoteKeyboard(self)
        self._closed = False
        self._recoverable = False

    @property
    def frames(self):
        return [self, RemoteFrameLocator(self, "*")]

    def _update(self, data: dict) -> None:
        if "tabId" in data:
            self.tab_id = int(data["tabId"])
        self.url = data.get("url", self.url)
        self._closed = bool(data.get("closed", self._closed))

    async def _command(self, command: str, **payload):
        try:
            result = await self.bridge.request(command, tabId=self.tab_id, **payload)
        except ExtensionBridgeError as exc:
            lost_tab = (
                "No tab with id" in str(exc)
                or "non-dedicated tab" in str(exc)
            )
            if not self._recoverable or not lost_tab or command == "close_tab":
                raise
            logger.warning("Dedicated extension tab was lost; restoring it")
            restored = await self.bridge.request(
                "ensure_tab",
                url=self.url or "https://www.indeed.com/",
                reuse=True,
            )
            self._update(restored)
            result = await self.bridge.request(
                command,
                tabId=self.tab_id,
                **payload,
            )
        if isinstance(result, dict) and "tabId" in result:
            self._update(result)
        return result

    async def _dom(self, descriptor: dict, operation: str, **payload):
        result = await self._command(
            "dom", descriptor=descriptor, operation=operation, payload=payload
        )
        if isinstance(result, dict) and "value" in result:
            self.url = result.get("url", self.url)
            return result["value"]
        return result

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000):
        result = await self._command("navigate", url=url, timeout=timeout)
        self.url = result.get("url", url)
        return None

    async def wait_for_load_state(self, state="domcontentloaded", timeout=30000):
        await self._command("wait_for_load", state=state, timeout=timeout)

    async def wait_for_selector(self, selector: str, timeout: int = 30000, **_kwargs):
        locator = self.locator(selector).first
        await locator.wait_for(timeout=timeout)
        return locator

    async def wait_for_function(self, _expression: str, timeout: int = 30000, **_kwargs):
        try:
            await self._dom({"kind": "page"}, "wait_ready", timeout=timeout)
        except ExtensionBridgeError as exc:
            raise PlaywrightTimeoutError(str(exc)) from exc

    async def wait_for_timeout(self, timeout: int):
        await asyncio.sleep(timeout / 1000)

    def locator(self, selector: str):
        return RemoteLocator(self, [{"kind": "css", "selector": selector}])

    def get_by_role(self, role: str, name=None, **_kwargs):
        return RemoteLocator(
            self, [{"kind": "role", "role": role, "name": _pattern(name) if name else None}]
        )

    def get_by_text(self, text, exact: bool = False, **_kwargs):
        return RemoteLocator(
            self, [{"kind": "text", "text": _pattern(text), "exact": exact}]
        )

    def frame_locator(self, selector: str):
        return RemoteFrameLocator(self, selector)

    async def query_selector_all(self, selector: str):
        locator = self.locator(selector)
        return [locator.nth(index) for index in range(await locator.count())]

    async def query_selector(self, selector: str):
        locator = self.locator(selector)
        return locator.first if await locator.count() else None

    async def evaluate(self, expression: str, arg=None):
        return await self._dom(
            {"kind": "page"}, "evaluate_known", expression=expression, argument=arg
        )

    async def screenshot(self, path: str | None = None, full_page: bool = False, **_kwargs):
        result = await self._command("screenshot", fullPage=full_page)
        data = base64.b64decode(result["data"])
        if path:
            Path(path).write_bytes(data)
        return data

    async def bring_to_front(self):
        await self._command("activate")

    async def close(self):
        if not self._closed:
            await self._command("close_tab")
            self._closed = True

    def is_closed(self) -> bool:
        return self._closed

    def expect_file_chooser(self, **_kwargs):
        return _ExpectedFileChooser(self)


class RemoteFrameLocator:
    def __init__(self, page: RemotePage, frame_selector: str):
        self.page = page
        self.frame_selector = frame_selector

    def locator(self, selector: str):
        return RemoteLocator(
            self.page,
            [{"kind": "css", "selector": selector}],
            frame_selector=self.frame_selector,
        )

    def get_by_role(self, role: str, name=None, **_kwargs):
        return RemoteLocator(
            self.page,
            [{"kind": "role", "role": role, "name": _pattern(name) if name else None}],
            frame_selector=self.frame_selector,
        )

    def get_by_text(self, text, exact: bool = False, **_kwargs):
        return RemoteLocator(
            self.page,
            [{"kind": "text", "text": _pattern(text), "exact": exact}],
            frame_selector=self.frame_selector,
        )


class _ExpectedFileChooser:
    def __init__(self, page: RemotePage):
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(RemoteFileChooser(page))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class RemoteFileChooser:
    def __init__(self, page: RemotePage):
        self.page = page

    async def set_files(self, path: str):
        await self.page.locator('input[type="file"]').first.set_input_files(path)


class RemoteLocator:
    def __init__(
        self,
        page: RemotePage,
        steps: list[dict],
        frame_selector: str | None = None,
    ):
        self.page = page
        self.steps = steps
        self.frame_selector = frame_selector

    def _descriptor(self) -> dict:
        return {
            "kind": "locator",
            "steps": self.steps,
            "frameSelector": self.frame_selector,
        }

    @property
    def first(self):
        return self.nth(0)

    @property
    def last(self):
        return RemoteLocator(
            self.page, [*self.steps, {"kind": "index", "index": -1}], self.frame_selector
        )

    def nth(self, index: int):
        return RemoteLocator(
            self.page, [*self.steps, {"kind": "index", "index": index}], self.frame_selector
        )

    def locator(self, selector: str):
        return RemoteLocator(
            self.page,
            [*self.steps, {"kind": "css", "selector": selector}],
            self.frame_selector,
        )

    def filter(self, has_text=None, **_kwargs):
        return RemoteLocator(
            self.page,
            [*self.steps, {"kind": "filter_text", "text": _pattern(has_text)}],
            self.frame_selector,
        )

    async def count(self) -> int:
        return int(await self.page._dom(self._descriptor(), "count"))

    async def inner_text(self, **_kwargs) -> str:
        return str(await self.page._dom(self._descriptor(), "inner_text") or "")

    async def text_content(self, **_kwargs):
        return await self.page._dom(self._descriptor(), "text_content")

    async def get_attribute(self, name: str):
        return await self.page._dom(self._descriptor(), "get_attribute", name=name)

    async def input_value(self, **_kwargs) -> str:
        return str(await self.page._dom(self._descriptor(), "input_value") or "")

    async def is_visible(self, **_kwargs) -> bool:
        return bool(await self.page._dom(self._descriptor(), "is_visible"))

    async def is_enabled(self, **_kwargs) -> bool:
        return bool(await self.page._dom(self._descriptor(), "is_enabled"))

    async def is_checked(self, **_kwargs) -> bool:
        return bool(await self.page._dom(self._descriptor(), "is_checked"))

    async def click(self, **_kwargs):
        return await self.page._dom(self._descriptor(), "click")

    async def fill(self, value: str, **_kwargs):
        return await self.page._dom(self._descriptor(), "fill", value=str(value))

    async def press(self, key: str, **_kwargs):
        return await self.page._dom(self._descriptor(), "press", key=key)

    async def check(self, **_kwargs):
        return await self.page._dom(self._descriptor(), "check")

    async def select_option(self, value=None, label=None, **_kwargs):
        return await self.page._dom(
            self._descriptor(), "select_option", value=value, label=label
        )

    async def scroll_into_view_if_needed(self, **_kwargs):
        return await self.page._dom(self._descriptor(), "scroll")

    async def wait_for(self, state: str = "visible", timeout: int = 30000, **_kwargs):
        try:
            return await self.page._dom(
                self._descriptor(), "wait", state=state, timeout=timeout
            )
        except ExtensionBridgeError as exc:
            raise PlaywrightTimeoutError(str(exc)) from exc

    async def set_input_files(self, path: str, **_kwargs):
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        if file_path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Extension-mode uploads are limited to 10 MiB")
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return await self.page._dom(
            self._descriptor(),
            "set_files",
            name=file_path.name,
            mime=mime,
            data=data,
        )

    async def evaluate(self, expression: str, arg=None):
        return await self.page._dom(
            self._descriptor(), "evaluate_known", expression=expression, argument=arg
        )

    async def evaluate_handle(self, expression: str, arg=None):
        if "tagName === 'LABEL'" in expression:
            return RemoteLocator(
                self.page,
                [*self.steps, {"kind": "closest", "selector": "label"}],
                self.frame_selector,
            )
        return None

    def as_element(self):
        return self
