import os
from pathlib import Path

ROOT = Path(__file__).parent
FILES = [
    "extension/manifest.json",
    "extension/popup.html",
    "extension/popup.js",
    "extension/background.js",
    "extension/content.js",
    "extension/hub_match.js",
    "extension/hud.html",
    "extension/hud.js",
    ".env",
    "backend.py",
    "sentinel_traces.py",
    "bot.py",
    "bot_standalone.py",
    "core.py",
    "main.py",
    "gui.py",
    "answer_cache.py",
    "panel_config.py",
    "test_hub_matcher.node.js",
    "test_panel_hub_matcher.py",
    "logcli.py",
    "update_project_txt.py",
    "append_files.py",
    "smoke_survey_test.py",
    "launch.bat",
    "run_gui.bat",
    "launch_stealth_chrome.bat",
    "requirements.txt",
    "traces/debug.html",
    "traces/debug.js",
    "extension/traces/debug.html",
    "extension/traces/debug.js",
    "omniroute_docs.md",
    "survey-test.html",
]

EXCLUDE_DIRS = {"logs", "screenshots", "__pycache__", "profiles"}

def lang_for(path):
    if path.suffix == ".py":
        return "python"
    if path.suffix == ".js":
        return "javascript"
    if path.suffix == ".html":
        return "html"
    if path.suffix == ".json":
        return "json"
    if path.suffix == ".bat":
        return "batch"
    if path.suffix == ".md":
        return "markdown"
    if path.suffix == ".txt":
        return "text"
    return "text"

out = []
for rel in FILES:
    p = ROOT / rel
    if not p.exists():
        continue
    out.append("# ============================================\n")
    out.append(f"# FILE: {rel}\n")
    out.append("# ============================================\n")
    out.append(f"```{lang_for(p)}\n")
    out.append(p.read_text(encoding="utf-8"))
    if not out[-1].endswith("\n"):
        out.append("\n")
    out.append("```\n")

content = "".join(out)
(ROOT / "project.txt").write_text(content, encoding="utf-8")
(ROOT / "paste.txt").write_text(content, encoding="utf-8")
# Third alias kept current so legacy workflows pointing at
# full_project_paste.txt never read a stale aggregate again.
(ROOT / "full_project_paste.txt").write_text(content, encoding="utf-8")
print("Updated project.txt, paste.txt and full_project_paste.txt with",
      len(FILES), "files")
