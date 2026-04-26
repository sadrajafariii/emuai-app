"""
All tool implementations for EmulAItor.
Each tool returns a dict: {"output": str, "image_path": str | None}
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace").resolve()
SCREENSHOTS_DIR = Path("static/screenshots").resolve()
WORKSPACE.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

BROWSE_TIMEOUT = int(os.getenv("BROWSE_TIMEOUT", "60"))
CODE_TIMEOUT = int(os.getenv("CODE_TIMEOUT", "30"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_path(path: str) -> Path:
    """Resolve path inside workspace; raise if it escapes."""
    p = (WORKSPACE / path).resolve()
    if not str(p).startswith(str(WORKSPACE)):
        raise ValueError(f"Path '{path}' escapes workspace.")
    return p


def _save_screenshot(png_bytes: bytes) -> str:
    """Save PNG bytes and return web-accessible path."""
    name = f"{uuid.uuid4().hex}.png"
    dest = SCREENSHOTS_DIR / name
    dest.write_bytes(png_bytes)
    return f"/static/screenshots/{name}"


# ── tool: web_search ─────────────────────────────────────────────────────────

async def web_search(query: str, _send=None) -> dict:
    """Search via DuckDuckGo API + live browser visualisation running in parallel."""

    async def _api_search():
        try:
            from ddgs import DDGS
            loop = asyncio.get_event_loop()
            def _run():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=8))
            return await loop.run_in_executor(None, _run)
        except Exception as exc:
            logger.warning("DDG API error: %s", exc)
            return []

    async def _browser_visual():
        """Open a real browser, go to Bing, type + submit the query live."""
        try:
            from playwright.async_api import async_playwright
            SEARCH_HOME = "https://www.bing.com"

            if _send:
                await _send({"type": "browser_frame", "url": SEARCH_HOME,
                             "action": "Opening browser…", "image_path": None})

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                # Step 1 — Bing home page
                if _send:
                    await _send({"type": "browser_frame", "url": SEARCH_HOME,
                                 "action": "Navigating to Bing", "image_path": None})
                await page.goto(SEARCH_HOME, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(500)
                png = await page.screenshot(full_page=False)
                await _emit_frame(_send, page.url, "Bing loaded", png)

                # Step 2 — simulate typing delay then go to search results URL
                encoded_q = urllib.parse.quote_plus(query)
                search_url = f"https://www.bing.com/search?q={encoded_q}"

                # Brief pause so user sees home page
                await page.wait_for_timeout(400)
                if _send:
                    await _send({"type": "browser_frame", "url": search_url,
                                 "action": f'Searching: "{query}"', "image_path": None})

                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)

                png = await page.screenshot(full_page=False)
                await _emit_frame(_send, page.url, "Results loaded", png)

                # Step 3 — scroll to reveal more results
                await page.evaluate("window.scrollTo(0, 500)")
                await page.wait_for_timeout(500)
                png = await page.screenshot(full_page=False)
                await _emit_frame(_send, page.url, "Reading results...", png)

                await browser.close()
        except Exception as exc:
            logger.warning("Browser visual search failed (non-fatal): %s", exc)

    # Run API search and browser visual concurrently
    api_task      = asyncio.create_task(_api_search())
    browser_task  = asyncio.create_task(_browser_visual())
    results, _    = await asyncio.gather(api_task, browser_task)

    if not results:
        return {"output": "No results found.", "image_path": None}

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results[:6], 1):
        lines.append(f"{i}. **{r.get('title', '')}**")
        lines.append(f"   {r.get('href', '')}")
        lines.append(f"   {r.get('body', '')}\n")
    return {"output": "\n".join(lines), "image_path": None}


# ── tool: browse ─────────────────────────────────────────────────────────────

async def _browse_via_extension(url: str, task: str, send=None) -> dict:
    """Browse in user's real Chrome via the browser extension."""
    import main as _main
    import base64 as _b64

    if send:
        await send({"type": "browser_frame", "url": url, "action": "Opening in your Chrome…", "image_path": None})

    result = await _main.extension_command({"type": "navigate", "url": url}, timeout=20.0)

    screenshot_path = None
    if result.get("screenshot"):
        # screenshot is a base64 data URL from captureVisibleTab
        data_url = result["screenshot"]
        if "," in data_url:
            b64 = data_url.split(",", 1)[1]
            screenshot_path = _save_screenshot(_b64.b64decode(b64))
        if send:
            await send({"type": "browser_frame", "url": result.get("url", url), "action": task or "Done", "image_path": screenshot_path})

    return {
        "output": f"Opened {url} in your Chrome browser.\n\n{result.get('text', '')}",
        "image_path": screenshot_path,
    }


async def browse(url: str, task: str = "", _send=None, use_my_browser: bool = False) -> dict:
    """Navigate to URL. Uses user's real Chrome via extension if connected, else Playwright."""
    # Try extension first (user's real Chrome with tab group)
    try:
        import main as _main
        if _main._extension_ws is not None:
            return await _browse_via_extension(url, task, _send)
    except Exception as exc:
        logger.warning("Extension browse failed, falling back: %s", exc)

    # Fallback: headless Playwright
    try:
        return await _browse_playwright(url, task, _send)
    except Exception as exc:
        logger.warning("Playwright browse failed: %s", exc)
        return {"output": f"Could not browse {url}. Install the bud Chrome extension to enable real browser browsing.", "image_path": None}


async def _emit_frame(send, url: str, action: str, png: bytes) -> str:
    """Save screenshot and push a browser_frame event."""
    img_path = _save_screenshot(png)
    if send:
        await send({"type": "browser_frame", "url": url, "action": action, "image_path": img_path})
    return img_path


async def _browse_playwright(url: str, task: str, send=None) -> dict:
    from playwright.async_api import async_playwright

    # Announce intent before browser opens
    if send:
        await send({"type": "browser_frame", "url": url, "action": "Opening browser…", "image_path": None})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # ── navigate ──────────────────────────────────────────────────
            if send:
                await send({"type": "browser_frame", "url": url, "action": f"Navigating to {url}", "image_path": None})

            await page.goto(url, timeout=BROWSE_TIMEOUT * 1000, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)

            current_url = page.url
            png = await page.screenshot(full_page=False)
            last_img = await _emit_frame(send, current_url, "Page loaded", png)

            # ── task interactions with per-action screenshots ──────────────
            if task:
                last_img = await _interact_with_screenshots(page, task, send)

            # ── final scroll + screenshot ─────────────────────────────────
            await page.evaluate("window.scrollTo(0, 300)")
            await page.wait_for_timeout(400)
            png = await page.screenshot(full_page=False)
            last_img = await _emit_frame(send, page.url, "Done", png)

            # ── extract text ──────────────────────────────────────────────
            text = await page.evaluate("""() => {
                const sel = 'p, h1, h2, h3, h4, li, td, th, span, a, label, button';
                return Array.from(document.querySelectorAll(sel))
                    .map(e => e.innerText?.trim())
                    .filter(t => t && t.length > 2)
                    .join('\\n');
            }""")
            title = await page.title()
            summary = text[:4000] if text else "(no text extracted)"
            return {
                "output": f"**Page:** {title}\n**URL:** {page.url}\n\n{summary}",
                "image_path": last_img,
            }
        finally:
            await browser.close()


