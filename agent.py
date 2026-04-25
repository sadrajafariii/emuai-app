"""
Agent loop — orchestrates LLM calls, tool execution, and WebSocket streaming.
Supports concurrent sessions, live thinking display, and permission gating.
"""

import asyncio
import contextvars
import json
import logging
import re
from datetime import datetime
from typing import Callable, Awaitable

import db
import llm_router
import settings_store
from tools import TOOL_SCHEMAS, execute_tool

# Context variable so tools.py can pick up the current user_id without param threading
current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user_id", default=None)

logger = logging.getLogger(__name__)

# Tools that require user permission before running
PERMISSION_REQUIRED = {
    "run_terminal",
    "write_system_file",
    "list_directory",
    "read_system_file",
    "download_video",
    "browser_navigate",
}

# Per-session permission cache: session_id -> set of approved tool names
_session_permissions: dict[str, set] = {}

# Per-session pending permission futures: session_id -> {tool_name: Future}
_pending_permissions: dict[str, dict[str, asyncio.Future]] = {}

# Active agent tasks per session
_active_tasks: dict[str, asyncio.Task] = {}

today = datetime.utcnow().strftime("%B %d, %Y")

_BASE_SYSTEM_PROMPT = f"""You are bud — a powerful, autonomous personal AI agent running locally on the user's computer.
Today's date is {today}.

You can browse the web, execute code, generate images, manage files, download videos, and remember things across conversations.

## Core guidelines
- Be direct and efficient. Use tools immediately — don't describe what you're about to do, just do it.
- When you need information, search or browse for it rather than guessing.
- Always cite sources when using web search results.
- Workspace files are stored in ./workspace/ — use read_file/write_file for sandboxed work.
- Use remember() for facts the user wants retained across sessions.
- Chain multiple tool calls to accomplish complex tasks.

## Computer access
You have full access to the user's computer:
- run_terminal(command) — run anything in CMD: install packages, download files, move/copy/delete, compile, etc.
- list_directory(path) — browse any folder on the system.
- read_system_file(path) — read any file (Documents, Desktop, Downloads, etc).
- write_system_file(path, content) — save files anywhere.
- download_video(url) — download videos from YouTube, Twitter, TikTok, Instagram, etc.

## Browser control — CRITICAL RULES
You have a persistent visible Chromium browser the user can watch in real time.
Use these tools to control it like a human would:

- browser_navigate(url) — open a URL in the visible browser
- browser_screenshot() — **ALWAYS call this after every single action** to see the current state before deciding what to do next
- browser_click(selector) — click elements. Formats: `text=Submit`, `#id`, `.class`, `[name='q']`, `x=320,y=450`
- browser_type(selector, text) — type into inputs. Use `focused` as selector if already clicked
- browser_press(key) — press keys: Enter, Tab, Escape, ArrowDown, Control+a, Backspace, etc.
- browser_scroll(direction, amount) — scroll up or down
- browser_read_page() — extract all text from the current page
- browser_wait(milliseconds) — wait for page load or animation
- browser_close() — close the browser when done

### The vision loop (follow this EXACTLY):
1. browser_navigate(url) to open the page
2. browser_screenshot() to see the current state
3. Decide what to do — find the right element from the screenshot
4. browser_click() or browser_type() or browser_scroll()
5. browser_screenshot() again — see if it worked
6. Repeat steps 3-5 until task is done
7. browser_read_page() to extract any needed text/data
8. browser_close() when finished

NEVER assume a click or type worked — ALWAYS screenshot after every action.
If a selector fails, look at the screenshot and try coordinates (x=,y=) instead.

## Coding & debugging — CRITICAL RULES
When writing code to solve a problem, follow this loop:
1. Check if required packages are installed. If not, install them FIRST with run_terminal("pip install X") or run_terminal("npm install X").
2. Write the code and save it if it's a file-based project.
3. Run it immediately with run_code or run_terminal. Do NOT skip this step.
4. Read the output:
   - Clean run → report success and show output.
   - Any error → fix the bug and run again.
5. Repeat until it passes. Only say "done" after a successful run.
Never say "you can install X" or "try running this" — do it yourself.

## Interactive apps & games
When asked to build a game, app, dashboard, or any interactive UI:
- Build it as a single self-contained HTML file with embedded CSS and JS.
- Use serve_html_app(html, title) to launch it in a live browser window the user can interact with.
- Do NOT tell the user to open files manually — serve it yourself.
- For complex apps, write to workspace/ first then serve.

## Image generation
Use image_generate(prompt, style) for any image request.
Styles: "realistic", "artistic", "anime", "3d", "pixel", "sketch", "cinematic".
Make prompts very detailed and descriptive for best results.

## Clipboard
- clip_read() — read whatever the user has copied
- clip_write(text) — copy something to their clipboard

## YouTube
- youtube_transcript(url) — get full transcript instantly, no download needed

## Desktop control (full computer access) — CRITICAL RULES
- desktop_screenshot() — see the entire screen AND get a list of open windows
- desktop_click(x, y) — click anywhere using pixel coordinates
- desktop_type(text) — type into whatever currently has focus
- desktop_hotkey(keys) — e.g. 'ctrl+c', 'alt+tab', 'win+d', 'win+r'
- desktop_scroll_screen(direction, clicks) — scroll anywhere

### Desktop rules (follow EXACTLY):
1. NEVER ask the user where something is — use desktop_screenshot() to see the screen yourself
2. To open any app: use run_terminal("start notepad") or run_terminal("start brave") or run_terminal("start ms-settings:") — NEVER ask the user to open it
3. After opening an app: desktop_screenshot() to confirm it opened, then interact
4. To click something: desktop_screenshot() first, look at the window list and coordinates, then desktop_click(x, y)
5. To type: click the target field first with desktop_click(), then desktop_type()
6. After every action: desktop_screenshot() to verify it worked before continuing

## Email
- email_read(count) — read recent emails
- email_send(to, subject, body) — send an email
- email_search(query) — search inbox
Requires EMAIL_ADDRESS + EMAIL_PASSWORD in .env

## Scheduled tasks
- schedule_task(task, cron) — run a task on a schedule (e.g. '0 9 * * *' = 9am daily UTC)
- list_schedules() — see all scheduled tasks
- cancel_schedule(job_id) — remove a schedule

## Notifications
- notify_desktop(title, message) — send a native desktop notification

## PDF
- read_pdf(path) — extract text from any PDF file

## RAG Vault (document memory)
- vault_add(content, title) — save a document/text to the searchable vault
- vault_search(query) — semantic search across all vault documents
- vault_list() — list all documents in the vault
- vault_delete(doc_id) — remove a document from the vault

Respond in Markdown when it improves readability."""

