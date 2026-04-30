#!/usr/bin/env python3
"""
vast_manager.py — interactive TUI for vastai-gguf-launcher.

Spin up, monitor, diagnose, and tear down GGUF model endpoints on Vast.ai GPUs.
Recipes are loaded from recipes.toml — edit that file to add new models.

Controls: arrow keys to navigate, Enter to select, Ctrl-C to go back/exit.
"""

import os
import sys
import re
import json
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

try:
    import questionary
    from questionary import Style
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
except ImportError as e:
    print(f"Missing dep: {e}")
    print("Run: pip3 install questionary rich --break-system-packages")
    sys.exit(1)

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.resolve()
LAST_INST  = ROOT / ".last_instance"
TUNNEL_PID = Path("/tmp/vastai-gguf-tunnel.pid")
HF_PIN     = ROOT / ".hf_pin"

console    = Console()
LOCAL_PORT = 8800

MENU_STYLE = Style([
    ("qmark",       "fg:#7c6af7 bold"),
    ("question",    "bold"),
    ("answer",      "fg:#7c6af7 bold"),
    ("pointer",     "fg:#7c6af7 bold"),
    ("highlighted", "fg:#7c6af7 bold"),
    ("selected",    "fg:#9d8ff7"),
    ("separator",   "fg:#555555"),
    ("instruction", "fg:#555555 italic"),
])

# ── recipe loader ─────────────────────────────────────────────────────────────

def _load_toml(path):
    """Minimal TOML parser — handles the subset used in recipes.toml."""
    import re
    data   = {}
    cur    = data
    cur_key= None
    arr    = None   # current [[array]] list

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # [[array-of-tables]]
        m = re.match(r'^\[\[(.+?)\]\]$', line)
        if m:
            key = m.group(1).strip()
            if key not in data:
                data[key] = []
            arr = {}
            data[key].append(arr)
            cur = arr
            cur_key = None
            continue

        # [table]
        m = re.match(r'^\[(.+?)\]$', line)
        if m:
            parts = m.group(1).strip().split(".")
            cur = data
            for p in parts:
                cur = cur.setdefault(p, {})
            arr = None
            cur_key = None
            continue

        # key = value
        m = re.match(r'^(\w+)\s*=\s*(.+)$', line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if v.startswith('"') and v.endswith('"'):
                cur[k] = v[1:-1]
            elif v.startswith('[') and v.endswith(']'):
                # Simple string array: ["A", "B", "C"]
                cur[k] = re.findall(r'"([^"]+)"', v)
            elif v.isdigit():
                cur[k] = int(v)
            else:
                try:
                    cur[k] = float(v)
                except ValueError:
                    cur[k] = v

    return data


def load_config():
    """Load recipes.toml. Returns (cfg, recipes, gpu_tiers, docker_cfg)."""
    cfg_path = ROOT / "recipes.toml"
    if not cfg_path.exists():
        console.print(f"[red]recipes.toml not found at {cfg_path}[/red]")
        sys.exit(1)
    cfg        = _load_toml(cfg_path)
    recipes    = cfg.get("recipes", [])
    gpu_tiers  = cfg.get("gpu_tiers", {})
    docker_cfg = cfg.get("docker", {})
    # Fallback defaults if new keys not present
    docker_cfg.setdefault("prebuilt", "ghcr.io/buckster123/vastai-gguf:prebuilt")
    docker_cfg.setdefault("builder",  "ghcr.io/buckster123/vastai-gguf:builder")
    return cfg, recipes, gpu_tiers, docker_cfg


def image_for_type(docker_cfg, image_type):
    """Return the docker image string for a given image_type."""
    return docker_cfg.get(image_type, docker_cfg.get("prebuilt", "ghcr.io/buckster123/vastai-gguf:prebuilt"))


def cold_start_estimate(image_type):
    """Human-readable cold start estimate."""
    return {"prebuilt": "~2 min  (image pull only)",
            "builder":  "~12-18 min  (pull + SM compile)"}.get(image_type, "unknown")


# ── helpers ───────────────────────────────────────────────────────────────────

def capture(cmd, timeout=15):
    r = subprocess.run(cmd, shell=True, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def run(cmd, **kw):
    return subprocess.run(cmd, shell=True, cwd=ROOT, **kw)

def last_instance():
    try:
        return LAST_INST.read_text().strip()
    except FileNotFoundError:
        return None

def tunnel_running():
    if not TUNNEL_PID.exists():
        return False
    pid = TUNNEL_PID.read_text().strip()
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False

def get_instance_json(inst_id):
    raw, _, rc = capture(f"vastai show instance {inst_id} --raw 2>/dev/null", timeout=12)
    if rc != 0 or not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def get_ssh(inst_id):
    d = get_instance_json(inst_id)
    if not d:
        return None, None
    return d.get("ssh_host"), d.get("ssh_port")

def ssh_run(inst_id, remote_cmd, timeout=20):
    host, port = get_ssh(inst_id)
    if not host:
        return "", "no SSH info", 1
    cmd = (f"ssh -p {port} -o StrictHostKeyChecking=no "
           f"-o ConnectTimeout=8 root@{host} {repr(remote_cmd)}")
    return capture(cmd, timeout=timeout)

def ask_back(choices):
    return list(choices) + ["← Back"]

def hr(title=""):
    if title:
        console.rule(f"[bold #7c6af7]{title}[/bold #7c6af7]")
    else:
        console.rule()

def press_enter():
    input("\nPress Enter to continue...")

def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _hf_token():
    p = Path.home() / ".cache" / "huggingface" / "token"
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return None

# ── status panel ──────────────────────────────────────────────────────────────

def show_status():
    inst_id = last_instance()
    tun     = tunnel_running()

    t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t.add_column("key",   style="dim", width=18)
    t.add_column("value", style="bold")

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
                                "| jq -r '.data[0].id // \"loading\"'")
            sl, _, _ = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/slots "
                                "| jq 'length' 2>/dev/null")
            t.add_row("endpoint", "[green]healthy[/green]")
            t.add_row("model",    m  or "?")
            t.add_row("slots",    sl or "?")
        else:
            t.add_row("endpoint", "[yellow]unreachable[/yellow]")

    console.print(Panel(t, title="[bold]current state[/bold]",
                        border_style="#3d3d5c", padding=(0, 1)))