async def _interact_with_screenshots(page, task: str, send) -> str:
    """Run task interactions and stream a screenshot after each action."""
    task_lower = task.lower()
    last_img = None

    async def snap(action: str) -> str:
        png = await page.screenshot(full_page=False)
        return await _emit_frame(send, page.url, action, png)

    try:
        # ── search / fill input ───────────────────────────────────────────
        if any(kw in task_lower for kw in ("search", "type", "fill", "enter")):
            words = re.findall(r'(?:for|about|search|type|find)\s+"?([^"]+?)"?(?:\s|$)', task_lower)
            query_text = words[0].strip() if words else ""
            inp = page.locator(
                "input[type='search'], input[name='q'], input[type='text'], "
                "input[placeholder*='search' i], textarea"
            ).first
            if await inp.count() > 0 and query_text:
                await inp.click()
                last_img = await snap(f"Clicked input field")
                await page.wait_for_timeout(200)

                # Type character by character for realism
                for char in query_text:
                    await inp.press(char)
                    await asyncio.sleep(0.04)

                last_img = await snap(f"Typed: {query_text}")
                await page.wait_for_timeout(300)
                await inp.press("Enter")
                await page.wait_for_timeout(2000)
                last_img = await snap(f"Submitted search: {query_text}")

        # ── click button / link ───────────────────────────────────────────
        elif "click" in task_lower:
            words = re.findall(r'click\s+"?([^"]+?)"?(?:\s|$)', task_lower)
            if words:
                target = words[0].strip()
                el = page.get_by_text(target, exact=False).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    last_img = await snap(f"Found '{target}' — clicking")
                    await page.wait_for_timeout(300)
                    await el.click()
                    await page.wait_for_timeout(1500)
                    last_img = await snap(f"Clicked '{target}'")

        # ── scroll ────────────────────────────────────────────────────────
        elif "scroll" in task_lower:
            for distance in [400, 800, 1200]:
                await page.evaluate(f"window.scrollTo(0, {distance})")
                await page.wait_for_timeout(300)
                last_img = await snap(f"Scrolled to {distance}px")

        # ── read / look at ────────────────────────────────────────────────
        else:
            await page.wait_for_timeout(500)
            last_img = await snap("Reading page")

    except Exception as exc:
        logger.debug("Task interaction failed (non-fatal): %s", exc)

    return last_img or _save_screenshot(await page.screenshot(full_page=False))


# ── tool: run_code ────────────────────────────────────────────────────────────

async def run_code(language: str, code: str) -> dict:
    lang = language.lower().strip()
    try:
        return await _run_in_docker(lang, code)
    except Exception as docker_exc:
        logger.warning("Docker exec failed (%s), falling back to subprocess", docker_exc)
        return await _run_subprocess(lang, code)


async def _run_in_docker(language: str, code: str) -> dict:
    import docker as docker_sdk

    images = {
        "python": ("python:3.11-slim", ["python", "-c", code]),
        "js": ("node:20-slim", ["node", "-e", code]),
        "javascript": ("node:20-slim", ["node", "-e", code]),
        "bash": ("alpine:latest", ["sh", "-c", code]),
        "sh": ("alpine:latest", ["sh", "-c", code]),
    }
    if language not in images:
        raise ValueError(f"Unsupported language for Docker: {language}")

    image, cmd = images[language]
    client = docker_sdk.from_env(timeout=10)

    loop = asyncio.get_event_loop()

    def _run():
        container = client.containers.run(
            image,
            cmd,
            remove=True,
            stdout=True,
            stderr=True,
            mem_limit="128m",
            network_mode="none",
            timeout=CODE_TIMEOUT,
        )
        return container

    try:
        output = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=CODE_TIMEOUT + 5)
        if isinstance(output, bytes):
            text = output.decode("utf-8", errors="replace")
        else:
            text = str(output)
        return {"output": f"[Docker/{language}]\n{text}", "image_path": None}
    except asyncio.TimeoutError:
        return {"output": f"Code execution timed out after {CODE_TIMEOUT}s", "image_path": None}


async def _run_subprocess(language: str, code: str) -> dict:
    cmds = {
        "python": ["python", "-c", code],
        "js": ["node", "-e", code],
        "javascript": ["node", "-e", code],
        "bash": ["bash", "-c", code],
        "sh": ["bash", "-c", code],
    }
    if language not in cmds:
        return {"output": f"Unsupported language: {language}", "image_path": None}

    cmd = cmds[language]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CODE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return {"output": f"Code execution timed out after {CODE_TIMEOUT}s", "image_path": None}

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        combined = ""
        if out:
            combined += out
        if err:
            combined += f"\n[stderr]\n{err}"
        return {"output": f"[subprocess/{language}]\n{combined.strip()}", "image_path": None}
    except FileNotFoundError:
        return {"output": f"Runtime '{cmd[0]}' not found. Install it to run {language}.", "image_path": None}


# ── tool: image_generate ──────────────────────────────────────────────────────

# Style → Pollinations model mapping
_IMAGE_MODELS = {
    "realistic":  "flux-realism",
    "artistic":   "flux",
    "anime":      "flux",
    "3d":         "flux-3d",
    "pixel":      "flux",
    "sketch":     "flux",
    "cinematic":  "flux-cinematic",
}

async def image_generate(prompt: str, style: str = "realistic") -> dict:
    """Generate image via Pollinations.ai (free, no key needed). Supports multiple styles."""
    model = _IMAGE_MODELS.get(style.lower(), "flux-realism")

    # Enhance prompt based on style
    style_enhancers = {
        "realistic":  "photorealistic, 8k, highly detailed, professional photography",
        "artistic":   "artistic, painterly, detailed brushwork, vibrant colors",
        "anime":      "anime style, manga illustration, cel shading, vibrant",
        "3d":         "3D render, octane render, volumetric lighting, highly detailed",
        "pixel":      "pixel art, 16-bit style, retro game aesthetic",
        "sketch":     "pencil sketch, detailed line art, black and white drawing",
        "cinematic":  "cinematic shot, movie lighting, anamorphic lens, film grain",
    }
    enhancer = style_enhancers.get(style.lower(), "")
    full_prompt = f"{prompt}, {enhancer}" if enhancer else prompt

    encoded = urllib.parse.quote(full_prompt)
    seed = int(time.time()) % 99999
    img_url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?model={model}&width=1280&height=960&seed={seed}&nologo=true&enhance=true"
    )
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.get(img_url, follow_redirects=True)
            resp.raise_for_status()
            img_path = _save_screenshot(resp.content)
        return {
            "output": f"Image generated — style: {style}, model: {model}\nPrompt: \"{prompt}\"\nSaved to: {img_path}",
            "image_path": img_path,
        }
    except Exception as exc:
        return {"output": f"Image generation error: {exc}", "image_path": None}


# ── tool: download_video ─────────────────────────────────────────────────────

