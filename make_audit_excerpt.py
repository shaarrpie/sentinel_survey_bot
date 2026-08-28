#!/usr/bin/env python3
"""Create a truncation-safe Sentinel audit excerpt.

Reads the actual source files directly (no aggregate line-number guessing),
keeps complete content.js / sentinel_traces.py / examples/mock_form_bot.py, and
pulls function-scoped slices of the router-autostart and /decide pipeline.

Usage: python make_audit_excerpt.py
Output: audit_excerpt.txt
"""
import os
from pathlib import Path

ROOT = Path(__file__).parent


def lines_of(rel):
    return (ROOT / rel).read_text(encoding="utf-8").splitlines()


def complete_file(rel, lines):
    return (
        f"\n# ================================================================\n"
        f"# COMPLETE FILE: {rel}\n"
        f"# ================================================================\n"
        + "\n".join(lines) + "\n"
    )


def numbered(lines, start, end):
    """1-based inclusive slice with line numbers (start/end are file-local)."""
    start = max(1, start)
    end = min(len(lines), end)
    width = len(str(end))
    return "\n".join(f"{n:>{width}}: {lines[n-1]}" for n in range(start, end + 1))


def slice_from(lines, needle, before=3, after=0):
    """Return (file-local_start, file-local_end) around the line containing
    needle; returns (-1,-1) if not found."""
    for i, ln in enumerate(lines):
        if needle in ln:
            return (i + 1 - before, i + 1 + after)
    return (-1, -1)


out = []
out.append("SENTINEL LEAN AUDIT EXCERPT (round 30)")
out.append("")

# ── content.js (complete) ─────────────────────────────────────────
content = lines_of("extension/content.js")
out.append(complete_file("extension/content.js", content))

# ── backend.py: config + router autostart + heuristic + /decide + omni ──
backend = lines_of("backend.py")
# 1) imports + config needed by the router block (file-local 1..80)
out.append(
    "\n# ================================================================\n"
    "# backend.py — imports/config + router autostart (lines 1-80)\n"
    "# ================================================================\n"
    + numbered(backend, 1, 80)
)
# 2) heuristic engine (from try_heuristic to just before /decide)
hs = slice_from(backend, "def try_heuristic", before=6)
out.append(
    "\n# ================================================================\n"
    "# backend.py — heuristic engine (try_heuristic + helpers)\n"
    "# ================================================================\n"
    + numbered(backend, hs[0], hs[1])
    if hs[0] > 0
    else "\n# MISSING try_heuristic\n"
)
# 3) /decide handler + LLM structured/raw fallback
dd = slice_from(backend, '@app.post("/decide")')
fin = slice_from(backend, "def _finish", before=1)
out.append(
    "\n# ================================================================\n"
    "# backend.py — /decide handler + structured/raw LLM fallback\n"
    "# ================================================================\n"
    + numbered(backend, dd[0], fin[1] if fin[0] > 0 else min(dd[0] + 260, len(backend)))
    if dd[0] > 0
    else "\n# MISSING /decide\n"
)
# 4) omni detail endpoints
oe = slice_from(backend, "def _key_hint", before=1)
out.append(
    "\n# ================================================================\n"
    "# backend.py — omni detail panel endpoints\n"
    "# ================================================================\n"
    + numbered(backend, oe[0], len(backend))
    if oe[0] > 0
    else "\n# MISSING omni endpoints\n"
)

# ── sentinel_traces.py (complete) — omni_call + note_omni_call + probe ──
traces = lines_of("sentinel_traces.py")
out.append(complete_file("sentinel_traces.py", traces))

# ── bot.py: duplicate router autostart ────────────────────────────
bot = lines_of("bot.py")
bs = slice_from(bot, "def start_freellmapi_server", before=8, after=40)
out.append(
    "\n# ================================================================\n"
    "# bot.py — router autostart block\n"
    "# ================================================================\n"
    + numbered(bot, bs[0], bs[1])
    if bs[0] > 0
    else "\n# MISSING bot.py router autostart\n"
)

# ── examples/mock_form_bot.py (complete, newer) ─────────────────────
sa = lines_of("examples/mock_form_bot.py")
out.append(complete_file("examples/mock_form_bot.py", sa))

# ── omni router docs (the only omni source in this workspace) ─────
docs = lines_of("omniroute_docs.md")
out.append(complete_file("omniroute_docs.md", docs))

text = "\n".join(out)
dest = ROOT / "audit_excerpt.txt"
dest.write_text(text, encoding="utf-8", newline="\n")
size = dest.stat().st_size
print(f"wrote {dest} ({size:,} bytes)")
print("NOTE: no standalone omni router source/package.json exists inside this")
print("workspace — router is external at FREELLM_DIR. Docs included instead.")
if size > 180_000:
    print("warning: excerpt still large; split content.js vs backend into two pastes")