_AUTO_MEMORY_ADDON = """

## Auto-Memory (ENABLED)
You MUST proactively save useful facts about the user without being asked.
After every conversation turn, if you learned anything about the user (name, preferences, job,
location, projects, goals, habits, family, etc.) — call remember() to store it immediately.
Examples of things to save: their name, profession, location, hobbies, ongoing projects,
software they use, food preferences, relationship details, or anything they've mentioned about themselves.
Do not ask permission — just call remember() silently."""


def _build_system_prompt(cfg: dict) -> str:
    prompt = _BASE_SYSTEM_PROMPT
    name = cfg.get("user_name", "").strip()
    if name and name != "Friend":
        prompt = f"The user's name is {name}. Address them by name naturally.\n\n" + prompt
    if cfg.get("auto_memory", True):
        prompt += _AUTO_MEMORY_ADDON
    return prompt


async def run_agent(
    session_id: str,
    user_message: str,
    send: Callable[[dict], Awaitable[None]],
    user_id: str = None,
):
    """
    Main agent loop. Runs concurrently across sessions.
    All events are tagged with session_id so the UI can route them correctly.
    """
    global_cfg = settings_store.load()
    if user_id:
        user_settings = await db.get_user_settings(user_id)
        cfg = {**global_cfg, **user_settings}  # user settings override global
    else:
        cfg = global_cfg
    max_iterations = cfg.get("max_iterations", 100)

    # Make user_id available to tools (save_fact, search_facts, etc.)
    current_user_id.set(user_id)

    async def emit(payload: dict):
        payload["session_id"] = session_id
        await send(payload)

    # Persist user message (content may be string or list for multimodal)
    user_text = user_message if isinstance(user_message, str) else next(
        (p["text"] for p in user_message if isinstance(p, dict) and p.get("type") == "text"), "(image)"
    )
    await db.save_message(session_id, "user", user_text)
    await db.touch_session(session_id)
    await _maybe_set_title(session_id, user_text)

    history  = await db.get_messages(session_id)
    history  = await _auto_summarize(history, session_id)

    # Inject active persona system prompt
    sys_content = _build_system_prompt(cfg)
    session_personas = cfg.get("session_personas", {})
    persona_id = session_personas.get(session_id) or cfg.get("active_persona", "assistant")
    personas = cfg.get("personas", [])
    persona = next((p for p in personas if p["id"] == persona_id), None)
    if persona and persona.get("system_prompt"):
        sys_content = persona["system_prompt"] + "\n\n" + sys_content

    # For multimodal, pass the raw content (list) as the last user message
    if not isinstance(user_message, str):
        history[-1]["content"] = user_message  # replace last user msg with multimodal content

    messages = [{"role": "system", "content": sys_content}] + history

    # Cumulative token counts for this agent run
    total_prompt_tokens     = 0
    total_completion_tokens = 0

    iterations = 0

    while iterations < max_iterations:
        iterations += 1

        # ── call LLM ──────────────────────────────────────────────────────────
        await emit({"type": "status", "content": "Thinking…"})
        try:
            result = await llm_router.chat_completion(messages, tools=TOOL_SCHEMAS, user_cfg=cfg if user_id else None)
        except RuntimeError as exc:
            await emit({"type": "error", "content": str(exc)})
            return

        model_used  = result["model"]
        thinking    = result.get("thinking", "")
        token_usage = result.get("token_usage", {})

        total_prompt_tokens     += token_usage.get("prompt", 0)
        total_completion_tokens += token_usage.get("completion", 0)

        await emit({"type": "model_info", "model": model_used})

        # Track LLM call in analytics
        asyncio.create_task(db.track_event("llm", model=model_used, tokens=token_usage.get("total", 0), session_id=session_id))

        await emit({
            "type": "token_usage",
            "prompt":     total_prompt_tokens,
            "completion": total_completion_tokens,
            "total":      total_prompt_tokens + total_completion_tokens,
        })

        # ── stream thinking if present ────────────────────────────────────────
        if thinking and cfg.get("show_thinking", True):
            await emit({"type": "thinking", "content": thinking})

        response          = result["response"]
        used_prompt_tools = result.get("used_prompt_tools", False)
        prompt_tool_call  = result.get("prompt_tool_call")

        choice  = response.choices[0]
        msg     = choice.message
        content = msg.content or ""

        # ── determine tool calls ──────────────────────────────────────────────
        tool_calls = []

        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})

        elif prompt_tool_call:
            tool_calls.append({
                "id": f"ptc_{iterations}",
                "name": prompt_tool_call["name"],
                "arguments": prompt_tool_call["arguments"],
            })
            content = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()

        # ── final answer ──────────────────────────────────────────────────────
        if not tool_calls:
            await emit({"type": "token", "content": content})
            await emit({
                "type": "message_complete",
                "token_usage": {
                    "prompt": total_prompt_tokens,
                    "completion": total_completion_tokens,
                    "total": total_prompt_tokens + total_completion_tokens,
                },
            })
            await db.save_message(session_id, "assistant", content)

            # TTS: if enabled, tell the UI to speak the response
            if cfg.get("tts_enabled") and content:
                await emit({"type": "tts_speak", "text": content, "voice": cfg.get("tts_voice", "en-US-AriaNeural")})

            messages.append({"role": "assistant", "content": content})
            break

        # ── persist assistant tool-call message ───────────────────────────────
        if used_prompt_tools:
            messages.append({"role": "assistant", "content": msg.content or ""})
        else:
            tc_payload = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ]
            messages.append({"role": "assistant", "content": content or None, "tool_calls": tc_payload})
            await db.save_message(session_id, "assistant", content=content or None, tool_calls=tc_payload)

        # ── execute each tool call ────────────────────────────────────────────
        for tc in tool_calls:
            tool_name = tc["name"]
            tool_args = tc["arguments"]
            tool_id   = tc["id"]

            # ── permission gate ───────────────────────────────────────────────
            if tool_name in PERMISSION_REQUIRED:
                session_perms = _session_permissions.setdefault(session_id, set())
                if tool_name not in session_perms:
                    granted = await _request_permission(session_id, tool_name, tool_args, emit)
                    if not granted:
                        result_text = f"User denied permission for {tool_name}."
                        await emit({"type": "tool_result", "tool": tool_name,
                                    "output": result_text, "image_path": None, "status": "denied"})
                        messages.append({"role": "tool", "tool_call_id": tool_id,
                                         "name": tool_name, "content": result_text})
                        continue
                    session_perms.add(tool_name)

            # ── track tool call in analytics ──────────────────────────────────
            asyncio.create_task(db.track_event("tool", tool_name=tool_name, session_id=session_id))

            # ── notify UI: starting ───────────────────────────────────────────
            await emit({"type": "tool_start", "tool": tool_name, "args": tool_args})

            # ── execute ───────────────────────────────────────────────────────
            tool_result = await execute_tool(tool_name, tool_args, send=emit)
            output_text = tool_result["output"]
            image_path  = tool_result.get("image_path")
            app_url     = tool_result.get("app_url")
            status      = _infer_status(tool_name, output_text)

            await emit({
                "type": "tool_result", "tool": tool_name,
                "output": output_text, "image_path": image_path,
                "app_url": app_url, "status": status,
            })

            # ── add result to history ─────────────────────────────────────────
            if used_prompt_tools:
                note = f"[Tool result for {tool_name}]\n{output_text}"
                messages.append({"role": "user", "content": note})
                await db.save_message(session_id, "user", note)
            else:
                messages.append({"role": "tool", "tool_call_id": tool_id,
                                  "name": tool_name, "content": output_text})
                await db.save_message(session_id, "tool", content=output_text,
                                      tool_call_id=tool_id, name=tool_name)
    else:
        await emit({"type": "error", "content": f"Reached max tool iterations ({max_iterations})."})