# ── deep diagnostics ──────────────────────────────────────────────────────────

def _net_rx_delta(inst_id, seconds=4):
    script = (
        f"RX1=$(cat /proc/net/dev | awk '/eth0/{{print $2}}'); "
        f"sleep {seconds}; "
        f"RX2=$(cat /proc/net/dev | awk '/eth0/{{print $2}}'); "
        f"echo $((RX2-RX1))"
    )
    out, _, rc = ssh_run(inst_id, script, timeout=seconds + 15)
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None

def menu_diagnose():
    inst_id = last_instance()
    if not inst_id:
        console.print("[yellow]No .last_instance found.[/yellow]")
        press_enter(); return

    hr("Diagnostics")
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

# ── restart stalled download ──────────────────────────────────────────────────

def _get_container_env(inst_id):
    env_out, _, rc = ssh_run(inst_id,
        "cat /proc/$(pgrep -f 'bash /app/launch.sh' | head -1)/environ 2>/dev/null "
        "| tr '\\0' '\\n' | grep -E 'MODEL_|CTX|KV_TYPE|MODE|PARALLEL|MMPROJ|HF_TOKEN|HOST'",
        timeout=12)
    env = {}
    if rc == 0 and env_out:
        for line in env_out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def _restart_launch(inst_id):
    hr("Restarting launch.sh")
    env = _get_container_env(inst_id)
    if not env:
        console.print("[yellow]Could not read env from process — using fallback defaults.[/yellow]")
        env = {
            "MODEL_REPO":  "unsloth/Qwen3.6-35B-A3B-GGUF",
            "MODEL_QUANT": "UD-Q5_K_XL",
            "CTX":         "131072",
            "KV_TYPE":     "q8_0",
            "MODE":        "thinking",
            "PARALLEL":    "1",
        }
    env["HOST"] = "127.0.0.1"   # always harden on restart

    env_str = " ".join(f"{k}={v}" for k, v in env.items())
    script  = (
        "#!/bin/bash\n"
        "pkill -f 'bash /app/launch.sh' 2>/dev/null || true\n"
        "pkill -f 'hf download' 2>/dev/null || true\n"
        "sleep 2\n"
        f"{env_str} bash /app/launch.sh >> /var/log/launch.log 2>&1 &\n"
        "echo \"restarted pid=$!\"\n"
    )

    write_cmd = f"cat > /tmp/restart_launch.sh << 'HEREDOC'\n{script}\nHEREDOC\nchmod +x /tmp/restart_launch.sh"
    ssh_run(inst_id, write_cmd, timeout=10)

    host, port = get_ssh(inst_id)
    if not host:
        console.print("[red]No SSH info.[/red]"); press_enter(); return

    subprocess.run(
        ["ssh", "-f", "-p", str(port),
         "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
         f"root@{host}", "/tmp/restart_launch.sh"],
        capture_output=True, timeout=15,
    )
    console.print("[green]Restart command sent.[/green]  Tailing logs in 3s — Ctrl-C to stop.")
    time.sleep(3)
    run(f"bash {ROOT}/tools/vast_tunnel.sh logs")
    press_enter()

