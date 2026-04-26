"""
browser_session.py — Persistent Playwright browser session.

Uses launch_persistent_context so cookies, logins, localStorage, and
browser history survive across sessions — just like a real browser profile.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path("static/screenshots").resolve()
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Saved browser profile — logins persist here
PROFILE_DIR = Path("browser_profile").resolve()
PROFILE_DIR.mkdir(exist_ok=True)

# Optional: point to Brave, Firefox, or any Chromium-based browser
# Set BROWSER_EXECUTABLE in .env, e.g.:
#   BROWSER_EXECUTABLE=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
BROWSER_EXECUTABLE = os.getenv("BROWSER_EXECUTABLE", "").strip() or None


class BrowserSession:
    """
    Singleton visible browser with persistent profile.
    Thread-safe via asyncio.Lock. Auto-launches on first use.
    """

    def __init__(self):
        self._pw      = None
        self._context = None   # launch_persistent_context IS both browser + context
        self._page    = None
        self._lock    = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure(self):
        if self._page and not self._page.is_closed():
            return

        from playwright.async_api import async_playwright
        logger.info("Launching visible browser with persistent profile…")

        self._pw = await async_playwright().start()

        # launch_persistent_context = browser + saved profile in one call
        # On cloud servers (no display), force headless. Set BROWSER_HEADLESS=false for local visible browser.
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() != "false"
        launch_kwargs = dict(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        if BROWSER_EXECUTABLE:
            launch_kwargs["executable_path"] = BROWSER_EXECUTABLE
            logger.info("Using browser: %s", BROWSER_EXECUTABLE)

        self._context = await self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), **launch_kwargs
        )

        # Reuse existing page if profile already has one open
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        logger.info("Browser ready (profile: %s)", PROFILE_DIR)

    async def _snap(self, action: str, send=None) -> str:
        try:
            png = await self._page.screenshot(full_page=False)
        except Exception:
            return ""
        name = f"{uuid.uuid4().hex}.png"
        (SCREENSHOTS_DIR / name).write_bytes(png)
        web_path = f"/static/screenshots/{name}"
        if send:
            await send({
                "type": "browser_frame",
                "url": self._page.url,
                "action": action,
                "image_path": web_path,
            })
        return web_path

    async def _page_context(self) -> str:
        """Extract interactive elements and visible text so the LLM can 'see' the page."""
        try:
            data = await self._page.evaluate("""() => {
                const els = [];
                document.querySelectorAll(
                    'a, button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [role="menuitem"], [data-testid]'
                ).forEach(el => {
                    const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('data-testid') || '').trim().slice(0, 80);
                    const tag = el.tagName.toLowerCase();
                    const testid = el.getAttribute('data-testid') || '';
                    const role = el.getAttribute('role') || '';
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && text) {
                        els.push({tag, role, testid, text, x: Math.round(rect.x + rect.width/2), y: Math.round(rect.y + rect.height/2)});
                    }
                });
                // Deduplicate by text
                const seen = new Set();
                return els.filter(e => {
                    const key = e.text + e.testid;
                    if (seen.has(key)) return false;
                    seen.add(key);
                    return true;
                }).slice(0, 25);
            }""")
            if not data:
                return ""
            lines = [f"  [{e['tag']}]{' data-testid='+e['testid'] if e['testid'] else ''} \"{e['text']}\" @ x={e['x']},y={e['y']}" for e in data]
            return "\n\nVISIBLE ELEMENTS (use these for clicking):\n" + "\n".join(lines)
        except Exception:
            return ""

    # ── public methods ────────────────────────────────────────────────────────

    async def navigate(self, url: str, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            if send:
                await send({"type": "browser_frame", "url": url,
                            "action": f"Navigating to {url}", "image_path": None})
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._page.wait_for_timeout(1200)
            except Exception as exc:
                return {"output": f"Navigation error: {exc}", "image_path": None}
            img = await self._snap("Page loaded", send)
            ctx = await self._page_context()
            return {
                "output": f"Navigated to: {self._page.url}\nTitle: {await self._page.title()}{ctx}",
                "image_path": img,
            }

    async def screenshot(self, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            img = await self._snap("Screenshot", send)
            return {"output": f"Screenshot taken. URL: {self._page.url}", "image_path": img}

    async def click(self, selector: str, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            try:
                if selector.startswith("text="):
                    el = self._page.get_by_text(selector[5:].strip(), exact=False).first
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=8_000)
                elif selector.startswith("x="):
                    parts = dict(p.split("=") for p in selector.split(","))
                    await self._page.mouse.click(int(parts["x"]), int(parts["y"]))
                else:
                    el = self._page.locator(selector).first
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=8_000)
                await self._page.wait_for_timeout(900)
                img = await self._snap(f"Clicked: {selector[:40]}", send)
                ctx = await self._page_context()
                return {"output": f"Clicked '{selector}'. URL: {self._page.url}{ctx}", "image_path": img}
            except Exception as exc:
                img = await self._snap("Click failed", send)
                ctx = await self._page_context()
                return {"output": f"Click failed ({exc}). Try using x=,y= coordinates from VISIBLE ELEMENTS below.{ctx}", "image_path": img}

    async def type_text(self, selector: str, text: str, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            try:
                if selector == "focused":
                    await self._page.keyboard.type(text, delay=25)
                elif selector.startswith("text="):
                    label = selector[5:].strip()
                    el = self._page.get_by_label(label).first
                    if await el.count() == 0:
                        el = self._page.get_by_placeholder(label).first
                    await el.click()
                    await el.fill(text)
                else:
                    el = self._page.locator(selector).first
                    await el.click()
                    await el.fill(text)
                await self._page.wait_for_timeout(400)
                img = await self._snap(f"Typed into {selector[:30]}", send)
                ctx = await self._page_context()
                return {"output": f"Typed '{text[:80]}' into '{selector}'.{ctx}", "image_path": img}
            except Exception as exc:
                img = await self._snap("Type failed", send)
                ctx = await self._page_context()
                return {"output": f"Type failed ({exc}). Use x=,y= coordinates from VISIBLE ELEMENTS below.{ctx}", "image_path": img}

    async def press_key(self, key: str, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            await self._page.keyboard.press(key)
            await self._page.wait_for_timeout(700)
            img = await self._snap(f"Pressed {key}", send)
            return {"output": f"Pressed '{key}'. URL: {self._page.url}", "image_path": img}

    async def scroll(self, direction: str = "down", amount: int = 400, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            delta = amount if direction.lower() == "down" else -amount
            await self._page.evaluate(f"window.scrollBy(0, {delta})")
            await self._page.wait_for_timeout(400)
            img = await self._snap(f"Scrolled {direction}", send)
            return {"output": f"Scrolled {direction} {amount}px", "image_path": img}

    async def read_page(self, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            title = await self._page.title()
            text = await self._page.evaluate("""() => {
                return Array.from(document.querySelectorAll(
                    'p,h1,h2,h3,h4,h5,li,td,th,span,a,label,button,input,textarea'
                )).map(e => e.innerText?.trim()).filter(t => t && t.length > 1).join('\\n');
            }""")
            return {
                "output": f"**{title}**\n**URL:** {self._page.url}\n\n{text[:8000]}",
                "image_path": None,
            }

    async def wait(self, milliseconds: int = 1500, send=None) -> dict:
        async with self._get_lock():
            await self._ensure()
            ms = max(200, min(milliseconds, 10_000))
            await self._page.wait_for_timeout(ms)
            img = await self._snap(f"Waited {ms}ms", send)
            return {"output": f"Waited {ms}ms. URL: {self._page.url}", "image_path": img}

    async def close(self, send=None) -> dict:
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._context = None
            self._page    = None
            self._pw      = None
        return {"output": "Browser closed. Profile saved.", "image_path": None}

    @property
    def is_open(self) -> bool:
        return bool(self._page and not self._page.is_closed())


browser = BrowserSession()
