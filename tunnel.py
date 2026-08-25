"""Put the local server on a public https URL, using cloudflared.

Two modes:

  quick   `cloudflared tunnel --url http://127.0.0.1:PORT` -- no account, no
          domain, no port forwarding, works behind CGNAT. Cloudflare hands
          back a fresh https://<random>.trycloudflare.com each time. Quick
          tunnels are best-effort with no SLA, so this is the right mode for
          "let me show someone right now", not for a link you print on a card.

  named   `cloudflared tunnel run --token ...` -- your own hostname, stable
          across restarts. Needs a Cloudflare account and a domain; you create
          the tunnel in their dashboard and paste the token into settings.json.

Either way the child process's output is relayed into this console rather than
swallowed into a log file, so you can watch it connect and see it when it
doesn't. Set CANNACABANA_TUNNEL_VERBOSE=1 for every line cloudflared emits
instead of the interesting ones.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import paths

QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NAMED_URL = re.compile(r"https?://[a-z0-9.-]+\.[a-z]{2,}", re.I)

DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-windows-amd64.exe")

_VERBOSE = os.environ.get("CANNACABANA_TUNNEL_VERBOSE") == "1"

# cloudflared logs a lot of routine INF chatter. These are the lines worth
# putting in front of someone watching the app start.
_INTERESTING = ("ERR", "WRN", "error", "failed", "Registered tunnel connection",
                "Connection ", "trycloudflare.com", "Requesting new quick",
                "unauthorized", "expired")


def _tie_lifetime_to_ours(pid: int):
    """Make Windows kill the tunnel when this process dies, however it dies.

    Ctrl-C runs stop() and everything is tidy. A hard kill, a crash, or Task
    Manager does not -- and an orphaned quick tunnel keeps a public URL open
    with nothing behind it and nobody watching. A job object with
    KILL_ON_JOB_CLOSE closes that gap: when our last handle to the job goes
    away, so does the child.

    Returns the job handle, which the caller must keep alive.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in
                    ("ReadOperationCount", "WriteOperationCount",
                     "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None

    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info),
                                       ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None

    handle = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        k32.CloseHandle(job)
        return None
    ok = k32.AssignProcessToJobObject(job, handle)
    k32.CloseHandle(handle)
    if not ok:
        k32.CloseHandle(job)
        return None
    return job


def find_binary() -> str | None:
    """cloudflared.exe, from the app folder, the data folder, or PATH."""
    for candidate in (paths.app("cloudflared.exe"), paths.data("cloudflared.exe")):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("cloudflared")


class Tunnel:
    """A running cloudflared child process."""

    def __init__(self, echo=print):
        self.echo = echo
        self.url: str | None = None
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self._ready = threading.Event()
        # Held for the life of the app: closing it kills the tunnel.
        self._job = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, port: int, mode: str = "quick", token: str = "") -> bool:
        binary = find_binary()
        if not binary:
            self.error = ("cloudflared.exe not found. Put it next to the app, "
                          f"or download it from {DOWNLOAD}")
            return False

        if mode == "named":
            if not token:
                self.error = ('tunnel is set to "named" but tunnel_token is '
                              "empty in settings.json")
                return False
            argv = [binary, "tunnel", "--no-autoupdate", "run", "--token", token]
        else:
            argv = [binary, "tunnel", "--no-autoupdate",
                    "--url", f"http://127.0.0.1:{port}"]

        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.error = f"could not start cloudflared: {e}"
            return False

        self._job = _tie_lifetime_to_ours(self.proc.pid)
        threading.Thread(target=self._pump, name="tunnel-log",
                         daemon=True).start()
        return True

    def wait_for_url(self, timeout: float = 25.0) -> str | None:
        """Block briefly so the banner can print a real URL."""
        self._ready.wait(timeout)
        return self.url

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # -- output ------------------------------------------------------------

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue

            if not self.url:
                found = QUICK_URL.search(line)
                if found:
                    self.url = found.group(0)
                    self._ready.set()
                elif "hostname" in line.lower() and "=" in line:
                    # Named tunnels announce the route they registered.
                    named = NAMED_URL.search(line.split("=", 1)[1])
                    if named:
                        self.url = named.group(0)
                        self._ready.set()

            if _VERBOSE or any(k in line for k in _INTERESTING):
                self.echo(f"  [tunnel] {line}")

        code = self.proc.poll()
        if code:
            self.error = f"cloudflared exited with code {code}"
            self.echo(f"  [tunnel] {self.error}")
        self._ready.set()
