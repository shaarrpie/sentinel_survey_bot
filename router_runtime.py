from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


class RouterRuntime:
    """Owns the external omni router process and its real health.

    Replaces the import-time `_ensure_omni_router()` side effect: startup is
    explicit (called from FastAPI lifespan), the spawned process is tracked
    so shutdown can clean it up, and `health()` distinguishes "port open"
    from "the OpenAI-compatible API actually answers".
    """

    def __init__(self, directory: str, base_url: str, api_key: str,
                 model: str, npm_script: str = "dev"):
        self.directory = Path(directory).expanduser().resolve() if directory else None
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.npm_script = npm_script
        self.process: subprocess.Popen | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self._lock = threading.Lock()
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.4):
                return True
        except OSError:
            return False

    def _command(self) -> list[str]:
        override = os.getenv("OMNI_ROUTER_NPM", "").strip()
        npm = override or shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise FileNotFoundError(
                "npm was not found. Install Node.js or set OMNI_ROUTER_NPM "
                "to an absolute npm.cmd path."
            )
        if override and not Path(override).is_file():
            raise FileNotFoundError(f"OMNI_ROUTER_NPM does not exist: {override}")

        if os.name == "nt":
            # `npm` is a .cmd shim; CreateProcess can't launch .cmd files
            # without a shell (WinError 2 — the round-29 bug).
            comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
            if not comspec:
                raise FileNotFoundError("cmd.exe was not found")
            return [comspec, "/d", "/c", npm, "run", self.npm_script]
        return [npm, "run", self.npm_script]

    def start(self, timeout: float = 30.0) -> bool:
        with self._lock:
            if self._port_open():
                return True
            if not self.directory or not self.directory.is_dir():
                self.last_error = f"router directory missing: {self.directory}"
                return False
            if not (self.directory / "package.json").is_file():
                self.last_error = f"package.json missing: {self.directory}"
                return False

            try:
                log_path = self.directory / "sentinel-router.log"
                log_file = log_path.open("a", encoding="utf-8")
                self.process = subprocess.Popen(
                    self._command(),
                    cwd=str(self.directory),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP
                        if os.name == "nt" else 0
                    ),
                )
                self.started_at = time.time()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        self.last_error = (
                            f"router exited with code {self.process.returncode}; "
                            f"see {log_path}"
                        )
                        return False
                    if self._port_open():
                        self.last_error = None
                        return True
                    time.sleep(0.5)
                self.last_error = f"router did not open {self.host}:{self.port} in {timeout}s"
                return False
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                return False

    def stop(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()

    def health(self) -> dict:
        port_open = self._port_open()
        api_ready = False
        api_error = None
        if port_open:
            try:
                import httpx
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                response = httpx.get(
                    f"{self.base_url}/models",
                    headers=headers,
                    timeout=2.0,
                )
                api_ready = response.is_success
                if not api_ready:
                    api_error = f"HTTP {response.status_code}"
            except Exception as error:
                api_error = f"{type(error).__name__}: {error}"

        return {
            "configured": bool(self.directory and self.base_url),
            "directory": str(self.directory) if self.directory else None,
            "base_url": self.base_url,
            "host": self.host,
            "port": self.port,
            "port_open": port_open,
            "api_ready": api_ready,
            "api_error": api_error,
            "process": {
                "pid": self.process.pid,
                "alive": self.process.poll() is None,
                "returncode": self.process.poll(),
            } if self.process else None,
            "model": self.model,
            "last_error": self.last_error,
            "mode": "router" if api_ready else "heuristic",
        }