# ── boot watcher ──────────────────────────────────────────────────────────────

def menu_watch_boot():
    inst_id = last_instance()
    if not inst_id:
        console.print("[yellow]No .last_instance.[/yellow]"); press_enter(); return

    hr("Boot watcher")
    console.print(f"[dim]Polling instance {inst_id} every 10s — Ctrl-C to stop[/dim]\n")
    last_log = ""
    try:
        while True:
            d = get_instance_json(inst_id)
            if not d:
                console.print("[dim]vastai API unreachable, retrying...[/dim]")
                time.sleep(10); continue

            status = d.get("actual_status", "?")
            smsg   = d.get("status_msg", "")
            color  = {"running": "green", "loading": "yellow"}.get(status, "red")
            ts     = time.strftime("%H:%M:%S")
            console.print(f"  [dim]{ts}[/dim]  status=[{color}]{status}[/{color}]  {smsg}")

            host = d.get("ssh_host")
            port = d.get("ssh_port")
            if host and port:
                log, _, _ = capture(
                    f"ssh -p {port} -o StrictHostKeyChecking=no -o ConnectTimeout=5 "
                    f"root@{host} 'tail -1 /var/log/launch.log 2>/dev/null'",
                    timeout=10)
                if log and log != last_log:
                    console.print(f"  [dim]log »[/dim] {log}")
                    last_log = log

            if status == "running" and tunnel_running():
                h, _, _ = capture(f"curl -s --max-time 3 http://127.0.0.1:{LOCAL_PORT}/health")
                if '"ok"' in h:
                    console.print(f"\n  [bold green]✓ Endpoint healthy![/bold green]  "
                                  f"http://127.0.0.1:{LOCAL_PORT}/v1")
                    break

            if status in ("exited", "offline"):
                console.print("[red]Instance exited/offline. Stopping watcher.[/red]"); break

            time.sleep(10)
    except KeyboardInterrupt:
        console.print("\n[dim]Watcher stopped.[/dim]")

    press_enter()

# ── HF model browser ──────────────────────────────────────────────────────────

def _hf_list_files(repo_id, token=None):
    url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "vastai-gguf-launcher/1.0"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("siblings", [])
    except Exception:
        return []

def menu_hf_browser(recipes):
    hr("HuggingFace model browser")

    # build repo list from recipes.toml + custom option
    known_repos = {}
    for r in recipes:
        repo = r.get("model_repo", "")
        if repo and repo not in known_repos:
            known_repos[repo] = f"{repo}  (used in recipe: {r.get('label','')})"

    repo_choices = list(known_repos.values()) + ["[custom] enter repo ID manually"]

    sel = questionary.select(
        "HF repo to browse:",
        choices=ask_back(repo_choices),
        style=MENU_STYLE,
    ).ask()
    if sel is None or sel == "← Back":
        return

    if sel.startswith("[custom]"):
        repo_id = questionary.text(
            "Enter HF repo ID (e.g. unsloth/Qwen3.6-27B-GGUF):",
            style=MENU_STYLE,
        ).ask()
        if not repo_id:
            return
    else:
        repo_id = sel.split()[0]

    console.print(f"\n[dim]Fetching file list for {repo_id} from HuggingFace...[/dim]")
    files = _hf_list_files(repo_id, _hf_token())

    if not files:
        console.print("[red]Could not fetch file list — check repo ID or network.[/red]")
        press_enter(); return

    gguf_files = [f for f in files if f.get("rfilename", "").endswith(".gguf")]
    if not gguf_files:
        console.print("[yellow]No .gguf files found in this repo.[/yellow]")
        press_enter(); return

    quant_re   = re.compile(r'(UD-Q\d+[^.\s_-]*|Q\d+_K_[A-Z]+|Q\d+_\d+)', re.IGNORECASE)

    tbl = Table(title=f"[bold]{repo_id}[/bold]", box=box.SIMPLE, show_lines=False)
    tbl.add_column("filename",  style="bold", min_width=42)
    tbl.add_column("size",      style="cyan",  width=10)
    tbl.add_column("quant",     style="green", width=18)

    choice_map  = {}
    file_labels = []
    for f in gguf_files:
        fname    = f.get("rfilename", "?")
        size     = f.get("size", 0)
        size_str = _fmt_bytes(size) if size else "?"
        m        = quant_re.search(fname)
        quant    = m.group(0) if m else "?"
        tbl.add_row(fname, size_str, quant)
        label = f"{fname:<54} {size_str:>8}  {quant}"
        choice_map[label] = (fname, quant, size_str, repo_id)
        file_labels.append(label)

    console.print(tbl)
    console.print(f"  [dim]{len(gguf_files)} .gguf file(s) shown — sizes are per-shard[/dim]\n")

    action = questionary.select(
        "Action:",
        choices=["Pin a quant for the next launch wizard", "← Back to main menu"],
        style=MENU_STYLE,
    ).ask()
    if action is None or action.startswith("←"):
        return

    file_sel = questionary.select(
        "Select file to pin as MODEL_QUANT:",
        choices=ask_back(file_labels),
        style=MENU_STYLE,
        use_shortcuts=False,
    ).ask()
    if file_sel is None or file_sel == "← Back":
        return

    fname, quant, size_str, repo_id = choice_map[file_sel]
    pin = {"MODEL_REPO": repo_id, "MODEL_QUANT": quant, "filename": fname, "size": size_str}
    HF_PIN.write_text(json.dumps(pin))
    console.print(f"\n[green]Pinned:[/green]  MODEL_REPO={repo_id}  MODEL_QUANT={quant}")
    console.print(f"[dim]Next Launch wizard will offer to use this quant.[/dim]")
    press_enter()