async def download_video(url: str, output_dir: str = "~/Downloads") -> dict:
    """Download video from YouTube, Twitter, TikTok, Instagram, etc. using yt-dlp."""
    try:
        out_path = Path(output_dir).expanduser().resolve()
        out_path.mkdir(parents=True, exist_ok=True)

        # Try import first, guide install if missing
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            # Auto-install
            proc = await asyncio.create_subprocess_shell(
                "pip install yt-dlp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(out_path / "%(title)s.%(ext)s"),
            "--no-warnings",
            "--progress",
            url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            return {"output": "Video download timed out after 5 minutes.", "image_path": None}

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        combined = out or err
        if proc.returncode == 0:
            # Find the downloaded file
            lines = combined.splitlines()
            dest_line = next((l for l in lines if "Destination" in l or "Merging" in l or ".mp4" in l), "")
            return {
                "output": f"Video downloaded to {out_path}\n{dest_line or combined[-300:]}",
                "image_path": None,
            }
        else:
            return {"output": f"Download failed:\n{combined[-500:]}", "image_path": None}
    except Exception as exc:
        return {"output": f"Video download error: {exc}", "image_path": None}


# ── tool: serve_html_app ──────────────────────────────────────────────────────

# Registry of running app servers: port -> (server_thread, html_path)
_running_apps: dict[int, object] = {}


async def serve_html_app(html: str, title: str = "App") -> dict:
    """
    Save HTML to a temp file and serve it on a random free port.
    Opens the browser automatically. Returns the app URL.
    """
    import socket
    import threading
    import http.server
    import webbrowser

    # Find a free port
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    # Write HTML to static dir so it's accessible via the main app too
    app_dir = Path("static/apps").resolve()
    app_dir.mkdir(parents=True, exist_ok=True)
    app_name = f"{uuid.uuid4().hex[:8]}.html"
    app_file = app_dir / app_name
    app_file.write_text(html, encoding="utf-8")

    # Serve via simple HTTP server in a thread
    handler = http.server.SimpleHTTPRequestHandler
    httpd = http.server.HTTPServer(("0.0.0.0", port), handler)

    def _run_server():
        os.chdir(str(app_dir.parent))  # serve from static/
        httpd.serve_forever()

    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    _running_apps[port] = httpd

    app_url = f"http://localhost:{port}/apps/{app_name}"

    # Open in browser (non-blocking)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: webbrowser.open(app_url))

    return {
        "output": f"App '{title}' is live at: {app_url}\nOpened in browser automatically.",
        "image_path": None,
        "app_url": app_url,
    }


# ── tool: html_preview ────────────────────────────────────────────────────────

async def html_preview(html: str) -> dict:
    """Render HTML string to PNG via Playwright."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.set_content(html, wait_until="networkidle")
            png = await page.screenshot(full_page=True)
            await browser.close()

        img_path = _save_screenshot(png)
        return {"output": f"HTML rendered. Preview at: {img_path}", "image_path": img_path}
    except Exception as exc:
        return {"output": f"HTML preview error: {exc}", "image_path": None}


# ── tool: remember / recall ───────────────────────────────────────────────────

async def remember(fact: str) -> dict:
    from db import save_fact
    from agent import current_user_id
    await save_fact(fact.strip(), user_id=current_user_id.get())
    return {"output": f"Remembered: {fact}", "image_path": None}


async def recall(query: str) -> dict:
    from db import search_facts, get_all_facts
    from agent import current_user_id
    uid = current_user_id.get()
    facts = await search_facts(query, user_id=uid)
    if not facts:
        facts = await get_all_facts(user_id=uid)
    if not facts:
        return {"output": "No memories found.", "image_path": None}
    lines = [f"Memories matching '{query}':", ""]
    lines += [f"• {f}" for f in facts]
    return {"output": "\n".join(lines), "image_path": None}


# ── tool: read_file / write_file (sandboxed workspace) ───────────────────────

async def read_file(path: str) -> dict:
    try:
        p = _safe_path(path)
        if not p.exists():
            return {"output": f"File not found: {path}", "image_path": None}
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"output": f"**{path}** ({len(content)} chars):\n\n```\n{content[:8000]}\n```", "image_path": None}
    except ValueError as exc:
        return {"output": str(exc), "image_path": None}


async def write_file(path: str, content: str) -> dict:
    try:
        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"output": f"Written {len(content)} chars to workspace/{path}", "image_path": None}
    except ValueError as exc:
        return {"output": str(exc), "image_path": None}


# ── tool: run_terminal ────────────────────────────────────────────────────────

async def run_terminal(command: str, _send=None) -> dict:
    """Run a terminal command and stream output line by line."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines = []
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            lines.append(line)
            if _send:
                await _send({"type": "stream_output", "line": line, "session_id": ""})
        await proc.wait()
        output = "".join(lines).strip() or "(no output)"
        if proc.returncode != 0 and proc.returncode is not None:
            output = f"[exit {proc.returncode}]\n{output}"
        return {"output": output, "image_path": None}
    except Exception as exc:
        return {"output": f"Terminal error: {exc}", "image_path": None}


# ── tool: list_directory ──────────────────────────────────────────────────────

async def list_directory(path: str = "~") -> dict:
    """List files and folders at any path on the system."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"output": f"Path not found: {path}", "image_path": None}
        if not p.is_dir():
            return {"output": f"{path} is a file, not a directory.", "image_path": None}

        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"📁 {p}\n"]
        for item in items[:150]:
            try:
                if item.is_dir():
                    lines.append(f"  📁 {item.name}/")
                else:
                    size = item.stat().st_size
                    if size < 1024:
                        size_str = f"{size} B"
                    elif size < 1024 * 1024:
                        size_str = f"{size // 1024} KB"
                    else:
                        size_str = f"{size // 1024 // 1024} MB"
                    lines.append(f"  📄 {item.name}  ({size_str})")
            except PermissionError:
                lines.append(f"  🔒 {item.name}  (no access)")

        total = sum(1 for _ in p.iterdir())
        if total > 150:
            lines.append(f"\n  … {total - 150} more items not shown")
        return {"output": "\n".join(lines), "image_path": None}
    except PermissionError:
        return {"output": f"Permission denied: {path}", "image_path": None}
    except Exception as exc:
        return {"output": f"Error listing {path}: {exc}", "image_path": None}


# ── tool: read_system_file ────────────────────────────────────────────────────

async def read_system_file(path: str) -> dict:
    """Read any file anywhere on the system (Documents, Desktop, Downloads, etc.)."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"output": f"File not found: {path}", "image_path": None}
        if p.is_dir():
            return {"output": f"{path} is a directory. Use list_directory instead.", "image_path": None}

        size = p.stat().st_size
        if size > 5 * 1024 * 1024:
            return {"output": f"File too large to read ({size // 1024 // 1024} MB). Max 5 MB.", "image_path": None}

        content = p.read_text(encoding="utf-8", errors="replace")
        preview = content[:8000]
        truncated = len(content) > 8000
        return {
            "output": f"**{p.name}** ({size:,} bytes){' — truncated to 8000 chars' if truncated else ''}:\n\n```\n{preview}\n```",
            "image_path": None,
        }
    except PermissionError:
        return {"output": f"Permission denied: {path}", "image_path": None}
    except Exception as exc:
        return {"output": f"Error reading {path}: {exc}", "image_path": None}


# ── tool: write_system_file ───────────────────────────────────────────────────

async def write_system_file(path: str, content: str) -> dict:
    """Write a file to any location on the system."""
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"output": f"Written {len(content):,} chars to {p}", "image_path": None}
    except PermissionError:
        return {"output": f"Permission denied: {path}", "image_path": None}
    except Exception as exc:
        return {"output": f"Error writing {path}: {exc}", "image_path": None}


# ── tool: clipboard ──────────────────────────────────────────────────────────

async def clip_read() -> dict:
    """Read whatever is currently on the system clipboard."""
    try:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            return {"output": "Clipboard is empty.", "image_path": None}
        return {"output": f"Clipboard contents ({len(text)} chars):\n\n{text[:6000]}", "image_path": None}
    except Exception as exc:
        return {"output": f"Clipboard read error: {exc}", "image_path": None}


