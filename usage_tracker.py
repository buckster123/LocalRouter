#!/usr/bin/env python3
"""
usage_tracker.py — Completion tracking and cost estimation.

Logs every completion through the system with:
- Provider (together/vast-gguf/custom)
- Model ID
- Prompt/completion tokens  
- Estimated USD cost
- Timestamp

Provides session summaries, provider breakdowns, and rate limit awareness.
"""

import json
import time
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent.resolve()
PROVIDER_DIR = Path.home() / ".vastai-gguf"
USAGE_LOG  = PROVIDER_DIR / "usage.log"

# Pricing per 1M tokens (input/output) for Together models
TOGETHER_RATES = {
    "meta-llama/Llama-3.1-8B-Instruct-Turbo":      {"in": 0.18, "out": 0.18},
    "Qwen/Qwen2.5-Coder-32B-Instruct-Turbo":       {"in": 0.44, "out": 0.44},
    "meta-llama/Llama-3.3-70B-Instruct-Turbo":     {"in": 0.88, "out": 0.88},
    "Qwen/Qwen2.5-72B-Instruct-Turbo":             {"in": 0.88, "out": 0.88},
    "meta-llama/Llama-3.1-405B-Instruct-Turbo":    {"in": 3.50, "out": 3.50},
    "mistralai/Mixtral-8x7B-Instruct-v0.1":        {"in": 0.60, "out": 0.60},
    "Qwen/QwQ-32B-Preview":                        {"in": 0.44, "out": 0.44},
}

# Vast GGUF pricing: hourly rates per GPU tier (approximate)
VAST_HOURLY = {
    "5090": 0.34, "4090": 0.28, "6000pro": 0.93,
    "h100-sxm": 2.50, "a100-sxm": 1.20, "h200-sxm": 3.50,
}
VAST_AVG_HOURLY = sum(VAST_HOURLY.values()) / len(VAST_HOURLY)

# Default throughput assumptions (tokens/sec) for cost estimation
VAST_THROUGHPUT = {"5090": 80, "4090": 70, "6000pro": 120,
                   "h100-sxm": 136, "a100-sxm": 110, "h200-sxm": 150}


# ── logging ───────────────────────────────────────────────────────────────────

def ensure_log_dir():
    PROVIDER_DIR.mkdir(parents=True, exist_ok=True)


def log_completion(provider, model_id="", prompt_tokens=0, completion_tokens=0, 
                   gpu_tier=None, extra=None):
    """Log a completion event to usage.log.

    Args:
        provider: "together", "vast-gguf", or other
        model_id: Model identifier (Together slug or GGUF filename)
        prompt_tokens: Input token count  
        completion_tokens: Output token count
        gpu_tier: Vast GPU tier key for throughput estimation (optional)
        extra: Additional metadata dict (optional)

    Returns:
        Estimated cost in USD.
    """
    ensure_log_dir()
    
    # Estimate cost
    cost = 0.0
    if provider == "together":
        rates = TOGETHER_RATES.get(model_id, {"in": 0.88, "out": 0.88})
        cost = (prompt_tokens * rates["in"] + completion_tokens * rates["out"]) / 1_000_000
    elif provider == "vast-gguf":
        # Estimate based on throughput → hours needed → hourly cost
        tps = VAST_THROUGHPUT.get(gpu_tier or "", 80)  # Default 80 t/s
        hours_needed = (prompt_tokens + completion_tokens) / (tps * 3600)
        rate = VAST_HOURLY.get(gpu_tier or "", VAST_AVG_HOURLY)
        cost = hours_needed * rate
    elif provider == "local":
        # Local inference is free (already paid for hardware)
        cost = 0.0

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch": time.time(),
        "provider": provider,
        "model_id": model_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": round(cost, 6),
    }

    if extra:
        entry["extra"] = extra

    try:
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[usage] Failed to write log entry: {e}", flush=True)

    return cost


# ── aggregation & reporting ───────────────────────────────────────────────────

def load_entries():
    """Load all usage entries from log."""
    if not USAGE_LOG.exists():
        return []
    
    entries = []
    try:
        with open(USAGE_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    
    return entries


def get_session_summary(hours=24):
    """Summarize usage for the last N hours.

    Returns dict with provider breakdown and totals.
    """
    entries = load_entries()
    cutoff = time.time() - (hours * 3600)
    filtered = [e for e in entries if e.get("epoch", 0) >= cutoff]

    summary = {
        "total_cost": 0.0,
        "total_tokens": 0,
        "completions": 0,
        "by_provider": {},
        "by_model": {},
    }

    for e in filtered:
        provider = e.get("provider", "?")
        model_id = e.get("model_id", "?")
        cost = e.get("cost_usd", 0)
        tokens = e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)

        summary["total_cost"] += cost
        summary["total_tokens"] += tokens
        summary["completions"] += 1

        # Provider breakdown
        if provider not in summary["by_provider"]:
            summary["by_provider"][provider] = {"cost": 0.0, "tokens": 0}
        summary["by_provider"][provider]["cost"] += cost
        summary["by_provider"][provider]["tokens"] += tokens

        # Model breakdown
        key = f"{provider}/{model_id}"
        if key not in summary["by_model"]:
            summary["by_model"][key] = {"cost": 0.0, "tokens": 0}
        summary["by_model"][key]["cost"] += cost
        summary["by_model"][key]["tokens"] += tokens

    summary["total_cost"] = round(summary["total_cost"], 4)
    return summary


