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
import signal
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

# Provider config
PROVIDER_DIR   = Path.home() / ".vastai-gguf"
PROVIDER_CFG   = PROVIDER_DIR / "config.toml"

# Usage tracking / rate limiting (optional — graceful fallback)
try:
    from usage_tracker import format_summary, check_rate_limit, format_rate_status
except ImportError:
    # Stub if usage_tracker not available
    def format_summary(hours=24):
        return "(usage tracker not installed)"
    def check_rate_limit(base_url, api_key):
        return {"error": "usage tracker not available"}
    def format_rate_status(info):
        return str(info)

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


# ── provider config ─────────────────────────────────────────────────────────────

DEFAULT_PROVIDERS = {
    "together": {
        "base_url": "https://api.together.ai/v1",
        "label": "Together AI",
    },
}

def load_provider_config():
    """Load provider API keys and base URLs from ~/.vastai-gguf/config.toml.

    Returns dict of provider_name -> {api_key, base_url}.
    Falls back to environment variables (TOGETHER_API_KEY, etc.) if not in file.
    """
    config = {}

    # Load from config file if it exists
    if PROVIDER_CFG.exists():
        try:
            raw = _load_toml(PROVIDER_CFG)
            providers_raw = raw.get("providers", {})
            for pkey, pval in providers_raw.items():
                cfg = {}
                if isinstance(pval, dict):
                    cfg["base_url"] = pval.get("base_url", DEFAULT_PROVIDERS.get(pkey, {}).get("base_url", ""))
                    cfg["api_key"]  = pval.get("api_key", "")
                config[pkey] = cfg
        except Exception as e:
            console.print(f"[yellow]Warning: could not parse {PROVIDER_CFG}: {e}[/yellow]")

    # Merge defaults for any provider not yet in config
    for pkey, default in DEFAULT_PROVIDERS.items():
        if pkey not in config:
            config[pkey] = {"base_url": default.get("base_url", ""), "api_key": ""}
        elif not config[pkey].get("base_url"):
            config[pkey]["base_url"] = default.get("base_url", "")

    # Environment variable overrides
    env_map = {
        "together": "TOGETHER_API_KEY",
    }
    for pkey, env_var in env_map.items():
        env_val = os.environ.get(env_var)
        if env_val and pkey in config:
            # Only use env var if no file-based key (don't overwrite explicit config)
            if not config[pkey].get("api_key"):
                config[pkey]["api_key"] = env_val

    return config


def save_provider_config(config):
    """Write provider config back to ~/.vastai-gguf/config.toml."""
    PROVIDER_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ~/.vastai-gguf/config.toml — provider configuration",
        "#",
        "# Edit this file to add API keys and base URLs for external providers.",
        "# You can also set environment variables (TOGETHER_API_KEY, etc.) as fallback.",
        "",
    ]

    for pkey, pval in config.items():
        lines.append(f"[providers.{pkey}]")
        if pval.get("base_url"):
            lines.append(f'base_url  = "{pval["base_url"]}"')
        if pval.get("api_key"):
            lines.append(f'api_key   = "{pval["api_key"]}"')
        else:
            lines.append("# Set your API key here, or export the corresponding env var")
            lines.append("# api_key = \"...\"")
        lines.append("")

    PROVIDER_CFG.write_text("\n".join(lines))


