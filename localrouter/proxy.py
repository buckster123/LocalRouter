"""
localrouter.proxy — Endpoint proxy lifecycle management.

Extracted from vast_manager.py: start/stop the endpoint proxy,
tail its logs, and display detailed provider status.

Bug fix C2: proxy_status_detail() had a broken f-string on the curl
Authorization header — now correctly interpolates {api_key}.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich import box

from .config import console, ROOT, PROXY_PORT, LOCAL_PORT
from .helpers import hr

# Endpoint proxy resolver (optional — graceful fallback)
try:
    from endpoint_proxy import resolve_target
except ImportError:
    def resolve_target():
        return f"http://127.0.0.1:{LOCAL_PORT}/v1", None, "vast-gguf"

# Provider config loader (optional — graceful fallback)
try:
    from .providers import load_provider_config
except ImportError:
    def load_provider_config():
        return {}


# ── proxy lifecycle ───────────────────────────────────────────────────────────

def _proxy_up():
    """Start the endpoint proxy server as a background process.

    Writes PID to /tmp/vastai-gguf-proxy.pid. Skips if already running.
    """
    pid_file = Path("/tmp/vastai-gguf-proxy.pid")

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[yellow]Proxy already running (PID {pid})[/yellow]")
            return
        except (ProcessLookupError, ValueError):
            pid_file.unlink()

    base_url, _, provider = resolve_target()
    console.print(f"\n[dim]Starting proxy → {provider} ({base_url})[/dim]\n")

    # Start as background process
    log_fh = open("/tmp/vastai-gguf-proxy.log", "w")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "endpoint_proxy.py")],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    log_fh.close()  # Popen inherits the fd; we can close our handle

    pid_file.write_text(str(proc.pid))
    console.print(f"[green]✓ Proxy started (PID {proc.pid})[/green]")
    console.print("[dim]Waits for target to become available...[/dim]")


def _proxy_down(pid_file):
    """Stop the endpoint proxy server.

    Args:
        pid_file: Path to the PID file (e.g. /tmp/vastai-gguf-proxy.pid).
    """
    if not pid_file.exists():
        console.print("[yellow]Proxy not running.[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink()
        console.print(f"[green]✓ Proxy stopped (PID {pid})[/green]")
    except ProcessLookupError:
        console.print("[yellow]Proxy process already exited.[/yellow]")
        pid_file.unlink()


# ── log tailing ───────────────────────────────────────────────────────────────

def tail_proxy_logs():
    """Tail proxy log output in a loop (Ctrl-C to stop)."""
    log_file = Path("/tmp/vastai-gguf-proxy.log")
    if not log_file.exists():
        console.print("[yellow]No log file found. Start the proxy first.[/yellow]")
        return

    import time as t
    try:
        while True:
            content = log_file.read_text()
            console.clear()
            hr("Proxy Logs")
            console.print(content[-2000:] if len(content) > 2000 else content)
            t.sleep(1)
    except KeyboardInterrupt:
        pass


# ── detailed status ───────────────────────────────────────────────────────────

def proxy_status_detail():
    """Show detailed proxy status: backend availability for each provider.

    Checks Vast GGUF endpoint and Together AI reachability, then renders
    a summary table.

    BUG FIX C2: The curl Authorization header now correctly uses the
    actual api_key variable instead of literal '***'.
    """
    base_url, _, provider = resolve_target()

    # Check backend availability
    vast_ok = False
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3",
             f"http://127.0.0.1:{LOCAL_PORT}/v1/models"],
            capture_output=True, text=True, timeout=5
        )
        vast_ok = result.returncode == 0
    except Exception:
        pass

    togeth_ok = False
    togeth_cfg = load_provider_config()
    if togeth_cfg and "together" in togeth_cfg:
        api_key = togeth_cfg["together"].get("api_key", "") or os.environ.get("TOGETHER_API_KEY", "")
        if api_key:
            try:
                result = subprocess.run(
                    ["curl", "-s", "--max-time", "5",
                     "-H", f"Authorization: Bearer {api_key}",
                     "https://api.together.ai/v1/models"],
                    capture_output=True, text=True, timeout=8
                )
                togeth_ok = result.returncode == 0
            except Exception:
                pass

    t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t.add_column("provider", style="bold #7c6af7")
    t.add_column("status", width=12)
    t.add_column("endpoint")

    vast_status = "[green]available[/green]" if vast_ok else "[red]unreachable[/red]"
    togeth_status = "[green]available[/green]" if togeth_ok else "[yellow]not configured[/yellow]"

    t.add_row("Vast GGUF", vast_status, f"http://127.0.0.1:{LOCAL_PORT}/v1")
    t.add_row("Together AI", togeth_status, "https://api.together.ai/v1")

    console.print(Panel(t, border_style="#3d3d5c"))