def format_summary(hours=24):
    """Format usage summary as readable text for TUI."""
    data = get_session_summary(hours)
    
    if not data["completions"]:
        return "[dim]No completions logged in the last 24h[/dim]"

    lines = []
    label = f"last {hours}h" if hours != 24 else "today"
    lines.append(f"[bold]Usage ({label})[/bold]: ${data['total_cost']:.4f} | "
                 f"{data['total_tokens']} tokens | {data['completions']} completions")

    for provider, info in data["by_provider"].items():
        label = {"together": "Together AI", "vast-gguf": "Vast GGUF", "local": "Local"}.get(provider, provider)
        lines.append(f"  {label:<14} ${info['cost']:.4f}  ({info['tokens']} tok)")

    if data["by_model"]:
        lines.append("")
        lines.append("Top models:")
        for key, info in sorted(data["by_model"].items(), 
                                 key=lambda x: -x[1]["cost"])[:5]:
            short_key = key.replace("/together/", "/").replace("/vast-gguf/", "/")[:40]
            lines.append(f"    {short_key:<38} ${info['cost']:.4f}  ({info['tokens']} tok)")

    return "\n".join(lines)


# ── rate limit checker ────────────────────────────────────────────────────────

def check_rate_limit(base_url, api_key):
    """Check Together AI rate limits via a probe request.

    Returns dict with rate limit info or None on failure.
    """
    import urllib.request

    if not api_key or not base_url:
        return None

    url = f"{base_url}/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "LocalRouter/1.0",
    })

    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            # Parse rate limit from response headers if present
            headers = dict(r.headers)
            rate_info = {}

            for key in ("X-RateLimit-Limit", "X-RateLimit-Remaining", 
                        "X-RateLimit-Reset", "X-RateLimit-Window"):
                val = headers.get(key)
                if val:
                    rate_info[key.split("-", 1)[-1].lower()] = val

            # Check response body for model count as availability indicator
            data = json.loads(r.read())
            models = data.get("data", []) if isinstance(data, dict) else data
            rate_info["models_available"] = len(models)

            return rate_info or {"status": "ok", "models_available": 0}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "?")
            return {"error": "rate_limited", "retry_after": retry_after}
        return {"error": f"http_{e.code}", "message": str(e.reason)}
    except Exception as e:
        return {"error": "connection_failed", "message": str(e)}


def format_rate_status(rate_info):
    """Format rate limit info for TUI display."""
    if not rate_info:
        return "[dim]Rate limits: Not available[/dim]"

    if "error" in rate_info:
        err = rate_info["error"]
        if err == "rate_limited":
            retry = rate_info.get("retry_after", "?")
            return f"[red]Rate limited![/red] Retry after {retry}s"
        return f"[yellow]Check failed:[/yellow] {err} — {rate_info.get('message', '')}"

    parts = []
    if "models_available" in rate_info:
        parts.append(f"{rate_info['models_available']} models available")
    
    # Add any rate limit header info
    for key in ("limit", "remaining", "reset", "window"):
        val = rate_info.get(key)
        if val:
            parts.append(f"{key}: {val}")

    return "[green]OK[/green]" + (f" — {' | '.join(parts)}" if parts else "")


# ── batch comparison helpers ──────────────────────────────────────────────────

def run_batch_compare(model_configs, prompt, max_tokens=300):
    """Send the same prompt to multiple providers/models.

    Args:
        model_configs: List of dicts with {provider, base_url, api_key, model_id}
        prompt: The text prompt to send
        max_tokens: Maximum tokens per completion

    Returns:
        List of result dicts with provider/model/content/timing/tokens/cost.
    """
    import urllib.request

    results = []

    for cfg in model_configs:
        provider = cfg.get("provider", "unknown")
        base_url = cfg.get("base_url", "")
        api_key = cfg.get("api_key", "")
        model_id = cfg.get("model_id", "")

        start_time = time.time()

        try:
            url = f"{base_url}/chat/completions"
            payload = json.dumps({
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            }).encode()

            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content", "")
                usage = data.get("usage", {})

            latency = time.time() - start_time
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # Estimate cost
            cost = log_completion(provider, model_id, prompt_tokens, completion_tokens)

            results.append({
                "provider": provider,
                "model_id": model_id,
                "content": content.strip(),
                "latency": round(latency, 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
            })

        except Exception as e:
            latency = time.time() - start_time
            results.append({
                "provider": provider,
                "model_id": model_id,
                "content": f"[red]Error: {e}[/red]",
                "latency": round(latency, 2),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0,
            })

    return results


def format_batch_results(results):
    """Format batch comparison results for TUI display."""
    if not results:
        return "[dim]No results to display[/dim]"

    lines = []
    total_cost = sum(r.get("cost_usd", 0) for r in results)
    lines.append(f"[bold]Batch Results[/bold]: {len(results)} models | "
                 f"${total_cost:.4f} total cost\n")

    for i, result in enumerate(results):
        provider_label = {"together": "Together AI", "vast-gguf": "Vast GGUF"}.get(
            result["provider"], result["provider"])
        
        lines.append(f"[bold #7c6af7]── Result {i+1} ──[/bold]")
        lines.append(f"  Provider: {provider_label}")
        lines.append(f"  Model:    {result['model_id']}")
        lines.append(f"  Latency:  {result['latency']}s | "
                     f"Tokens: {result.get('completion_tokens', '?')} out | "
                     f"Cost: ${result.get('cost_usd', 0):.4f}")
        lines.append(f"  Output:   {result['content'][:200]}...")
        lines.append("")

    return "\n".join(lines)
