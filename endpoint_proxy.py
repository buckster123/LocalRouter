#!/usr/bin/env python3
"""
endpoint_proxy.py — Unified local OpenAI-compatible endpoint.

Forwards all requests to whichever provider is currently active:
- Vast GGUF instances (via SSH tunnel at localhost:8000)
- Together AI managed endpoints (direct API calls)
- Any custom OpenAI-compatible backend

Listen on: http://localhost:8888/v1/...
"""

import os
import sys
import json
import time
import asyncio
import signal
from pathlib import Path

# Usage tracking
USAGE_LOG  = Path.home() / ".vastai-gguf/usage.log"
USAGE_DIR  = USAGE_LOG.parent
from urllib.parse import urlparse, urljoin

try:
    from aiohttp import web, ClientSession, ClientTimeout
except ImportError:
    print("Missing dep: aiohttp")
    print("Run: pip3 install aiohttp --break-system-packages")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).parent.resolve()
PROVIDER_DIR = Path.home() / ".vastai-gguf"
PROXY_PID   = Path("/tmp/vastai-gguf-proxy.pid")
PORT        = int(os.environ.get("PROXY_PORT", "8888"))

# Default Vast tunnel port (set by vast_tunnel.sh)
VAST_TUNNEL_PORT = 8000


# ── endpoint resolver ────────────────────────────────────────────────────────

def resolve_target():
    """Read .active_endpoint and return (base_url, auth_header)."""
    ep_file = ROOT / ".active_endpoint"

    # Check managed endpoint first
    if ep_file.exists():
        try:
            data = json.loads(ep_file.read_text())
            provider = data.get("provider")

            if provider == "together":
                base_url = data.get("base_url", "https://api.together.ai/v1")
                # Get API key from config or env
                api_key = os.environ.get("TOGETHER_API_KEY")
                if not api_key:
                    cfg_file = PROVIDER_DIR / "config.toml"
                    if cfg_file.exists():
                        for line in cfg_file.read_text().splitlines():
                            if line.startswith("api_key"):
                                key_val = line.split("=", 1)[1].strip().strip('"')
                                if key_val and not key_val.startswith("#"):
                                    api_key = key_val
                                    break
                auth = f"Bearer {api_key}" if api_key else None
                return base_url, auth, provider

            elif provider == "local":
                host = data.get("host", "127.0.0.1")
                port = data.get("port", 8100)
                api_key = data.get("api_key", "")
                auth = f"Bearer {api_key}" if api_key else None
                return f"http://{host}:{port}/v1", auth, provider

        except Exception as e:
            print(f"[proxy] Failed to parse active endpoint: {e}", file=sys.stderr)

    # Fall back to Vast tunnel (localhost:8000)
    return f"http://127.0.0.1:{VAST_TUNNEL_PORT}/v1", None, "vast-gguf"


# ── request forwarder ────────────────────────────────────────────────────────

async def forward_request(request: web.Request) -> web.Response:
    """Forward an HTTP request to the resolved target endpoint."""
    base_url, auth_header, provider = resolve_target()

    # Build target URL
    path = request.rel_url.path
    query = request.rel_url.query_string if request.rel_url.query_string else ""
    target_url = f"{base_url.rstrip('/')}{path}"
    if query:
        target_url += f"?{query}"

    # Prepare headers
    headers = dict(request.headers)
    if auth_header:
        headers["Authorization"] = auth_header
    # Strip any proxy-specific headers from client
    for h in ("X-Proxy-Forwarded", "X-Provider"):
        headers.pop(h, None)

    timeout = ClientTimeout(total=300)  # 5 min for long generations

    try:
        async with ClientSession(timeout=timeout) as session:
            method = request.method.upper()

            # Read body (for POST/PUT/PATCH)
            body = await request.read() if method in ("POST", "PUT", "PATCH") else None

            if method == "GET":
                async with session.get(target_url, headers=headers) as resp:
                    return await build_response(resp)
            elif method == "DELETE":
                async with session.delete(target_url, headers=headers) as resp:
                    return await build_response(resp)
            else:
                async with session.post(target_url, headers=headers, data=body) as resp:
                    return await build_streaming_response(resp)

    except ConnectionRefusedError:
        return web.json_response(
            {"error": f"Cannot connect to {base_url} — is the backend running?"},
            status=502,
        )
    except Exception as e:
        return web.json_response(
            {"error": str(e)},
            status=503,
        )


