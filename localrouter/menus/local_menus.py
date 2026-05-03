"""Local endpoint menus — config, status, launch wizard, and dispatch."""
from __future__ import annotations

from pathlib import Path

import questionary
from rich.panel import Panel
from rich.table import Table
from rich import box

from ..config import console, MENU_STYLE, ROOT, LOCAL_LOGS, load_config
from ..local_endpoint import (
    discover_local,
    start_local_instance,
    stop_local_instance,
    is_local_running,
    list_local_instances,
)
from ..helpers import ask_back, hr, press_enter
from ..providers import get_active_endpoint, load_provider_config


# ── Local hardware configuration ─────────────────────────────────────────────

def menu_local_config():
    """Configure local LLM settings and auto-discover hardware."""
    while True:
        hr("Local Endpoint Configuration")

        disc = discover_local()

        t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
        t.add_column("key", style="dim", width=14)
        t.add_column("value", style="bold")

        if disc["binaries"]:
            bins = "\n".join(f"  {b['path']}" for b in disc["binaries"][:3])
            t.add_row("binaries", f"[green]{len(disc['binaries'])} found[/green]\n{bins}")
        else:
            t.add_row("binaries", "[red]none found[/red]")

        if disc["backends"]:
            backends = ", ".join(f"[{'green' if b == disc['backends'][0] else ''}]{b}[/{'green' if b == disc['backends'][0] else ''}]" for b in disc["backends"])
            t.add_row("backends", backends)
        else:
            t.add_row("backends", "[yellow]cpu (fallback)[/yellow]")

        if disc["models"]:
            top = "\n".join(f"  {m['name'][:50]} ({m['size_mb']} MB)" for m in disc["models"][:3])
            t.add_row("models", f"[green]{len(disc['models'])} found[/green]\n{top}")
        else:
            t.add_row("models", "[red]none found[/red]")

        console.print(Panel(t, border_style="#3d3d5c"))

        choice = questionary.select(
            "Action:",
            choices=[
                "Refresh scan  — rescan for binaries and models",
                "Set models dir  — custom directory to scan",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            return

        if choice.startswith("Refresh"):
            disc = discover_local()
            console.print(f"\n[dim]Scan complete.[/dim]")
        elif choice.startswith("Set models"):
            current = str(Path.home() / "models")
            new_dir = questionary.text(
                "Models directory (default ~/models):",
                default=current,
                style=MENU_STYLE,
            ).ask()
            if new_dir and new_dir.strip():
                console.print(f"\n[dim]To use this permanently, add to recipes.toml:[/dim]")
                console.print(f"  [local]\n  models_dir = \"{new_dir.strip()}\"")

        press_enter()


# ── Local endpoint status ────────────────────────────────────────────────────

def menu_local_status(provider_cfg=None):
    """View and manage running local instances."""
    hr("Local Endpoint Status")

    instances = list_local_instances()

    if not instances:
        console.print("[dim]No local instances configured. Run 'Local → Launch' to start one.[/dim]")
        press_enter()
        return

    t = Table(box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Name", style="bold #7c6af7")
    t.add_column("Status", width=10)
    t.add_column("Port")
    t.add_column("Model")
    t.add_column("Started")

    for inst in instances:
        name = inst.get("name", "?")
        status = inst.get("status", "?")
        color = {"running": "green", "starting": "yellow", "stopped": "red"}.get(status, "dim")
        model_short = Path(inst.get("model_path", "")).name[:35] if inst.get("model_path") else "?"
        t.add_row(
            name,
            f"[{color}]{status}[/{color}]",
            str(inst.get("port", "?")),
            model_short,
            inst.get("started_at", "?")[-8:] if inst.get("started_at") else "?",
        )

    console.print(Panel(t, border_style="#3d3d5c"))

    # Action selection
    names = [inst["name"] for inst in instances]
    sel = questionary.select(
        "Select instance:",
        choices=ask_back(names),
        style=MENU_STYLE,
    ).ask()

    if sel is None or sel == "← Back":
        return

    chosen = next((i for i in instances if i["name"] == sel), None)
    if not chosen:
        return

    running = chosen.get("running", False)
    action_choices = ["View logs"] + (["Stop instance"] if running else []) + ["← Back"]

    action = questionary.select(
        f"Action for '{sel}':",
        choices=ask_back(action_choices),
        style=MENU_STYLE,
    ).ask()

    if action is None or action == "← Back":
        return

    if action.startswith("View"):
        log_file = LOCAL_LOGS / f"{sel}.log"
        if not log_file.exists():
            console.print("[yellow]No log file found.[/yellow]")
        else:
            hr(f"Logs for {sel}")
            content = log_file.read_text()
            console.print(content[-2000:] if len(content) > 2000 else content)
    elif action.startswith("Stop"):
        ok, msg = stop_local_instance(sel)
        color = "green" if ok else "yellow"
        console.print(f"\n[{color}]{msg}[/{color}]")

    press_enter()


# ── Local endpoint launch wizard ─────────────────────────────────────────────

def menu_local_launch(recipes):
    """Launch wizard for local LLM endpoints."""
    hr("Local Endpoint Launch Wizard")

    # Filter local recipes
    local_recipes = [r for r in recipes if r.get("provider") == "local"]

    if not local_recipes:
        console.print("[yellow]No local recipes defined.[/yellow]")
        console.print("[dim]Add recipes with provider=local to recipes.toml, then try again.[/dim]")
        press_enter()
        return

    # Auto-discover for defaults
    disc = discover_local()

    if not disc["binaries"]:
        console.print("[red]No llama-server binary found on this system.[/red]")
        console.print("[dim]Run 'Local → Configure' to scan, or install llama.cpp.[/dim]")
        press_enter()
        return

    # Show available recipes
    t = Table(box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Name", style="bold")
    t.add_column("Label")
    t.add_column("Model")
    t.add_column("Port")
    t.add_column("Status")

    for r in local_recipes:
        name = r.get("name", "?")
        model_short = Path(r.get("model_path", "")).name[:35] if r.get("model_path") else "?"
        port = r.get("port", "8100")
        running = is_local_running(name)
        status = "[green]running[/green]" if running else "[dim]stopped[/dim]"
        t.add_row(name, r.get("label", name), model_short, str(port), status)

    console.print(Panel(t, border_style="#3d3d5c"))

    # Pick recipe
    labels = [r.get("label", r["name"]) for r in local_recipes]
    sel = questionary.select(
        "Recipe to launch:",
        choices=ask_back(labels),
        style=MENU_STYLE,
    ).ask()

    if sel is None or sel == "← Back":
        return

    try:
        idx = labels.index(sel)
    except ValueError:
        return

    recipe = local_recipes[idx]
    name = recipe.get("name", "?")

    # Check if already running
    if is_local_running(name):
        console.print(f"[yellow]'{name}' is already running. Use 'Local → Status' to manage.[/yellow]")
        press_enter()
        return

    # Confirm summary
    hr()
    t2 = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
    t2.add_column("k", style="dim", width=14)
    t2.add_column("v", style="bold")
    t2.add_row("name",     name)
    t2.add_row("model",    recipe.get("model_path", "?"))
    t2.add_row("ctx",      str(recipe.get("ctx", 32768)))
    t2.add_row("parallel", str(recipe.get("parallel", 1)))
    t2.add_row("kv type",  recipe.get("kv_type", "q8_0"))
    t2.add_row("mode",     recipe.get("mode", "thinking"))
    t2.add_row("port",     str(recipe.get("port", 8100)))
    t2.add_row("backend",  recipe.get("backend", "auto"))
    if recipe.get("description"):
        t2.add_row("desc",   recipe["description"])

    console.print(Panel(t2, title="[bold]Launch config[/bold]", border_style="#3d3d5c"))

    if not questionary.confirm("Start this local instance?", style=MENU_STYLE, default=True).ask():
        return

    # Launch!
    ok, msg = start_local_instance(recipe)
    color = "green" if ok else "red"
    console.print(f"\n[{color}]{msg}[/{color}]")
    press_enter()


# ── Local endpoint dispatch (umbrella menu) ──────────────────────────────────

def menu_local_dispatch(provider_cfg=None):
    """Local endpoint management — umbrella menu."""
    while True:
        hr("Local Endpoints")

        ep = get_active_endpoint()
        if ep and ep.get("provider") == "local":
            status_str = "[green]running[/green]" if ep.get("status") == "running" else "[yellow]stopped[/yellow]"
            console.print(f"  Active: [bold]{ep.get('name', '?')}[/bold] ({status_str})  port {ep.get('port', '?')}")
        else:
            console.print("  [dim]No local endpoint active.[/dim]")

        instances = list_local_instances()
        running_count = sum(1 for i in instances if i.get("running"))

        choice = questionary.select(
            "Action:",
            choices=[
                f"Launch    — start a local instance ({len([r for r in load_config()[1] if r.get('provider') == 'local'])} recipes)",
                "Status    — view / manage running instances",
                "Configure — scan hardware, set options",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            return

        if choice.startswith("Launch"):
            cfg, recipes, _, _ = load_config()
            menu_local_launch(recipes)
        elif choice.startswith("Status"):
            menu_local_status(provider_cfg)
        elif choice.startswith("Configure"):
            menu_local_config()
