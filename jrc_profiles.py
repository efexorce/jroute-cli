"""JRC route profilleri (yalnızca standart kütüphane).

router.py ve jrc_gate.py ortak kullanır; odoo / hermes gibi typesafe_sdk
olmayan ortamlar da bu modülü içe aktarabilsin diye ayrı tutulur.
"""

from __future__ import annotations

EFFORT_ORDER = ["low", "medium", "high", "xhigh"]


# Route profilleri: Tek adımda (araç + alt model + varsayılan efor)
ROUTE_PROFILES: dict[str, dict[str, str]] = {
    "agy_flash": {
        "tool": "agy",
        "model": "gemini-3.8-flash-medium",
        "default_effort": "low",
        "label": "Antigravity · Gemini 3.8 Flash",
        "criteria": (
            "Quick lookups, status checks, log inspection, simple shell commands, "
            "or trivial single-line questions that need fast execution."
        ),
    },
    "agy_argon": {
        "tool": "agy",
        "model": "gemini-4-argon-medium",
        "default_effort": "medium",
        "label": "Antigravity · Gemini 4 Argon",
        "criteria": (
            "Full-stack web development, UI/frontend design, API/webhook integration, "
            "automation bots (Telegram/WhatsApp), deployment, or multi-step workspace tasks."
        ),
    },
    "agy_carbon": {
        "tool": "agy",
        "model": "gemini-4-carbon-medium",
        "default_effort": "medium",
        "label": "Antigravity · Gemini 4 Carbon",
        "criteria": (
            "Balanced high-quality reasoning, visual product editing, structured tasks, "
            "and general engineering with Gemini 4 Carbon."
        ),
    },
    "agy_hydrogen": {
        "tool": "agy",
        "model": "gemini-4-hydrogen-high",
        "default_effort": "high",
        "label": "Antigravity · Gemini 4 Hydrogen",
        "criteria": (
            "Deep research, multi-agent orchestration, complex algorithmic design, "
            "or system-wide investigation requiring maximum reasoning depth."
        ),
    },
    "claude_haiku": {
        "tool": "claude",
        "model": "claude-haiku-5-5",
        "default_effort": "low",
        "label": "Claude Code · Haiku 5.5",
        "criteria": (
            "Small script tweaks, formatting, README updates, memory/status checks, "
            "or lightweight single-file edits."
        ),
    },
    "claude_sonnet": {
        "tool": "claude",
        "model": "claude-sonnet-5-5",
        "default_effort": "medium",
        "label": "Claude Code · Sonnet 5.5",
        "criteria": (
            "Standard software engineering, bug fixing, feature implementation, "
            "code refactoring, writing unit tests, and clean code edits."
        ),
    },
    "claude_opus": {
        "tool": "claude",
        "model": "claude-opus-5-5",
        "default_effort": "high",
        "label": "Claude Code · Opus 5.5",
        "criteria": (
            "Complex multi-file refactoring, critical production bug debugging, "
            "database schema/ORM architecture (Odoo, PostgreSQL), and high-risk backend code."
        ),
    },
    "claude_fable": {
        "tool": "claude",
        "model": "fable",
        "default_effort": "high",
        "label": "Claude Code · Fable 5.2",
        "criteria": (
            "Flagship creative 3D/WebGL engineering, complex interactive game & animation systems, "
            "or tasks where Claude Fable is explicitly requested."
        ),
    },
}


def agy_model_for(base_model: str, effort: str) -> str:
    """agy model adındaki efor sonekini seçilen efora uyarlar."""
    suffix = "high" if effort in ("high", "xhigh") else ("low" if effort == "low" else "medium")
    if base_model.startswith("gemini-4-argon"):
        return f"gemini-4-argon-{suffix}"
    if base_model.startswith("gemini-4-carbon"):
        return f"gemini-4-carbon-{suffix}"
    if base_model.startswith("gemini-3.8-flash"):
        return f"gemini-3.8-flash-{suffix}"
    if base_model.startswith("gemini-4-hydrogen"):
        return "gemini-4-hydrogen-xhigh" if effort == "xhigh" else f"gemini-4-hydrogen-{suffix}"
    return base_model
