"""Deterministic fallback used only when the LLM route is unavailable.

Element-map schema (the ONE contract every producer/consumer shares —
see README "Element map schema"):

- ``tag``        lowercase DOM tag (input, label, select, textarea, ...)
- ``type``       semantic type. Producers emit the RAW html input type for
                 inputs ("radio", "checkbox", "text", ...) — content.js
                 derives it from the associated control, core.py from
                 ``el.type``. Older copies of the docstring claimed a
                 high-level "input" category; that was wrong and is retired.
- ``role``       ARIA role (radio/checkbox on div-based widgets)
- ``input_type`` optional alias some producers emit for the raw type
- ``checked``    bool, for radio/checkbox
- ``value``      current value, for inputs/selects/editables
- ``options``    [{value, text, disabled}], for <select>
- ``editable``   true for contenteditable nodes

``real_input_kind()`` is the single place that classifies an entry and is
deliberately tolerant: it accepts the extension payload, the core.py map,
and legacy shapes alike. If a producer stops conforming, break here, not in
three consumers.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


SCREEN_OUT = {"advertising", "market research", "marketing", "mr", "mkt"}
NONE_WORDS = {"none", "none of the above", "no", "neither", "not applicable",
              "prefer not to say", "don't know", "not sure"}
NON_TEXT_INPUTS = {"hidden", "button", "submit", "reset", "image", "file"}
TEXT_KINDS = {"text", "email", "tel", "number", "date", "datetime-local",
              "time", "month", "week", "password", "url", "search", "range"}


def real_input_kind(element: dict[str, Any]) -> str:
    """Classify one element-map entry.

    Returns: radio | checkbox | select | textarea | button | label |
    editable | a member of TEXT_KINDS | <tag> | "other".
    """
    el_type = str(element.get("type") or "").strip().lower()
    # Legacy producer shape: type="input" as a HIGH-LEVEL category with the
    # real type in input_type (the old docstring's contract). "input" is a
    # category, not a raw type — when it's all `type` has to say, prefer
    # input_type. Current producers (content.js, core.py) emit the raw
    # type, so this branch is compatibility, not the hot path.
    if el_type == "input":
        el_type = str(element.get("input_type") or "").strip().lower()
    role = str(element.get("role") or "").strip().lower()
    tag = str(element.get("tag") or "").strip().lower()

    if el_type in ("radio", "checkbox"):
        return el_type
    if role in ("radio", "checkbox"):
        return role
    if tag == "select" or el_type == "select":
        return "select"
    if tag == "textarea" or el_type == "textarea":
        return "textarea"
    if tag == "button" or el_type in ("button", "submit", "reset", "image", "file"):
        return "button"
    if tag == "input" or el_type in TEXT_KINDS | NON_TEXT_INPUTS:
        return el_type or "text"
    if tag == "label":
        return "label"
    if element.get("editable"):
        return "editable"
    if element.get("options"):
        return "select"
    return tag or "other"


def key_for(element: dict[str, Any]) -> str:
    return str(element.get("name") or element.get("sid") or element.get("id"))


def normalized_options(element: dict[str, Any]) -> list[dict[str, Any]]:
    options = element.get("options") or []
    return [o for o in options if not o.get("disabled") and str(o.get("value", ""))]


def fill_value(element: dict[str, Any]) -> str:
    name = str(element.get("name") or "").lower()
    text = str(element.get("text") or "").lower()
    kind = real_input_kind(element)
    clue = f"{name} {text}"

    if "age" in clue:
        return "32"
    if "postal" in clue or "zip" in clue:
        return "400001"
    if "email" in clue:
        return "tester@example.com"
    if kind == "date":
        return "1994-01-01"
    if kind == "datetime-local":
        return "2026-01-15T12:00"
    if kind == "time":
        return "12:00"
    if kind in {"number", "range"}:
        return "5"
    if element.get("editable") or kind == "textarea":
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
        if real_input_kind(element) in {"radio", "checkbox"}:
            groups[key_for(element)].append(element)

    actions: list[dict[str, Any]] = []
    handled_groups: set[str] = set()
    unsupported: list[dict[str, Any]] = []

    for element in actionable:
        kind = real_input_kind(element)

        if kind in {"radio", "checkbox"}:
            group_key = key_for(element)
            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            group = groups[group_key]

            if kind == "radio":
                if any(bool(item.get("checked")) for item in group):
                    continue
                pick = choose_radio(group)
                actions.append({
                    "action_type": "click",
                    "element_id": pick["id"],
                    "reasoning": f"answer radio group {group_key}",
                })
            else:
                local_context = " ".join(
                    str(item.get("context") or item.get("text") or "")
                    for item in group
                ).lower()
                select_all = "select all" in local_context or (
                    len([g for g in groups.values()
                         if g and real_input_kind(g[0]) == "checkbox"]) == 1 and
                    "select all" in page_text.lower()
                )
                safe = [
                    item for item in group
                    if str(item.get("option_value") or item.get("text") or "")
                    .strip().lower() not in NONE_WORDS
                ]
                if select_all:
                    picks = [item for item in safe if not item.get("checked")]
                else:
                    if any(bool(item.get("checked")) for item in group):
                        continue
                    picks = safe[:1]

                for pick in picks:
                    actions.append({
                        "action_type": "click",
                        "element_id": pick["id"],
                        "reasoning": f"answer checkbox group {group_key}",
                    })
            continue

        if kind == "select":
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

        if kind in TEXT_KINDS | {"editable", "textarea"}:
            if str(element.get("value") or "").strip():
                continue
            if kind in NON_TEXT_INPUTS:
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