# ── permission system ─────────────────────────────────────────────────────────

async def _request_permission(
    session_id: str,
    tool_name: str,
    args: dict,
    emit: Callable,
) -> bool:
    descriptions = {
        "run_terminal":      f"Run terminal command: `{args.get('command', '')[:80]}`",
        "write_system_file": f"Write to: `{args.get('path', '')}`",
        "list_directory":    f"Browse folder: `{args.get('path', '~')}`",
        "read_system_file":  f"Read file: `{args.get('path', '')}`",
        "download_video":    f"Download video from: `{args.get('url', '')[:60]}`",
        "browser_navigate":  f"Open browser and go to: `{args.get('url', '')[:80]}`",
    }
    description = descriptions.get(tool_name, f"Use tool: {tool_name}")

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_permissions.setdefault(session_id, {})[tool_name] = fut

    await emit({
        "type": "permission_request",
        "tool": tool_name,
        "description": description,
        "args": args,
    })

    try:
        granted = await asyncio.wait_for(fut, timeout=120)
        return granted
    except asyncio.TimeoutError:
        return False
    finally:
        _pending_permissions.get(session_id, {}).pop(tool_name, None)


def resolve_permission(session_id: str, tool_name: str, granted: bool):
    fut = _pending_permissions.get(session_id, {}).get(tool_name)
    if fut and not fut.done():
        fut.set_result(granted)


