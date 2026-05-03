"""Cost estimation, usage tracking, and rate-limit awareness.

Provides token-based cost estimation for Together AI and hourly cost
estimation for Vast GGUF, plus JSONL usage logging and rate-limit probing.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from .config import PROVIDER_DIR, console
from .providers import DEFAULT_PROVIDERS

# ---------------------------------------------------------------------------
# Usage tracking paths
# ---------------------------------------------------------------------------

USAGE_LOG = PROVIDER_DIR / "usage.log"
USAGE_DIR = PROVIDER_DIR


def ensure_usage_dir() -> None:
    """Ensure the usage tracking directory exists."""
    USAGE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_cost(
    ctx_tokens: int,
    output_tokens: int,
    provider_cfg: dict | None,
) -> dict:
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

    estimates: dict = {}

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
            avg_together_rate = sum(
                rates["in"] for rates in together_rates.values()
            ) / len(together_rates)
            together_cost = (ctx_tokens + output_tokens) * (avg_together_rate / 1_000_000)
            estimates["together"] = {
                "cost_usd": round(together_cost, 4),
                "rate": f"${avg_together_rate:.2f}/M tok",
                "type": "per-token",
            }

    return estimates


def format_cost_comparison(
    ctx_tokens: int = 1000,
    output_tokens: int = 500,
    provider_cfg: dict | None = None,
) -> str:
    """Format cost comparison as a readable string for TUI."""
    ests = estimate_cost(ctx_tokens, output_tokens, provider_cfg)

    lines: list[str] = []
    lines.append(f"Usage: {ctx_tokens} prompt + {output_tokens} completion tokens")

    for provider, data in ests.items():
        label = {"vast-gguf": "Vast GGUF", "together": "Together AI"}.get(provider, provider)
        lines.append(f"  {label:<12} ${data['cost_usd']:.4f}  ({data['rate']})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Usage logging (JSONL)
# ---------------------------------------------------------------------------


def log_completion(
    provider: str,
    model_id: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Log a completion request to usage.jsonl for cost tracking."""
    ensure_usage_dir()
    entry: dict = {
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


def get_session_costs() -> dict:
    """Summarize usage costs for current session.

    Returns dict with provider totals and grand total.
    """
    if not USAGE_LOG.exists():
        return {}

    try:
        entries: list[dict] = []
        with open(USAGE_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        costs: dict = {}
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


def format_usage_summary(provider_cfg: dict | None = None) -> str:
    """Format usage summary as readable text for TUI."""
    data = get_session_costs()

    if not data:
        return "[dim]No usage tracked yet[/dim]"

    lines: list[str] = []
    lines.append(f"Total sessions: {data['total_entries']} completions")
    lines.append(f"Grand total: ${data['grand_total']:.4f}")

    for provider, info in data["by_provider"].items():
        label = {"together": "Together AI", "vast-gguf": "Vast GGUF"}.get(provider, provider)
        lines.append(f"  {label:<12} ${info['cost']:.4f}  ({info['tokens']} tokens)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Together AI rate-limit awareness
# ---------------------------------------------------------------------------


def check_together_rate_limits(provider_cfg: dict | None) -> dict | None:
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
            "User-Agent": "LocalRouter/1.0",
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


def format_rate_limits(provider_cfg: dict | None = None) -> str:
    """Format rate limit info for TUI display."""
    rl = check_together_rate_limits(provider_cfg)

    if not rl:
        return "[dim]Rate limits: Not available (check provider config)[/dim]"

    parts: list[str] = []
    if rl.get("limit"):
        parts.append(f"Limit: {rl['limit']}/period")
    if rl.get("remaining") is not None:
        parts.append(f"Remaining: {rl['remaining']}")
    if rl.get("reset"):
        import datetime
        reset_time = datetime.datetime.fromtimestamp(rl["reset"]).strftime("%H:%M:%S")
        parts.append(f"Resets: {reset_time}")

    return " | ".join(parts)
