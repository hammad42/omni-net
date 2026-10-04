"""
core/netutil.py — the two primitives everything else is built on.

1. PowerShellSession
   A *persistent* `powershell -Command -` child process. Spawning powershell.exe
   costs 300-900 ms; failover budgets are 500 ms, so we cannot afford a new
   process per command. Commands are shipped as base64 UTF-16LE script blocks so
   multi-line scripts survive the stdin pipe intact, and a GUID sentinel marks
   the end of each result. CREATE_NO_WINDOW keeps console flashes off screen.

2. bind_socket_to_uplink()
   The single most important call in this project.

   Binding a socket to a source IP does NOT reliably control which NIC the
   packet leaves through. Windows picks the egress interface from the routing
   table (destination first), and with the default *weak host send* model a
   packet carrying NIC A's source address is happily emitted out of NIC B. Your
   ISP then drops it (uRPF / BCP38) and the connection silently dies.

   IP_UNICAST_IF (level IPPROTO_IP, option 31) overrides the route lookup and
   pins the egress interface by index. We set it *and* bind the source address:
   the option chooses the wire, the bind chooses the address.

   Gotcha that costs people days: for IPv4 the interface index must be passed in
   NETWORK byte order (big-endian). For IPv6 (IPPROTO_IPV6, option 31) it is
   HOST byte order. Yes, really.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import uuid
from typing import Any, Optional

log = logging.getLogger("omninet.netutil")

IS_WINDOWS = os.name == "nt"

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
DETACHED_PROCESS = 0x00000008 if IS_WINDOWS else 0

IP_UNICAST_IF = 31
IPV6_UNICAST_IF = 31


# --------------------------------------------------------------------------- #
# Elevation
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Re-exec the current program through the UAC 'runas' verb.
    Returns True if a new elevated process was started (caller should exit)."""
    if not IS_WINDOWS or is_admin():
        return False
    params = " ".join(f'"{a.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"' for a in sys.argv[1:])
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, params
    else:
        script = os.path.abspath(sys.argv[0]).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))
        exe, args = sys.executable, f'"{script}" {params}'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
    return int(rc) > 32


# --------------------------------------------------------------------------- #
# Socket binding
# --------------------------------------------------------------------------- #
def set_unicast_if(sock: socket.socket, if_index: int) -> None:
    """Pin the socket's egress interface. Harmless if it fails (older stacks)."""
    if not if_index:
        return
    try:
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, IPV6_UNICAST_IF,
                            struct.pack("I", if_index))       # host byte order
        else:
            sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF,
                            struct.pack("!I", if_index))      # network byte order
    except OSError as e:
        log.debug("IP_UNICAST_IF(%s) failed: %s", if_index, e)


def bind_socket_to_uplink(sock: socket.socket,
                          src_ip: Optional[str],
                          if_index: Optional[int],
                          src_port: int = 0,
                          allow_ephemeral_fallback: bool = True) -> None:
    """Force this socket out of a specific uplink, before connect()."""
    if if_index:
        set_unicast_if(sock, int(if_index))
    if src_ip:
        try:
            sock.bind((src_ip, src_port))
        except OSError as e:
            if src_port and allow_ephemeral_fallback:          # port collision -> retry ephemeral
                sock.bind((src_ip, 0))
            else:
                raise e


def socket_options_for(src_ip: Optional[str], if_index: Optional[int]) -> list:
    """urllib3/requests-compatible socket_options list (applied pre-connect)."""
    opts = []
    if if_index:
        opts.append((socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", int(if_index))))
    return opts


# --------------------------------------------------------------------------- #
# PowerShell
# --------------------------------------------------------------------------- #
class PowerShellError(RuntimeError):
    pass


class PowerShellSession:
    """One long-lived powershell.exe, reused for every command."""

    PREAMBLE = (
        "$ProgressPreference='SilentlyContinue';"
        "$ErrorActionPreference='SilentlyContinue';"
        "$WarningPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8"
    )

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------- #
    def _spawn(self) -> None:
        if not IS_WINDOWS:
            raise PowerShellError("PowerShell is only available on Windows")
        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self._write(self.PREAMBLE)

    def _write(self, line: str) -> None:
        proc = self._proc
        if not proc or not proc.stdin:
            raise PowerShellError("PowerShell session not running")
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise PowerShellError(f"PowerShell pipe broken: {e}") from e

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            if self._alive():
                try:
                    self._write("exit")
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None

    # -- execution -------------------------------------------------------- #
    def run(self, script: str, timeout: float = 20.0, retry: bool = False) -> str:
        """Run a script block, return its stdout (stderr merged in)."""
        with self._lock:
            for attempt in (0, 1):
                if not self._alive():
                    self._spawn()
                try:
                    return self._run_once(script, timeout)
                except PowerShellError:
                    self.close()
                    if attempt or not retry:
                        raise
            raise PowerShellError("unreachable")

    def _run_once(self, script: str, timeout: float) -> str:
        token = uuid.uuid4().hex
        b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        cmd = ("& ([ScriptBlock]::Create("
               f"[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))))")
        self._write(cmd)
        self._write(f"Write-Output '@@OMNI_END:{token}'")

        out: list[str] = []
        done = threading.Event()
        marker = f"@@OMNI_END:{token}"

        proc = self._proc
        if not proc or not proc.stdout:
            raise PowerShellError("PowerShell session not running")
        stdout = proc.stdout

        def reader() -> None:
            try:
                while True:
                    line = stdout.readline()
                    if not line:
                        break
                    if marker in line:
                        prefix = line.split(marker)[0]
                        if prefix:
                            out.append(prefix)
                        done.set()
                        return
                    out.append(line)
            except Exception:
                pass
            finally:
                done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        if not done.wait(timeout):
            raise PowerShellError(f"PowerShell timed out after {timeout}s")
        if not self._alive():
            raise PowerShellError("PowerShell session died")
        return "".join(out)

    def run_json(self, script: str, timeout: float = 20.0) -> Any:
        return parse_json_loose(self.run(script, timeout, retry=True))


def parse_json_loose(text: str) -> Any:
    """ConvertTo-Json output can be preceded by stray warnings; find the payload.
    Also normalises PowerShell's habit of emitting a bare object instead of a
    one-element array."""
    if not text:
        return None
    s = text.strip()
    start = min([i for i in (s.find("["), s.find("{")) if i >= 0], default=-1)
    if start < 0:
        return None
    end = max(s.rfind("]"), s.rfind("}"))
    if end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def as_list(obj: Any) -> list:
    if obj is None:
        return []
    return obj if isinstance(obj, list) else [obj]


# Module-level shared session (adapter + router share one child process).
SHELL = PowerShellSession()


def run_hidden(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """For one-off native tools (netsh, ForceBindIP) without a console flash."""
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