async def clip_write(text: str) -> dict:
    """Write text to the system clipboard."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return {"output": f"Copied to clipboard ({len(text)} chars).", "image_path": None}
    except Exception as exc:
        return {"output": f"Clipboard write error: {exc}", "image_path": None}


# ── tool: youtube transcript ──────────────────────────────────────────────────

async def youtube_transcript(url: str) -> dict:
    """Get the full transcript of a YouTube video with timestamps."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        import re
        match = re.search(r"(?:v=|youtu\.be/|shorts/)([^&\n?#]+)", url)
        if not match:
            return {"output": "Could not extract video ID from URL.", "image_path": None}
        video_id = match.group(1).strip()

        loop = asyncio.get_event_loop()

        def _fetch():
            # v1.x API uses instance method .fetch()
            try:
                api = YouTubeTranscriptApi()
                fetched = api.fetch(video_id)
                return [{"start": s.start, "text": s.text} for s in fetched]
            except Exception:
                # Fallback: 0.x class method
                return YouTubeTranscriptApi.get_transcript(video_id)

        segments = await loop.run_in_executor(None, _fetch)
        lines = [
            f"[{int(s['start'] // 60):02d}:{int(s['start'] % 60):02d}] {s['text']}"
            for s in segments
        ]
        return {
            "output": f"Transcript ({len(lines)} segments):\n\n" + "\n".join(lines[:500]),
            "image_path": None,
        }
    except Exception as exc:
        return {"output": f"Transcript error: {exc}\nThe video may have transcripts disabled or be region-locked.", "image_path": None}


# ── tool: desktop notifications ───────────────────────────────────────────────

async def notify_desktop(title: str, message: str) -> dict:
    """Send a native desktop notification (Windows/Mac/Linux)."""
    try:
        from plyer import notification
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: notification.notify(
            title=title[:64],
            message=message[:256],
            app_name="EmulAItor",
            timeout=8,
        ))
        return {"output": f"Notification sent: {title}", "image_path": None}
    except Exception as exc:
        return {"output": f"Notification error: {exc}", "image_path": None}


# ── tool: desktop control (pyautogui) ─────────────────────────────────────────

def _desktop_snap() -> str:
    """Take a full-desktop screenshot and return web path."""
    import pyautogui
    from io import BytesIO
    img = pyautogui.screenshot()
    buf = BytesIO()
    img.save(buf, format="PNG")
    name = f"{uuid.uuid4().hex}.png"
    dest = SCREENSHOTS_DIR / name
    dest.write_bytes(buf.getvalue())
    return f"/static/screenshots/{name}"


async def desktop_screenshot(_send=None) -> dict:
    """Take a full-desktop screenshot and return visible window titles so the AI knows what's on screen."""
    try:
        loop = asyncio.get_event_loop()

        def _capture():
            import pyautogui
            import pygetwindow as gw
            from io import BytesIO

            img = pyautogui.screenshot()
            w, h = img.size
            buf = BytesIO()
            img.save(buf, format="PNG")

            # Get all visible, titled windows
            try:
                wins = [w.title for w in gw.getAllWindows() if w.title.strip()]
            except Exception:
                wins = []

            return buf.getvalue(), w, h, wins

        png, width, height, windows = await loop.run_in_executor(None, _capture)

        name = f"{uuid.uuid4().hex}.png"
        (SCREENSHOTS_DIR / name).write_bytes(png)
        img_path = f"/static/screenshots/{name}"

        if _send:
            await _send({"type": "browser_frame", "url": "desktop://",
                         "action": "Desktop screenshot", "image_path": img_path})

        wins_text = "\n".join(f"  • {w}" for w in windows) if windows else "  (none detected)"
        output = (
            f"Desktop screenshot taken ({width}×{height}px).\n\n"
            f"Open windows:\n{wins_text}\n\n"
            f"To open an app: use run_terminal('start notepad') or run_terminal('start brave').\n"
            f"To click something: call desktop_click(x, y) using coordinates from the screenshot."
        )
        return {"output": output, "image_path": img_path}
    except Exception as exc:
        return {"output": f"Desktop screenshot error: {exc}", "image_path": None}


async def desktop_click(x: int, y: int, _send=None) -> dict:
    """Click at desktop coordinates (x, y)."""
    try:
        import pyautogui
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: pyautogui.click(x, y))
        await asyncio.sleep(0.5)
        img_path = await loop.run_in_executor(None, _desktop_snap)
        if _send:
            await _send({"type": "browser_frame", "url": "desktop://",
                         "action": f"Clicked ({x},{y})", "image_path": img_path})
        return {"output": f"Clicked at ({x}, {y})", "image_path": img_path}
    except Exception as exc:
        return {"output": f"Desktop click error: {exc}", "image_path": None}


async def desktop_type(text: str, _send=None) -> dict:
    """Type text at the current cursor position (wherever focus is)."""
    try:
        import pyautogui
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: pyautogui.typewrite(text, interval=0.03))
        await asyncio.sleep(0.3)
        img_path = await loop.run_in_executor(None, _desktop_snap)
        if _send:
            await _send({"type": "browser_frame", "url": "desktop://",
                         "action": f"Typed: {text[:30]}", "image_path": img_path})
        return {"output": f"Typed: {text[:80]}", "image_path": img_path}
    except Exception as exc:
        return {"output": f"Desktop type error: {exc}", "image_path": None}


async def desktop_hotkey(keys: str, _send=None) -> dict:
    """
    Press a keyboard shortcut. Examples:
    'ctrl+c', 'ctrl+v', 'alt+tab', 'win+d', 'ctrl+shift+esc'
    """
    try:
        import pyautogui
        key_list = [k.strip() for k in keys.split("+")]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: pyautogui.hotkey(*key_list))
        await asyncio.sleep(0.5)
        img_path = await loop.run_in_executor(None, _desktop_snap)
        if _send:
            await _send({"type": "browser_frame", "url": "desktop://",
                         "action": f"Hotkey: {keys}", "image_path": img_path})
        return {"output": f"Pressed hotkey: {keys}", "image_path": img_path}
    except Exception as exc:
        return {"output": f"Desktop hotkey error: {exc}", "image_path": None}


async def desktop_scroll_screen(direction: str = "down", clicks: int = 3, _send=None) -> dict:
    """Scroll the mouse wheel on the desktop."""
    try:
        import pyautogui
        amount = -clicks if direction == "down" else clicks
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: pyautogui.scroll(amount))
        await asyncio.sleep(0.3)
        img_path = await loop.run_in_executor(None, _desktop_snap)
        if _send:
            await _send({"type": "browser_frame", "url": "desktop://",
                         "action": f"Scrolled {direction}", "image_path": img_path})
        return {"output": f"Scrolled {direction} {clicks} clicks", "image_path": img_path}
    except Exception as exc:
        return {"output": f"Desktop scroll error: {exc}", "image_path": None}


# ── tool: email ───────────────────────────────────────────────────────────────

def _get_email_config() -> tuple[str, str, str, str, int, int]:
    addr   = os.getenv("EMAIL_ADDRESS", "")
    pwd    = os.getenv("EMAIL_PASSWORD", "")
    imap   = os.getenv("IMAP_SERVER",   "imap.gmail.com")
    smtp   = os.getenv("SMTP_SERVER",   "smtp.gmail.com")
    iport  = int(os.getenv("IMAP_PORT", "993"))
    sport  = int(os.getenv("SMTP_PORT", "587"))
    return addr, pwd, imap, smtp, iport, sport


