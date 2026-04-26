"""
LLM Router — concurrent model racing, task-aware routing, latency tracking.

Key upgrades vs previous version:
  - Race top-3 models simultaneously → first winner returns, others cancelled
  - Task-type detection (code / reason / quick / default) → best model family first
  - Per-model latency history → statistically faster models preferred
  - Streaming kept as fallback path; all timeouts reduced to 20 s
  - Groq / Ollama still work as before (unchanged paths)
"""

import asyncio
import logging
import os
import re
import time

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── model fallback chain ───────────────────────────────────────────────────────
MODELS = [
    "z-ai/glm-4.5-air:free",                        # GLM 4.5 Air — MoE, thinking + tool use
    "deepseek/deepseek-r1:free",                    # Best reasoning, shows thinking
    "deepseek/deepseek-chat-v3-0324:free",          # Best coding/chat
    "meta-llama/llama-4-maverick:free",             # Llama 4 Maverick
    "qwen/qwen3-235b-a22b:free",                    # Qwen 3 235B MoE
    "qwen/qwen3-30b-a3b:free",                      # Qwen 3 30B
    "qwen/qwen3-8b:free",                           # Qwen 3 8B fast
    "nousresearch/hermes-3-llama-3.1-405b:free",    # Hermes 3 405B
    "mistralai/mistral-7b-instruct:free",           # Mistral 7B
    "tngtech/deepseek-r1t-chimera:free",            # DeepSeek R1T Chimera
    "google/gemma-3-27b-it:free",                   # Gemma 3 27B
    "google/gemma-3-12b-it:free",                   # Gemma 3 12B
    "google/gemma-3-4b-it:free",                    # Gemma 3 4B
    "google/gemma-3n-e4b-it:free",                  # Gemma 3n 4B
    "meta-llama/llama-3.3-70b-instruct:free",       # Llama 3.3 70B
    "meta-llama/llama-3.1-8b-instruct:free",        # Llama 3.1 8B fast
    "meta-llama/llama-3.2-3b-instruct:free",        # Llama 3.2 3B last resort
]

COOLDOWN_SECONDS  = 30      # 429 cooldown
_MAX_WAIT_SECONDS = 45      # max time to block waiting for a model
_RACE_WIDTH       = 3       # how many models to fire concurrently

_cooldowns: dict[str, float] = {}

# Models without native function calling (use prompt injection instead)
_NO_FUNCTION_CALLING = {
    "google/gemma-3-12b-it:free",
    "google/gemma-3-4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "google/gemma-3n-e2b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
}

# Models that emit <think> reasoning blocks
_REASONING_MODELS = {
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-r1-zero:free",
    "qwen/qwen3-235b-a22b:free",
    "qwen/qwen3-30b-a3b:free",
    "qwen/qwq-32b:free",
    "tngtech/deepseek-r1t-chimera:free",
}

# Best model families per task type (must exist in MODELS)
_TASK_PRIORITY: dict[str, list[str]] = {
    "code": [
        "deepseek/deepseek-chat-v3-0324:free",
        "deepseek/deepseek-r1:free",
        "meta-llama/llama-4-maverick:free",
        "qwen/qwen3-235b-a22b:free",
        "z-ai/glm-4.5-air:free",
    ],
    "reason": [
        "qwen/qwen3-235b-a22b:free",
        "deepseek/deepseek-r1:free",
        "tngtech/deepseek-r1t-chimera:free",
        "z-ai/glm-4.5-air:free",
        "qwen/qwen3-30b-a3b:free",
    ],
    "quick": [
        "qwen/qwen3-8b:free",
        "google/gemma-3-4b-it:free",
        "meta-llama/llama-3.1-8b-instruct:free",
        "z-ai/glm-4.5-air:free",
        "google/gemma-3-12b-it:free",
    ],
}

# Per-model latency history (last 10 calls)
_latency: dict[str, list[float]] = {}


# ── internal helpers ───────────────────────────────────────────────────────────

def _make_openrouter_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key or "missing",
        default_headers={
            "HTTP-Referer": "http://localhost:8000",
            "X-Title":      "bud.app",
        },
    )


def _make_ollama_client(base_url: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )


def _is_available(model: str) -> bool:
    expiry = _cooldowns.get(model)
    if expiry is None:
        return True
    if time.time() >= expiry:
        del _cooldowns[model]
        return True
    return False