# ── offer browser ─────────────────────────────────────────────────────────────

def browse_offers(gpu_key, geo_key, max_price, tier_cfg=None, num_gpus=1, min_cuda="12.8"):
    geo_re_map = {
        "EU_NORDIC": "SE|NO|FI|DK|IS",
        "EU":        "SE|NO|FI|DK|IS|DE|NL|FR|BE|UK|IE|EE|LV|LT|PL|CZ|AT|CH|ES|PT|IT",
        "US":        "US",
        "ANY":       ".*",
    }
    geo_re = geo_re_map.get(geo_key, ".*")
    console.print(f"\n[dim]Searching Vast offers for {gpu_key} in {geo_key}...[/dim]")

    # Build gpu_filter from tier's vast_names list, or fall back to key-derived name
    vast_names = (tier_cfg or {}).get("vast_names", [])
    if isinstance(vast_names, str):
        vast_names = [vast_names]
    if vast_names:
        if len(vast_names) == 1:
            gpu_filter = f"gpu_name={vast_names[0]}"
        else:
            gpu_filter = f"gpu_name in [{','.join(vast_names)}]"
    elif gpu_key == "6000pro":
        gpu_filter = 'gpu_name in [RTX_PRO_6000_WS,RTX_PRO_6000_S]'
    else:
        gpu_filter = f"gpu_name=RTX_{gpu_key}"

    raw, _, rc = capture(
        f'vastai search offers "{gpu_filter} num_gpus={num_gpus} reliability>0.97 '
        f'inet_down>300 dph_total<{max_price} disk_space>60 '
        f'cuda_vers>={min_cuda} rentable=true" --order dph_total --raw 2>/dev/null',
        timeout=20)

    if rc != 0 or not raw:
        console.print("[red]vastai search failed or no results.[/red]"); return None

    try:
        offers = json.loads(raw)
    except Exception:
        console.print("[red]Could not parse offers JSON.[/red]"); return None

    pattern  = re.compile(rf", ({geo_re})$")
    filtered = [o for o in offers if pattern.search(o.get("geolocation", ""))]
    if not filtered:
        console.print(f"[yellow]No offers in {geo_key}, showing all.[/yellow]")
        filtered = offers

    tbl = Table(title=f"Available {gpu_key} offers", box=box.SIMPLE, show_lines=False)
    tbl.add_column("ID",      style="dim",    width=11)
    tbl.add_column("$/hr",    style="cyan",   width=7)
    tbl.add_column("rel",     style="green",  width=6)
    tbl.add_column("VRAM",    width=7)
    tbl.add_column("↓ Mbps",  style="yellow", width=9)
    tbl.add_column("CUDA",    width=7)
    tbl.add_column("geo",     style="dim")

    choices_map = {}
    choice_list = []
    for o in filtered[:12]:
        oid      = str(o.get("id", "?"))
        dph      = f"{o.get('dph_total', 0):.3f}"
        rel      = f"{o.get('reliability2', 0):.2f}"
        vram     = str(int(o.get("gpu_ram", 0) / 1024))
        bw_raw   = o.get("inet_down", 0)
        bw       = f"{bw_raw:.0f}"
        cuda_raw = float(o.get("cuda_max_good", 0))
        cuda_str = str(o.get("cuda_max_good", "?"))
        geo      = o.get("geolocation", "?")
        bw_col   = "green" if bw_raw >= 2000 else ("yellow" if bw_raw >= 500 else "red")
        # Warn on CUDA >= 13.0 — Unsloth notes quality issues above this version
        cuda_col = "yellow" if cuda_raw >= 13.0 else "white"
        cuda_disp = f"[{cuda_col}]{cuda_str}{'⚠' if cuda_raw >= 13.0 else ''}[/{cuda_col}]"
        tbl.add_row(oid, dph, rel, f"{vram}GB",
                    f"[{bw_col}]{bw}[/{bw_col}]", cuda_disp, geo)
        label = f"{oid:<11} ${dph}/hr  rel={rel}  {vram}GB  {bw}Mbps  cuda={cuda_str}  {geo}"
        choices_map[label] = oid
        choice_list.append(label)

    console.print(tbl)
    if not choice_list:
        console.print("[red]No offers available.[/red]"); return None

    sel = questionary.select(
        "Pick an offer:",
        choices=["[auto] cheapest matching"] + ask_back(choice_list),
        style=MENU_STYLE, use_shortcuts=False,
    ).ask()

    if sel is None or sel == "← Back": return None
    if sel.startswith("[auto]"): return ""
    return choices_map.get(sel, "")