def test_together_connection(base_url, api_key):
    """Test Together AI connection by listing models. Returns (ok, message)."""
    if not api_key:
        return False, "No API key configured"

    url = f"{base_url}/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "vastai-gguf-launcher/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

            # Handle both {"data": [...]} and raw list formats
            if isinstance(data, dict):
                models = data.get("data", [])
            elif isinstance(data, list):
                models = data
            else:
                return False, f"Unexpected response format: {type(data).__name__}"

            # Filter to actual dicts with "id" field
            model_ids = []
            for m in (models or []):
                if isinstance(m, dict) and "id" in m:
                    model_ids.append(m["id"])

            if model_ids:
                examples = ", ".join(model_ids[:5])
                return True, f"OK — {len(model_ids)} models available. Examples: {examples}..."
            else:
                return False, "API responded but returned no usable models"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Authentication failed — check your API key"
        elif e.code == 429:
            return False, f"Rate limited ({e.code}) — try again later"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def run_together_completion(base_url, api_key, model_id, prompt="Say hello in 5 words"):
    """Run a quick completion through Together API. Returns (ok, message)."""
    url = f"{base_url}/chat/completions"
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 20,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            prompt_t = usage.get("prompt_tokens", "?")
            output_t = usage.get("completion_tokens", "?")
            return True, f"OK — '{content}' ({output_t} tokens)"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return False, str(e)


# ── provider config menu ──────────────────────────────────────────────────────

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
        "User-Agent": "vastai-gguf-launcher/1.0",
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
    family_choices = sorted(families.keys()) + ["[all families]", "← Back"]
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


# ── Together endpoint activation ──────────────────────────────────────────────

