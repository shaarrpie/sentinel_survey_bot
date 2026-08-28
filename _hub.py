# -*- coding: utf-8 -*-
import io
p = r"c:\Users\tiajungba\.gemini\antigravity-ide\scratch\sentinel_survey_bot\core.py"
src = io.open(p, encoding="utf-8").read()
log=[]
def rep(old,new,tag):
    global src
    if old not in src:
        log.append("MISS:"+tag); return
    src = src.replace(old,new,1); log.append("ok:"+tag)

# 1) import urlparse
rep("from pathlib import Path\nfrom typing import List, Optional, Literal",
"from pathlib import Path\nfrom typing import List, Optional, Literal\nfrom urllib.parse import urlparse", "import_urlparse")

# 2) panel-hub domain list + helper (module level)
rep("logger = logging.getLogger(__name__)\n\nclass Action(BaseModel):",
"""logger = logging.getLogger(__name__)

# ── survey-routing / panel login hubs ──────────────────────────────
# Landing on one of these means the survey TERMINATED the session and
# bounced it back to the panel for a human to re-route. The bot must
# STOP and hand control back — never fill out a login wall.
PANEL_HUB_DOMAINS = ()

def is_survey_router_hub(url: str) -> bool:
    try:
        u = (url or "").lower().strip()
        host = urlparse(u).netloc or u
    except Exception:
        host = (url or "").lower()
    host = host.replace("www.", "").split(":")[0]
    return any(host == d or host.endswith("." + d) for d in PANEL_HUB_DOMAINS)

class Action(BaseModel):""", "panel_hub_const")

# 3) _check_response also flags panel hubs
rep('''        try:
            url = response.url.lower()
            if any(x in url for x in ["disqualified", "screenout", "terminate", "quota_full"]):
                self.disqualified = True
        except Exception as e:
            print(f"[!] Response check error: {e}")''',
'''        try:
            url = response.url.lower()
            if (any(x in url for x in ["disqualified", "screenout", "terminate", "quota_full"])
                    or is_survey_router_hub(url)):
                self.disqualified = True
        except Exception as e:
            print(f"[!] Response check error: {e}")''', "check_response_hub")

# 4) is_disqualified includes panel-hub host
rep('''    def is_disqualified(self) -> bool:
        url = self.page.url.lower()
        text = self.page.inner_text("body").lower()[:2000]
        flags = ["disqualified", "screenout", "not qualify", "quota full", "reward=0", "terminated"]
        return any(f in url or f in text for f in flags)''',
'''    def is_disqualified(self) -> bool:
        url = self.page.url.lower()
        text = self.page.inner_text("body").lower()[:2000]
        flags = ["disqualified", "screenout", "not qualify", "quota full", "reward=0", "terminated"]
        return any(f in url or f in text for f in flags) or is_survey_router_hub(url)''', "is_disqualified_hub")

io.open(p, "w", encoding="utf-8", newline="\n").write(src)
io.open(r"c:\Users\tiajungba\.gemini\antigravity-ide\scratch\sentinel_survey_bot\_chk.txt", "w", encoding="utf-8").write("\n".join(log))
print("\n".join(log))