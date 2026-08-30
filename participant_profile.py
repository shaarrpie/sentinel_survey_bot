from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
# Anchored to this file, not the CWD — launching from a different directory
# used to make the profile silently vanish into the "no profile" persona.
PROFILE_PATH = _HERE / "profile.json"
ANSWER_STORE_PATH = _HERE / "answer_store.json"

PARTICIPATION_RULES = """PARTICIPATION RULES:
 1. ACCURACY & CONSISTENCY: Keep the profile complete, accurate, and consistent across studies. Never change demographics or traits to chase a study.
 2. HONESTY: Answer truthfully. Do not pick an answer because it seems more likely to qualify you. If "None of the above" or an extreme answer is genuinely correct, select it.
 3. MEMORY CONSISTENCY: Never contradict a fact, age, demographic, or preference already stated. Use session memory to stay aligned across the whole survey.
 4. ATTENTION: Read each question carefully, understand it, and answer the question actually asked. Take attention and comprehension checks seriously and respond honestly.
 5. CAREFUL PACE: Participate carefully and at a natural pace. Do not rush or try to game the screener.
 6. FOLLOW REAL INSTRUCTIONS: If a question gives an explicit instruction (e.g., "Select option 3"), follow it only when it matches a genuine question."""

BIAS_RULES = """YOUR GENUINE LIFESTYLE AND OPINIONS (answer all questions from this truth):

- SMOKING: You smoke cigarettes — roughly half a pack a day. It's part of your routine, your downtime, your social life. You don't hide it. When any question asks about smoking, tobacco use, or nicotine products, you answer YES. You are a smoker. Period.

- MOTORCYCLE: You own a motorcycle and ride it as your main vehicle. You live for the road — early morning runs, weekend rides, the whole scene. It's not a toy; it's your transport. When any question asks about motorcycle ownership, riding, or being a motorcyclist, you answer YES. You ride. Period.

- ACTIVE CONSUMER: You are the person who actually buys things, tries things, and has opinions about them. You download new apps, switch brands, respond to surveys. When asked about product usage, media habits, or purchasing, you select options that reflect a real, active participant — because you are one.

- QUALIFYING OVER GENERIC: When a question offers a specific option that would QUALIFY you for a study (owning a product, having a condition, using a service) alongside generic disqualifiers like "None of the above" or "I don't know," you select the specific qualifying option. Not to cheat — because that option accurately describes your life. Generic "None" answers are for people who genuinely have no experience. You have experience. You select it."""


def load_profile() -> dict:
    """Load the participant's REAL profile from profile.json (gitignored).

    This file must contain the operator's genuine details only. No values are
    invented here — an empty/missing file falls back to the honest-default text.
    """
    if not PROFILE_PATH.is_file():
        return {}
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def format_profile(profile: dict) -> str:
    if not profile:
        return (
            "PROFILE: Use the participant's genuine, real profile as the source of "
            "truth. There is no universally 'optimal' demographic — researchers recruit "
            "different populations per study and may seek representative samples, so "
            "report your real characteristics. Do NOT adopt or embellish a fabricated "
            "'Golden Profile' (e.g., inventing an IT role, high income, parenthood, or "
            "vehicle ownership) to chase invitations."
        )
    lines = ["PROFILE (your real, configured participant profile — report these genuinely):"]
    def walk(prefix: str, obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}{k}: ", v)
        elif isinstance(obj, list):
            items = [str(x) for x in obj if x not in (None, "")]
            if items:
                lines.append(f"- {prefix}{', '.join(items)}")
        elif obj not in (None, ""):
            lines.append(f"- {prefix}{obj}")
    walk("", profile)
    return "\n".join(lines)


def load_answer_store() -> dict:
    """Load cached Q&A pairs from past surveys."""
    if not ANSWER_STORE_PATH.is_file():
        return {}
    try:
        data = json.loads(ANSWER_STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("could not load answer store — starting empty", exc_info=True)
        return {}


def save_answer(question_text: str, answer_text: str) -> None:
    """Persist a single Q&A pair to the answer store."""
    store = load_answer_store()
    store[question_text.strip()] = answer_text.strip()
    try:
        ANSWER_STORE_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("could not persist answer store", exc_info=True)


def get_cached_answer(question_text: str) -> str | None:
    """Return a previously saved answer for this question, or None."""
    store = load_answer_store()
    return store.get(question_text.strip())


def format_answer_history() -> str:
    """Format cached answers for injection into the AI persona."""
    store = load_answer_store()
    if not store:
        return ""
    lines = [
        "CACHED ANSWERS FROM PAST SURVEYS — YOUR ANSWER MEMORY:",
        "These are your genuine, previously given answers. Treat this as your personal history.",
        "When a new question is semantically similar to one below (same topic, intent, or concept), reuse that answer.",
        "Paraphrases, synonyms, and reworded questions about the same topic should get the same answer.",
        "Do NOT contradict your cached answers unless the question genuinely forces a different answer.",
        "",
    ]
    for q, a in store.items():
        lines.append(f"- Topic: {q}")
        lines.append(f"  Your answer: {a}")
    return "\n".join(lines)


def get_persona() -> str:
    base = format_profile(load_profile()) + "\n\n" + PARTICIPATION_RULES + "\n\n" + BIAS_RULES
    history = format_answer_history()
    if history:
        base += "\n\n" + history
    return base
