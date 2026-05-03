"""Small utility functions shared across localrouter sub-modules.

BUG FIX (M1): The original vast_manager.py contained duplicate definitions of
capture() and run() at lines 1095-1100 (dead code after the ``return`` in
run()).  Only the first, correct definitions are kept here.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import ROOT, LAST_INST, TUNNEL_PID, console


# ── shell helpers ─────────────────────────────────────────────────────────────

def capture(cmd: str, timeout: int = 15):
    """Run *cmd* in a shell, return (stdout, stderr, returncode)."""
    r = subprocess.run(cmd, shell=True, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def run(cmd: str, **kw):
    """Run *cmd* in a shell (pass-through to subprocess.run)."""
    return subprocess.run(cmd, shell=True, cwd=ROOT, **kw)


# ── instance helpers ──────────────────────────────────────────────────────────

def last_instance():
    """Return the saved instance ID, or None."""
    try:
        return LAST_INST.read_text().strip()
    except FileNotFoundError:
        return None


def tunnel_running() -> bool:
    """Check whether the SSH tunnel PID file points to a live process."""
    if not TUNNEL_PID.exists():
        return False
    pid = TUNNEL_PID.read_text().strip()
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False


def get_instance_json(inst_id: str):
    """Fetch the raw JSON object for a Vast.ai instance, or None."""
    raw, _, rc = capture(f"vastai show instance {inst_id} --raw 2>/dev/null", timeout=12)
    if rc != 0 or not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def get_ssh(inst_id: str):
    """Return (ssh_host, ssh_port) for an instance, or (None, None)."""
    d = get_instance_json(inst_id)
    if not d:
        return None, None
    return d.get("ssh_host"), d.get("ssh_port")


def ssh_run(inst_id: str, remote_cmd: str, timeout: int = 20):
    """Execute *remote_cmd* over SSH on the given instance."""
    host, port = get_ssh(inst_id)
    if not host:
        return "", "no SSH info", 1
    cmd = (f"ssh -p {port} -o StrictHostKeyChecking=no "
           f"-o ConnectTimeout=8 root@{host} {repr(remote_cmd)}")
    return capture(cmd, timeout=timeout)


# ── TUI micro-helpers ─────────────────────────────────────────────────────────

def ask_back(choices):
    """Append a '← Back' sentinel to a choices list."""
    return list(choices) + ["← Back"]


def hr(title: str = ""):
    """Print a horizontal rule, optionally with a centered title."""
    if title:
        console.rule(f"[bold #7c6af7]{title}[/bold #7c6af7]")
    else:
        console.rule()


def press_enter():
    """Block until the user presses Enter."""
    input("\nPress Enter to continue...")


# ── formatting / token helpers ────────────────────────────────────────────────

def _fmt_bytes(n: float) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _hf_token():
    """Read the cached Hugging Face token, or return None."""
    p = Path.home() / ".cache" / "huggingface" / "token"
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return None


def _expand_tilde(p: str) -> str:
    """Expand ``~`` and resolve a path string."""
    return str(Path(p).expanduser().resolve())