async def email_read(count: int = 5, folder: str = "INBOX") -> dict:
    """Read the most recent emails from your inbox."""
    addr, pwd, imap_server, _, imap_port, _ = _get_email_config()
    if not addr or not pwd:
        return {"output": "Email not configured. Add EMAIL_ADDRESS and EMAIL_PASSWORD to .env", "image_path": None}
    try:
        import imaplib, email as emaillib
        from email.header import decode_header

        loop = asyncio.get_event_loop()

        def _fetch():
            mail = imaplib.IMAP4_SSL(imap_server, imap_port)
            mail.login(addr, pwd)
            mail.select(folder)
            _, data = mail.search(None, "ALL")
            ids = data[0].split()
            recent = ids[-(min(count, len(ids))):][::-1]
            results = []
            for mid in recent:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                msg = emaillib.message_from_bytes(msg_data[0][1])
                subject = decode_header(msg["Subject"])[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode(errors="replace")
                sender = msg.get("From", "")
                date   = msg.get("Date", "")
                body   = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")
                results.append(f"**From:** {sender}\n**Date:** {date}\n**Subject:** {subject}\n\n{body[:800]}\n\n---")
            mail.logout()
            return results

        messages = await loop.run_in_executor(None, _fetch)
        return {"output": f"Last {len(messages)} emails:\n\n" + "\n".join(messages), "image_path": None}
    except Exception as exc:
        return {"output": f"Email read error: {exc}", "image_path": None}


async def email_send(to: str, subject: str, body: str) -> dict:
    """Send an email."""
    addr, pwd, _, smtp_server, _, smtp_port = _get_email_config()
    if not addr or not pwd:
        return {"output": "Email not configured. Add EMAIL_ADDRESS and EMAIL_PASSWORD to .env", "image_path": None}
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart()
        msg["From"]    = addr
        msg["To"]      = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        loop = asyncio.get_event_loop()

        def _send():
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(addr, pwd)
                server.sendmail(addr, to, msg.as_string())

        await loop.run_in_executor(None, _send)
        return {"output": f"Email sent to {to} — Subject: {subject}", "image_path": None}
    except Exception as exc:
        return {"output": f"Email send error: {exc}", "image_path": None}


async def email_search(query: str, count: int = 5) -> dict:
    """Search emails by keyword (searches subject and body)."""
    addr, pwd, imap_server, _, imap_port, _ = _get_email_config()
    if not addr or not pwd:
        return {"output": "Email not configured. Add EMAIL_ADDRESS and EMAIL_PASSWORD to .env", "image_path": None}
    try:
        import imaplib, email as emaillib
        from email.header import decode_header

        loop = asyncio.get_event_loop()

        def _search():
            mail = imaplib.IMAP4_SSL(imap_server, imap_port)
            mail.login(addr, pwd)
            mail.select("INBOX")
            # Search in subject
            _, data = mail.search(None, f'SUBJECT "{query}"')
            ids = data[0].split()
            if not ids:
                # Fallback: search body
                _, data = mail.search(None, f'BODY "{query}"')
                ids = data[0].split()
            recent = ids[-(min(count, len(ids))):][::-1]
            results = []
            for mid in recent:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                msg = emaillib.message_from_bytes(msg_data[0][1])
                subject = decode_header(msg["Subject"])[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode(errors="replace")
                results.append(f"• From: {msg.get('From','')} | {subject} | {msg.get('Date','')}")
            mail.logout()
            return results

        results = await loop.run_in_executor(None, _search)
        if not results:
            return {"output": f"No emails found matching '{query}'", "image_path": None}
        return {"output": f"Found {len(results)} emails matching '{query}':\n\n" + "\n".join(results), "image_path": None}
    except Exception as exc:
        return {"output": f"Email search error: {exc}", "image_path": None}


# ── tool: scheduled tasks ─────────────────────────────────────────────────────

async def schedule_task(task: str, cron: str) -> dict:
    """
    Schedule a recurring task using a cron expression.
    cron format: 'minute hour day month day_of_week'
    Examples:
      '0 9 * * 1'    = every Monday at 9am UTC
      '0 8 * * *'    = every day at 8am UTC
      '*/30 * * * *' = every 30 minutes
    """
    try:
        import scheduler as sched
        job_id = sched.add_job(task, cron)
        return {"output": f"Task scheduled! ID: {job_id}\nTask: {task}\nSchedule: {cron}", "image_path": None}
    except Exception as exc:
        return {"output": f"Schedule error: {exc}", "image_path": None}


async def list_schedules() -> dict:
    """List all scheduled tasks."""
    try:
        import scheduler as sched
        jobs = sched.list_jobs()
        if not jobs:
            return {"output": "No scheduled tasks.", "image_path": None}
        lines = ["Scheduled tasks:\n"]
        for j in jobs:
            lines.append(f"• **{j['id']}** — {j['task']}\n  Next run: {j['next_run']}")
        return {"output": "\n".join(lines), "image_path": None}
    except Exception as exc:
        return {"output": f"List schedules error: {exc}", "image_path": None}


async def cancel_schedule(job_id: str) -> dict:
    """Cancel a scheduled task by its ID."""
    try:
        import scheduler as sched
        ok = sched.remove_job(job_id)
        return {"output": f"Task {job_id} {'cancelled' if ok else 'not found'}.", "image_path": None}
    except Exception as exc:
        return {"output": f"Cancel error: {exc}", "image_path": None}


# ── tool: pdf reader ──────────────────────────────────────────────────────────

async def read_pdf(path: str) -> dict:
    """Extract text from a PDF file."""
    try:
        import pdfplumber
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"output": f"File not found: {path}", "image_path": None}

        loop = asyncio.get_event_loop()

        def _extract():
            with pdfplumber.open(str(p)) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages[:30], 1):  # max 30 pages
                    text = page.extract_text() or ""
                    pages_text.append(f"--- Page {i} ---\n{text}")
                return "\n\n".join(pages_text)

        text = await loop.run_in_executor(None, _extract)
        return {"output": f"**{p.name}** ({len(text)} chars):\n\n{text[:10000]}", "image_path": None}
    except ImportError:
        return {"output": "pdfplumber not installed. Run: pip install pdfplumber", "image_path": None}
    except Exception as exc:
        return {"output": f"PDF read error: {exc}", "image_path": None}


# ── tool: RAG vault ──────────────────────────────────────────────────────────

async def get_secret(name: str) -> dict:
    """Retrieve a secret the user stored in their personal secrets vault."""
    import db as _db
    from agent import current_user_id
    uid = current_user_id.get()
    if not uid:
        return {"output": "No user session — cannot access secrets.", "image_path": None}
    value = await _db.get_secret(uid, name)
    if value is None:
        return {"output": f"Secret '{name}' not found. Ask the user to store it first with set_secret().", "image_path": None}
    return {"output": value, "image_path": None}


async def set_secret(name: str, value: str) -> dict:
    """Store a secret in the user's encrypted personal vault."""
    import db as _db
    from agent import current_user_id
    uid = current_user_id.get()
    if not uid:
        return {"output": "No user session — cannot store secrets.", "image_path": None}
    await _db.set_secret(uid, name, value)
    return {"output": f"Secret '{name}' stored securely.", "image_path": None}


async def list_secrets() -> dict:
    """List the names of all secrets stored in the user's vault (values are never shown)."""
    import db as _db
    from agent import current_user_id
    uid = current_user_id.get()
    if not uid:
        return {"output": "No user session.", "image_path": None}
    secrets = await _db.list_secrets(uid)
    if not secrets:
        return {"output": "No secrets stored yet.", "image_path": None}
    lines = [f"• {s['name']} (saved {s['created_at'][:10]})" for s in secrets]
    return {"output": "Stored secrets:\n" + "\n".join(lines), "image_path": None}


async def vault_add(content: str, title: str) -> dict:
    """Save a document to the personal knowledge vault."""
    import db
    doc_id = await db.vault_add(title=title, content=content)
    return {"output": f"✓ Saved to vault (ID: {doc_id}) — '{title}' ({len(content):,} chars)", "image_path": None}


