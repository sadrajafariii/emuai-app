"""
EmulAItor — FastAPI application entry point.
"""

import asyncio
import json
import logging
import os
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import db
import settings_store
from agent import run_agent, resolve_permission, reset_session_permissions
from auth import (
    hash_password, verify_password, create_token,
    get_current_user, get_user_from_ws,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

WORKSPACE = Path("workspace").resolve()

# ── active WebSocket senders (for broadcast) ──────────────────────────────────
_ws_senders: list = []


async def _broadcast(payload: dict):
    """Push an event to all connected WebSocket clients."""
    dead = []
    for send in _ws_senders:
        try:
            await send(payload)
        except Exception:
            dead.append(send)
    for s in dead:
        _ws_senders.remove(s)


# ── lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    Path("workspace").mkdir(exist_ok=True)
    Path("static/screenshots").mkdir(parents=True, exist_ok=True)
    Path("static/apps").mkdir(parents=True, exist_ok=True)
    Path("browser_profile").mkdir(exist_ok=True)

    # Start scheduler
    import scheduler as sched
    sched.start(broadcast_fn=_broadcast)

    # Start system tray
    try:
        import tray
        tray.start(host=HOST, port=PORT)
    except Exception as exc:
        logger.warning("Tray startup skipped: %s", exc)

    # Global hotkey
    try:
        import hotkey
        hotkey.start(port=PORT)
    except Exception as exc:
        logger.warning("Hotkey startup skipped: %s", exc)

    logger.info("bud ready at http://%s:%d", HOST, PORT)
    yield

    sched.stop()


app = FastAPI(title="EmulAItor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── REST endpoints ────────────────────────────────────────────────────────────

# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.get("/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request)
    return JSONResponse({"id": user["id"], "email": user["email"]})


@app.post("/auth/register")
async def auth_register(request: Request):
    body = await request.json()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password", "")
    name     = body.get("name", "").strip()

    if not email or "@" not in email:
        return JSONResponse({"error": "Valid email required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)

    existing = await db.get_user_by_email(email)
    if existing:
        return JSONResponse({"error": "Email already registered"}, status_code=409)

    user = await db.create_user(email, hash_password(password), name)
    token = create_token(user["id"], user["email"])
    resp = JSONResponse({"ok": True, "user": {"id": user["id"], "email": user["email"], "name": name}})
    resp.set_cookie("bud_token", token, httponly=True, samesite="lax", max_age=60*60*24*30, secure=False)
    return resp


@app.post("/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    user = await db.get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)

    token = create_token(user["id"], user["email"])
    resp = JSONResponse({"ok": True, "user": {"id": user["id"], "email": user["email"], "name": user.get("name", "")}})
    resp.set_cookie("bud_token", token, httponly=True, samesite="lax", max_age=60*60*24*30, secure=False)
    return resp


@app.post("/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("bud_token")
    return resp


@app.get("/auth/usercount")
async def auth_usercount():
    count = await db.user_count()
    return JSONResponse({"count": count})

@app.get("/privacy")
async def privacy_policy():
    return HTMLResponse("""<!DOCTYPE html><html><head><title>bud — Privacy Policy</title>
<style>body{background:#0d0d0d;color:#e4d8c4;font-family:Georgia,serif;max-width:700px;margin:60px auto;padding:0 20px;line-height:1.8;}
h1{color:#c8a45e;}h2{color:#c8a45e;font-size:1.1rem;margin-top:2rem;}a{color:#c8a45e;}</style></head>
<body>
<h1>bud Privacy Policy</h1>
<p><em>Last updated: April 2026</em></p>
<h2>What bud collects</h2>
<p>bud stores your conversation history, memories, and settings locally in a SQLite database on the server you run it on. No data is sent to third parties except the AI API providers you configure (Groq, OpenRouter, Ollama).</p>
<h2>Browser extension</h2>
<p>The bud Chrome extension connects only to your local bud server (127.0.0.1:8082) or emuai.org. It does not collect, transmit, or store any browsing data. It only acts on explicit commands from bud.</p>
<h2>AI providers</h2>
<p>When you send a message, your text is sent to the AI provider you have configured (Groq, OpenRouter, or a local Ollama model). Please review their respective privacy policies.</p>
<h2>Cookies</h2>
<p>bud uses a single session cookie for authentication if a password is set. No tracking or analytics cookies are used.</p>
<h2>Contact</h2>
<p>For any privacy questions, contact us at <a href="mailto:hello@emuai.org">hello@emuai.org</a>.</p>
</body></html>""")

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/settings")
async def get_settings(request: Request):
    try:
        user = get_current_user(request)
        cfg = await db.get_user_settings(user["id"])
    except Exception:
        cfg = settings_store.load()
    # Mask the raw API key
    key = cfg.get("openrouter_api_key", "")
    cfg["openrouter_api_key_set"] = bool(key)
    cfg["openrouter_api_key"] = ("sk-or-..." + key[-4:]) if len(key) > 8 else ("*" * len(key) if key else "")
    return JSONResponse(cfg)


@app.post("/api/settings")
async def update_settings(request: Request, request_body: dict):
    try:
        user = get_current_user(request)
        updated = await db.save_user_settings(user["id"], request_body)
    except Exception:
        key = request_body.get("openrouter_api_key", "")
        if key.startswith("sk-or-...") or key == "":
            request_body.pop("openrouter_api_key", None)
        updated = settings_store.save(request_body)
    return JSONResponse({"ok": True, "settings": updated})


@app.get("/api/sessions")
async def list_sessions(request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    return JSONResponse(await db.get_sessions(user_id=uid))


@app.post("/api/sessions")
async def create_session(request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    return JSONResponse(await db.create_session(user_id=uid))


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    await db.delete_session(session_id, user_id=uid)
    return JSONResponse({"ok": True})


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    return JSONResponse(await db.get_messages(session_id))


@app.post("/api/sessions/{session_id}/pin")
async def pin_session(session_id: str, request_body: dict, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    pinned = bool(request_body.get("pinned", False))
    await db.pin_session(session_id, pinned, user_id=uid)
    return JSONResponse({"ok": True, "pinned": pinned})


@app.post("/api/sessions/reorder")
async def reorder_sessions(request_body: dict, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    order = request_body.get("order", [])
    await db.reorder_sessions(order, user_id=uid)
    return JSONResponse({"ok": True})


@app.get("/api/export/{session_id}")
async def export_session_markdown(session_id: str, format: str = "markdown"):
    msgs = await db.get_messages(session_id)
    sessions = await db.get_sessions()
    title = next((s["title"] for s in sessions if s["id"] == session_id), "Chat Export")

    lines = [f"# {title}\n"]
    for m in msgs:
        role = m.get("role", "")
        content = m.get("content") or ""
        if not content:
            continue
        if role == "user":
            lines.append(f"**User:**\n{content}\n")
        elif role == "assistant":
            lines.append(f"**bud:**\n{content}\n")
    md = "\n".join(lines)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


# ── Voice transcription ───────────────────────────────────────────────────────

_whisper_model = None

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        # "tiny" model — ~75MB, downloads once, fast on CPU
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        logger.info("Whisper model loaded")
    return _whisper_model

@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Transcribe audio — tries Groq Whisper first, falls back to local faster-whisper."""
    import tempfile
    data = await file.read()
    suffix = ".webm" if "webm" in (file.content_type or "") else ".wav"

    # Try Groq Whisper (free, fast, no local model needed)
    import httpx as _httpx
    groq_key = settings_store.load().get("groq_api_key", "") or os.getenv("GROQ_API_KEY", "")
    if groq_key:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    files={"file": (f"audio{suffix}", data, file.content_type or "audio/webm")},
                    data={"model": "whisper-large-v3-turbo", "response_format": "json"},
                )
            if resp.status_code == 200:
                return JSONResponse({"text": resp.json().get("text", "").strip()})
        except Exception as exc:
            logger.warning("Groq transcription failed, falling back: %s", exc)

    # Fallback: local faster-whisper
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        def _transcribe():
            model = _get_whisper()
            segments, _ = model.transcribe(tmp_path, language="en", beam_size=1)
            return " ".join(s.text.strip() for s in segments).strip()
        text = await loop.run_in_executor(None, _transcribe)
        return JSONResponse({"text": text})
    except Exception as exc:
        logger.error("Transcription error: %s", exc)
        return JSONResponse({"text": "", "error": str(exc)}, status_code=500)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── File upload ───────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Save uploaded file to workspace/ and extract text content if possible."""
    WORKSPACE.mkdir(exist_ok=True)
    safe_name = Path(file.filename).name
    dest = WORKSPACE / safe_name
    data = await file.read()
    dest.write_bytes(data)

    content_summary = ""
    mime = mimetypes.guess_type(safe_name)[0] or ""

    # PDF extraction
    if safe_name.lower().endswith(".pdf"):
        try:
            import pdfplumber
            from io import BytesIO
            with pdfplumber.open(BytesIO(data)) as pdf:
                texts = [p.extract_text() or "" for p in pdf.pages[:10]]
            content_summary = "\n".join(texts)[:6000]
        except Exception as exc:
            content_summary = f"(PDF text extraction failed: {exc})"

    # Plain text / code / csv / json
    elif mime and (mime.startswith("text/") or safe_name.lower().endswith(
            (".txt", ".md", ".py", ".js", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini")
    )):
        content_summary = data.decode("utf-8", errors="replace")[:6000]

    # Image — just note the path
    elif mime and mime.startswith("image/"):
        content_summary = f"[Image file saved to workspace/{safe_name}]"

    else:
        content_summary = f"[File saved to workspace/{safe_name} — {len(data):,} bytes]"

    return JSONResponse({
        "path": f"workspace/{safe_name}",
        "filename": safe_name,
        "size": len(data),
        "content_summary": content_summary,
        "mime": mime,
    })


# ── Workspace file tree ───────────────────────────────────────────────────────

@app.get("/api/workspace")
async def workspace_tree():
    """Return workspace file list for the file tree panel."""
    WORKSPACE.mkdir(exist_ok=True)
    files = []
    for p in sorted(WORKSPACE.rglob("*")):
        if p.is_file():
            try:
                stat = p.stat()
                files.append({
                    "path": str(p.relative_to(WORKSPACE)),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            except Exception:
                pass
    return JSONResponse(files)


@app.get("/api/workspace/read")
async def workspace_read(path: str):
    """Read a workspace file (for the file tree panel)."""
    safe = (WORKSPACE / path).resolve()
    if not str(safe).startswith(str(WORKSPACE)) or not safe.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        content = safe.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"content": content[:20000]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Export chat ───────────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "md"):
    msgs  = await db.get_messages(session_id)
    sessions = await db.get_sessions()
    title = next((s["title"] for s in sessions if s["id"] == session_id), "Chat Export")

    if format == "md":
        lines = [f"# {title}\n"]
        for m in msgs:
            role = m["role"].capitalize()
            content = m.get("content") or ""
            if m["role"] == "tool":
                lines.append(f"**[Tool result: {m.get('name','')}]**\n```\n{content[:500]}\n```\n")
            elif content:
                lines.append(f"**{role}:** {content}\n")
        md = "\n".join(lines)
        return HTMLResponse(
            content=md,
            headers={"Content-Disposition": f'attachment; filename="{session_id}.md"',
                     "Content-Type": "text/markdown; charset=utf-8"},
        )

    # HTML export (printable, same theme)
    lines = []
    for m in msgs:
        content = m.get("content") or ""
        if not content:
            continue
        if m["role"] == "user":
            lines.append(f'<div class="user-msg">{_esc(content)}</div>')
        elif m["role"] == "assistant":
            lines.append(f'<div class="ai-msg">{_esc(content)}</div>')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<title>{_esc(title)}</title>
<style>
  body{{font-family:Georgia,serif;max-width:800px;margin:40px auto;background:#0e0c0a;color:#e4d8c4;padding:20px;}}
  h1{{color:#c8a45e;font-size:1.4rem;border-bottom:1px solid #2a2520;padding-bottom:.5rem;}}
  .user-msg{{background:#1e1b17;border:1px solid #2a2520;border-radius:8px;padding:.75rem 1rem;margin:.75rem 0;text-align:right;}}
  .ai-msg{{padding:.5rem 0;margin:.5rem 0;line-height:1.7;white-space:pre-wrap;}}
  @media print{{body{{background:#fff;color:#000;}} .user-msg{{background:#f5f5f5;border-color:#ccc;}} }}
</style></head>
<body><h1>{_esc(title)}</h1>{"".join(lines)}</body></html>"""

    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="{session_id}.html"'},
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── Text-to-Speech (edge-tts) ─────────────────────────────────────────────────

@app.post("/api/tts")
async def text_to_speech(request_body: dict):
    """Generate speech audio using edge-tts (Microsoft Azure voices, free)."""
    text  = request_body.get("text", "").strip()[:1500]
    voice = request_body.get("voice", "en-US-AriaNeural")
    if not text:
        return JSONResponse({"error": "No text"}, status_code=400)
    try:
        import edge_tts, io, asyncio
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio_bytes = buf.getvalue()
        if not audio_bytes:
            return JSONResponse({"error": "No audio generated"}, status_code=500)
        from fastapi.responses import Response
        return Response(content=audio_bytes, media_type="audio/mpeg")
    except ImportError:
        return JSONResponse({"error": "edge-tts not installed. Run: pip install edge-tts"}, status_code=500)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def get_analytics(request: Request):
    """Return usage analytics: tool counts, model usage, token totals."""
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    stats = await db.get_analytics(user_id=uid)
    return JSONResponse(stats)


@app.post("/api/analytics/event")
async def track_event(request_body: dict):
    """Track a usage event (tool call, token count, model used)."""
    await db.track_event(
        event_type = request_body.get("event_type", "tool"),
        tool_name  = request_body.get("tool_name"),
        model      = request_body.get("model"),
        tokens     = request_body.get("tokens", 0),
        session_id = request_body.get("session_id"),
    )
    return JSONResponse({"ok": True})


# ── Personas ─────────────────────────────────────────────────────────────────

@app.get("/api/personas")
async def list_personas(request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    personas = await db.get_personas(user_id=uid)
    return JSONResponse(personas)


@app.post("/api/personas")
async def create_persona(request_body: dict, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    persona = await db.save_persona(request_body, user_id=uid)
    return JSONResponse(persona)


@app.delete("/api/personas/{persona_id}")
async def remove_persona(persona_id: str, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    personas = await db.get_personas(user_id=uid)
    target = next((p for p in personas if p["id"] == persona_id), None)
    if target and target.get("builtin"):
        return JSONResponse({"ok": False, "error": "Cannot delete built-in personas"}, status_code=400)
    deleted = await db.delete_persona(persona_id, user_id=uid)
    return JSONResponse({"ok": deleted})


@app.post("/api/sessions/{session_id}/persona")
async def set_session_persona(session_id: str, request_body: dict):
    persona_id = request_body.get("persona_id", "assistant")
    cfg = settings_store.load()
    session_personas = cfg.get("session_personas", {})
    session_personas[session_id] = persona_id
    settings_store.save({"session_personas": session_personas})
    return JSONResponse({"ok": True, "session_id": session_id, "persona_id": persona_id})


# ── Memory browser ────────────────────────────────────────────────────────────

@app.get("/api/memories")
async def list_memories(request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    facts = await db.get_all_facts_with_ids(user_id=uid)
    return JSONResponse({"facts": facts})


@app.delete("/api/memories/{fact_id}")
async def delete_memory(fact_id: int, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    deleted = await db.delete_fact(fact_id, user_id=uid)
    return JSONResponse({"ok": deleted})


# ── Custom tools ──────────────────────────────────────────────────────────────

@app.get("/api/custom-tools")
async def list_custom_tools(request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    tools = await db.get_custom_tools(user_id=uid)
    return JSONResponse(tools)


@app.post("/api/custom-tools")
async def create_custom_tool(request_body: dict, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    tool = await db.save_custom_tool(request_body, user_id=uid)
    return JSONResponse(tool)


@app.delete("/api/custom-tools/{tool_id}")
async def remove_custom_tool(tool_id: str, request: Request):
    try:
        user = get_current_user(request)
        uid = user["id"]
    except Exception:
        uid = None
    deleted = await db.delete_custom_tool(tool_id, user_id=uid)
    return JSONResponse({"ok": deleted})


# ── Scheduled tasks ───────────────────────────────────────────────────────────

@app.get("/api/scheduled")
async def list_scheduled():
    try:
        import scheduler as sched
        jobs = sched.list_jobs()
        return JSONResponse({"jobs": jobs})
    except Exception as exc:
        return JSONResponse({"jobs": [], "error": str(exc)})


# ── User's real browser via CDP ───────────────────────────────────────────────
_CDP_PORT = int(os.getenv("CDP_PORT", "9222"))

@app.get("/api/browser/status")
async def browser_status():
    """Check if user's Chrome is open with --remote-debugging-port."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"http://127.0.0.1:{_CDP_PORT}/json/version")
            if r.status_code == 200:
                info = r.json()
                return JSONResponse({"connected": True, "browser": info.get("Browser", "Chrome")})
    except Exception:
        pass
    return JSONResponse({"connected": False})

@app.post("/api/browser/navigate")
async def browser_navigate_cdp(request_body: dict):
    """Navigate user's real Chrome to a URL via CDP."""
    url = request_body.get("url", "")
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # Get list of tabs
            r = await client.get(f"http://127.0.0.1:{_CDP_PORT}/json")
            tabs = r.json()
            if not tabs:
                return JSONResponse({"ok": False, "error": "No tabs found"})
            # Navigate first tab
            tab_id = tabs[0]["id"]
            ws_url = tabs[0]["webSocketDebuggerUrl"]
            # Use CDP websocket to navigate
            import websockets as _ws
            async with _ws.connect(ws_url) as cdp:
                await cdp.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
                await cdp.recv()
            return JSONResponse({"ok": True, "url": url, "tab": tab_id})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


# ── Browser extension WebSocket bridge ───────────────────────────────────────
# The Chrome extension connects here so bud can browse in the user's real browser

_extension_ws: WebSocket | None = None
_extension_pending: dict[str, asyncio.Future] = {}  # cmd_id -> Future

@app.websocket("/ws/extension")
async def extension_websocket(websocket: WebSocket):
    global _extension_ws
    await websocket.accept()
    _extension_ws = websocket
    logger.info("Browser extension connected")
    # Notify all UI clients
    await _broadcast({"type": "extension_status", "connected": True})
    try:
        while True:
            data = await websocket.receive_json()
            cmd_id = data.get("id")
            if cmd_id and cmd_id in _extension_pending:
                fut = _extension_pending.pop(cmd_id)
                if not fut.done():
                    fut.set_result(data)
    except Exception:
        pass
    finally:
        _extension_ws = None
        logger.info("Browser extension disconnected")
        await _broadcast({"type": "extension_status", "connected": False})

@app.get("/api/extension/status")
async def extension_status():
    return JSONResponse({"connected": _extension_ws is not None})

async def extension_command(cmd: dict, timeout: float = 30.0):
    """Send a command to the Chrome extension and await the result."""
    if _extension_ws is None:
        raise RuntimeError("Chrome extension not connected")
    import uuid
    cmd_id = str(uuid.uuid4())[:8]
    cmd["id"] = cmd_id
    fut = asyncio.get_event_loop().create_future()
    _extension_pending[cmd_id] = fut
    await _extension_ws.send_json(cmd)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _extension_pending.pop(cmd_id, None)
        raise RuntimeError("Extension did not respond in time")

# ── Share / collaborative sessions ────────────────────────────────────────────

# room_id -> set of send functions
_collab_rooms: dict[str, set] = {}

@app.get("/api/sessions/{session_id}/share")
async def create_share_link(session_id: str):
    """Generate a share token for a session so others can join in real-time."""
    import hashlib
    token = hashlib.sha256(session_id.encode()).hexdigest()[:12]
    return JSONResponse({"share_url": f"http://127.0.0.1:{PORT}/?room={token}", "token": token, "session_id": session_id})


@app.websocket("/ws/room/{token}")
async def collab_websocket(ws: WebSocket, token: str):
    """Collaborative WebSocket — all clients with the same token see the same session."""
    await ws.accept()

    async def send(payload: dict):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    if token not in _collab_rooms:
        _collab_rooms[token] = set()
    _collab_rooms[token].add(send)
    _ws_senders.append(send)

    async def broadcast_room(payload: dict):
        dead = set()
        for s in list(_collab_rooms.get(token, set())):
            try:
                await s(payload)
            except Exception:
                dead.add(s)
        for s in dead:
            _collab_rooms[token].discard(s)

    try:
        while True:
            raw  = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")
            if msg_type == "message":
                sid     = data.get("session_id")
                content = data.get("content", "").strip()
                if sid and content:
                    asyncio.create_task(run_agent(sid, content, broadcast_room))
            elif msg_type == "list_sessions":
                await send({"type": "sessions", "sessions": await db.get_sessions()})
            elif msg_type == "load_session":
                sid  = data.get("session_id")
                msgs = await db.get_messages(sid)
                await send({"type": "history", "messages": msgs, "sessions": await db.get_sessions()})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _collab_rooms.get(token, set()).discard(send)
        if send in _ws_senders:
            _ws_senders.remove(send)


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # Extract authenticated user from cookie
    user = get_user_from_ws(ws)
    user_id = user["id"] if user else None
    logger.info("WebSocket connected — user=%s", user_id or "anonymous")

    async def send(payload: dict):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    _ws_senders.append(send)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await send({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "load_session":
                sid  = data.get("session_id")
                msgs = await db.get_messages(sid)
                sessions = await db.get_sessions(user_id=user_id)
                await send({"type": "history", "messages": msgs, "sessions": sessions})

            elif msg_type == "list_sessions":
                await send({"type": "sessions", "sessions": await db.get_sessions(user_id=user_id)})

            elif msg_type == "message":
                sid     = data.get("session_id")
                content = data.get("content", "")
                if isinstance(content, str):
                    content = content.strip()
                if not sid or not content:
                    await send({"type": "error", "content": "session_id and content required"})
                    continue
                asyncio.create_task(run_agent(sid, content, send, user_id=user_id))

                async def _refresh():
                    await asyncio.sleep(0.5)
                    await send({"type": "sessions", "sessions": await db.get_sessions(user_id=user_id)})
                asyncio.create_task(_refresh())

            elif msg_type == "permission_response":
                sid     = data.get("session_id")
                tool    = data.get("tool")
                granted = bool(data.get("granted", False))
                if sid and tool:
                    resolve_permission(sid, tool, granted)

            elif msg_type == "new_session":
                session = await db.create_session(user_id=user_id)
                reset_session_permissions(session["id"])
                await send({"type": "session_created", "session": session})

            else:
                await send({"type": "error", "content": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as exc:
        logger.exception("WebSocket error: %s", exc)
    finally:
        if send in _ws_senders:
            _ws_senders.remove(send)


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="info")
