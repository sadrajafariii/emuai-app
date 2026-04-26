"""
settings_store.py — Persistent user settings backed by settings.json.
All settings can be changed from the UI without restarting the server.
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = Path("settings.json")

DEFAULTS: dict = {
    # Profile
    "user_name": "Friend",

    # Appearance
    "theme": "old_money",   # old_money | dark_minimal | hacker | light

    # AI Engine — OpenRouter
    "openrouter_api_key": os.getenv("OPENROUTER_API_KEY", ""),
    "preferred_model": "auto",  # "auto" = use fallback chain, or a specific model ID

    # AI Engine — Ollama (local / offline)
    "ollama_enabled": False,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.2",

    # Behaviour
    "auto_memory": True,       # AI automatically saves facts it learns about you
    "show_thinking": True,     # Show reasoning panel for DeepSeek / Qwen
    "max_iterations": int(os.getenv("MAX_TOOL_ITERATIONS", "100")),

    # Voice
    "tts_enabled": False,
    "tts_voice": "en-US-AriaNeural",
    "tts_backend": "edge-tts",  # "edge-tts" (server, high quality) or "browser" (Web Speech API)

    # Context
    "max_tokens": 4096,

    # Groq (ultra-fast free Llama)
    "groq_enabled": True,
    "groq_api_key": os.getenv("GROQ_API_KEY", ""),
    "groq_model": "llama-3.3-70b-versatile",

    # Personas
    "personas": [
        {
            "id": "assistant",
            "name": "Assistant",
            "description": "Helpful general assistant",
            "system_prompt": "",
            "builtin": True,
        },
        {
            "id": "coder",
            "name": "Code Reviewer",
            "description": "Senior engineer focused on code quality",
            "system_prompt": "You are a senior software engineer. Focus on code quality, performance, security. Be terse and technical.",
            "builtin": True,
        },
        {
            "id": "writer",
            "name": "Writing Coach",
            "description": "Helps with clear, engaging writing",
            "system_prompt": "You are a writing coach. Help with clarity, structure, and voice. Give specific actionable feedback.",
            "builtin": True,
        },
    ],

    # Session personas map: session_id -> persona_id
    "session_personas": {},
}


def load() -> dict:
    base = DEFAULTS.copy()
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            base.update(saved)
        except Exception:
            pass
    return base


def save(updates: dict) -> dict:
    current = load()
    current.update(updates)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


def get(key: str, default=None):
    return load().get(key, default)