# ── launch wizard ─────────────────────────────────────────────────────────────

GEOS = {
    "EU Nordic   (SE/NO/FI/DK/IS)":    "EU_NORDIC",
    "EU Broad    (+ DE/NL/FR/UK/...)": "EU",
    "US":                               "US",
    "Any":                              "ANY",
}

MODES = {
    "thinking    (temp 1.0, top-p 0.95, presence 1.5)":  "thinking",
    "coding      (temp 0.6, top-p 0.95, presence 0.0)":  "coding",
    "nonthinking (temp 0.7, top-p 0.80, thinking OFF)":  "nonthinking",
}

KV_TYPES = {
    "q8_0  (half KV VRAM, good quality — default)": "q8_0",
    "q4_0  (quarter KV VRAM, tight-fit)":            "q4_0",
    "bf16  (full precision, most VRAM)":              "bf16",
}

def menu_launch(recipes, gpu_tiers, docker_cfg):
    hr("Launch wizard")

    # ── pinned quant from HF browser ──────────────────────────────────────────
    pin = None
    if HF_PIN.exists():
        try:
            pin = json.loads(HF_PIN.read_text())
            console.print(Panel(
                f"[green]Pinned quant available:[/green]\n"
                f"  repo  = {pin['MODEL_REPO']}\n"
                f"  quant = {pin['MODEL_QUANT']}  ({pin['size']})\n"
                f"[dim]Select the pinned option in the recipe step to use it.[/dim]",
                border_style="#3d3d5c", padding=(0, 1),
            ))
        except Exception:
            pin = None

    # 1. GPU tier
    gpu_choices = {v.get("label", k): k for k, v in gpu_tiers.items()}
    if not gpu_choices:
        console.print("[red]No GPU tiers defined in recipes.toml[/red]")
        press_enter(); return

    gpu_label = questionary.select(
        "GPU tier:", choices=ask_back(list(gpu_choices.keys())), style=MENU_STYLE,
    ).ask()
    if gpu_label is None or gpu_label == "← Back": return
    gpu_key = gpu_choices[gpu_label]

    # 2. Recipe
    gpu_recipes = [r for r in recipes if r.get("gpu") == gpu_key]
    if not gpu_recipes:
        console.print(f"[yellow]No recipes for GPU={gpu_key} in recipes.toml[/yellow]")
        press_enter(); return

    recipe_labels = [r.get("label", r["name"]) for r in gpu_recipes]
    extra_labels  = []
    if pin:
        extra_labels = [f"[pinned] {pin['MODEL_QUANT']} from HF browser"]

    recipe_label = questionary.select(
        "Model recipe:",
        choices=ask_back(extra_labels + recipe_labels),
        style=MENU_STYLE,
    ).ask()
    if recipe_label is None or recipe_label == "← Back": return

    chosen_recipe = None
    custom_repo   = None
    custom_quant  = None

    if pin and recipe_label.startswith("[pinned]"):
        chosen_recipe = gpu_recipes[0]   # use first recipe for defaults
        custom_repo   = pin["MODEL_REPO"]
        custom_quant  = pin["MODEL_QUANT"]
    else:
        idx = recipe_labels.index(recipe_label)
        chosen_recipe = gpu_recipes[idx]

    # 3. Mode
    mode_label = questionary.select(
        "Inference mode:", choices=ask_back(list(MODES.keys())), style=MENU_STYLE,
    ).ask()
    if mode_label is None or mode_label == "← Back": return
    mode_key = MODES[mode_label]

    # 4. GEO
    geo_label = questionary.select(
        "Geographic preference:", choices=ask_back(list(GEOS.keys())), style=MENU_STYLE,
    ).ask()
    if geo_label is None or geo_label == "← Back": return
    geo_key = GEOS[geo_label]

    # 5. KV type (default from recipe, override offered)
    default_kv    = chosen_recipe.get("kv_type", "q8_0")
    default_kv_lbl = next((k for k, v in KV_TYPES.items() if v == default_kv),
                           list(KV_TYPES.keys())[0])
    kv_label = questionary.select(
        f"KV cache type (recipe default: {default_kv}):",
        choices=ask_back(list(KV_TYPES.keys())),
        style=MENU_STYLE,
        default=default_kv_lbl,
    ).ask()
    if kv_label is None or kv_label == "← Back": return
    kv_key = KV_TYPES[kv_label]

    # 6. Vision — auto-enable if recipe declares mmproj, else prompt
    recipe_mmproj = chosen_recipe.get("mmproj", "")
    if recipe_mmproj:
        mmproj_val = recipe_mmproj
        console.print(f"[dim]Vision: mmproj={mmproj_val} (from recipe)[/dim]")
    else:
        mmproj = questionary.select(
            "Vision support (mmproj, adds ~2 GB VRAM):",
            choices=["No (text-only, recommended)", "Yes — enable mmproj F16", "← Back"],
            style=MENU_STYLE,
        ).ask()
        if mmproj is None or mmproj == "← Back": return
        mmproj_val = "F16" if mmproj.startswith("Yes") else ""

    # 7. Max price
    tier_cfg      = gpu_tiers.get(gpu_key, {})
    default_price = str(tier_cfg.get("max_price", "0.55"))
    price_ans = questionary.text(
        f"Max price $/hr ceiling (default {default_price}):",
        default=default_price, style=MENU_STYLE,
    ).ask()
    if price_ans is None: return
    max_price = price_ans.strip() or default_price

    # 8. Offer
    browse_sel = questionary.select(
        "Offer selection:",
        choices=["Auto — cheapest matching offer",
                 "Browse — pick from live offer list",
                 "← Back"],
        style=MENU_STYLE,
    ).ask()
    if browse_sel is None or browse_sel == "← Back": return

    offer_id = ""
    if browse_sel.startswith("Browse"):
        tier_cfg   = gpu_tiers.get(gpu_key, {})
        num_gpus   = chosen_recipe.get("num_gpus", tier_cfg.get("num_gpus", 1))
        min_cuda   = chosen_recipe.get("min_cuda", tier_cfg.get("min_cuda", "12.8"))
        offer_id   = browse_offers(gpu_key, geo_key, max_price,
                                   tier_cfg=tier_cfg, num_gpus=num_gpus, min_cuda=min_cuda)
        if offer_id is None: return

    # ── summary ───────────────────────────────────────────────────────────────
    tier_cfg    = gpu_tiers.get(gpu_key, {})
    image_type  = chosen_recipe.get("image_type", tier_cfg.get("image_type", "prebuilt"))
    docker_img  = image_for_type(docker_cfg, image_type)
    num_gpus    = chosen_recipe.get("num_gpus", tier_cfg.get("num_gpus", 1))
    min_cuda    = chosen_recipe.get("min_cuda", tier_cfg.get("min_cuda", "12.8"))
    vast_names  = tier_cfg.get("vast_names", [])
    if isinstance(vast_names, list):
        vast_names_str = " ".join(vast_names)
    else:
        vast_names_str = str(vast_names)

    hr()
    t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t.add_column("k", style="dim", width=16)
    t.add_column("v", style="bold")
    t.add_row("GPU",        gpu_key)
    t.add_row("Recipe",     chosen_recipe.get("label", chosen_recipe["name"]))
    t.add_row("HF repo",    custom_repo or chosen_recipe.get("model_repo", "?"))
    t.add_row("Quant",      custom_quant or chosen_recipe.get("model_quant", "?"))
    t.add_row("Ctx",        str(chosen_recipe.get("ctx", "?")))
    t.add_row("Parallel",   str(chosen_recipe.get("parallel", 1)))
    t.add_row("Mode",       mode_key)
    t.add_row("KV type",    kv_key)
    t.add_row("GEO",        geo_key)
    t.add_row("Vision",     mmproj_val or "off")
    t.add_row("Max $/hr",   max_price)
    t.add_row("Num GPUs",   str(num_gpus))
    t.add_row("Min CUDA",   str(min_cuda))
    t.add_row("Offer ID",   offer_id or "auto-select")
    t.add_row("Image type", f"[{'green' if image_type == 'prebuilt' else 'yellow'}]{image_type}[/]  {cold_start_estimate(image_type)}")
    t.add_row("Image",      docker_img)
    t.add_row("HOST",       "[green]127.0.0.1[/green]  (tunnel-only)")
    console.print(Panel(t, title="[bold]Launch config[/bold]", border_style="#3d3d5c"))

    if not questionary.confirm("Proceed with launch?", style=MENU_STYLE, default=True).ask():
        return

    # ── build env and fire vast_up.sh ─────────────────────────────────────────
    env = os.environ.copy()
    env["GPU"]          = gpu_key
    env["MODEL_REPO"]   = custom_repo  or chosen_recipe.get("model_repo", "")
    env["MODEL_QUANT"]  = custom_quant or chosen_recipe.get("model_quant", "")
    env["CTX"]          = str(chosen_recipe.get("ctx", 65536))
    env["PARALLEL"]     = str(chosen_recipe.get("parallel", 1))
    env["KV_TYPE"]      = kv_key
    env["MODE"]         = mode_key
    env["GEO"]          = geo_key
    env["MAX_PRICE"]    = max_price
    env["DOCKER_IMAGE"] = docker_img
    env["IMAGE_TYPE"]   = image_type
    env["MIN_DISK_GB"]  = str(chosen_recipe.get("min_disk_gb", tier_cfg.get("min_disk_gb", 60)))
    env["NUM_GPUS"]     = str(num_gpus)
    env["MIN_CUDA"]     = str(min_cuda)
    env["MODEL"]        = chosen_recipe.get("name", "custom")
    if vast_names_str:
        env["VAST_NAMES"] = vast_names_str

    if mmproj_val:
        env["MMPROJ"] = mmproj_val
    if offer_id:
        env["OFFER_ID"] = offer_id

    hr("Launching...")
    r = subprocess.run(["bash", str(ROOT / "vast_up.sh")], cwd=ROOT, env=env)
    if r.returncode == 0:
        console.print("\n[green]Instance created![/green]")
        console.print("  → Use [bold]Watch[/bold] to follow boot progress.")
        console.print("  → Once healthy: [bold]Tunnel → up[/bold] then [bold]Smoke[/bold].")
        if HF_PIN.exists():
            HF_PIN.unlink()
    else:
        console.print(f"\n[red]vast_up.sh exited {r.returncode}[/red]")

    press_enter()