def _cooldown(model: str, seconds: int = None):
    _cooldowns[model] = time.time() + (seconds if seconds is not None else COOLDOWN_SECONDS)
    logger.warning("⏳ %s cooling down %ds", model, seconds or COOLDOWN_SECONDS)


def _record_latency(model: str, latency: float):
    history = _latency.setdefault(model, [])
    history.append(latency)
    if len(history) > 10:
        history.pop(0)


def _avg_latency(model: str) -> float:
    h = _latency.get(model, [])
    return sum(h) / len(h) if h else 99.0


def _detect_task_type(messages: list[dict]) -> str:
    """Analyse the last few messages to classify the task type."""
    content = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else ""
        for m in messages[-4:]
    ).lower()

    code_kws = {
        "python", "javascript", "typescript", "golang", "rust", "java", "swift",
        "code", "function", "class", "bug", "debug", "error", "import", "module",
        "sql", "html", "css", "api", "json", "yaml", "compile", "algorithm",
        "array", "dict", "database", "query", "script", "syntax",
    }
    reason_kws = {
        "analyze", "analyse", "explain", "reason", "math", "calculate", "compare",
        "evaluate", "research", "theory", "why", "how does", "understand", "prove",
        "difference", "summarize", "summarise", "what is", "complex",
    }

    code_score   = sum(1 for w in code_kws   if w in content)
    reason_score = sum(1 for w in reason_kws if w in content)

    if code_score >= 2:
        return "code"
    if reason_score >= 2:
        return "reason"
    if len(content) < 100:
        return "quick"
    return "default"


def _build_model_order(base: list[str], task: str) -> list[str]:
    """
    Return a re-ordered model list:
    1. Task-specific priority models (available, sorted by avg latency)
    2. Remaining models (available, sorted by avg latency)
    """
    priority = _TASK_PRIORITY.get(task, [])
    prio_set = set(priority)

    available_prio  = sorted(
        [m for m in priority  if m in base and _is_available(m)],
        key=_avg_latency,
    )
    available_rest  = sorted(
        [m for m in base if m not in prio_set and _is_available(m)],
        key=_avg_latency,
    )
    cooling_prio    = [m for m in priority  if m in base and not _is_available(m)]
    cooling_rest    = [m for m in base      if m not in prio_set and not _is_available(m)]

    return available_prio + available_rest + cooling_prio + cooling_rest


# ── tool-prompt injection (models without native function calling) ──────────────

def _build_tool_prompt(tools: list[dict]) -> str:
    lines = [
        "\n\n## Tool Use",
        "When you need to use a tool, output ONLY this exact format:",
        '<tool_call>{"name": "TOOL_NAME", "arguments": {ARGS_JSON}}</tool_call>',
        "\nAvailable tools:",
    ]
    for t in tools:
        fn = t["function"]
        props    = fn.get("parameters", {}).get("properties", {})
        required = fn.get("parameters", {}).get("required", [])
        args = ", ".join(
            f"{k}: {v.get('type', 'any')}" + ("" if k in required else "?")
            for k, v in props.items()
        )
        lines.append(f"- **{fn['name']}**({args}): {fn['description']}")
    lines.append(
        "\nIf you do NOT need a tool, reply normally. "
        "Never output a tool_call tag in your final answer."
    )
    return "\n".join(lines)


def _inject_tool_prompt(messages: list[dict], tools: list[dict]) -> list[dict]:
    tool_block = _build_tool_prompt(tools)
    patched, injected = [], False
    for m in messages:
        if m["role"] == "system" and not injected:
            patched.append({**m, "content": m["content"] + tool_block})
            injected = True
        else:
            patched.append(m)
    if not injected:
        patched = [{"role": "system", "content": tool_block.strip()}] + patched
    return patched


def _parse_prompt_tool_call(text: str) -> dict | None:
    import json
    match = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        name = data.get("name")
        if not name:
            return None
        return {"name": name, "arguments": data.get("arguments", {})}
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_thinking(content: str) -> tuple[str, str]:
    """Extract <think>…</think> blocks. Returns (thinking_text, clean_content)."""
    parts = re.findall(r"<think>(.*?)</think>", content, re.DOTALL)
    clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return "\n\n".join(p.strip() for p in parts), clean


# ── single-model attempt (used both in race and in sequential fallback) ────────