async def vault_search(query: str) -> dict:
    """Search the vault for relevant documents."""
    import db
    results = await db.vault_search(query)
    if not results:
        return {"output": "No documents found in vault matching that query.", "image_path": None}
    lines = [f"Found {len(results)} document(s) matching '{query}':\n"]
    for r in results:
        snippet = r["content"][:300].replace("\n", " ")
        lines.append(f"**[{r['id']}] {r['title']}** ({r['created_at'][:10]})\n{snippet}…\n")
    return {"output": "\n".join(lines), "image_path": None}


async def vault_list() -> dict:
    """List all documents in the vault."""
    import db
    docs = await db.vault_list()
    if not docs:
        return {"output": "The vault is empty. Use vault_add() to save documents.", "image_path": None}
    lines = [f"**Vault** — {len(docs)} document(s):\n"]
    for d in docs:
        lines.append(f"• **[{d['id']}]** {d['title']} — {d['chars']:,} chars — {d['created_at'][:10]}")
    return {"output": "\n".join(lines), "image_path": None}


async def vault_delete(doc_id: str) -> dict:
    """Delete a document from the vault by ID."""
    import db
    deleted = await db.vault_delete(doc_id)
    if deleted:
        return {"output": f"✓ Deleted vault document {doc_id}", "image_path": None}
    return {"output": f"Document {doc_id} not found in vault.", "image_path": None}


# ── tool: git integration ─────────────────────────────────────────────────────

async def git_status(repo_path: str = ".") -> dict:
    """Show git status of a repository."""
    try:
        path = Path(repo_path).expanduser().resolve()
        loop = asyncio.get_event_loop()
        def _run():
            result = subprocess.run(["git", "status", "--short", "--branch"], cwd=str(path), capture_output=True, text=True, timeout=15)
            return result.stdout + result.stderr
        out = await loop.run_in_executor(None, _run)
        return {"output": f"**git status** ({path.name}):\n```\n{out.strip()}\n```", "image_path": None}
    except Exception as exc:
        return {"output": f"git status error: {exc}", "image_path": None}

async def git_diff(repo_path: str = ".", file_path: str = "") -> dict:
    """Show git diff for a repository or specific file."""
    try:
        path = Path(repo_path).expanduser().resolve()
        cmd = ["git", "diff"] + ([file_path] if file_path else [])
        loop = asyncio.get_event_loop()
        def _run():
            result = subprocess.run(cmd, cwd=str(path), capture_output=True, text=True, timeout=15)
            return result.stdout[:8000] or "(no changes)"
        out = await loop.run_in_executor(None, _run)
        return {"output": f"**git diff**:\n```diff\n{out}\n```", "image_path": None}
    except Exception as exc:
        return {"output": f"git diff error: {exc}", "image_path": None}

async def git_log(repo_path: str = ".", count: int = 10) -> dict:
    """Show recent git commits."""
    try:
        path = Path(repo_path).expanduser().resolve()
        loop = asyncio.get_event_loop()
        def _run():
            result = subprocess.run(["git", "log", f"--max-count={count}", "--oneline", "--decorate"], cwd=str(path), capture_output=True, text=True, timeout=15)
            return result.stdout + result.stderr
        out = await loop.run_in_executor(None, _run)
        return {"output": f"**git log** (last {count}):\n```\n{out.strip()}\n```", "image_path": None}
    except Exception as exc:
        return {"output": f"git log error: {exc}", "image_path": None}

async def git_commit(message: str, repo_path: str = ".", add_all: bool = True) -> dict:
    """Stage all changes and create a git commit."""
    try:
        path = Path(repo_path).expanduser().resolve()
        loop = asyncio.get_event_loop()
        def _run():
            out = ""
            if add_all:
                r = subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, text=True)
                out += r.stdout + r.stderr
            r = subprocess.run(["git", "commit", "-m", message], cwd=str(path), capture_output=True, text=True)
            out += r.stdout + r.stderr
            return out
        out = await loop.run_in_executor(None, _run)
        return {"output": f"**git commit**:\n```\n{out.strip()}\n```", "image_path": None}
    except Exception as exc:
        return {"output": f"git commit error: {exc}", "image_path": None}

async def git_push(repo_path: str = ".", remote: str = "origin", branch: str = "") -> dict:
    """Push commits to remote."""
    try:
        path = Path(repo_path).expanduser().resolve()
        cmd = ["git", "push", remote] + ([branch] if branch else [])
        loop = asyncio.get_event_loop()
        def _run():
            result = subprocess.run(cmd, cwd=str(path), capture_output=True, text=True, timeout=60)
            return result.stdout + result.stderr
        out = await loop.run_in_executor(None, _run)
        return {"output": f"**git push**:\n```\n{out.strip()}\n```", "image_path": None}
    except Exception as exc:
        return {"output": f"git push error: {exc}", "image_path": None}


# ── tool: workflow recorder ───────────────────────────────────────────────────

_active_recordings: dict[str, list] = {}  # session_id -> list of recorded steps

async def workflow_start(name: str, session_id: str = "default") -> dict:
    """Start recording a workflow. All subsequent tool calls will be recorded."""
    _active_recordings[session_id] = {"name": name, "steps": [], "started": True}
    return {"output": f"✓ Recording started: '{name}'. Use tools normally — every action will be recorded. Call workflow_save() when done.", "image_path": None}

