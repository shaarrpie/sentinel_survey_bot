"""
Sentinel trace bus.
- Thread-safe ring buffer, monotonic seq, 1000 events.
- GET /traces?since=N  (poll endpoint for the debug console)
- omni_call() context manager — wrap every LLM call with it.
- probe_omni() — call once at startup with your router instance.
- snapshot() — merge into your existing /status response.
Framework-agnostic core; FastAPI wiring at the bottom.
"""
import copy
import itertools
import threading
import time
import uuid
from contextlib import contextmanager


class TraceBus:
    MAX = 1000

    def __init__(self):
        self._lock = threading.Lock()
        self._buf = []                        # newest last
        self._seq = itertools.count(1)
        self.boot_id = uuid.uuid4().hex[:8]
        self.started_at = time.time()
        self._lat = []                        # rolling window for p50
        self.omni = {
            "loaded": False,
            "provider": None,                  # e.g. "openrouter"
            "model": None,                     # e.g. "anthropic/claude-sonnet-4.5"
            "base_url": None,
            "api_key_set": False,
            "calls": 0,
            "errors": 0,
            "last_ms": None,
            "p50_ms": None,
            "last_call_at": None,
            "last_error": None,
            # Round-eleven detail panel fields:
            "models": {},                      # model -> {calls, errors, total_ms, last_ms}
            "tokens_in": 0,
            "tokens_out": 0,
            "fallback_chain": [],              # ordered model ids (if router exposes one)
            "recent_calls": [],                # last 20: {t, cycle, model, ms, ok, err}
            "latency_history": [],             # last 20 ms values for the sparkline
            "last_ping": None,                 # {t, ms, ok, reply}
        }

    def record(self, src, kind, msg, data=None, level="info", ms=None):
        """src: content|backend|omni|sys — kind: freeform verb — data: dict"""
        ev = {
            "seq": next(self._seq),
            "t": time.time(),
            "src": src,
            "kind": kind,
            "level": level,                    # info|warn|error
            "msg": msg,
            "data": data,
            "ms": round(ms, 1) if ms is not None else None,
        }
        with self._lock:
            self._buf.append(ev)
            if len(self._buf) > self.MAX:
                del self._buf[: len(self._buf) - self.MAX]
        return ev

    def since(self, seq):
        with self._lock:
            out = [e for e in self._buf if e["seq"] > seq]
            last = self._buf[-1]["seq"] if self._buf else seq
            return last, out

    def note_omni_latency(self, ms, ok, err=None, provider=None, model=None):
        o = self.omni
        o["calls"] += 1
        o["last_ms"] = round(ms, 1)
        o["last_call_at"] = time.time()
        if provider:
            o["provider"] = provider
        if model:
            o["model"] = model
        if not ok:
            o["errors"] += 1
            o["last_error"] = err
        self._lat.append(ms)
        if len(self._lat) > 200:
            del self._lat[: len(self._lat) - 200]
        s = sorted(self._lat)
        o["p50_ms"] = round(s[len(s) // 2], 1)

    def note_omni_call(self, provider=None, model=None, ms=0, ok=True,
                       err=None, cycle=None, tokens_in=0, tokens_out=0):
        """Detail-panel feed: aggregates per-model stats, latency history and
        recent-call log on top of what note_omni_latency records."""
        self.note_omni_latency(ms, ok, err, provider, model)   # base counters
        m = model or "?"
        md = self.omni["models"].setdefault(m, {
            "calls": 0, "errors": 0, "total_ms": 0.0, "last_ms": None})
        md["calls"] += 1
        md["total_ms"] += ms
        md["last_ms"] = round(ms, 1)
        if not ok:
            md["errors"] += 1
        self.omni["tokens_in"] += tokens_in or 0
        self.omni["tokens_out"] += tokens_out or 0
        hist = self.omni["latency_history"]
        hist.append(round(ms, 1))
        if len(hist) > 20:
            del hist[: len(hist) - 20]
        rc = self.omni["recent_calls"]
        rc.append({"t": time.time(), "cycle": cycle, "model": m,
                   "ms": round(ms, 1), "ok": ok, "err": err})
        if len(rc) > 20:
            del rc[: len(rc) - 20]

    def snapshot(self):
        with self._lock:
            count = len(self._buf)
            last = self._buf[-1]["seq"] if self._buf else 0
            omni = copy.deepcopy(self.omni)   # nested mutables — copy under lock
        return {
            "boot_id": self.boot_id,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "trace_buffer": count,
            "last_seq": last,
            "omni": omni,
        }
bus = TraceBus()


@contextmanager
def omni_call(provider=None, model=None, cycle=None):
    """Wrap the actual LLM request. Records request + result + latency."""
    t0 = time.time()
    bus.record("omni", "call",
               f"LLM request -> {provider or '?'}/{model or '?'}",
               {"provider": provider, "model": model, "cycle": cycle})
    ok, err = True, None
    try:
        yield
    except Exception as e:
        ok, err = False, str(e)
        raise
    finally:
        ms = (time.time() - t0) * 1000
        bus.note_omni_call(provider, model, ms, ok, err, cycle)
        bus.record("omni", "result" if ok else "error",
                   f"LLM response in {ms:.0f} ms" if ok else f"LLM failed: {err}",
                   {"provider": provider, "model": model, "cycle": cycle,
                    "ms": round(ms, 1)},
                   level="info" if ok else "error", ms=ms)


def probe_omni(router_obj=None):
    """Call once at startup with your omni router instance. Adapts to
    common shapes: .status(), .provider/.model attrs, or a plain dict."""
    o = bus.omni
    try:
        if router_obj is None:
            raise RuntimeError("no router instance passed to probe_omni()")
        info = router_obj.status() if hasattr(router_obj, "status") else {}
        if isinstance(router_obj, dict):
            info = router_obj
        info = info or {}
        o["loaded"] = True
        o["provider"] = info.get("provider") or getattr(router_obj, "provider", None)
        o["model"] = info.get("model") or getattr(router_obj, "model", None)
        o["base_url"] = info.get("base_url") or getattr(router_obj, "base_url", None)
        o["api_key_set"] = bool(info.get("api_key_set",
                                         getattr(router_obj, "api_key", None)))
    except Exception as e:
        o["loaded"] = False
        o["last_error"] = f"probe failed: {e}"
    bus.record("sys", "state",
               f"omni router {'LOADED' if o['loaded'] else 'NOT LOADED'}"
               + (f" — {o['provider']}/{o['model']}" if o["loaded"] else ""),
               dict(o), level="info" if o["loaded"] else "error")
# ── FastAPI wiring ─────────────────────────────────────────────────────────
# from sentinel_traces import bus, router as trace_router, trace_middleware
# app.include_router(trace_router)
# app.middleware("http")(trace_middleware)   # or register via decorator
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/traces")
async def traces(since: int = 0):
    last, events = bus.since(since)
    return {
        "last_seq": last,
        "events": events,
        "boot": {"boot_id": bus.boot_id, "started_at": bus.started_at},
    }


async def trace_middleware(request: Request, call_next):
    # Auto-instrument every backend route EXCEPT the poll endpoint itself.
    # Exceptions from call_next are recorded too, then re-raised (round 30).
    if request.url.path == "/traces":
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        elapsed = (time.perf_counter() - started) * 1000
        bus.record(
            "backend", "http_error",
            f"{request.method} {request.url.path} raised {type(error).__name__}",
            {"path": request.url.path, "error": str(error)},
            level="error", ms=elapsed,
        )
        raise
    elapsed = (time.perf_counter() - started) * 1000
    bus.record(
        "backend", "http",
        f"{request.method} {request.url.path} -> {response.status_code}",
        {"path": request.url.path, "status": response.status_code},
        level="warn" if response.status_code >= 400 else "info",
        ms=elapsed,
    )
    return response
