"""Provider configuration, connection testing, and endpoint activation.

Handles Together AI provider config loading/saving, connection testing,
completion testing, endpoint activation, and active endpoint resolution.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import questionary
except ImportError:
    questionary = None  # type: ignore[assignment]
from .config import PROVIDER_DIR, PROVIDER_CFG, ROOT, console, _load_toml, MENU_STYLE
from .helpers import last_instance, get_instance_json

# ---------------------------------------------------------------------------
# Default provider definitions
# ---------------------------------------------------------------------------

DEFAULT_PROVIDERS = {
    "together": {
        "base_url": "https://api.together.ai/v1",
        "label": "Together AI",
    },
}

# ---------------------------------------------------------------------------
# Config load / save
# ---------------------------------------------------------------------------


def load_provider_config() -> dict:
    """Load provider API keys and base URLs from ~/.vastai-gguf/config.toml.

    Returns dict of provider_name -> {api_key, base_url}.
    Falls back to environment variables (TOGETHER_API_KEY, etc.) if not in file.
    """
    config: dict = {}

    # Load from config file if it exists
    if PROVIDER_CFG.exists():
        try:
            raw = _load_toml(PROVIDER_CFG)
            providers_raw = raw.get("providers", {})
            for pkey, pval in providers_raw.items():
                cfg: dict = {}
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


def save_provider_config(config: dict) -> None:
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
            lines.append('# api_key = "..."')
        lines.append("")

    PROVIDER_CFG.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Together AI connection helpers
# ---------------------------------------------------------------------------


def test_together_connection(base_url: str, api_key: str) -> tuple[bool, str]:
    """Test Together AI connection by listing models. Returns (ok, message)."""
    if not api_key:
        return False, "No API key configured"

    url = f"{base_url}/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "LocalRouter/1.0",
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


def run_together_completion(
    base_url: str,
    api_key: str,
    model_id: str,
    prompt: str = "Say hello in 5 words",
) -> tuple[bool, str]:
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


# ---------------------------------------------------------------------------
# Endpoint activation
# ---------------------------------------------------------------------------


def activate_together_endpoint(provider_cfg: dict, model_id: str) -> bool:
    """Activate a Together AI endpoint. Validates and records in .active_endpoint."""
    from .cost import log_completion  # avoid circular at module level

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
        try:
            body = e.read().decode()
        except Exception:
            pass
        ok2 = False
        msg2 = f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        ok2 = False
        msg2 = str(e)

    if not ok2:
        console.print(f"[yellow]Completion test failed: {msg2}[/yellow]")
        if questionary is None or not questionary.confirm(
            "Activate anyway?", style=MENU_STYLE, default=False
        ).ask():
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


# ---------------------------------------------------------------------------
# Active endpoint resolution
# ---------------------------------------------------------------------------


def get_active_endpoint() -> dict | None:
    """Get currently active endpoint (Vast instance, Together, or Local).

    Returns a dict describing the active endpoint, or None if nothing is active.
    """
    # Lazy import to break circular dependency with local_endpoint
    from .local_endpoint import is_local_running

    # Check explicit endpoint first (Together / Local)
    endpoint_file = ROOT / ".active_endpoint"
    if endpoint_file.exists():
        try:
            data = json.loads(endpoint_file.read_text())
            prov = data.get("provider")
            if prov == "together":
                return data
            elif prov == "local":
                # Validate PID is still alive
                name = data.get("name", "")
                if name and is_local_running(name):
                    data["status"] = "running"
                else:
                    data["status"] = "stopped"
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