def activate_together_endpoint(provider_cfg, model_id):
    """Activate a Together AI endpoint. Validates and records in .last_instance."""
    togeth = provider_cfg.get("together", {})
    api_key  = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
    base_url = togeth.get("base_url", DEFAULT_PROVIDERS["together"]["base_url"])

    if not api_key:
        console.print("[red]No Together AI API key configured.[/red]")
        return False

    # Validate model exists
    ok, msg = test_together_connection(base_url, api_key)
    if not ok:
        console.print(f"[red]Connection failed: {msg}[/red]")
        return False

    # Quick smoke test + usage logging
    console.print(f"\n[dim]Testing completion with {model_id}...[/dim]")
    
    url_comp = f"{base_url}/chat/completions"
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Say hello in 5 words"}],
        "max_tokens": 20,
    }).encode()

    req = urllib.request.Request(url_comp, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
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
            log_completion("together", model_id, prompt_t, output_t)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()
        except Exception: pass
        ok2 = False
        msg2 = f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        ok2 = False
        msg2 = str(e)
    if not ok2:
        console.print(f"[yellow]Completion test failed: {msg2}[/yellow]")
        if not questionary.confirm("Activate anyway?", style=MENU_STYLE, default=False).ask():
            return False

    # Record as active endpoint
    endpoint_info = {
        "provider": "together",
        "model_id": model_id,
        "base_url": base_url,
        "endpoint": f"{base_url}/chat/completions",
        "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Save alongside .last_instance (which tracks Vast instances)
    endpoint_file = ROOT / ".active_endpoint"
    endpoint_file.write_text(json.dumps(endpoint_info, indent=2))

    console.print(f"\n[green]✓ Together endpoint activated![/green]")
    console.print(f"  Model:    {model_id}")
    console.print(f"  Endpoint: {endpoint_info['endpoint']}")
    console.print(f"  Config:   ~/.vastai-gguf/config.toml")

    return True


def get_active_endpoint():
    """Get currently active endpoint (Vast instance or Together)."""
    # Check Together endpoint first
    endpoint_file = ROOT / ".active_endpoint"
    if endpoint_file.exists():
        try:
            data = json.loads(endpoint_file.read_text())
            if data.get("provider") == "together":
                return data
        except Exception:
            pass

    # Fall back to Vast instance
    inst_id = last_instance()
    if inst_id:
        d = get_instance_json(inst_id)
        if d:
            return {
                "provider": "vast-gguf",
                "instance_id": inst_id,
                "status": d.get("actual_status", "?"),
            }

    return None


# ── cost estimator ────────────────────────────────────────────────────────────

def estimate_cost(ctx_tokens, output_tokens, provider_cfg):
    """Estimate endpoint costs for different providers.

    Returns dict with provider estimates:
    - vast_gguf: $/hr equivalent for given usage
    - together: actual token-based cost
    """
    # Default throughput assumptions (tokens/sec)
    vast_throughput = 100  # Conservative estimate for consumer GPUs

    # Vast cost model (per hour, based on typical pricing)
    vast_hourly_rates = {
        "5090": 0.34, "4090": 0.28, "6000pro": 0.93,
        "h100-sxm": 2.50, "a100-sxm": 1.20, "h200-sxm": 3.50,
    }

    # Together pricing per 1M tokens (input/output)
    together_rates = {
        "meta-llama/Llama-3.1-8B-Instruct-Turbo":      {"in": 0.18, "out": 0.18},
        "Qwen/Qwen2.5-Coder-32B-Instruct-Turbo":       {"in": 0.44, "out": 0.44},
        "meta-llama/Llama-3.3-70B-Instruct-Turbo":     {"in": 0.88, "out": 0.88},
        "Qwen/Qwen2.5-72B-Instruct-Turbo":             {"in": 0.88, "out": 0.88},
        "meta-llama/Llama-3.1-405B-Instruct-Turbo":    {"in": 3.50, "out": 3.50},
        "mistralai/Mixtral-8x7B-Instruct-v0.1":        {"in": 0.60, "out": 0.60},
        "Qwen/QwQ-32B-Preview":                        {"in": 0.44, "out": 0.44},
    }

    estimates = {}

    # Vast GGUF estimate (hourly cost to process this many tokens)
    total_tokens = ctx_tokens + output_tokens
    vast_hours_needed = total_tokens / (vast_throughput * 3600)  # hours to process
    avg_vast_rate = sum(vast_hourly_rates.values()) / len(vast_hourly_rates)
    estimates["vast-gguf"] = {
        "cost_usd": round(vast_hours_needed * avg_vast_rate, 4),
        "rate": f"${avg_vast_rate:.2f}/hr (avg)",
        "type": "hourly",
    }

    # Together estimate (token-based)
    if provider_cfg and "together" in provider_cfg:
        together_key = provider_cfg["together"].get("api_key")
        if together_key:  # Only show Together costs if configured
            avg_together_rate = sum(rates["in"] for rates in together_rates.values()) / len(together_rates)
            together_cost = (ctx_tokens + output_tokens) * (avg_together_rate / 1_000_000)
            estimates["together"] = {
                "cost_usd": round(together_cost, 4),
                "rate": f"${avg_together_rate:.2f}/M tok",
                "type": "per-token",
            }

    return estimates


def format_cost_comparison(ctx_tokens=1000, output_tokens=500, provider_cfg=None):
    """Format cost comparison as a readable string for TUI."""
    ests = estimate_cost(ctx_tokens, output_tokens, provider_cfg)

    lines = []
    lines.append(f"Usage: {ctx_tokens} prompt + {output_tokens} completion tokens")

    for provider, data in ests.items():
        label = {"vast-gguf": "Vast GGUF", "together": "Together AI"}.get(provider, provider)
        lines.append(f"  {label:<12} ${data['cost_usd']:.4f}  ({data['rate']})")

    return "\n".join(lines)


# ── usage tracking ─────────────────────────────────────────────────────────────

USAGE_LOG  = PROVIDER_DIR / "usage.log"
USAGE_DIR  = PROVIDER_DIR

def ensure_usage_dir():
    USAGE_DIR.mkdir(parents=True, exist_ok=True)

def log_completion(provider, model_id="", prompt_tokens=0, completion_tokens=0):
    """Log a completion request to usage.jsonl for cost tracking."""
    ensure_usage_dir()
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": provider,
        "model_id": model_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }

    # Estimate cost
    if provider == "together":
        avg_rate = 0.88 / 1_000_000  # Average Together pricing per token
        entry["cost_usd"] = round((prompt_tokens + completion_tokens) * avg_rate, 6)
    else:
        hours_used = (prompt_tokens + completion_tokens) / (100 * 3600)  # tokens / throughput
        entry["cost_usd"] = round(hours_used * 0.50, 4)

    try:
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[usage] Failed to log: {e}", file=sys.stderr)


def get_session_costs():
    """Summarize usage costs for current session.

    Returns dict with provider totals and grand total.
    """
    if not USAGE_LOG.exists():
        return {}

    try:
        entries = []
        with open(USAGE_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        costs = {}
        grand_total = 0.0

        for entry in entries:
            provider = entry.get("provider", "unknown")
            cost = entry.get("cost_usd", 0)
            prompt_t = entry.get("prompt_tokens", 0)
            output_t = entry.get("completion_tokens", 0)

            if provider not in costs:
                costs[provider] = {"cost": 0.0, "tokens": 0}

            costs[provider]["cost"] += cost
            costs[provider]["tokens"] += prompt_t + output_t
            grand_total += cost

        return {
            "by_provider": costs,
            "grand_total": round(grand_total, 4),
            "total_entries": len(entries),
        }
    except Exception:
        return {}


def format_usage_summary(provider_cfg=None):
    """Format usage summary as readable text for TUI."""
    data = get_session_costs()

    if not data:
        return "[dim]No usage tracked yet[/dim]"

    lines = []
    lines.append(f"Total sessions: {data['total_entries']} completions")
    lines.append(f"Grand total: ${data['grand_total']:.4f}")

    for provider, info in data["by_provider"].items():
        label = {"together": "Together AI", "vast-gguf": "Vast GGUF"}.get(provider, provider)
        lines.append(f"  {label:<12} ${info['cost']:.4f}  ({info['tokens']} tokens)")

    return "\n".join(lines)


# ── batch comparison mode ────────────────────────────────────────────────────────

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

    # Run comparisons concurrently using asyncio and aiohttp
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
                prompt_tokens=len(prompt.split()) * 1.3,  # Rough estimate
                completion_tokens=result["tokens"],
            )

    press_enter()


# ── rate limit awareness ─────────────────────────────────────────────────────────

def check_together_rate_limits(provider_cfg):
    """Check Together AI rate limits and usage.

    Returns dict with rate limit info or None if not configured.
    """
    togeth = provider_cfg.get("together", {}) if provider_cfg else {}
    api_key = togeth.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))

    if not api_key:
        return None

    base_url = togeth.get("base_url", DEFAULT_PROVIDERS["together"]["base_url"])
    url = f"{base_url}/models"  # Simple probe request
    
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "vastai-gguf-launcher/1.0",
        })
        
        with urllib.request.urlopen(req, timeout=10) as r:
            rate_limit = r.headers.get("X-RateLimit-Limit")
            remaining = r.headers.get("X-RateLimit-Remaining")
            reset = r.headers.get("X-RateLimit-Reset")

            return {
                "limit": int(rate_limit) if rate_limit else None,
                "remaining": int(remaining) if remaining else None,
                "reset": int(reset) if reset else None,
            }
    except Exception:
        return None