async def _attempt_model(
    model: str,
    or_client: AsyncOpenAI,
    messages: list[dict],
    tools: list[dict] | None,
    stream: bool,
) -> dict:
    """
    Try one model. Returns result dict on success. Raises on any failure.
    Caller is responsible for cooldown logic.
    """
    use_prompt_tools = model in _NO_FUNCTION_CALLING
    is_reasoning     = model in _REASONING_MODELS

    kwargs: dict = {
        "model":    model,
        "messages": _inject_tool_prompt(messages, tools) if (tools and use_prompt_tools) else messages,
        "timeout":  20,
    }
    if tools and not use_prompt_tools:
        kwargs["tools"]       = tools
        kwargs["tool_choice"] = "auto"
    if is_reasoning:
        kwargs["max_tokens"] = 8000
    if stream:
        kwargs["stream"] = True

    t0       = time.time()
    response = await or_client.chat.completions.create(**kwargs)
    latency  = time.time() - t0

    thinking = ""
    if not stream:
        choice         = response.choices[0]
        content        = choice.message.content or ""
        has_tool_calls = bool(getattr(choice.message, "tool_calls", None))

        thinking, clean = extract_thinking(content)
        if clean != content:
            choice.message.content = clean
            content = clean

        # Prompt-tool parsing for models without native FC
        if use_prompt_tools and tools and not has_tool_calls:
            ptc = _parse_prompt_tool_call(content)
            if ptc is not None:
                _record_latency(model, latency)
                usage = getattr(response, "usage", None)
                return {
                    "model":            model,
                    "response":         response,
                    "used_prompt_tools": True,
                    "prompt_tool_call": ptc,
                    "thinking":         thinking,
                    "token_usage": {
                        "prompt":     getattr(usage, "prompt_tokens", 0)     if usage else 0,
                        "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                        "total":      getattr(usage, "total_tokens", 0)      if usage else 0,
                    },
                    "latency": latency,
                }

        if not content and not has_tool_calls:
            raise ValueError(f"{model} returned empty response")

    _record_latency(model, latency)
    logger.info("✓ %s (%.1fs, prompt_tools=%s)", model, latency, use_prompt_tools)
    usage = getattr(response, "usage", None)
    return {
        "model":             model,
        "response":          response,
        "used_prompt_tools": use_prompt_tools,
        "thinking":          thinking if not stream else "",
        "token_usage": {
            "prompt":     getattr(usage, "prompt_tokens", 0)     if usage else 0,
            "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
            "total":      getattr(usage, "total_tokens", 0)      if usage else 0,
        },
        "latency": latency,
    }


# ── concurrent race ────────────────────────────────────────────────────────────

async def _race(
    candidates: list[str],
    or_client: AsyncOpenAI,
    messages: list[dict],
    tools: list[dict] | None,
    stream: bool,
) -> dict:
    """
    Fire up to _RACE_WIDTH models simultaneously.
    First successful response wins; all others are cancelled.
    Falls back to sequential if batch fails completely.
    """
    batch     = candidates[:_RACE_WIDTH]
    remaining = candidates[_RACE_WIDTH:]

    if not batch:
        raise RuntimeError("No available models to race")

    logger.info("🏁 Racing %s", batch)

    async def _guarded(model: str):
        try:
            return await _attempt_model(model, or_client, messages, tools, stream)
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate limit" in msg.lower():
                _cooldown(model)
            elif "502" in msg or "503" in msg or "provider" in msg.lower():
                _cooldown(model, 30)
            else:
                logger.warning("✗ %s: %s", model, exc)
            raise

    tasks   = [asyncio.create_task(_guarded(m)) for m in batch]
    pending = set(tasks)
    errors  = []

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is None:
                # Winner — cancel the rest
                for p in pending:
                    p.cancel()
                return task.result()
            errors.append(task.exception())

    # Entire batch failed — try remaining models sequentially
    for model in remaining:
        if not _is_available(model):
            continue
        try:
            return await _attempt_model(model, or_client, messages, tools, stream)
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate limit" in msg.lower():
                _cooldown(model)
            elif "502" in msg or "503" in msg or "provider" in msg.lower():
                _cooldown(model, 30)
            errors.append(exc)

    raise errors[-1] if errors else RuntimeError("All models failed")


# ── public API ─────────────────────────────────────────────────────────────────

