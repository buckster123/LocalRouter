"""
localrouter.vast_ops — Vast.ai instance operations and offer browsing.

Extracted from vast_manager.py: network diagnostics, container env reading,
stalled-download restart, and GPU offer browser.
"""

import json
import re
import subprocess
import time

import questionary
from rich.table import Table
from rich import box

from .config import console, GEOS, ROOT, MENU_STYLE
from .helpers import capture, ssh_run, get_ssh, get_instance_json, ask_back, hr, press_enter, _fmt_bytes


# ── network diagnostics ──────────────────────────────────────────────────────

def _net_rx_delta(inst_id, seconds=4):
    """Measure bytes received on eth0 over *seconds* via SSH.

    Returns the byte delta (int) or None on failure.
    """
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


# ── container environment ─────────────────────────────────────────────────────

def _get_container_env(inst_id):
    """Read environment variables from the running launch.sh process via SSH.

    Returns a dict of env vars (MODEL_*, CTX, KV_TYPE, MODE, etc.).
    """
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


# ── restart stalled download ──────────────────────────────────────────────────

def _restart_launch(inst_id):
    """Kill a stalled launch.sh process and restart it with the same env vars.

    Reads env from the running process, writes a restart script to /tmp,
    and executes it via SSH in the background.
    """
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
    subprocess.run(f"bash {ROOT}/tools/vast_tunnel.sh logs", shell=True)
    press_enter()


# ── offer browser ─────────────────────────────────────────────────────────────

def browse_offers(gpu_key, geo_key, max_price, tier_cfg=None, num_gpus=1, min_cuda="12.8"):
    """Search Vast.ai for GPU offers matching the given criteria.

    Args:
        gpu_key:   GPU short name (e.g. '4090', '6000pro').
        geo_key:   Geographic region key from GEOS.
        max_price: Maximum $/hr.
        tier_cfg:  Optional tier config dict with 'vast_names' list.
        num_gpus:  Number of GPUs required.
        min_cuda:  Minimum CUDA version string.

    Returns:
        Offer ID string, empty string for auto-cheapest, or None if cancelled.
    """
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
