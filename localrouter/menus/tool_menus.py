"""
localrouter.menus.tool_menus — Batch comparison, status, diagnostics, smoke, proxy.

Extracted from vast_manager.py lines 867-1026, 1556-1631, 1649-1809,
2710-2740, 2745-2806.

BUG FIX M2: menu_diagnose() now checks the active endpoint provider
before running SSH/Vast-specific diagnostics. Local and Together AI
endpoints skip the SSH probe sections entirely.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import questionary
from rich.panel import Panel
from rich.table import Table
from rich import box

from ..config import (
    console, MENU_STYLE, LOCAL_PORT, PROXY_PORT, ROOT,
    load_config,
)
from ..helpers import (
    capture, run, last_instance, tunnel_running,
    get_instance_json, get_ssh, ssh_run, ask_back, hr, press_enter,
    _fmt_bytes,
)
from ..providers import get_active_endpoint, load_provider_config, DEFAULT_PROVIDERS
from ..cost import (
    log_completion, get_session_costs, format_usage_summary,
    check_together_rate_limits, format_rate_limits, format_cost_comparison,
)
from ..vast_ops import _net_rx_delta, _get_container_env, _restart_launch
from ..proxy import _proxy_up, _proxy_down, tail_proxy_logs, proxy_status_detail

# Usage functions are now in localrouter.cost — no external usage_tracker needed


# Endpoint proxy resolver (optional)
try:
    from endpoint_proxy import resolve_target
except ImportError:
    def resolve_target():
        return f"http://127.0.0.1:{LOCAL_PORT}/v1", None, "vast-gguf"


# ── batch comparison ─────────────────────────────────────────────────────────

def menu_batch_compare(provider_cfg):
    """Compare same prompt across multiple providers/models side-by-side."""
    hr("Batch Comparison")

    # Get available providers/models
    togeth = provider_cfg.get("together", {}) if provider_cfg else {}
    together_key = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
    base_url = togeth.get("base_url", DEFAULT_PROVIDERS["together"]["base_url"])

    # Check Vast tunnel availability
    vast_available = False
    if tunnel_running():
        h, _, rc = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/health")
        vast_available = '"ok"' in h and rc == 0

    available_providers = []
    if together_key:
        popular_models = [
            "meta-llama/Llama-3.1-8B-Instruct-Turbo",
            "Qwen/Qwen2.5-Coder-32B-Instruct-Turbo",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ]
        for mid in popular_models:
            available_providers.append(("together", mid))

    if vast_available:
        model_name = "?"
        try:
            m, _, _ = capture("curl -s --max-time 3 http://127.0.0.1:%s/v1/models | jq -r '.data[0].id // \"loading\"'" % LOCAL_PORT)
            model_name = m if m else "?"
        except Exception:
            pass
        available_providers.append(("vast-gguf", model_name))

    if not available_providers:
        console.print("[red]No providers available for comparison.[/red]")
        console.print("[dim]Configure Together AI or start a Vast instance first.[/dim]")
        press_enter()
        return

    # Show available options
    t = Table(box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Provider", style="bold #7c6af7")
    t.add_column("Model")
    for provider, model_id in available_providers:
        label = {"together": "Together AI", "vast-gguf": "Vast GGUF"}.get(provider, provider)
        t.add_row(label, model_id)

    console.print(Panel(t, border_style="#3d3d5c"))

    # Select which providers to compare
    choices = []
    for i, (prov, mid) in enumerate(available_providers):
        label = {"together": "Together", "vast-gguf": "Vast GGUF"}.get(prov, prov)
        choices.append(f"{label} / {mid}")

    sel = questionary.checkbox(
        "Select providers to compare:",
        choices=choices,
        style=MENU_STYLE,
    ).ask()

    if not sel:
        return

    # Get prompt
    prompt = questionary.text(
        "Prompt to send to all selected providers:",
        multiline=True,
        qmark="?",
        style=MENU_STYLE,
    ).ask()

    if not prompt or not prompt.strip():
        return

    # Run comparisons
    console.print(f"\n[dim]Sending '{prompt[:50]}...' to {len(sel)} providers...[/dim]\n")

    results = []
    for choice in sel:
        idx = choices.index(choice)
        provider, model_id = available_providers[idx]

        start_time = time.time()

        try:
            if provider == "together":
                url = f"{base_url}/chat/completions"
                payload = json.dumps({
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt.strip()}],
                    "max_tokens": 200,
                }).encode()

                req = urllib.request.Request(url, data=payload, headers={
                    "Authorization": f"Bearer {together_key}",
                    "Content-Type": "application/json",
                })

                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read())
                    choice_data = data.get("choices", [{}])[0]
                    content = choice_data.get("message", {}).get("content", "")
                    usage = data.get("usage", {})

            elif provider == "vast-gguf":
                url = f"http://127.0.0.1:{LOCAL_PORT}/chat/completions"
                payload = json.dumps({
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt.strip()}],
                    "max_tokens": 200,
                }).encode()

                req = urllib.request.Request(url, data=payload)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read())
                    choice_data = data.get("choices", [{}])[0]
                    content = choice_data.get("message", {}).get("content", "")
                    usage = data.get("usage", {})

            latency = time.time() - start_time
            results.append({
                "provider": provider,
                "model_id": model_id,
                "content": content.strip(),
                "latency": round(latency, 2),
                "tokens": usage.get("completion_tokens", "?"),
            })

        except Exception as e:
            results.append({
                "provider": provider,
                "model_id": model_id,
                "content": f"[red]Error: {e}[/red]",
                "latency": "N/A",
                "tokens": 0,
            })

    # Display results side-by-side style
    hr("Comparison Results")

    for i, result in enumerate(results):
        label = {"together": "Together AI", "vast-gguf": "Vast GGUF"}.get(result["provider"], result["provider"])
        console.print(f"[bold #7c6af7]Provider {i+1}[/bold]: {label}")
        console.print(f"  Model:   {result['model_id']}")
        console.print(f"  Latency: {result['latency']}s | Tokens: {result['tokens']}")
        console.print(f"[dim]{result['content'][:200]}...[/dim]\n")

    # Log to usage tracker
    for result in results:
        if isinstance(result.get("tokens"), int):
            log_completion(
                provider=result["provider"],
                model_id=result["model_id"],
                prompt_tokens=int(len(prompt.split()) * 1.3),  # Rough estimate
                completion_tokens=result["tokens"],
            )

    press_enter()


# ── status panel ─────────────────────────────────────────────────────────────

def show_status(provider_cfg=None):
    inst_id = last_instance()
    tun     = tunnel_running()
    ep      = get_active_endpoint()

    t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t.add_column("key",   style="dim", width=18)
    t.add_column("value", style="bold")

    # Show active endpoint type
    if ep:
        prov = ep.get("provider", "unknown")
        if prov == "together":
            model_id = ep.get("model_id", "?")
            t.add_row("endpoint", "[#7c6af7]Together AI[/] (managed)")
            t.add_row("model",    model_id)
            # Try to fetch pricing from config
            recipe_name = None
            for r in load_config()[1]:  # recipes list
                if r.get("provider") == "together" and r.get("model_id") == model_id:
                    recipe_name = r.get("name")
                    break
            if recipe_name:
                cost_str = format_cost_comparison(1000, 500, provider_cfg)
                t.add_row("est. cost", "$0.00x (per-token)")
        elif prov == "local":
            name = ep.get("name", "?")
            port = ep.get("port", "?")
            status_str = "[green]running[/green]" if ep.get("status") == "running" else "[red]stopped[/red]"
            t.add_row("endpoint", f"[#7c6af7]Local[/] ({name})  {status_str}")
            model_short = Path(ep.get("model_path", "")).name[:50] if ep.get("model_path") else "?"
            t.add_row("model",    model_short)
            t.add_row("port",     f"127.0.0.1:{port}")
        else:
            t.add_row("endpoint", "[#7c6af7]Vast GGUF[/] (self-hosted)")

    if inst_id:
        d = get_instance_json(inst_id)
        if d:
            status = d.get("actual_status", "?")
            color  = {"running": "green", "loading": "yellow",
                      "exited": "red", "offline": "red"}.get(status, "yellow")
            t.add_row("instance id", str(inst_id))
            t.add_row("status",      f"[{color}]{status}[/{color}]")
            t.add_row("gpu",         str(d.get("gpu_name", "?")))
            t.add_row("$/hr",        f"${d.get('dph_total', 0):.3f}")
            t.add_row("geo",         str(d.get("geolocation", "?")))
            host = d.get("ssh_host", "")
            port = d.get("ssh_port", "")
            if host:
                t.add_row("ssh", f"root@{host}:{port}")
        else:
            t.add_row("instance", f"[red]{inst_id}[/red]  (vastai lookup failed)")
    else:
        t.add_row("instance", "[dim]none[/dim]")

    t.add_row("tunnel", "[green]up[/green]" if tun else "[red]down[/red]")

    if tun:
        h, _, _ = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/health")
        if '"ok"' in h:
            m,  _, _ = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/v1/models "
                                "| jq -r '.data[0].id // \\\"loading\\\"'")
            sl, _, _ = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/slots "
                                "| jq 'length' 2>/dev/null")
            t.add_row("endpoint", "[green]healthy[/green]")
            t.add_row("model",    m  or "?")
            t.add_row("slots",    sl or "?")
        else:
            t.add_row("endpoint", "[yellow]unreachable[/yellow]")

    console.print(Panel(t, title="[bold]current state[/bold]",
                        border_style="#3d3d5c", padding=(0, 1)))


# ── deep diagnostics ─────────────────────────────────────────────────────────

def menu_diagnose(provider_cfg=None):
    inst_id = last_instance()
    if not inst_id and not get_active_endpoint():
        console.print("[yellow]No active instance or endpoint found.[/yellow]")
        press_enter(); return

    hr("Diagnostics")

    # Show usage summary
    usage_str = format_usage_summary()
    t_usage = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t_usage.add_column("key", style="dim", width=16)
    t_usage.add_column("value")
    for line in usage_str.split("\n"):
        if "[" in line:  # Rich markup
            key_val = line.split(":", 1)
            if len(key_val) == 2:
                t_usage.add_row(key_val[0].strip(), key_val[1])
            else:
                t_usage.add_row("", line)
        else:
            parts = line.split(":", 1)
            if len(parts) == 2:
                t_usage.add_row(parts[0].strip(), parts[1].strip())

    console.print(Panel(t_usage, title="[bold]Usage Summary (24h)[/bold]", border_style="#3d3d5c"))

    # Show rate limit status if Together is configured
    if provider_cfg and "together" in provider_cfg:
        togeth = provider_cfg["together"]
        api_key = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
        base_url = togeth.get("base_url", "")
        if api_key and base_url:
            rl_str = format_rate_limits(provider_cfg)
            console.print(Panel(rl_str, title="[bold]Together Rate Status[/bold]",
                               border_style="#3d3d5c"))

    # ── BUG FIX M2: Guard SSH/Vast-specific diagnostics ─────────────────────
    # If the active endpoint is local or Together, skip SSH probing entirely.
    active = get_active_endpoint()
    active_provider = active.get("provider", "unknown") if active else None

    if active_provider in ("local", "together"):
        # Non-Vast endpoint — SSH diagnostics don't apply
        console.print(f"[dim]Active provider: {active_provider} — SSH/Vast diagnostics skipped.[/dim]")
        press_enter()
        return

    if not inst_id:
        console.print("[yellow]No Vast instance found for SSH diagnostics.[/yellow]")
        press_enter()
        return

    # ── Vast-specific diagnostics from here on ───────────────────────────────
    press_enter()
    console.print(f"[dim]Instance {inst_id} — gathering data...[/dim]\n")

    d = get_instance_json(inst_id)
    if not d:
        console.print("[red]Could not reach vastai API.[/red]")
        press_enter(); return

    status     = d.get("actual_status", "?")
    status_msg = d.get("status_msg", "")
    ssh_host   = d.get("ssh_host", "")
    ssh_port   = d.get("ssh_port", "")
    color      = {"running": "green", "loading": "yellow",
                  "exited": "red", "offline": "red"}.get(status, "yellow")

    t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t.add_column("k", style="dim", width=20)
    t.add_column("v", style="bold")
    t.add_row("status",     f"[{color}]{status}[/{color}]")
    t.add_row("status_msg", status_msg or "[dim]—[/dim]")
    t.add_row("gpu",        f"{d.get('gpu_name','?')}  geo={d.get('geolocation','?')}")
    t.add_row("$/hr",       f"${d.get('dph_total', 0):.3f}")
    t.add_row("inet_down",  f"{d.get('inet_down', 0):.0f} Mbps  (rated)")
    t.add_row("disk",       f"{d.get('disk_util', 0):.0f}% used of {d.get('disk_space', 0):.0f} GB")
    if ssh_host:
        t.add_row("ssh",    f"root@{ssh_host}:{ssh_port}")
    console.print(Panel(t, title="[bold]instance[/bold]", border_style="#3d3d5c"))

    if status not in ("running",):
        console.print(f"[yellow]Instance not running (status={status}). SSH diagnostics skipped.[/yellow]")
        press_enter(); return

    if not ssh_host:
        console.print("[yellow]No SSH info yet — still provisioning.[/yellow]")
        press_enter(); return

    # SSH probes
    console.print("[dim]SSH: gathering process/disk/file info...[/dim]")
    procs_out,  _, _ = ssh_run(inst_id,
        "ps -eo pid,etime,pcpu,pmem,cmd --sort=-pcpu | head -15", timeout=15)
    disk_out,   _, _ = ssh_run(inst_id,
        "df -h /workspace 2>/dev/null || df -h /", timeout=10)
    models_out, _, _ = ssh_run(inst_id,
        "find /workspace/models -type f \\( -name '*.gguf' -o -name '*.incomplete' \\) "
        "-exec ls -lh {} \\; 2>/dev/null | head -20", timeout=12)
    log_out,    _, _ = ssh_run(inst_id,
        "tail -30 /var/log/launch.log 2>/dev/null || echo '(no log yet)'", timeout=12)

    # Live network speed
    console.print("[dim]Measuring live download speed (4s sample)...[/dim]")
    rx_bytes = _net_rx_delta(inst_id, seconds=4)

    # Render
    hr("Processes (top CPU)")
    console.print(procs_out or "[dim]—[/dim]")

    hr("Disk /workspace")
    console.print(disk_out or "[dim]—[/dim]")

    hr("Model files")
    if models_out:
        for line in models_out.splitlines():
            parts    = line.split()
            size_str = parts[4] if len(parts) > 5 else "?"
            fname    = parts[-1].split("/")[-1] if parts else "?"
            if ".incomplete" in line:
                console.print(f"  [yellow]⟳ downloading[/yellow]  {fname}  ({size_str} so far)")
            elif ".gguf" in line:
                console.print(f"  [green]✓ complete[/green]    {fname}  ({size_str})")
    else:
        console.print("  [dim]No model files found yet[/dim]")

    hr("Network download speed")
    if rx_bytes is not None:
        speed_mbps = (rx_bytes * 8) / (4 * 1_000_000)
        speed_str  = f"{speed_mbps:.0f} Mbps  ({_fmt_bytes(rx_bytes / 4)}/s)"
        if rx_bytes < 1000:
            console.print(f"  [red]⚠  STALLED[/red]  {speed_str}")
        elif speed_mbps < 50:
            console.print(f"  [yellow]  slow[/yellow]  {speed_str}")
        else:
            console.print(f"  [green]✓  active[/green]  {speed_str}")
    else:
        console.print("  [yellow]Could not measure (SSH error)[/yellow]")

    hr("launch.log  (last 30 lines)")
    console.print(log_out or "[dim]—[/dim]")

    # Stall recovery
    if rx_bytes is not None and rx_bytes < 1000:
        console.print()
        console.print(Panel(
            "[yellow]Download appears stalled.[/yellow]\n"
            "HF transfer connection likely hung. Restart will kill the stalled\n"
            "process and re-run launch.sh — HF hub resumes from the .incomplete file.",
            title="[bold yellow]⚠  Stall detected[/bold yellow]",
            border_style="yellow",
        ))
        if questionary.confirm("Kill stalled download and restart launch.sh?",
                               style=MENU_STYLE, default=True).ask():
            _restart_launch(inst_id)
            return

    # Slot info if endpoint is up
    if tunnel_running():
        hr("Endpoint slots")
        h, _, _ = capture(f"curl -s --max-time 5 http://127.0.0.1:{LOCAL_PORT}/health")
        if '"ok"' in h:
            slots_raw, _, _ = capture(
                f"curl -s --max-time 5 http://127.0.0.1:{LOCAL_PORT}/slots 2>/dev/null")
            try:
                for i, s in enumerate(json.loads(slots_raw)):
                    state  = s.get("state", "?")
                    n_past = s.get("n_past", 0)
                    n_ctx  = s.get("n_ctx", 0)
                    console.print(f"  slot {i}: [green]{state}[/green]  "
                                  f"ctx used {n_past}/{n_ctx} tokens")
            except Exception:
                console.print("  [green]health OK[/green]  (could not parse slots)")
        else:
            console.print("  [yellow]unreachable — still loading[/yellow]")

    press_enter()


# ── smoke test ───────────────────────────────────────────────────────────────

def menu_smoke(provider_cfg=None):
    hr("Smoke test")

    # Check for active Together endpoint
    ep = get_active_endpoint()
    default_url = f"http://127.0.0.1:{LOCAL_PORT}" if tunnel_running() else ""
    if ep and ep.get("provider") == "together":
        default_url = ep.get("endpoint", "").replace("/chat/completions", "")

    # Offer proxy-based testing if available
    proxy_pid_file = Path("/tmp/vastai-gguf-proxy.pid")
    has_proxy = False
    if proxy_pid_file.exists():
        try:
            pid = int(proxy_pid_file.read_text().strip())
            os.kill(pid, 0)
            has_proxy = True
        except (ProcessLookupError, ValueError):
            pass

    if has_proxy:
        default_url = "http://127.0.0.1:8888"

    url = questionary.text(
        "Endpoint base URL (no /v1 suffix):",
        default=default_url, style=MENU_STYLE,
    ).ask()
    if not url: return
    hr()
    run(f"bash {ROOT}/smoke.sh {url}")
    press_enter()


# ── proxy management ─────────────────────────────────────────────────────────

def menu_proxy():
    """Manage the local proxy server."""
    while True:
        hr("Proxy Manager")

        pid_file = Path("/tmp/vastai-gguf-proxy.pid")
        running = False
        pid = None

        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                running = True
            except (ProcessLookupError, ValueError):
                pass

        status = "[green]running[/green]" if running else "[red]stopped[/red]"
        console.print(f"  Proxy: {status}")
        if running and pid:
            console.print(f"  PID:   {pid}  (port 8888)")

        # Show active target if running
        if running:
            try:
                base_url, _, provider = resolve_target()
                console.print(f"  Target: {provider} → {base_url}")
            except Exception:
                pass

        t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
        t.add_column("command", style="bold #7c6af7")
        t.add_column("description")
        t.add_row("up",   "Start proxy server (forward to active provider)")
        t.add_row("down", "Stop proxy server")
        t.add_row("logs", "Tail proxy output")
        t.add_row("status", "Detailed status and provider list")

        console.print(Panel(t, border_style="#3d3d5c"))

        choice = questionary.select(
            "Action:",
            choices=["up — start proxy", "down — stop proxy",
                     "logs — tail output", "status — detailed info", "← Back"],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice.startswith("←"):
            return

        action = choice.split()[0]

        if action == "up":
            _proxy_up()
        elif action == "down":
            _proxy_down(pid_file)
        elif action == "logs":
            tail_proxy_logs()
        elif action == "status":
            proxy_status_detail()

        press_enter()
