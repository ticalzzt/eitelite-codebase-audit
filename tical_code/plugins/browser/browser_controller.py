"""CDP Browser Controller — connect to real or headless Chrome via DevTools Protocol.

Supports:
  - Headless Chrome (default, fast)
  - Real Chrome with user profile (stealth, anti-bot-detection)
  - Both modes use CDP WebSocket, no Playwright/Selenium dependency.

Usage:
  bc = BrowserController(cdp_url="ws://127.0.0.1:9222/...")
  asyncio.run(bc.navigate("https://example.com"))
  text = asyncio.run(bc.extract())
  asyncio.run(bc.click("#button"))
"""

import asyncio
import base64
import json
import logging
import re
import time
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("tical-code.cdp_browser")

# ============================================================
# Minimal CDP over WebSocket
# ============================================================

class CDPConnection:
    """Single CDP WebSocket connection to a Chrome DevTools Protocol endpoint."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._msg_id = 0
        self._pending = {}

    async def connect(self):
        import websockets
        self._ws = await websockets.connect(self.ws_url, max_size=2**24)
        logger.info(f"CDP connected: {self.ws_url[:60]}...")

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, method: str, params: dict = None) -> dict:
        """Send CDP command and wait for result."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))
        # Read responses until we get our id
        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
            # Handle events (ignore for now)

    async def send_async(self, method: str, params: dict = None):
        """Fire-and-forget CDP command."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))


# ============================================================
# BrowserController
# ============================================================

class BrowserController:
    """
    CDP-based browser controller.

    Two modes:
      1. Connect to existing Chrome via CDP URL
      2. Auto-launch headless Chrome (fallback)

    Anti-detection:
      - Evades navigator.webdriver detection
      - Overrides User-Agent to non-headless string
      - Hides Chrome automation flags
    """

    def __init__(self, cdp_url: str = None, headless: bool = True,
                 user_data_dir: str = None, window_size: tuple = (1280, 720),
                 proxy: str = None):
        self._cdp_url = cdp_url
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._window_size = window_size
        self._proxy = proxy
        self._conn = None
        self._page_id = None  # Target ID for the page we control
        self._chrome_proc = None
        self._stealth_applied = False

    # ---- Lifecycle ----

    async def start(self):
        """Start or connect to Chrome."""
        if self._cdp_url:
            # Connect to existing Chrome instance
            ws_url = await self._resolve_cdp_url(self._cdp_url)
        else:
            # Launch our own Chrome
            ws_url = await self._launch_chrome()

        # Connect to browser-level WebSocket
        self._conn = CDPConnection(ws_url)
        await self._conn.connect()

        # Get or create a page target
        await self._ensure_page()

        # Apply stealth
        await self._apply_stealth()

        # Set viewport
        w, h = self._window_size
        await self._conn.send("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False
        })
        logger.info(f"Browser ready: {w}x{h}, headless={self._headless}")

    async def stop(self):
        """Close browser and clean up temp profile."""
        import shutil
        if self._conn:
            try:
                await self._conn.close()
            except Exception:
                pass
        if self._chrome_proc:
            try:
                self._chrome_proc.terminate()
                try:
                    self._chrome_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._chrome_proc.kill()
            except Exception:
                pass
        # Clean up temp user data dir (蒸馏自 undetected-chromedriver quit lines 778-796)
        if self._user_data_dir and os.path.exists(self._user_data_dir):
            for _ in range(5):
                try:
                    shutil.rmtree(self._user_data_dir, ignore_errors=False)
                    logger.info(f"Cleaned up temp profile: {self._user_data_dir}")
                    break
                except (FileNotFoundError, PermissionError, OSError) as e:
                    logger.debug(f"Cleanup retry: {e}")
                    await asyncio.sleep(0.1)

    # ---- Page-level CDP connection ----

    async def _ensure_page(self):
        """Find or create a page target and connect to its WebSocket directly."""
        import urllib.request
        # Get targets via HTTP
        http_base = self._cdp_url.rstrip("/").replace("/json/version", "")
        req = urllib.request.Request(f"{http_base}/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            targets = json.loads(resp.read())

        pages = [t for t in targets if t["type"] == "page"]
        if pages:
            target = pages[0]
        else:
            # Create new page via browser WS
            result = await self._conn.send("Target.createTarget", {
                "url": "about:blank"
            })
            target_id = result["targetId"]
            # Get WebSocket URL for the new target
            req2 = urllib.request.Request(f"{http_base}/json")
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                targets2 = json.loads(resp2.read())
            target = next(t for t in targets2 if t["id"] == target_id)

        page_ws_url = target["webSocketDebuggerUrl"]
        self._page_id = target["id"]
        logger.info(f"Page target: {self._page_id[:12]}...")

        # Close browser-level connection, open page-level connection
        await self._conn.close()
        self._conn = CDPConnection(page_ws_url)
        await self._conn.connect()

    # ---- Stealth (蒸馏自 undetected-chromedriver + browser-use) ----

    async def _apply_stealth(self):
        """Apply comprehensive anti-detection measures.

        Distilled from:
          - undetected-chromedriver (12.6k ⭐): Proxy-based navigator patch,
            full chrome.runtime spoof, Function.prototype.toString native code
          - browser-use (95k ⭐): CAPTCHA detection pattern
        """
        if self._stealth_applied:
            return

        # 1. Proxy-based navigator.webdriver (deeper than defineProperty)
        #    undetected-chromedriver lines 503-514
        js_stealth = """
        // Proxy-based navigator patch — catches 'has' trap too
        window.navigator = new Proxy(window.navigator, {
            has: (target, key) => (key === 'webdriver' ? false : key in target),
            get: (target, key) =>
                key === 'webdriver' ? false :
                typeof target[key] === 'function' ? target[key].bind(target) : target[key],
        });

        // Full window.chrome spoof (undetected-chromedriver lines 536-591)
        window.chrome = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
            }
        };

        // Touch & connection API spoofing (undetected-chromedriver lines 532-533)
        Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 1 });
        if (navigator.connection) {
            Object.defineProperty(navigator.connection, 'rtt', { get: () => 100 });
        }

        // Notification permission (undetected-chromedriver lines 594-604)
        if (!window.Notification) {
            window.Notification = { permission: 'denied' };
        }
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: window.Notification.permission })
                : origQuery(params);

        // Function.prototype.toString native code spoofing (undetected-chromedriver lines 606-625)
        // Prevents "not native code" detection used by Cloudflare
        const nativeToStringStr = Error.toString().replace(/Error/g, 'toString');
        const origToString = Function.prototype.toString;
        Function.prototype.toString = function() {
            if (this === navigator.permissions.query ||
                this === window.navigator.permissions.query) {
                return 'function query() { [native code] }';
            }
            if (this === arguments.callee) return nativeToStringStr;
            return origToString.apply(this, arguments);
        };
        """
        await self._conn.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": js_stealth,
        })

        # 2. Dynamic User-Agent cleaning (remove "Headless" substring)
        real_ua = await self._conn.send("Runtime.evaluate", {
            "expression": "navigator.userAgent",
            "returnByValue": True,
        })
        ua_value = real_ua.get("result", {}).get("value", "")
        clean_ua = ua_value.replace("Headless", "").strip()
        await self._conn.send("Network.setUserAgentOverride", {
            "userAgent": clean_ua,
            "acceptLanguage": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        })

        # 3. Enable Network + Page events
        await self._conn.send("Network.enable")
        await self._conn.send("Page.enable")

        self._stealth_applied = True
        logger.info(f"Stealth applied (Proxy navigator + chrome.runtime + native-code spoof)")
        logger.info(f"  UA: {clean_ua[:80]}")

    # ---- Navigation ----

    async def navigate(self, url: str) -> dict:
        """Navigate to URL and wait for page load."""
        await self._conn.send("Page.navigate", {"url": url})
        # Wait for load event
        await asyncio.sleep(2)
        # Poll until ready
        for _ in range(30):
            result = await self._conn.send("Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True,
            })
            state = result.get("result", {}).get("value", "")
            if state == "complete":
                break
            await asyncio.sleep(0.5)
        return {"ok": True, "url": url}

    async def snapshot(self) -> str:
        """Get page content as text (rendered)."""
        result = await self._conn.send("Runtime.evaluate", {
            "expression": "document.body ? document.body.innerText : ''",
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "")

    async def extract(self) -> str:
        """Extract visible text and interactive elements."""
        js = """
        (() => {
            const items = [];
            const elts = document.querySelectorAll('a, button, input, textarea, select, [role=button], [tabindex]:not([tabindex=-1])');
            elts.forEach((e, i) => {
                const tag = e.tagName.toLowerCase();
                const type = e.type || '';
                const text = (e.textContent || '').trim().slice(0, 60);
                const placeholder = e.placeholder || '';
                const href = e.href || '';
                const label = e.getAttribute('aria-label') || '';
                let info = `[${i}] <${tag}`;
                if (type) info += ` type="${type}"`;
                if (text) info += ` "${text}"`;
                if (placeholder) info += ` ph="${placeholder}"`;
                if (label) info += ` aria="${label}"`;
                if (href && href !== '#') info += ` -> ${href}`;
                info += '>';
                items.push(info);
            });
            const title = document.title;
            const text = (document.body ? document.body.innerText : '').slice(0, 3000);
            return JSON.stringify({ title, interactive: items, text: text.slice(0, 1000) });
        })();
        """
        result = await self._conn.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "{}")

    async def screenshot(self) -> str:
        """Take screenshot, returns base64 PNG data."""
        result = await self._conn.send("Page.captureScreenshot", {
            "format": "png", "fromSurface": True
        })
        return result.get("data", "")

    async def click(self, ref: str) -> dict:
        """Click element by index ref like '[5]' or CSS selector."""
        idx = None
        selector = ref
        m = re.match(r'^\[(\d+)\]$', ref)
        if m:
            idx = int(m.group(1))
            js = f"""
            (() => {{
                const elts = document.querySelectorAll('a, button, input, textarea, select, [role=button], [tabindex]:not([tabindex=-1])');
                const e = elts[{idx}];
                if (!e) return 'element not found';
                const rect = e.getBoundingClientRect();
                return JSON.stringify({{
                    x: rect.x + rect.width/2, y: rect.y + rect.height/2,
                    tag: e.tagName, text: (e.textContent||'').trim().slice(0,40)
                }});
            }})();
            """
            result = await self._conn.send("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            })
            info = result.get("result", {}).get("value", "")
            if not info or info == "element not found":
                return {"error": f"Element [{idx}] not found"}
            try:
                pos = json.loads(info)
            except json.JSONDecodeError:
                return {"error": f"Cannot find element position: {info}"}
        else:
            # CSS selector: find position via JS
            js = f"""
            (() => {{
                try {{
                    const e = document.querySelector('{selector}');
                    if (!e) return 'not found';
                    const rect = e.getBoundingClientRect();
                    return JSON.stringify({{x: rect.x + rect.width/2, y: rect.y + rect.height/2}});
                }} catch(e) {{ return 'error: ' + e.message; }}
            }})();
            """
            # Escape single quotes in selector
            js = js.replace("'", "\\'")
            result = await self._conn.send("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            })
            info = result.get("result", {}).get("value", "")
            if not info:
                return {"error": f"Selector '{selector}' not found"}
            if info in ("not found",):
                return {"error": f"Element '{selector}' not found"}
            try:
                pos = json.loads(info)
            except json.JSONDecodeError:
                return {"error": f"Cannot find selector: {info}"}

        # Click at coordinates
        await self._conn.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1,
        })
        await self._conn.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1,
        })
        await asyncio.sleep(0.5)
        return {"ok": True, "ref": ref, "clicked": pos.get("text", "")}

    async def type_text(self, ref: str, text: str) -> dict:
        """Type text into an input field."""
        # Click first to focus
        click_result = await self.click(ref)
        if "error" in click_result:
            return click_result
        await asyncio.sleep(0.2)
        # Clear existing
        await self._conn.send("Input.insertText", {"text": text})
        return {"ok": True, "ref": ref, "text": text[:40]}

    async def get_url(self) -> str:
        """Get current page URL."""
        result = await self._conn.send("Runtime.evaluate", {
            "expression": "window.location.href",
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "")

    async def get_title(self) -> str:
        """Get current page title."""
        result = await self._conn.send("Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "")

    # ---- CDP URL Resolution ----

    async def _resolve_cdp_url(self, cdp_url: str) -> str:
        """Convert http://host:port/json/version to ws://... URL."""
        if cdp_url.startswith("ws://") or cdp_url.startswith("wss://"):
            return cdp_url
        # It's an HTTP endpoint - fetch the WebSocket URL
        import urllib.request
        try:
            http_url = cdp_url.rstrip("/")
            if "/json/version" not in http_url:
                http_url += "/json/version"
            req = urllib.request.Request(http_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            ws = data.get("webSocketDebuggerUrl", "")
            if ws:
                logger.info(f"Resolved CDP URL: {ws[:60]}...")
                return ws
        except Exception as e:
            logger.warning(f"CDP URL resolution failed: {e}")
        raise RuntimeError(f"Cannot resolve CDP URL: {cdp_url}")

    async def _launch_chrome(self) -> str:
        """Launch a new Chrome instance and return its CDP WebSocket URL."""
        import tempfile
        import random

        chrome_paths = [
            "/snap/chromium/current/usr/lib/chromium-browser/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
        chrome_bin = None
        for p in chrome_paths:
            if os.path.exists(p):
                chrome_bin = p
                break
        if not chrome_bin:
            raise RuntimeError("No Chrome/Chromium binary found")

        user_dir = self._user_data_dir or tempfile.mkdtemp(prefix="cdp-browser-")
        port = random.randint(10000, 20000)
        cmd = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            cmd += ["--headless", "--disable-gpu", "--no-sandbox",
                    "--disable-dev-shm-usage"]
        if self._proxy:
            cmd += [f"--proxy-server={self._proxy}"]
            logger.info(f"Chrome proxy: {self._proxy}")

        logger.info(f"Launching Chrome: {' '.join(cmd[:4])}...")
        self._chrome_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for CDP to become available
        for i in range(30):
            try:
                ws_url = await self._resolve_cdp_url(f"http://127.0.0.1:{port}")
                return ws_url
            except Exception:
                await asyncio.sleep(1)
        raise RuntimeError("Chrome failed to start within 30s")