def reset_session_permissions(session_id: str):
    _session_permissions.pop(session_id, None)


# ── auto-summarize long context ───────────────────────────────────────────────

_SUMMARIZE_THRESHOLD = 60   # messages before compression kicks in
_KEEP_RECENT         = 20   # always keep the N most recent messages verbatim

async def _auto_summarize(history: list, session_id: str) -> list:
    """If conversation is long, summarise the older half with a quick LLM call."""
    if len(history) <= _SUMMARIZE_THRESHOLD:
        return history

    to_compress = history[:-_KEEP_RECENT]
    recent      = history[-_KEEP_RECENT:]

    # Build a plain text digest of old messages
    digest_lines = []
    for m in to_compress:
        role    = m.get("role", "?")
        content = (m.get("content") or "")[:300]
        if content:
            digest_lines.append(f"{role}: {content}")
    digest = "\n".join(digest_lines)

    summary_prompt = [
        {"role": "system", "content": "You are a concise summariser. Summarise the following conversation history in 3-5 sentences, preserving all important facts, decisions, and context. Be factual and brief."},
        {"role": "user",   "content": digest},
    ]

    try:
        result  = await llm_router.chat_completion(summary_prompt)
        summary = result["response"].choices[0].message.content or ""
        compressed = {
            "role":    "system",
            "content": f"[Earlier conversation summary — {len(to_compress)} messages compressed]\n{summary}",
        }
        logger.info("Auto-summarised %d messages for session %s", len(to_compress), session_id)
        return [compressed] + recent
    except Exception as exc:
        logger.warning("Auto-summarise failed: %s — keeping full history", exc)
        return history


# ── helpers ───────────────────────────────────────────────────────────────────

def _infer_status(tool_name: str, output: str) -> str:
    if tool_name not in ("run_code", "run_terminal"):
        return "info"
    low = output.lower()
    error_markers = (
        "error", "exception", "traceback", "syntaxerror", "nameerror",
        "typeerror", "valueerror", "importerror", "runtimeerror",
        "stderr", "timed out", "not found", "exitcode", "fatal",
        "failed", "cannot", "denied", "no such file",
    )
    if any(m in low for m in error_markers):
        return "fail"
    return "pass"


async def _maybe_set_title(session_id: str, user_message: str):
    sessions = await db.get_sessions()
    for s in sessions:
        if s["id"] == session_id and s["title"] == "New Chat":
            title = user_message.strip().replace("\n", " ")[:55]
            if len(user_message) > 55:
                title += "…"
            await db.update_session_title(session_id, title)
            break