async def build_response(resp):
    """Build a standard non-streaming response."""
    content = await resp.read()
    headers = dict(resp.headers)

    # Add provider info
    headers["X-Provider"] = resolve_target()[2]
    headers["Access-Control-Allow-Origin"] = "*"

    # Try to estimate cost from usage in response body
    try:
        data = json.loads(content)
        usage = data.get("usage", {})
        if usage:
            prompt_t = usage.get("prompt_tokens", 0)
            output_t = usage.get("completion_tokens", 0)
            headers["X-Usage"] = f"{prompt_t}+{output_t}"
    except Exception:
        pass

    return web.Response(
        status=resp.status,
        body=content,
        headers=headers,
    )


async def build_streaming_response(resp):
    """Build an SSE streaming response (for chat/completions with stream=true)."""
    headers = dict(resp.headers)
    headers["X-Provider"] = resolve_target()[2]
    headers["Access-Control-Allow-Origin"] = "*"

    # Use web.Response with streaming body for SSE
    async def stream():
        async for chunk in resp.content.iter_chunked(4096):
            yield chunk

    return web.StreamResponse(
        status=resp.status,
        headers=headers,
    )


# ── management endpoints ─────────────────────────────────────────────────────

async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint."""
    base_url, _, provider = resolve_target()
    return web.json_response({
        "ok": True,
        "provider": provider,
        "target": base_url,
        "uptime": time.time() - START_TIME,
    })


async def switch_provider(request: web.Request) -> web.Response:
    """Switch active provider."""
    data = await request.json()
    provider_name = data.get("provider")

    if provider_name == "together":
        api_key = data.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
        base_url = data.get("base_url", "https://api.together.ai/v1")

        # Get model_id from request or use default
        model_id = data.get("model_id", "meta-llama/Llama-3.1-8B-Instruct-Turbo")

        ep = {
            "provider": "together",
            "model_id": model_id,
            "base_url": base_url,
            "endpoint": f"{base_url}/chat/completions",
            "switched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        (ROOT / ".active_endpoint").write_text(json.dumps(ep, indent=2))
        return web.json_response({"status": "ok", "provider": "together"})

    elif provider_name == "vast-gguf":
        # Remove active endpoint file to fall back to Vast
        ep_file = ROOT / ".active_endpoint"
        if ep_file.exists():
            ep_file.unlink()
        return web.json_response({"status": "ok", "provider": "vast-gguf"})

    elif provider_name == "local":
        # Switch to a local instance by name
        name = data.get("name", "")
        if not name:
            return web.json_response({"error": "Missing 'name' for local provider"}, status=400)
        instances_dir = ROOT.parent / ".vastai-gguf" / "local_instances"
        meta_file = instances_dir / f"{name}.json"
        if not meta_file.exists():
            return web.json_response({"error": f"Local instance '{name}' not found"}, status=404)
        meta = json.loads(meta_file.read_text())
        ep = {
            "provider": "local",
            "name": name,
            "host": meta.get("host", "127.0.0.1"),
            "port": meta.get("port", 8100),
            "model_path": meta.get("model_path", ""),
            "switched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (ROOT / ".active_endpoint").write_text(json.dumps(ep, indent=2))
        return web.json_response({"status": "ok", "provider": "local"})

    else:
        return web.json_response({"error": f"Unknown provider: {provider_name}"}, status=400)


async def list_providers(request: web.Request) -> web.Response:
    """List available providers and their status."""
    base_url, auth, provider = resolve_target()

    # Check Vast tunnel status
    vast_ok = False
    try:
        async with ClientSession(timeout=ClientTimeout(3)) as session:
            async with session.get(f"http://127.0.0.1:{VAST_TUNNEL_PORT}/v1/models") as r:
                vast_ok = r.status == 200
    except Exception:
        pass

    # Check Together status (if configured)
    together_ok = False
    togeth_key = os.environ.get("TOGETHER_API_KEY", "")
    if togeth_key:
        try:
            async with ClientSession(timeout=ClientTimeout(5)) as session:
                async with session.get(
                    "https://api.together.ai/v1/models",
                    headers={"Authorization": f"Bearer {togeth_key}"},
                ) as r:
                    together_ok = r.status == 200
        except Exception:
            pass

    # Check local instances
    local_instances = []
    from pathlib import Path as P
    instances_dir = ROOT.parent / ".vastai-gguf" / "local_instances"
    if instances_dir.exists():
        for meta_file in instances_dir.glob("*.json"):
            try:
                import json as j
                meta = j.loads(meta_file.read_text())
                name = meta.get("name", "")
                port = meta.get("port", 0)
                pid_file = instances_dir / f"{name}.pid"
                running = False
                if pid_file.exists():
                    try:
                        import os as o
                        o.kill(int(pid_file.read_text().strip()), 0)
                        running = True
                    except Exception:
                        pass
                local_instances.append({
                    "name": name,
                    "port": port,
                    "running": running,
                })
            except Exception:
                pass

    return web.json_response({
        "active": provider,
        "target": base_url,
        "providers": {
            "vast-gguf": {"available": vast_ok, "url": f"http://127.0.0.1:{VAST_TUNNEL_PORT}/v1"},
            "together": {"available": together_ok, "url": "https://api.together.ai/v1"},
        },
        "local_instances": local_instances,
    })


# ── server startup ───────────────────────────────────────────────────────────

START_TIME = time.time()


def ensure_usage_dir():
    USAGE_DIR.mkdir(parents=True, exist_ok=True)

async def log_completion(data):
    """Log a completion request to usage.jsonl for cost tracking."""
    ensure_usage_dir()
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": resolve_target()[2],
    }

    # Extract provider, model, and usage from response data
    if isinstance(data, dict):
        usage = data.get("usage", {})
        entry["prompt_tokens"] = usage.get("prompt_tokens", 0)
        entry["completion_tokens"] = usage.get("completion_tokens", 0)

        # Try to extract model from choices or directly from data
        model = data.get("model")
        if not model:
            choices = data.get("choices", [])
            for c in choices:
                msg = c.get("message", {})
                if msg and "role" in msg:
                    model_id = None
                    break

        # Estimate cost based on provider
        est_provider = entry["provider"]
        prompt_t = entry["prompt_tokens"]
        output_t = entry["completion_tokens"]

        if est_provider == "together":
            # Average Together pricing per 1M tokens
            avg_rate = 0.88 / 1_000_000
            entry["cost_usd"] = round((prompt_t + output_t) * avg_rate, 6)
        else:
            # Vast hourly rate (approximate)
            hours_used = (prompt_t + output_t) / (100 * 3600)  # tokens / (tok/s * s/hour)
            entry["cost_usd"] = round(hours_used * 0.50, 4)

        if model:
            entry["model"] = model

    # Append to JSONL log
    try:
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[proxy] Failed to log usage: {e}", file=sys.stderr)


def create_app() -> web.Application:
    ensure_usage_dir()  # Ensure usage dir exists on startup
    app = web.Application()

    # Management endpoints
    app.router.add_get("/health", health_check)
    app.router.add_get("/providers", list_providers)
    app.router.add_post("/switch", switch_provider)

    # Catch-all for OpenAI-compatible routes
    app.router.add_route("*", "/{tail:.*}", forward_request)

    return app


def run():
    """Run the proxy server."""
    app = create_app()

    # Write PID file
    PROXY_PID.write_text(str(os.getpid()))

    # Handle graceful shutdown
    loop = asyncio.new_event_loop()

    def signal_handler(sig, frame):
        print(f"\n[proxy] Received signal {sig}, shutting down...")
        if PROXY_PID.exists():
            PROXY_PID.unlink()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    base_url, _, provider = resolve_target()
    print(f"[proxy] Starting on http://localhost:{PORT}/")
    print(f"[proxy] Forwarding to: {provider} → {base_url}")
    print(f"[proxy] Management: /health, /providers, /switch")
    print(f"[proxy] PID: {os.getpid()} (saved to {PROXY_PID})")

    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    run()
