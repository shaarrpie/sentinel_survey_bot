"""Deterministic fallback used only when the LLM route is unavailable."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


SCREEN_OUT = {"advertising", "market research", "marketing", "mr", "mkt"}
NONE_WORDS = {"none", "none of the above", "neither", "not applicable"}
NON_TEXT_INPUTS = {"hidden", "button", "submit", "reset", "image", "file"}


def key_for(element: dict[str, Any]) -> str:
    return str(element.get("name") or element.get("sid") or element.get("id"))


def normalized_options(element: dict[str, Any]) -> list[dict[str, Any]]:
    options = element.get("options") or []
    return [o for o in options if not o.get("disabled") and str(o.get("value", ""))]


def fill_value(element: dict[str, Any]) -> str:
    name = str(element.get("name") or "").lower()
    text = str(element.get("text") or "").lower()
    input_type = str(element.get("type") or "text").lower()
    clue = f"{name} {text}"

    if "age" in clue:
        return "32"
    if "postal" in clue or "zip" in clue:
        return "400001"
    if "email" in clue:
        return "tester@example.com"
    if input_type == "date":
        return "1994-01-01"
    if input_type == "datetime-local":
        return "2026-01-15T12:00"
    if input_type == "time":
        return "12:00"
    if input_type in {"number", "range"}:
        return "5"
    if element.get("editable") or element.get("tag") == "textarea":
        return "I value reliable products, clear information, and fair pricing."
    return "N/A"


def choose_radio(group: list[dict[str, Any]]) -> dict[str, Any]:
    def label(item: dict[str, Any]) -> str:
        return str(item.get("option_value") or item.get("text") or "").strip().lower()

    safe = [item for item in group if label(item) not in SCREEN_OUT]
    pool = safe or group
    for item in pool:
        if label(item) in NONE_WORDS:
            return item
    return pool[0]


def heuristic_decide(elements: list[dict[str, Any]], page_text: str = "") -> dict[str, Any]:
    actionable = [e for e in elements if not e.get("disabled")]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for element in actionable:
        if element.get("type") in {"radio", "checkbox"}:
            groups[key_for(element)].append(element)

    actions: list[dict[str, Any]] = []
    handled_groups: set[str] = set()
    unsupported: list[dict[str, Any]] = []

    for element in actionable:
        tag = str(element.get("tag") or "")
        input_type = str(element.get("type") or "").lower()

        if input_type in {"radio", "checkbox"}:
            group_key = key_for(element)
            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            group = groups[group_key]
            if any(bool(item.get("checked")) for item in group):
                continue

            if input_type == "radio":
                pick = choose_radio(group)
                actions.append({
                    "action_type": "click",
                    "element_id": pick["id"],
                    "reasoning": f"answer radio group {group_key}",
                })
            else:
                select_all = "select all" in page_text.lower()
                safe = [g for g in group
                        if str(g.get("option_value") or g.get("text") or "").strip().lower()
                        not in NONE_WORDS]
                picks = safe if select_all else safe[:1]
                for pick in picks:
                    actions.append({
                        "action_type": "click",
                        "element_id": pick["id"],
                        "reasoning": f"answer checkbox group {group_key}",
                    })
            continue

        if tag == "select":
            if str(element.get("value") or "").strip():
                continue
            options = normalized_options(element)
            if options:
                pick = options[0]
                actions.append({
                    "action_type": "select_option",
                    "element_id": element["id"],
                    "value": pick["text"] or pick["value"],
                    "reasoning": f"select valid option for {key_for(element)}",
                })
            else:
                unsupported.append(element)
            continue

        if element.get("editable") or tag == "textarea" or tag == "input":
            if str(element.get("value") or "").strip():
                continue
            if tag == "input" and input_type in NON_TEXT_INPUTS:
                continue
            actions.append({
                "action_type": "type",
                "element_id": element["id"],
                "value": fill_value(element),
                "reasoning": f"fill {key_for(element)}",
            })

    if actions:
        return {
            "question_type": "mixed",
            "confidence": 0.4,
            "page_summary": f"heuristic: {len(actions)} action(s)",
            "actions": actions,
            "memory_note": None,
            "source": "heuristic",
        }

    if unsupported:
        return {
            "question_type": "unknown",
            "confidence": 0.1,
            "page_summary": f"heuristic blocked by {len(unsupported)} unsupported field(s)",
            "actions": [{
                "action_type": "human_help",
                "reasoning": "unsupported required field; refusing to skip it",
            }],
            "memory_note": None,
            "source": "heuristic",
        }

    return {
        "question_type": "unknown",
        "confidence": 0.4,
        "page_summary": "heuristic: no unanswered mapped fields",
        "actions": [{
            "action_type": "next",
            "reasoning": "page answered -> next",
        }],
        "memory_note": None,
        "source": "heuristic",
    }