async def workflow_save(session_id: str = "default") -> dict:
    """Stop recording and save the workflow to a JSON file."""
    rec = _active_recordings.pop(session_id, None)
    if not rec:
        return {"output": "No active recording. Start one with workflow_start().", "image_path": None}
    name = rec["name"]
    safe_name = re.sub(r"[^\w\-]", "_", name).lower()
    wf_dir = Path("workflows")
    wf_dir.mkdir(exist_ok=True)
    wf_path = wf_dir / f"{safe_name}.json"
    import json as json_lib
    wf_data = {"name": name, "steps": rec.get("steps", []), "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    wf_path.write_text(json_lib.dumps(wf_data, indent=2))
    steps = len(rec.get("steps", []))
    return {"output": f"✓ Workflow '{name}' saved ({steps} steps) → {wf_path}", "image_path": None}

async def workflow_list() -> dict:
    """List all saved workflows."""
    wf_dir = Path("workflows")
    if not wf_dir.exists():
        return {"output": "No workflows saved yet. Use workflow_start() to record one.", "image_path": None}
    import json as json_lib
    lines = ["**Saved Workflows:**\n"]
    for f in sorted(wf_dir.glob("*.json")):
        try:
            data = json_lib.loads(f.read_text())
            steps = len(data.get("steps", []))
            lines.append(f"• **{data['name']}** ({steps} steps) — {f.stem}")
        except Exception:
            lines.append(f"• {f.stem}")
    if len(lines) == 1:
        return {"output": "No workflows saved yet.", "image_path": None}
    return {"output": "\n".join(lines), "image_path": None}

async def workflow_run(workflow_name: str, _send=None) -> dict:
    """Run a saved workflow by name."""
    import json as json_lib
    wf_dir = Path("workflows")
    safe = re.sub(r"[^\w\-]", "_", workflow_name).lower()
    wf_path = wf_dir / f"{safe}.json"
    if not wf_path.exists():
        # Try fuzzy match
        matches = list(wf_dir.glob("*.json"))
        matches = [f for f in matches if workflow_name.lower() in f.stem]
        if matches:
            wf_path = matches[0]
        else:
            return {"output": f"Workflow '{workflow_name}' not found. Use workflow_list() to see available workflows.", "image_path": None}
    data = json_lib.loads(wf_path.read_text())
    steps = data.get("steps", [])
    if not steps:
        return {"output": f"Workflow '{data['name']}' has no steps.", "image_path": None}
    results = [f"**Running workflow: {data['name']}** ({len(steps)} steps)\n"]
    for i, step in enumerate(steps, 1):
        tool_name = step.get("tool")
        tool_args = step.get("args", {})
        results.append(f"Step {i}: {tool_name}({', '.join(f'{k}={repr(v)}' for k,v in tool_args.items())})")
        result = await execute_tool(tool_name, tool_args, send=_send)
        results.append(f"→ {result['output'][:200]}\n")
    return {"output": "\n".join(results), "image_path": None}


# ── tool: browser control (persistent visible session) ────────────────────────

async def browser_navigate(url: str, _send=None) -> dict:
    from browser_session import browser
    return await browser.navigate(url, send=_send)

async def browser_screenshot(_send=None) -> dict:
    from browser_session import browser
    return await browser.screenshot(send=_send)

async def browser_click(selector: str, _send=None) -> dict:
    from browser_session import browser
    return await browser.click(selector, send=_send)

async def browser_type(selector: str, text: str, _send=None) -> dict:
    from browser_session import browser
    return await browser.type_text(selector, text, send=_send)

async def browser_press(key: str, _send=None) -> dict:
    from browser_session import browser
    return await browser.press_key(key, send=_send)

async def browser_scroll(direction: str = "down", amount: int = 400, _send=None) -> dict:
    from browser_session import browser
    return await browser.scroll(direction, amount, send=_send)

async def browser_read_page(_send=None) -> dict:
    from browser_session import browser
    return await browser.read_page(send=_send)

async def browser_wait(milliseconds: int = 1500, _send=None) -> dict:
    from browser_session import browser
    return await browser.wait(milliseconds, send=_send)

async def browser_close(_send=None) -> dict:
    from browser_session import browser
    return await browser.close(send=_send)


# ── tool registry ─────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using Brave Search. Use for current events, facts, research.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": "Navigate to a URL, take a screenshot, and return the page content. Can perform simple interactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to navigate to"},
                    "task": {"type": "string", "description": "Optional: what to do on the page (e.g. 'search for python tutorials')"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Execute code in a sandbox. Supports python, javascript (js), bash.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["python", "js", "javascript", "bash"], "description": "Language"},
                    "code": {"type": "string", "description": "Code to execute"},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": "Generate an image from a text prompt using AI. Supports multiple styles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description"},
                    "style": {
                        "type": "string",
                        "enum": ["realistic", "artistic", "anime", "3d", "pixel", "sketch", "cinematic"],
                        "description": "Visual style. Default: realistic",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_video",
            "description": "Download videos from YouTube, Twitter/X, TikTok, Instagram, Reddit, and 1000+ other sites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Video URL to download"},
                    "output_dir": {"type": "string", "description": "Where to save the video. Default: ~/Downloads"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serve_html_app",
            "description": (
                "Save an HTML app/game to a file and serve it on a local port. "
                "Opens in the user's browser automatically. Use this for games, dashboards, interactive apps, "
                "visualizations — anything that needs a browser UI. Do NOT just save HTML files and tell the user to open them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "Complete self-contained HTML (with embedded CSS and JS)"},
                    "title": {"type": "string", "description": "App name shown to the user"},
                },
                "required": ["html", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "html_preview",
            "description": "Render an HTML string to a PNG screenshot. Use to preview web designs or UI mockups.",
            "parameters": {
                "type": "object",
                "properties": {"html": {"type": "string", "description": "Full HTML string to render"}},
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a fact or piece of information in long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "The fact or information to remember"}},
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search long-term memory for stored facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look for in memory"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path relative to workspace/"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace/"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    # ── Clipboard ─────────────────────────────────────────────────────────────
    {"type":"function","function":{"name":"clip_read","description":"Read the current system clipboard contents.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"clip_write","description":"Write text to the system clipboard.","parameters":{"type":"object","properties":{"text":{"type":"string","description":"Text to copy"}},"required":["text"]}}},

    # ── YouTube transcript ────────────────────────────────────────────────────
    {"type":"function","function":{"name":"youtube_transcript","description":"Get the full transcript (with timestamps) of any YouTube video. Much faster than downloading the video.","parameters":{"type":"object","properties":{"url":{"type":"string","description":"YouTube video URL"}},"required":["url"]}}},

    # ── Desktop notifications ─────────────────────────────────────────────────
    {"type":"function","function":{"name":"notify_desktop","description":"Send a native desktop notification to the user (Windows/Mac/Linux).","parameters":{"type":"object","properties":{"title":{"type":"string"},"message":{"type":"string"}},"required":["title","message"]}}},

    # ── Desktop control ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "Take a screenshot of the entire desktop. Use to see what's on screen before clicking.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "Click at desktop pixel coordinates. Always take a desktop_screenshot first to find the right x,y.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "Type text at the current cursor position anywhere on the desktop.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to type"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_hotkey",
            "description": "Press a keyboard shortcut on the desktop. Examples: 'ctrl+c', 'ctrl+v', 'alt+tab', 'win+d', 'ctrl+shift+esc'.",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string", "description": "Keys joined with +, e.g. 'ctrl+c'"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll_screen",
            "description": "Scroll the mouse wheel on the desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "clicks": {"type": "integer", "description": "Number of scroll clicks (default 3)"},
                },
                "required": ["direction"],
            },
        },
    },

    # ── Email ─────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "email_read",
            "description": "Read recent emails from inbox. Requires EMAIL_ADDRESS and EMAIL_PASSWORD in .env.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of emails to fetch (default 5)"},
                    "folder": {"type": "string", "description": "Folder name (default INBOX)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_send",
            "description": "Send an email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Plain text email body"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_search",
            "description": "Search emails by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "count": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },

    # ── Scheduled tasks ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": (
                "Schedule a recurring task using a cron expression (UTC time).\n"
                "Examples: '0 9 * * 1' = every Monday 9am, '0 8 * * *' = daily 8am, '*/30 * * * *' = every 30min."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What to do (e.g. 'Search for Bitcoin price and notify me')"},
                    "cron": {"type": "string", "description": "5-part cron expression"},
                },
                "required": ["task", "cron"],
            },
        },
    },
    {"type":"function","function":{"name":"list_schedules","description":"List all scheduled tasks.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"cancel_schedule","description":"Cancel a scheduled task by its ID.","parameters":{"type":"object","properties":{"job_id":{"type":"string"}},"required":["job_id"]}}},

    # ── PDF reader ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": "Extract text from a PDF file anywhere on the system.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Full path to the PDF file"}},
                "required": ["path"],
            },
        },
    },

    # ── Browser control (persistent visible session) ──────────────────────────
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open the visible browser and navigate to a URL. Use this to start browser control sessions.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full URL including https://"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser state. Use after every action to see the result before deciding what to do next.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element in the browser. Selector formats:\n"
                "  text=Submit          — click by visible text\n"
                "  #my-button           — CSS id selector\n"
                "  .btn-primary         — CSS class\n"
                "  [name='q']           — attribute selector\n"
                "  x=320,y=450          — click by pixel coordinates (use after screenshot)"
            ),
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "Element selector or coordinates"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": (
                "Type text into an input field. Selector formats same as browser_click.\n"
                "Use 'focused' as selector to type into the currently focused element."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Element selector (e.g. #search-input, text=Email, focused)"},
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press",
            "description": "Press a keyboard key. Examples: Enter, Tab, Escape, ArrowDown, Control+a, Backspace, F5.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name (Playwright key format)"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the page up or down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                    "amount": {"type": "integer", "description": "Pixels to scroll (default 400)"},
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read_page",
            "description": "Extract all visible text from the current page. Use to read content after navigating.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for a page to load or an animation to finish before taking the next action.",
            "parameters": {
                "type": "object",
                "properties": {"milliseconds": {"type": "integer", "description": "How long to wait (200–10000ms, default 1500)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "Close the browser window when done.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": (
                "Run any command directly in the system terminal (CMD on Windows). "
                "Use this to download files (curl, wget, winget), install packages (pip, npm, winget), "
                "move/copy/delete files, run scripts, check system info, or do anything you'd do in a command prompt. "
                "Prefer this over run_code for system-level tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run"},
                    "working_dir": {"type": "string", "description": "Optional working directory (e.g. C:/Users/User/Documents)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders at any path on the system. Use ~ for home, or full paths like C:/Users/User/Documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list. Supports ~ for home directory."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_system_file",
            "description": "Read any file from anywhere on the system — Documents, Desktop, Downloads, or any absolute path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path, e.g. C:/Users/User/Documents/report.txt or ~/Desktop/notes.txt"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_system_file",
            "description": "Write or create a file at any location on the system — Desktop, Documents, Downloads, or any path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path to write to"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    # ── RAG Vault ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "vault_add",
            "description": "Save a document or text to the personal knowledge vault for future retrieval. Use this to save important content like articles, notes, research, or anything the user wants to remember.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The full text content to save"},
                    "title":   {"type": "string", "description": "A short descriptive title for this document"},
                },
                "required": ["content", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vault_search",
            "description": "Search the personal knowledge vault for documents matching a query. Use this to find previously saved articles, notes, or research.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms to find relevant documents"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vault_list",
            "description": "List all documents currently saved in the personal knowledge vault.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vault_delete",
            "description": "Delete a document from the personal knowledge vault by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "The document ID to delete"},
                },
                "required": ["doc_id"],
            },
        },
    },

    # ── Secrets vault ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_secret",
            "description": "Retrieve a secret (API key, password, token) from the user's encrypted personal vault by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The name/key of the secret (e.g. 'twitter_password', 'openai_key')"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_secret",
            "description": "Store a secret (API key, password, token) in the user's encrypted personal vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "A short name for this secret (e.g. 'twitter_password')"},
                    "value": {"type": "string", "description": "The secret value to store"},
                },
                "required": ["name", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_secrets",
            "description": "List the names of all secrets stored in the user's vault. Does not reveal values.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },

    # ── Git tools ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show the git status of a repository (staged, unstaged, and untracked files).",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current directory)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff for a repository or a specific file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current directory)"},
                    "file_path": {"type": "string", "description": "Optional specific file path to diff"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent git commit history for a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current directory)"},
                    "count": {"type": "integer", "description": "Number of commits to show (default: 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and create a git commit with a message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current directory)"},
                    "add_all": {"type": "boolean", "description": "Whether to stage all changes before committing (default: true)"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Push commits to a remote git repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current directory)"},
                    "remote": {"type": "string", "description": "Remote name (default: origin)"},
                    "branch": {"type": "string", "description": "Branch name to push (default: current branch)"},
                },
                "required": [],
            },
        },
    },

    # ── Workflow recorder ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "workflow_start",
            "description": "Start recording a workflow. All subsequent tool calls will be captured as workflow steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for this workflow"},
                    "session_id": {"type": "string", "description": "Session identifier (default: 'default')"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_save",
            "description": "Stop recording and save the current workflow to a JSON file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session identifier (default: 'default')"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_list",
            "description": "List all saved workflows.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_run",
            "description": "Run a previously saved workflow by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_name": {"type": "string", "description": "Name of the workflow to run"},
                },
                "required": ["workflow_name"],
            },
        },
    },
]

