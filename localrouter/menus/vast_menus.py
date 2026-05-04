"""
localrouter.menus.vast_menus — Vast.ai launch wizard, tunnel, destroy, instances, boot watcher.

Extracted from vast_manager.py lines 2366-2660, 2664-2681, 2685-2706,
2921-2963, 1871-1917.

BUG FIX C1: Added ``gpu_choices`` dict construction before first use at
the GPU-tier selection step (was previously undefined, causing a crash).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import questionary
from rich.panel import Panel
from rich.table import Table
from rich import box

from ..config import (
    console, MENU_STYLE, GEOS, MODES, KV_TYPES, ROOT, LOCAL_PORT,
    LOCAL_TUNNEL_PORT, HF_PIN, LAST_INST, PROVIDER_DIR,
    load_config, image_for_type, cold_start_estimate,
)
from ..helpers import (
    capture, run, last_instance, tunnel_running,
    get_instance_json, ask_back, hr, press_enter,
)
from ..providers import activate_together_endpoint, get_active_endpoint
from ..cost import estimate_cost, format_cost_comparison, log_completion
from ..vast_ops import browse_offers

# menu_local_launch may not exist yet (created in a later split step)
try:
    from .local_menus import menu_local_launch
except ImportError:
    def menu_local_launch(recipes):  # type: ignore[misc]
        console.print("[yellow]Local launch not yet available.[/yellow]")
        press_enter()


# ── launch wizard ────────────────────────────────────────────────────────────

def menu_launch(recipes, gpu_tiers, docker_cfg, provider_cfg=None):
    hr("Launch wizard")

    # ── pinned quant from HF browser ─────────────────────────────────────────
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

    # ── pinned provider from Together browser ────────────────────────────────
    pinned_provider = None
    pin_prov_file = PROVIDER_DIR / ".pinned_provider"
    if pin_prov_file.exists():
        try:
            pinned_provider = json.loads(pin_prov_file.read_text())
        except Exception:
            pinned_provider = None

    # 0. Provider selection
    provider_label = questionary.select(
        "Compute type:",
        choices=[
            "Vast GGUF   — rent a GPU, run your own llama.cpp instance",
            "Local       — run llama-server on your own hardware",
            "Together AI — managed inference, pay per token",
            "← Back",
        ],
        style=MENU_STYLE,
    ).ask()

    if provider_label is None or provider_label == "← Back":
        return

    if provider_label.startswith("Local"):
        menu_local_launch(recipes)
        return

    if provider_label.startswith("Together"):
        # Managed endpoint flow
        hr("Activate Together AI Endpoint")

        togeth = provider_cfg.get("together", {}) if provider_cfg else {}
        api_key = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))

        if not api_key:
            console.print("[red]No Together AI API key configured.[/red]")
            console.print("[dim]Run 'Providers → Configure Together AI' first, or set TOGETHER_API_KEY.[/dim]")
            press_enter()
            return

        # Model selection — show popular models + custom option
        popular_models = [
            ("meta-llama/Llama-3.1-8B-Instruct-Turbo",  "$0.18/M tok"),
            ("Qwen/Qwen2.5-Coder-32B-Instruct-Turbo",   "$0.44/M tok"),
            ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "$0.88/M tok"),
            ("Qwen/Qwen2.5-72B-Instruct-Turbo",         "$0.88/M tok"),
            ("meta-llama/Llama-3.1-405B-Instruct-Turbo", "$3.50/M tok"),
        ]

        model_choices = [f"{mid}  ({price})" for mid, price in popular_models] + ["[custom] enter model ID manually"]
        if pinned_provider and "model_id" in pinned_provider:
            mid = pinned_provider["model_id"]
            model_choices.insert(0, f"[pinned] {mid}")

        sel = questionary.select(
            "Model:", choices=model_choices + ["← Back"], style=MENU_STYLE,
        ).ask()

        if sel is None or sel == "← Back":
            return

        if sel.startswith("[custom]"):
            model_id = questionary.text(
                "Enter Together model ID (e.g. meta-llama/Llama-3.1-8B-Instruct-Turbo):",
                style=MENU_STYLE,
            ).ask()
            if not model_id or not model_id.strip():
                return
            model_id = model_id.strip()
        elif sel.startswith("[pinned]"):
            model_id = pinned_provider["model_id"]
        else:
            model_id = sel.split()[0]

        # Activate the endpoint
        if activate_together_endpoint(provider_cfg, model_id):
            press_enter()
            return

    # ── BUG FIX C1: build gpu_choices from gpu_tiers ─────────────────────────
    # Previously gpu_choices was used without being defined, causing a crash
    # when launching a Vast instance.
    gpu_choices = {tier.get('label', key): key for key, tier in gpu_tiers.items()}

    # 1. GPU tier
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

    # ── summary ──────────────────────────────────────────────────────────────
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
    if chosen_recipe.get("llama_cpp_repo") or chosen_recipe.get("llama_cpp_ref"):
        repo = chosen_recipe.get("llama_cpp_repo", "ggml-org/llama.cpp")
        ref = chosen_recipe.get("llama_cpp_ref", "master")
        t.add_row("llama.cpp", f"[yellow]{repo} @ {ref}[/yellow]")
    t.add_row("HOST",       "[green]127.0.0.1[/green]  (tunnel-only)")

    # Add cost comparison if provider_cfg is available
    ctx_tokens = int(chosen_recipe.get("ctx", 65536))
    ests = estimate_cost(ctx_tokens, 1000, provider_cfg)
    if ests:
        cost_lines = []
        for prov, data in ests.items():
            label = {"vast-gguf": "Vast GGUF", "together": "Together AI"}.get(prov, prov)
            cost_lines.append(f"${data['cost_usd']:.4f} ({data['rate']})")
        t.add_row("est. cost", " / ".join(cost_lines))

    console.print(Panel(t, title="[bold]Launch config[/bold]", border_style="#3d3d5c"))

    if not questionary.confirm("Proceed with launch?", style=MENU_STYLE, default=True).ask():
        return

    # ── build env and fire vast_up.sh ────────────────────────────────────────
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

    # Custom llama.cpp repo/branch (for models needing unmerged PRs, e.g. DSv4)
    if chosen_recipe.get("llama_cpp_repo"):
        env["LLAMA_CPP_REPO"] = chosen_recipe["llama_cpp_repo"]
    if chosen_recipe.get("llama_cpp_ref"):
        env["LLAMA_CPP_REF"] = chosen_recipe["llama_cpp_ref"]

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


# ── tunnel ───────────────────────────────────────────────────────────────────

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


# ── destroy ──────────────────────────────────────────────────────────────────

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


# ── instances ────────────────────────────────────────────────────────────────

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


# ── boot watcher ─────────────────────────────────────────────────────────────

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
