"""Provider configuration menus — Together AI config wizard and model browser."""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

import questionary
from rich.panel import Panel
from rich.table import Table
from rich import box

from ..config import console, MENU_STYLE, PROVIDER_DIR
from ..providers import (
    DEFAULT_PROVIDERS,
    load_provider_config,
    save_provider_config,
    test_together_connection,
    run_together_completion,
    activate_together_endpoint,
)
from ..helpers import ask_back, hr, press_enter
from ..cost import log_completion


# ── Provider configuration menu ──────────────────────────────────────────────

def menu_providers(provider_cfg):
    """Configure and test external providers."""
    while True:
        hr("Provider Configuration")

        t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
        t.add_column("provider", style="bold #7c6af7", width=18)
        t.add_column("status",   width=24)
        t.add_column("base_url")

        for pkey in sorted(provider_cfg.keys()):
            cfg = provider_cfg[pkey]
            has_key = "✓ set" if cfg.get("api_key") else "[red]not configured[/red]"
            url     = (cfg.get("base_url", "") or "[dim]—[/dim]")[:50]
            label   = DEFAULT_PROVIDERS.get(pkey, {}).get("label", pkey.title())
            t.add_row(label, has_key, url)

        console.print(Panel(t, border_style="#3d3d5c"))

        choice = questionary.select(
            "Action:",
            choices=["Configure Together AI", "← Back"],
            style=MENU_STYLE,
        ).ask()
        if choice is None or choice == "← Back":
            return

        if choice.startswith("Configure Together"):
            _configure_together(provider_cfg)


def _configure_together(provider_cfg):
    """Interactive Together AI config flow."""
    hr("Configure Together AI")

    togeth = provider_cfg.setdefault("together", {})

    # 1. API key
    current_key = togeth.get("api_key", "")
    console.print(f"[dim]Current: {'****' + current_key[-4:] if current_key else '(none)'}[/dim]\n")

    new_key = questionary.password(
        "Together AI API key (leave empty to keep current):",
        style=MENU_STYLE,
    ).ask()

    if new_key is None:
        return
    elif new_key.strip():
        togeth["api_key"] = new_key.strip()
    elif not current_key:
        console.print("[yellow]No key set — test will fail.[/yellow]")

    # 2. Base URL
    current_url = togeth.get("base_url", DEFAULT_PROVIDERS["together"]["base_url"])
    new_url = questionary.text(
        "Base URL (default: https://api.together.ai/v1):",
        default=current_url,
        style=MENU_STYLE,
    ).ask()

    if new_url is not None and new_url.strip():
        togeth["base_url"] = new_url.strip()

    # 3. Test connection
    console.print("\n[dim]Testing connection...[/dim]")
    ok, msg = test_together_connection(togeth.get("base_url", ""), togeth.get("api_key", ""))
    color = "green" if ok else "red"
    console.print(f"  [{color}]{msg}[/{color}]")

    # 4. Quick smoke test with a model
    if ok:
        if questionary.confirm("Run a quick completion test?", style=MENU_STYLE, default=True).ask():
            model = questionary.text(
                "Model ID (default: meta-llama/Llama-3.1-8B-Instruct-Turbo):",
                default="meta-llama/Llama-3.1-8B-Instruct-Turbo",
                style=MENU_STYLE,
            ).ask()
            if model and model.strip():
                console.print(f"\n[dim]Testing completion with {model}...[/dim]")
                # Parse the response for usage logging
                url_comp = f"{togeth.get('base_url', '')}/chat/completions"
                payload = json.dumps({
                    "model": model.strip(),
                    "messages": [{"role": "user", "content": "Say hello in 5 words"}],
                    "max_tokens": 20,
                }).encode()

                req = urllib.request.Request(url_comp, data=payload, headers={
                    "Authorization": f"Bearer {togeth.get('api_key', '')}",
                    "Content-Type": "application/json",
                })

                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        data = json.loads(r.read())
                        choice = data.get("choices", [{}])[0]
                        content = choice.get("message", {}).get("content", "")
                        usage = data.get("usage", {})
                        prompt_t = usage.get("prompt_tokens", 0)
                        output_t = usage.get("completion_tokens", 0)
                        ok2 = True
                        msg2 = f"OK — '{content}' ({output_t} tokens)"

                        # Log to usage tracker
                        log_completion("together", model.strip(), prompt_t, output_t)
                except urllib.error.HTTPError as e:
                    body = ""
                    try: body = e.read().decode()
                    except Exception: pass
                    ok2 = False
                    msg2 = f"HTTP {e.code}: {body[:200]}"
                except Exception as e:
                    ok2 = False
                    msg2 = str(e)
                color2 = "green" if ok2 else "yellow"
                console.print(f"  [{color2}]{msg2}[/{color2}]")

    # 5. Save
    save_provider_config(provider_cfg)
    console.print("\n[green]✓ Configuration saved to ~/.vastai-gguf/config.toml[/green]")
    press_enter()