# ── tunnel ────────────────────────────────────────────────────────────────────

def menu_tunnel():
    while True:
        hr("Tunnel")
        tun = "[green]running[/green]" if tunnel_running() else "[red]down[/red]"
        console.print(f"  Tunnel: {tun}  (local :{LOCAL_PORT} → container :8000)")

        choice = questionary.select(
            "Action:",
            choices=["up — start tunnel", "status — detailed info",
                     "down — stop tunnel", "logs — tail container log", "← Back"],
            style=MENU_STYLE,
        ).ask()
        if choice is None or choice == "← Back": return

        cmd = choice.split()[0]
        console.rule()
        run(f"bash {ROOT}/tools/vast_tunnel.sh {cmd}")
        press_enter()

# ── destroy ───────────────────────────────────────────────────────────────────

def menu_destroy():
    inst_id = last_instance()
    if not inst_id:
        console.print("[yellow]No .last_instance found.[/yellow]")
        press_enter(); return

    hr("Destroy instance")
    console.print(f"  Will destroy: [bold]{inst_id}[/bold]")
    if tunnel_running():
        console.print("  [yellow]Tunnel is running — will be stopped first.[/yellow]")

    if not questionary.confirm(
        f"Destroy instance {inst_id}? This is irreversible.",
        style=MENU_STYLE, default=False,
    ).ask():
        return

    if tunnel_running():
        run(f"bash {ROOT}/tools/vast_tunnel.sh down")
    hr()
    run(f"bash {ROOT}/vast_down.sh")
    press_enter()

