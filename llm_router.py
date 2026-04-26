"""
LLM Router — tries OpenRouter models in order (or Ollama if enabled).
Rate-limited models are cooled down for COOLDOWN_SECONDS before retry.
Settings are read fresh from settings_store on each call so changes
take effect without restarting the server.
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

# ── editable model fallback chain ─────────────────────────────────────────────
MODELS = [
    "qwen/qwen3-235b-a22b-instruct-2507",           # Primary: cheapest, best value
    "deepseek/deepseek-r1:free",                    # Best reasoning, shows thinking
    "deepseek/deepseek-chat-v3-0324:free",          # Best coding/chat
    "meta-llama/llama-4-maverick:free",             # Llama 4
    "meta-llama/llama-4-scout:free",                # Llama 4 Scout
    "qwen/qwen3-235b-a22b:free",                    # Qwen 3 large (free)
    "qwen/qwen3-30b-a3b:free",                      # Qwen 3 medium
    "nousresearch/hermes-3-llama-3.1-405b:free",    # Hermes 405B
    "google/gemma-3-27b-it:free",                   # Gemma 3 27B
    "meta-llama/llama-3.3-70b-instruct:free",       # Llama 3.3 70B
]

COOLDOWN_SECONDS = 60
_cooldowns: dict[str, float] = {}

# Models that don't support native function calling
_NO_FUNCTION_CALLING = {
    "google/gemma-3-12b-it:free",
    "google/gemma-3-4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "google/gemma-3n-e2b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
}

# Models that support native <think> reasoning blocks
_REASONING_MODELS = {
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-r1-zero:free",
    "qwen/qwen3-235b-a22b:free",
    "qwen/qwen3-30b-a3b:free",
    "qwen/qwq-32b:free",
}


def _make_openrouter_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key or "missing",
        default_headers={
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "bud.app",
        },
    )


def _make_ollama_client(base_url: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=base_url.rstrip("/") + "/v1",
        api_key="ollama",  # Ollama doesn't check the key
    )


def _is_available(model: str) -> bool:
    expiry = _cooldowns.get(model)
    if expiry is None:
        return True
    if time.time() >= expiry:
        del _cooldowns[model]
        return True
    return False


def _cooldown(model: str):
    _cooldowns[model] = time.time() + COOLDOWN_SECONDS
    logger.warning("⏳ Model %s rate-limited — cooling down %ds", model, COOLDOWN_SECONDS)


def _build_tool_prompt(tools: list[dict]) -> str:
    lines = [
        "\n\n## Tool Use",
        "When you need to use a tool, output ONLY this exact format:",
        '<tool_call>{"name": "TOOL_NAME", "arguments": {ARGS_JSON}}</tool_call>',
        "\nAvailable tools:",
    ]
    for t in tools:
        fn = t["function"]
        props = fn.get("parameters", {}).get("properties", {})
        required = fn.get("parameters", {}).get("required", [])
        args = ", ".join(
            f"{k}: {v.get('type','any')}" + ("" if k in required else "?")
            for k, v in props.items()
        )
        lines.append(f"- **{fn['name']}**({args}): {fn['description']}")
    lines.append("\nIf you do NOT need a tool, reply normally. Never output a tool_call tag in your final answer.")
    return "\n".join(lines)


def _inject_tool_prompt(messages: list[dict], tools: list[dict]) -> list[dict]:
    tool_block = _build_tool_prompt(tools)
    patched = []
    injected = False
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
        arguments = data.get("arguments", {})
        if not name:
            return None
        return {"name": name, "arguments": arguments}
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_thinking(content: str) -> tuple[str, str]:
    """Extract <think>...</think> blocks. Returns (thinking_text, clean_content)."""
    thinking_parts = []
    think_matches = re.findall(r"<think>(.*?)</think>", content, re.DOTALL)
    for t in think_matches:
        thinking_parts.append(t.strip())
    clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return "\n\n".join(thinking_parts), clean


async def chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    stream: bool = False,
    user_cfg: dict | None = None,
) -> dict:
    """
    Returns: {
        "model": str,
        "response": OpenAI response object,
        "used_prompt_tools": bool,
        "thinking": str,
        "token_usage": {"prompt": int, "completion": int, "total": int},
    }
    user_cfg: per-user settings that override global settings (API keys, model prefs, etc.)
    """
    import settings_store
    cfg = settings_store.load()
    if user_cfg:
        cfg = {**cfg, **user_cfg}  # user settings take priority

    # ── Groq path (ultra-fast Llama) ──────────────────────────────────────────
    if cfg.get("groq_enabled"):
        groq_key   = cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY", "")
        groq_model = cfg.get("groq_model", "llama-3.3-70b-versatile")
        groq_client = AsyncOpenAI(
            base_url = "https://api.groq.com/openai/v1",
            api_key  = groq_key or "missing",
        )
        logger.info("⚡ Using Groq: %s", groq_model)
        try:
            kwargs: dict = {"model": groq_model, "messages": messages, "timeout": 30}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if stream:
                kwargs["stream"] = True
            response = await groq_client.chat.completions.create(**kwargs)
            thinking = ""
            if not stream:
                content = response.choices[0].message.content or ""
                thinking, clean = extract_thinking(content)
                if clean != content:
                    response.choices[0].message.content = clean
            usage = getattr(response, "usage", None)
            return {
                "model": f"groq/{groq_model}",
                "response": response,
                "used_prompt_tools": False,
                "thinking": thinking,
                "token_usage": {
                    "prompt":     getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total":      getattr(usage, "total_tokens", 0) if usage else 0,
                },
            }
        except Exception as exc:
            logger.error("Groq error: %s — falling back to OpenRouter", exc)

    # ── Ollama path ────────────────────────────────────────────────────────────
    if cfg.get("ollama_enabled"):
        ollama_url   = cfg.get("ollama_url", "http://localhost:11434")
        ollama_model = cfg.get("ollama_model", "llama3.2")
        ol_client = _make_ollama_client(ollama_url)
        logger.info("🦙 Using Ollama: %s @ %s", ollama_model, ollama_url)
        try:
            kwargs: dict = {
                "model": ollama_model,
                "messages": messages,
                "timeout": 120,
            }
            if tools:
                # Ollama supports function calling for most models
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if stream:
                kwargs["stream"] = True

            response = await ol_client.chat.completions.create(**kwargs)

            thinking = ""
            if not stream:
                content = response.choices[0].message.content or ""
                thinking, clean = extract_thinking(content)
                if clean != content:
                    response.choices[0].message.content = clean

            usage = getattr(response, "usage", None)
            token_usage = {
                "prompt":     getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                "total":      getattr(usage, "total_tokens", 0) if usage else 0,
            }
            return {
                "model": f"ollama/{ollama_model}",
                "response": response,
                "used_prompt_tools": False,
                "thinking": thinking,
                "token_usage": token_usage,
            }
        except Exception as exc:
            logger.error("Ollama error: %s — falling back to OpenRouter", exc)
            # Fall through to OpenRouter

    # ── OpenRouter path ────────────────────────────────────────────────────────
    api_key = cfg.get("openrouter_api_key") or os.getenv("OPENROUTER_API_KEY", "")
    or_client = _make_openrouter_client(api_key)

    preferred = cfg.get("preferred_model", "auto")
    model_list = MODELS if preferred in ("auto", "", None) else [preferred] + MODELS

    last_error = None

    for model in model_list:
        if not _is_available(model):
            logger.debug("⏭  Skipping %s (cooling down)", model)
            continue

        use_prompt_tools = model in _NO_FUNCTION_CALLING
        is_reasoning     = model in _REASONING_MODELS

        try:
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "timeout": 90,
            }

            if tools:
                if use_prompt_tools:
                    kwargs["messages"] = _inject_tool_prompt(messages, tools)
                else:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

            if is_reasoning:
                kwargs["max_tokens"] = 8000

            if stream:
                kwargs["stream"] = True

            logger.info("🔄 Trying %s (prompt_tools=%s, reasoning=%s)", model, use_prompt_tools, is_reasoning)
            response = await or_client.chat.completions.create(**kwargs)

            thinking = ""
            if not stream:
                choice = response.choices[0]
                content = choice.message.content or ""
                has_tool_calls = bool(getattr(choice.message, "tool_calls", None))

                thinking, clean_content = extract_thinking(content)
                if clean_content != content:
                    choice.message.content = clean_content
                    content = clean_content

                if use_prompt_tools and tools and not has_tool_calls:
                    ptc = _parse_prompt_tool_call(content)
                    if ptc is not None:
                        logger.info("✓ %s (prompt tool: %s)", model, ptc["name"])
                        usage = getattr(response, "usage", None)
                        return {
                            "model": model,
                            "response": response,
                            "used_prompt_tools": True,
                            "prompt_tool_call": ptc,
                            "thinking": thinking,
                            "token_usage": {
                                "prompt":     getattr(usage, "prompt_tokens", 0) if usage else 0,
                                "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                                "total":      getattr(usage, "total_tokens", 0) if usage else 0,
                            },
                        }

                if not content and not has_tool_calls:
                    logger.warning("⚠  %s returned empty — trying next", model)
                    continue

            logger.info("✓ %s answered", model)
            usage = getattr(response, "usage", None)
            return {
                "model": model,
                "response": response,
                "used_prompt_tools": use_prompt_tools,
                "thinking": thinking if not stream else "",
                "token_usage": {
                    "prompt":     getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total":      getattr(usage, "total_tokens", 0) if usage else 0,
                },
            }

        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate limit" in msg.lower():
                _cooldown(model)
            else:
                logger.warning("✗ %s failed: %s", model, exc)
            last_error = exc

    raise RuntimeError(f"All models exhausted. Last error: {last_error}")