# ── Together model browser ────────────────────────────────────────────────────

def menu_together_models(provider_cfg):
    """Browse available models on Together AI."""
    hr("Together AI Model Browser")

    togeth = provider_cfg.get("together", {})
    api_key  = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
    base_url = togeth.get("base_url", DEFAULT_PROVIDERS["together"]["base_url"])

    if not api_key:
        console.print("[red]No Together AI API key configured.[/red]")
        console.print("[dim]Run 'Providers → Configure Together AI' first.[/dim]")
        press_enter()
        return

    console.print(f"\n[dim]Fetching model catalog from {base_url}...[/dim]\n")

    url = f"{base_url}/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "LocalRouter/1.0",
    })

    all_models = []
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            # Handle both {"data": [...]} and raw list formats
            if isinstance(data, dict):
                all_models = data.get("data", [])
            elif isinstance(data, list):
                all_models = data
    except Exception as e:
        console.print(f"[red]Failed to fetch models: {e}[/red]")
        press_enter()
        return

    if not all_models:
        console.print("[yellow]No models returned.[/yellow]")
        press_enter()
        return

    # Parse model info (Together returns id and sometimes description)
    model_list = []
    for m in (all_models or []):
        # Skip non-dict entries defensively
        if not isinstance(m, dict):
            continue
        mid = m.get("id", "")
        if not mid:
            continue
        model_list.append({
            "id": mid,
            "name": mid.split("/")[-1] if "/" in mid else mid,
        })

    # Group by org/family
    families = {}
    for m in model_list:
        parts = m["id"].split("/", 1)
        family = parts[0] if len(parts) > 1 else "other"
        families.setdefault(family, []).append(m)

    # Show summary table
    t = Table(title="Model Families", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Family", style="bold #7c6af7")
    t.add_column("Models", style="cyan", justify="right")
    for family in sorted(families.keys()):
        count = len(families[family])
        t.add_row(family, str(count))

    console.print(Panel(t, border_style="#3d3d5c"))
    console.print(f"\n[dim]Total: {len(model_list)} models available[/dim]\n")

    # Browse by family
    family_choices = sorted(families.keys()) + ["[all families]"]
    sel = questionary.select(
        "Browse family:",
        choices=ask_back(family_choices),
        style=MENU_STYLE,
    ).ask()

    if sel is None or sel == "← Back":
        return

    if sel.startswith("[all"):
        show_models = model_list
    elif sel in families:
        show_models = families[sel]
    else:
        return

    # Show models table
    tbl = Table(title=f"Models ({len(show_models)})", box=box.SIMPLE, show_lines=False)
    tbl.add_column("Model ID", style="bold")
    tbl.add_column("Short name", style="dim")

    for m in show_models[:50]:  # Limit to avoid overwhelming terminal
        tbl.add_row(m["id"], m["name"])

    if len(show_models) > 50:
        console.print(f"[dim]... and {len(show_models) - 50} more[/dim]\n")
    console.print(tbl)

    # Pin option
    action = questionary.select(
        "\nAction:",
        choices=["Pin a model for the next launch wizard", "← Back to models"],
        style=MENU_STYLE,
    ).ask()

    if action and action.startswith("Pin"):
        model_id = questionary.autocomplete(
            "Select or type model ID:",
            choices=[m["id"] for m in show_models],
            style=MENU_STYLE,
        ).ask()
        if model_id and model_id.strip():
            # Store as pinned provider recipe
            pin_file = PROVIDER_DIR / ".pinned_provider"
            pin_data = {
                "provider": "together",
                "model_id": model_id.strip(),
                "base_url": base_url,
            }
            pin_file.write_text(json.dumps(pin_data))
            console.print(f"\n[green]Pinned:[/green]  provider=together  model={model_id.strip()}")
            console.print("[dim]Next Launch wizard will offer to use this.[/dim]")

    press_enter()
