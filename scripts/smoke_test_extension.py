"""Disposable end-to-end smoke test for Chrome extension mode.

This never visits Indeed. It loads the unpacked extension in a temporary Chrome
profile and exercises navigation, locators, form filling, file upload, and a
screenshot against a local test page.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from src.extension_browser import ExtensionBrowserManager


HTML = b"""<!doctype html><html><body>
<h1>Bridge smoke test</h1>
<label>Name <input id="name"></label>
<label>Role <select id="role"><option>Choose</option><option>Engineer</option></select></label>
<input id="resume" type="file">
<button id="apply" onclick="document.querySelector('#result').textContent='clicked'">Apply now</button>
<a id="popup" href="/" target="_blank">Open popup</a>
<div id="result"></div>
</body></html>"""

IFRAME_HTML = b"""<!doctype html><html><body>
<input id="top-only">
<iframe title="Apply form" src="/apply-frame"></iframe>
</body></html>"""

APPLY_FRAME_HTML = b"""<!doctype html><html><body>
<label>Frame field <input id="inside"></label>
<button onclick="document.querySelector('#frame-result').textContent='continued'">Continue</button>
<div id="frame-result"></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            IFRAME_HTML if self.path == "/iframe"
            else APPLY_FRAME_HTML if self.path == "/apply-frame"
            else HTML
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


async def run(headless: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web_port = server.server_address[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        bridge_port = probe.getsockname()[1]
    token = "smoke-test-" + ("x" * 40)
    os.environ["INDEED_EXTENSION_TOKEN"] = token
    source_extension = Path("extension/chrome").resolve()

    with tempfile.TemporaryDirectory(prefix="indeed-extension-smoke-") as temp_root:
        root = Path(temp_root)
        profile = root / "profile"
        extension_dir = root / "extension"
        shutil.copytree(source_extension, extension_dir)
        # Pre-grant the optional capture host access in the disposable profile
        # so Chrome's permission bubble cannot block an unattended smoke test.
        manifest_path = extension_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_permissions"].append("<all_urls>")
        manifest["optional_host_permissions"].remove("<all_urls>")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                headless=headless,
                args=[
                    f"--disable-extensions-except={extension_dir}",
                    f"--load-extension={extension_dir}",
                ],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else (
                    await context.wait_for_event("serviceworker", timeout=15000)
                )
                extension_id = worker.url.split("/")[2]
                options = await context.new_page()
                await options.goto(f"chrome-extension://{extension_id}/options.html")

                manager = ExtensionBrowserManager({
                    "bot": {"browser": {"extension": {
                        "host": "127.0.0.1",
                        "port": bridge_port,
                        "connect_timeout_seconds": 15,
                        "command_timeout_seconds": 10,
                        "start_url": f"http://127.0.0.1:{web_port}/",
                        "reuse_dedicated_tab": False,
                    }}}
                })
                start_task = asyncio.create_task(manager.start())
                await options.fill("#url", f"ws://127.0.0.1:{bridge_port}")
                await options.fill("#token", token)
                await options.check("#screenshots")
                await options.click("#save")
                page = await start_task

                await page.locator("#name").fill("Ada")
                assert await page.locator("#name").input_value() == "Ada"
                await page.locator("#role").select_option(label="Engineer")
                assert await page.locator("#role").input_value() == "Engineer"
                resume = root / "resume.txt"
                resume.write_text("smoke", encoding="utf-8")
                await page.locator("#resume").set_input_files(str(resume))
                await page.get_by_role("button", name="Apply now").click()
                assert await page.locator("#result").inner_text() == "clicked"
                await page.goto(f"http://127.0.0.1:{web_port}/iframe")
                await page.locator("#inside").fill("inside-frame")
                assert await page.locator("#inside").input_value() == "inside-frame"
                await page.get_by_role("button", name="Continue").click()
                assert await page.locator("#frame-result").inner_text() == "continued"
                await page.goto(f"http://127.0.0.1:{web_port}/")
                async with manager.context.expect_page() as new_page:
                    await page.locator("#popup").click()
                popup = await asyncio.wait_for(new_page.value, 5)
                await popup.wait_for_load_state(timeout=5000)
                assert await popup.locator("h1").inner_text() == "Bridge smoke test"
                await popup.close()

                agent_page = await manager.context.new_page()
                await agent_page.goto(f"http://127.0.0.1:{web_port}/")
                assert await agent_page.locator("#name").count() == 1
                await agent_page.close()
                image = await page.screenshot()
                assert image.startswith(b"\x89PNG")
                print("Extension smoke test passed")
                await manager.stop()
            finally:
                await context.close()
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    asyncio.run(run(parser.parse_args().headless))