def format_rate_limits(provider_cfg=None):
    """Format rate limit info for TUI display."""
    rl = check_together_rate_limits(provider_cfg)

    if not rl:
        return "[dim]Rate limits: Not available (check provider config)[/dim]"

    parts = []
    if rl.get("limit"):
        parts.append(f"Limit: {rl['limit']}/period")
    if rl.get("remaining") is not None:
        parts.append(f"Remaining: {rl['remaining']}")
    if rl.get("reset"):
        import datetime
        reset_time = datetime.datetime.fromtimestamp(rl["reset"]).strftime("%H:%M:%S")
        parts.append(f"Resets: {reset_time}")

    return " | ".join(parts)


# ── helpers ───────────────────────────────────────────────────────────────────

def capture(cmd, timeout=15):
    r = subprocess.run(cmd, shell=True, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def run(cmd, **kw):
    return subprocess.run(cmd, shell=True, cwd=ROOT, **kw)

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

def menu_diagnose(provider_cfg=None):
    inst_id = last_instance()
    if not inst_id and not get_active_endpoint():
        console.print("[yellow]No active instance or endpoint found.[/yellow]")
        press_enter(); return

    hr("Diagnostics")

    # Show usage summary
    usage_str = format_summary(24)
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
            rate_info = check_rate_limit(base_url, api_key)
            rl_str = format_rate_status(rate_info)
            console.print(Panel(rl_str, title="[bold]Together Rate Status[/bold]", 
                               border_style="#3d3d5c"))

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

def menu_launch(recipes, gpu_tiers, docker_cfg, provider_cfg=None):
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

    # ── pinned provider from Together browser ──────────────────────────────────
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
            "Together AI — managed inference, pay per token",
            "← Back",
        ],
        style=MENU_STYLE,
    ).ask()

    if provider_label is None or provider_label == "← Back":
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