TOOL_MAP = {
    "web_search": web_search,
    "browse": browse,
    "run_code": run_code,
    "image_generate": image_generate,
    "download_video": download_video,
    "serve_html_app": serve_html_app,
    "html_preview": html_preview,
    "remember": remember,
    "recall": recall,
    "read_file": read_file,
    "write_file": write_file,
    "run_terminal": run_terminal,
    "list_directory": list_directory,
    "read_system_file": read_system_file,
    "write_system_file": write_system_file,
    # Clipboard
    "clip_read":   clip_read,
    "clip_write":  clip_write,
    # YouTube
    "youtube_transcript": youtube_transcript,
    # Notifications
    "notify_desktop": notify_desktop,
    # Desktop control
    "desktop_screenshot":  desktop_screenshot,
    "desktop_click":       desktop_click,
    "desktop_type":        desktop_type,
    "desktop_hotkey":      desktop_hotkey,
    "desktop_scroll_screen": desktop_scroll_screen,
    # Email
    "email_read":   email_read,
    "email_send":   email_send,
    "email_search": email_search,
    # Scheduled tasks
    "schedule_task":   schedule_task,
    "list_schedules":  list_schedules,
    "cancel_schedule": cancel_schedule,
    # PDF
    "read_pdf": read_pdf,
    # Git tools
    "git_status": git_status,
    "git_diff":   git_diff,
    "git_log":    git_log,
    "git_commit": git_commit,
    "git_push":   git_push,
    # Workflow recorder
    "workflow_start": workflow_start,
    "workflow_save":  workflow_save,
    "workflow_list":  workflow_list,
    "workflow_run":   workflow_run,
    # RAG Vault
    "vault_add":    vault_add,
    "vault_search": vault_search,
    "vault_list":   vault_list,
    "vault_delete": vault_delete,
    # Secrets vault
    "get_secret":   get_secret,
    "set_secret":   set_secret,
    "list_secrets": list_secrets,
    # Browser control
    "browser_navigate":   browser_navigate,
    "browser_screenshot": browser_screenshot,
    "browser_click":      browser_click,
    "browser_type":       browser_type,
    "browser_press":      browser_press,
    "browser_scroll":     browser_scroll,
    "browser_read_page":  browser_read_page,
    "browser_wait":       browser_wait,
    "browser_close":      browser_close,
}

# Tools that receive the send callback for live streaming
_STREAMING_TOOLS = {
    "browse", "web_search",
    "browser_navigate", "browser_screenshot", "browser_click",
    "browser_type", "browser_press", "browser_scroll",
    "browser_read_page", "browser_wait", "browser_close",
    "desktop_screenshot", "desktop_click", "desktop_type",
    "desktop_hotkey", "desktop_scroll_screen",
    "run_terminal", "workflow_run",
}


async def execute_tool(name: str, arguments: dict, send=None) -> dict:
    fn = TOOL_MAP.get(name)
    if fn is None:
        # Check custom tools
        import db as _db
        custom = await _db.get_custom_tools()
        ct = next((t for t in custom if t["name"] == name), None)
        if ct:
            try:
                cmd = ct["command"].replace("{input}", json.dumps(arguments))
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                return {"output": result.stdout or result.stderr, "image_path": None}
            except Exception as exc:
                return {"output": f"Custom tool '{name}' error: {exc}", "image_path": None}
        return {"output": f"Unknown tool: {name}", "image_path": None}
    try:
        if name in _STREAMING_TOOLS:
            return await fn(**arguments, _send=send)
        return await fn(**arguments)
    except TypeError as exc:
        return {"output": f"Tool argument error for '{name}': {exc}", "image_path": None}
    except Exception as exc:
        logger.exception("Tool '%s' raised: %s", name, exc)
        return {"output": f"Tool '{name}' error: {exc}", "image_path": None}
