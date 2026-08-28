"""Sentinel /decide heuristic — audit rounds 19/21/27, complete.
Batch per page, group-aware radios (the oscillator killer), trap-aware
picks, honest confidence, self-diagnosing singleton log."""

TEXT_TYPES = {"text", "number", "email", "tel", "url", ""}
SCREEN_OUT_VALUES = {"advertising", "mr", "mkt", "market research", "marketing"}
NONE_VALUES = {"none", "none of the above", "no", "neither"}


def is_answered(e):
    if e.get("type") in ("radio", "checkbox"):
        return bool(e.get("checked"))
    if e.get("tag") in ("input", "textarea", "select") or e.get("editable"):
        return str(e.get("value") or "").strip() != ""
    return True  # nav/buttons/labels-without-state: not answerable


def group_key(e):
    return e.get("name") or e.get("sid") or e.get("id")


def build_groups(elements):
    groups = {}
    for e in elements:
        if e.get("type") in ("radio", "checkbox"):
            groups.setdefault(group_key(e), []).append(e)
    return groups


def trap_aware_radio_pick(group):
    safe = [g for g in group
            if str(g.get("text", "")).strip().lower() not in SCREEN_OUT_VALUES]
    pool = safe or group
    for g in pool:                       # prefer the explicit escape hatch
        if str(g.get("text", "")).strip().lower() in NONE_VALUES:
            return g
    return pool[0]


def fill_value(e):
    name = str(e.get("name") or "").lower()
    text = str(e.get("text") or "").lower()
    if "age" in name or "age" in text:
        return "32"
    if "zip" in name or "postal" in name or "zip" in text:
        return "400001"
    if e.get("type") == "number":
        return "5"
    if e.get("editable") or e.get("tag") == "textarea":
        return "I value durability and fair pricing in the products I buy."
    return "N/A"


def heuristic_decide(elements, page_text=""):
    answerable = [e for e in elements if e.get("tag") not in ("button", "a")]
    pending = [e for e in answerable if not is_answered(e)]
    groups = build_groups(answerable)
    singletons = sum(1 for g in groups.values() if len(g) == 1)
    print(f"[heuristic] {len(groups)} radio/checkbox groups, "
          f"{singletons} singletons, {len(pending)} pending elements")
    if groups and singletons == len(groups):
        print("[heuristic] WARNING: all groups are singletons — element "
              "entries are missing `name` (ship content.js r28 label "
              "inheritance, audit 2.2/2.3)")

    actions, chosen = [], set()
    for e in pending:
        tag, typ = e.get("tag"), e.get("type", "")
        if typ == "radio":
            key = group_key(e)
            if key in chosen:
                continue
            if any(g.get("checked") for g in groups.get(key, [])):
                continue                 # group answered — oscillator killer
            pick = trap_aware_radio_pick(groups.get(key, [e]))
            chosen.add(key)
            actions.append({"action_type": "click", "element_id": pick["id"],
                            "reasoning": f"pick {pick.get('sid', pick['id'])}"})
        elif typ == "checkbox":
            key = group_key(e)
            if key in chosen:
                continue
            chosen.add(key)              # select-all-that-apply, dodge "none"
            for g in groups.get(key, [e]):
                if g.get("checked"):
                    continue
                if str(g.get("text", "")).strip().lower() in NONE_VALUES:
                    continue
                actions.append({"action_type": "click", "element_id": g["id"],
                                "reasoning": f"check {g.get('sid', g['id'])}"})
        elif tag == "select":
            actions.append({"action_type": "select_option",
                            "element_id": e["id"], "value": "Full-time",
                            "reasoning": f"select {e.get('sid', e['id'])}"})
        elif tag == "textarea" or e.get("editable") or \
                (tag == "input" and typ in TEXT_TYPES):
            actions.append({"action_type": "type", "element_id": e["id"],
                            "value": fill_value(e),
                            "reasoning": f"fill {e.get('sid', e['id'])}"})

    if actions:
        return {"question_type": "form", "confidence": 0.4,
                "page_summary": f"heuristic: {len(actions)} actions, "
                                f"{len(pending)} pending",
                "actions": actions, "memory_note": None}
    return {"question_type": "navigation", "confidence": 0.4,
            "page_summary": "heuristic: page complete -> next",
            "actions": [{"action_type": "next",
                         "reasoning": "page answered -> next"}],
            "memory_note": None}