def _proxy_up():
    """Start the proxy server."""
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
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "endpoint_proxy.py")],
        stdout=open("/tmp/vastai-gguf-proxy.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )

    pid_file.write_text(str(proc.pid))
    console.print(f"[green]✓ Proxy started (PID {proc.pid})[/green]")
    console.print("[dim]Waits for target to become available...[/dim]")


def _proxy_down(pid_file):
    """Stop the proxy server."""
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


def tail_proxy_logs():
    """Tail proxy output."""
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


def proxy_status_detail():
    """Show detailed proxy status."""
    base_url, _, provider = resolve_target()

    # Check backend availability
    vast_ok = False
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3",
             f"http://127.0.0.1:{VAST_TUNNEL_PORT}/v1/models"],
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

    t.add_row("Vast GGUF", vast_status, f"http://127.0.0.1:{VAST_TUNNEL_PORT}/v1")
    t.add_row("Together AI", togeth_status, "https://api.together.ai/v1")

    console.print(Panel(t, border_style="#3d3d5c"))


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
    provider_cfg = load_provider_config()

    while True:
        console.clear()
        banner(docker_cfg.get("prebuilt", "ghcr.io/buckster123/vastai-gguf:prebuilt"))
        show_status(provider_cfg)

        choice = questionary.select(
            "What do you want to do?",
            choices=[
                "Launch     — spin up a new instance or activate managed endpoint",
                "Providers  — configure API keys and base URLs",
                "Together   — browse Together AI models",
                "Batch      — compare multiple providers/models side-by-side",
                "Watch      — live boot progress watcher",
                "Diagnose   — usage stats, rate limits, deep diagnostics",
                "Instances  — list / reattach to all active instances",
                "HF Browse  — browse model files on HuggingFace, pin a quant",
                "Tunnel     — manage SSH tunnel",
                "Smoke      — run smoke test against endpoint",
                "Proxy      — unified local endpoint (localhost:8888)",
                "Destroy    — tear down current instance",
                "Exit",
            ],
            style=MENU_STYLE, use_shortcuts=False,
        ).ask()

        if choice is None or choice.startswith("Exit"):
            console.print("[dim]bye[/dim]"); break
        elif choice.startswith("Launch"):     menu_launch(recipes, gpu_tiers, docker_cfg, provider_cfg)
        elif choice.startswith("Providers"):  menu_providers(provider_cfg)
        elif choice.startswith("Together"):   menu_together_models(provider_cfg)
        elif choice.startswith("Batch"):     menu_batch_compare(provider_cfg)
        elif choice.startswith("Watch"):     menu_watch_boot()
        elif choice.startswith("Diagnose"):  menu_diagnose(provider_cfg)
        elif choice.startswith("Instances"): menu_instances()
        elif choice.startswith("HF Browse"):  menu_hf_browser(recipes)
        elif choice.startswith("Tunnel"):     menu_tunnel()
        elif choice.startswith("Smoke"):      menu_smoke(provider_cfg)
        elif choice.startswith("Proxy"):      menu_proxy()
        elif choice.startswith("Destroy"):    menu_destroy()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        sys.exit(0)