async def chat_completion(
    messages:  list[dict],
    tools:     list[dict] | None = None,
    stream:    bool              = False,
    user_cfg:  dict | None       = None,
) -> dict:
    """
    Returns:
        model, response, used_prompt_tools, thinking, token_usage, latency
    user_cfg overrides global settings (API keys, model prefs, etc.)
    """
    import settings_store
    cfg = settings_store.load()
    if user_cfg:
        cfg = {**cfg, **user_cfg}

    # ── Groq path ──────────────────────────────────────────────────────────────
    if cfg.get("groq_enabled"):
        groq_key   = cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY", "")
        groq_model = cfg.get("groq_model", "llama-3.3-70b-versatile")
        groq_client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_key or "missing",
        )
        logger.info("⚡ Groq: %s", groq_model)
        try:
            kwargs: dict = {"model": groq_model, "messages": messages, "timeout": 30}
            if tools:
                kwargs["tools"]       = tools
                kwargs["tool_choice"] = "auto"
            if stream:
                kwargs["stream"] = True
            response = await groq_client.chat.completions.create(**kwargs)
            thinking = ""
            if not stream:
                content          = response.choices[0].message.content or ""
                thinking, clean  = extract_thinking(content)
                if clean != content:
                    response.choices[0].message.content = clean
            usage = getattr(response, "usage", None)
            return {
                "model":             f"groq/{groq_model}",
                "response":          response,
                "used_prompt_tools": False,
                "thinking":          thinking,
                "token_usage": {
                    "prompt":     getattr(usage, "prompt_tokens", 0)     if usage else 0,
                    "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total":      getattr(usage, "total_tokens", 0)      if usage else 0,
                },
                "latency": 0.0,
            }
        except Exception as exc:
            logger.error("Groq error: %s — falling back to OpenRouter", exc)

    # ── Ollama path ────────────────────────────────────────────────────────────
    if cfg.get("ollama_enabled"):
        ollama_url   = cfg.get("ollama_url", "http://localhost:11434")
        ollama_model = cfg.get("ollama_model", "llama3.2")
        ol_client    = _make_ollama_client(ollama_url)
        logger.info("🦙 Ollama: %s @ %s", ollama_model, ollama_url)
        try:
            kwargs: dict = {"model": ollama_model, "messages": messages, "timeout": 120}
            if tools:
                kwargs["tools"]       = tools
                kwargs["tool_choice"] = "auto"
            if stream:
                kwargs["stream"] = True
            response = await ol_client.chat.completions.create(**kwargs)
            thinking = ""
            if not stream:
                content         = response.choices[0].message.content or ""
                thinking, clean = extract_thinking(content)
                if clean != content:
                    response.choices[0].message.content = clean
            usage = getattr(response, "usage", None)
            return {
                "model":             f"ollama/{ollama_model}",
                "response":          response,
                "used_prompt_tools": False,
                "thinking":          thinking,
                "token_usage": {
                    "prompt":     getattr(usage, "prompt_tokens", 0)     if usage else 0,
                    "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total":      getattr(usage, "total_tokens", 0)      if usage else 0,
                },
                "latency": 0.0,
            }
        except Exception as exc:
            logger.error("Ollama error: %s — falling back to OpenRouter", exc)

    # ── OpenRouter path ────────────────────────────────────────────────────────
    api_key   = cfg.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
    or_client = _make_openrouter_client(api_key)

    preferred  = cfg.get("preferred_model", "auto")
    base_list  = MODELS if preferred in ("auto", "", None) else [preferred] + MODELS

    task_type  = _detect_task_type(messages)
    candidates = _build_model_order(base_list, task_type)

    logger.info("🎯 Task type: %s | Top candidates: %s", task_type, candidates[:3])

    try:
        return await _race(candidates, or_client, messages, tools, stream)
    except Exception:
        pass  # fall through to wait-and-retry

    # All models exhausted — wait for the soonest cooldown then retry
    if _cooldowns:
        soonest = min(_cooldowns.values())
        wait    = soonest - time.time()
        if 0 < wait <= _MAX_WAIT_SECONDS:
            logger.warning("⏳ All cooling down — waiting %.0fs", wait)
            await asyncio.sleep(wait + 0.5)
            candidates2 = _build_model_order(base_list, task_type)
            try:
                return await _race(candidates2, or_client, messages, tools, stream)
            except Exception as exc:
                raise RuntimeError(
                    f"⏳ All free models are rate-limited. Wait 30–60 s and try again.\n"
                    f"Last error: {exc}"
                ) from exc

    raise RuntimeError("⏳ All free models are rate-limited. Wait 30–60 s and try again.")