# ── smoke test ────────────────────────────────────────────────────────────────

def menu_smoke():
    hr("Smoke test")
    default_url = f"http://127.0.0.1:{LOCAL_PORT}" if tunnel_running() else ""
    url = questionary.text(
        "Endpoint base URL (no /v1 suffix):",
        default=default_url, style=MENU_STYLE,
    ).ask()
    if not url: return
    hr()
    run(f"bash {ROOT}/smoke.sh {url}")
    press_enter()

# ── instance list ─────────────────────────────────────────────────────────────

def menu_instances():
    hr("All your Vast instances")
    raw, err, rc = capture("vastai show instances --raw 2>/dev/null", timeout=15)
    if rc != 0 or not raw or raw in ("[]", "null", ""):
        console.print("[dim]No instances found.[/dim]")
        if err: console.print(f"[red]{err}[/red]")
        press_enter(); return

    try:
        instances = json.loads(raw)
    except Exception:
        console.print("[red]Could not parse instances JSON[/red]"); press_enter(); return

    if not instances:
        console.print("[dim]No active instances.[/dim]"); press_enter(); return

    tbl = Table(box=box.SIMPLE, show_lines=False, title="Active Instances")
    tbl.add_column("ID",     style="dim",  width=11)
    tbl.add_column("Status", width=10)
    tbl.add_column("GPU",    width=18)
    tbl.add_column("$/hr",   style="cyan", width=7)
    tbl.add_column("geo",    style="dim")
    ids, labels = [], []
    for i in instances:
        status = str(i.get("actual_status", "?"))
        color  = {"running": "green", "loading": "yellow"}.get(status, "red")
        tbl.add_row(str(i.get("id","?")),
                    f"[{color}]{status}[/{color}]",
                    str(i.get("gpu_name","?")),
                    f"{i.get('dph_total',0):.3f}",
                    str(i.get("geolocation","?")))
        ids.append(str(i.get("id","?")))
        labels.append(f"{i.get('id')}  ({status})  {i.get('gpu_name','')}  {i.get('geolocation','')}")

    console.print(tbl)
    sel = questionary.select(
        "Set .last_instance to:", choices=labels + ["← Skip"], style=MENU_STYLE,
    ).ask()
    if sel and sel != "← Skip":
        new_id = ids[labels.index(sel)]
        LAST_INST.write_text(new_id)
        console.print(f"[green]Set .last_instance → {new_id}[/green]")
    press_enter()

