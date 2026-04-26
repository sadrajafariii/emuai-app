"""
skill_router.py — Loads SKILL.md playbooks and injects the best-matching one
into the agent system prompt based on the user's message.

Skills live in ./skills/<name>/SKILL.md and follow the antigravity-awesome-skills format.
Each SKILL.md has a '## Triggers' section listing keywords that activate it.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Load skills at import time ────────────────────────────────────────────────

_SKILLS_DIR = Path(__file__).parent / "skills"

# skill_name -> {"content": str, "triggers": list[str]}
_skills: dict[str, dict] = {}


def _load_skills():
    if not _SKILLS_DIR.exists():
        logger.info("No skills/ directory found — skill router disabled")
        return

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            triggers = _parse_triggers(content)
            _skills[skill_dir.name] = {"content": content, "triggers": triggers}
            logger.info("Loaded skill: %s (%d triggers)", skill_dir.name, len(triggers))
        except Exception as exc:
            logger.warning("Failed to load skill %s: %s", skill_dir.name, exc)


def _parse_triggers(content: str) -> list[str]:
    """Extract keywords from the ## Triggers section of a SKILL.md."""
    match = re.search(r"##\s+Triggers\s*\n(.+?)(?:\n##|\Z)", content, re.DOTALL)
    if not match:
        return []
    raw = match.group(1).strip()
    # Triggers are comma-separated on one line
    triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]
    return triggers


# Load on import
_load_skills()


# ── Public API ─────────────────────────────────────────────────────────────────

def match_skill(user_message: str) -> Optional[str]:
    """
    Return the SKILL.md content for the best-matching skill, or None if no match.
    Scores each skill by how many of its trigger phrases appear in the message.
    """
    if not _skills:
        return None

    msg_lower = user_message.lower()
    best_skill = None
    best_score = 0

    for name, skill in _skills.items():
        score = sum(1 for t in skill["triggers"] if t in msg_lower)
        if score > best_score:
            best_score = score
            best_skill = name

    if best_score == 0:
        return None

    logger.info("Skill matched: %s (score=%d)", best_skill, best_score)
    return _skills[best_skill]["content"]


def list_skills() -> list[dict]:
    """Return a summary of all loaded skills for display/debugging."""
    return [
        {
            "name": name,
            "triggers": skill["triggers"][:5],  # first 5 for brevity
            "lines": len(skill["content"].splitlines()),
        }
        for name, skill in _skills.items()
    ]


def inject_skill_context(system_prompt: str, user_message: str) -> str:
    """
    If a skill matches, append its content to the system prompt.
    The skill content is appended as an additional operating instruction block.
    """
    skill_content = match_skill(user_message)
    if not skill_content:
        return system_prompt

    return (
        system_prompt
        + "\n\n---\n"
        + "## Active Skill Playbook\n"
        + "The following operating instructions apply to this specific request. "
        + "Follow them precisely — they override general guidelines for this turn.\n\n"
        + skill_content
    )