async def stream_chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    user_cfg: dict | None = None,
):
    """
    Async generator for true token-by-token streaming.
    Yields: str  (text chunks to emit to client)
    Final yield: dict with key '__done__': True  plus result fields:
        model, thinking, tool_calls (list of {id,name,arguments}),
        used_prompt_tools, prompt_tool_call, token_usage
    """
    import settings_store
    cfg = settings_store.load()
    if user_cfg:
        cfg = {**cfg, **user_cfg}

    api_key   = cfg.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
    or_client = _make_openrouter_client(api_key)

    preferred  = cfg.get("preferred_model", "auto")
    base_list  = MODELS if preferred in ("auto", "", None) else [preferred] + MODELS
    task_type  = _detect_task_type(messages)
    candidates = _build_model_order(base_list, task_type)

    # Groq fast path
    if cfg.get("groq_enabled"):
        groq_key    = cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY", "")
        groq_model  = cfg.get("groq_model", "llama-3.3-70b-versatile")
        groq_client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key or "missing")
        try:
            kwargs = {"model": groq_model, "messages": messages, "timeout": 30, "stream": True}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            stream = await groq_client.chat.completions.create(**kwargs)
            full_text = ""
            tc_map: dict[int, dict] = {}
            async for chunk in stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                if delta.content:
                    full_text += delta.content
                    yield delta.content
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        e = tc_map.setdefault(tc.index, {"id":"","name":"","arguments":""})
                        if tc.id: e["id"] = tc.id
                        if tc.function:
                            if tc.function.name: e["name"] += tc.function.name
                            if tc.function.arguments: e["arguments"] += tc.function.arguments
            thinking, clean = extract_thinking(full_text)
            yield {"__done__": True, "model": f"groq/{groq_model}", "thinking": thinking,
                   "text": clean, "tool_calls": list(tc_map.values()),
                   "used_prompt_tools": False, "prompt_tool_call": None,
                   "token_usage": {"prompt":0,"completion":0,"total":0}}
            return
        except Exception as exc:
            logger.error("Groq stream error: %s — falling back", exc)

    last_error = None
    for model in candidates:
        if not _is_available(model):
            continue
        use_prompt_tools = model in _NO_FUNCTION_CALLING
        is_reasoning     = model in _REASONING_MODELS
        try:
            msgs = _inject_tool_prompt(messages, tools) if (tools and use_prompt_tools) else messages
            kwargs: dict = {"model": model, "messages": msgs, "timeout": 20, "stream": True}
            if tools and not use_prompt_tools:
                kwargs["tools"]       = tools
                kwargs["tool_choice"] = "auto"
            if is_reasoning:
                kwargs["max_tokens"] = 8000

            t0     = time.time()
            stream = await or_client.chat.completions.create(**kwargs)

            full_text = ""
            tc_map: dict[int, dict] = {}

            async for chunk in stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                if delta.content:
                    full_text += delta.content
                    yield delta.content
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        e = tc_map.setdefault(tc.index, {"id":"","name":"","arguments":""})
                        if tc.id: e["id"] = tc.id
                        if tc.function:
                            if tc.function.name: e["name"] += tc.function.name
                            if tc.function.arguments: e["arguments"] += tc.function.arguments

            _record_latency(model, time.time() - t0)

            thinking, clean = extract_thinking(full_text)

            # Prompt-tool call extraction (models without native FC)
            prompt_tool_call = None
            if use_prompt_tools and tools and not tc_map:
                prompt_tool_call = _parse_prompt_tool_call(clean)
                if prompt_tool_call:
                    import re as _re
                    clean = _re.sub(r"<tool_call>.*?</tool_call>", "", clean, flags=_re.DOTALL).strip()

            if not clean and not tc_map and not prompt_tool_call:
                logger.warning("⚠ %s streamed empty — trying next", model)
                continue

            logger.info("✓ stream %s complete", model)
            yield {"__done__": True, "model": model, "thinking": thinking,
                   "text": clean, "tool_calls": list(tc_map.values()),
                   "used_prompt_tools": use_prompt_tools,
                   "prompt_tool_call": prompt_tool_call,
                   "token_usage": {"prompt":0,"completion":0,"total":0}}
            return

        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate limit" in msg.lower(): _cooldown(model)
            elif "502" in msg or "503" in msg or "provider" in msg.lower(): _cooldown(model, 30)
            else: logger.warning("✗ stream %s: %s", model, exc)
            last_error = exc

    yield {"__done__": True, "error": str(last_error or "All models exhausted")}