# ── main ──────────────────────────────────────────────────────────────────────

def banner(docker_img):
    console.print(Panel(
        "[bold #7c6af7]vastai-gguf-launcher[/bold #7c6af7]  "
        "[dim]GGUF model endpoints on Vast.ai GPUs[/dim]\n"
        f"[dim]image: {docker_img}  |  recipes: recipes.toml[/dim]",
        border_style="#3d3d5c", padding=(0, 2),
    ))

def main():
    cfg, recipes, gpu_tiers, docker_cfg = load_config()

    while True:
        console.clear()
        banner(docker_cfg.get("prebuilt", "ghcr.io/buckster123/vastai-gguf:prebuilt"))
        show_status()

        choice = questionary.select(
            "What do you want to do?",
            choices=[
                "Launch    — spin up a new instance",
                "Watch     — live boot progress watcher",
                "Diagnose  — deep-dive: processes, download speed, stall recovery",
                "Instances — list / reattach to all active instances",
                "HF Browse — browse model files on HuggingFace, pin a quant",
                "Tunnel    — manage SSH tunnel",
                "Smoke     — run smoke test against endpoint",
                "Destroy   — tear down current instance",
                "Exit",
            ],
            style=MENU_STYLE, use_shortcuts=False,
        ).ask()

        if choice is None or choice.startswith("Exit"):
            console.print("[dim]bye[/dim]"); break
        elif choice.startswith("Launch"):    menu_launch(recipes, gpu_tiers, docker_cfg)
        elif choice.startswith("Watch"):     menu_watch_boot()
        elif choice.startswith("Diagnose"):  menu_diagnose()
        elif choice.startswith("Instances"): menu_instances()
        elif choice.startswith("HF Browse"): menu_hf_browser(recipes)
        elif choice.startswith("Tunnel"):    menu_tunnel()
        elif choice.startswith("Smoke"):     menu_smoke()
        elif choice.startswith("Destroy"):   menu_destroy()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        sys.exit(0)
