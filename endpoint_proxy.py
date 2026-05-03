#!/usr/bin/env python3
"""
endpoint_proxy.py — Unified local OpenAI-compatible endpoint.

Forwards all requests to whichever provider is currently active:
- Vast GGUF instances (via SSH tunnel at localhost:8800)
- Together AI managed endpoints (direct API calls)
- Local llama-server instances
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

try:
    from aiohttp import web, ClientSession, ClientTimeout
except ImportError:
    print("Missing dep: aiohttp")
    print("Run: pip3 install aiohttp --break-system-packages")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────

ROOT         = Path(__file__).parent.resolve()
PROVIDER_DIR = Path.home() / ".vastai-gguf"
PROXY_PID    = Path("/tmp/vastai-gguf-proxy.pid")
PORT         = int(os.environ.get("PROXY_PORT", "8888"))

# Local SSH tunnel port (vast_tunnel.sh forwards remote:8000 → local:8800)
LOCAL_TUNNEL_PORT = 8800

# Usage tracking
USAGE_LOG = PROVIDER_DIR / "usage.log"
USAGE_DIR = PROVIDER_DIR

# Headers that should NOT be forwarded to the backend
_HOP_BY_HOP = frozenset({
    "host", "transfer-encoding", "connection", "keep-alive",
    "upgrade", "te", "trailer", "x-proxy-forwarded", "x-provider",
})

START_TIME = time.time()


# ── endpoint resolver ────────────────────────────────────────────────────────

def resolve_target():
    """Read .active_endpoint and return (base_url, auth_header, provider)."""
    ep_file = ROOT / ".active_endpoint"

    if ep_file.exists():
        try:
            data = json.loads(ep_file.read_text())
            provider = data.get("provider")

            if provider == "together":
                base_url = data.get("base_url", "https://api.together.ai/v1")
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

    # Fall back to Vast tunnel (localhost:8800)
    return f"http://127.0.0.1:{LOCAL_TUNNEL_PORT}/v1", None, "vast-gguf"


# ── request forwarder ────────────────────────────────────────────────────────

def _clean_headers(raw_headers: dict) -> dict:
    """Strip hop-by-hop and proxy-internal headers before forwarding."""
    return {k: v for k, v in raw_headers.items() if k.lower() not in _HOP_BY_HOP}


async def forward_request(request: web.Request) -> web.Response:
    """Forward an HTTP request to the resolved target endpoint."""
    base_url, auth_header, provider = resolve_target()

    # Build target URL
    path = request.rel_url.path
    query = request.rel_url.query_string or ""
    target_url = f"{base_url.rstrip('/')}{path}"
    if query:
        target_url += f"?{query}"

    # Prepare headers — strip hop-by-hop, inject auth
    headers = _clean_headers(dict(request.headers))
    if auth_header:
        headers["Authorization"] = auth_header

    timeout = ClientTimeout(total=300)  # 5 min for long generations

    try:
        async with ClientSession(timeout=timeout) as session:
            method = request.method.upper()

            # Read body for methods that carry one
            body = await request.read() if method in ("POST", "PUT", "PATCH") else None

            # Detect streaming from JSON body
            use_streaming = False
            if body and method == "POST":
                try:
                    req_json = json.loads(body)
                    use_streaming = req_json.get("stream", False)
                except (json.JSONDecodeError, Exception):
                    pass

            # FIX M3: use session.request() so PUT/PATCH aren't sent as POST
            if method == "GET":
                async with session.get(target_url, headers=headers) as resp:
                    return await build_response(resp, provider)
            elif method == "DELETE":
                async with session.delete(target_url, headers=headers) as resp:
                    return await build_response(resp, provider)
            else:
                async with session.request(method, target_url, headers=headers, data=body) as resp:
                    if use_streaming:
                        return await build_streaming_response(resp, request, provider)
                    else:
                        return await build_response(resp, provider)

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


async def build_response(resp, provider: str):
    """Build a standard non-streaming response."""
    content = await resp.read()
    headers = dict(resp.headers)

    # Add provider info + CORS
    headers["X-Provider"] = provider
    headers["Access-Control-Allow-Origin"] = "*"

    # Extract usage stats for transparency header
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


async def build_streaming_response(resp, request: web.Request, provider: str):
    """Build an SSE streaming response (for chat/completions with stream=true).

    FIX C3: prepare() requires the request object.
    FIX: Added error handling for client disconnect.
    """
    headers = dict(resp.headers)
    headers["X-Provider"] = provider
    headers["Access-Control-Allow-Origin"] = "*"
    # Ensure SSE content type
    headers["Content-Type"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"

    sresp = web.StreamResponse(
        status=resp.status,
        headers=headers,
    )
    await sresp.prepare(request)  # FIX C3: was missing `request` arg

    try:
        async for chunk in resp.content.iter_chunked(4096):
            await sresp.write(chunk)
        await sresp.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        # Client disconnected mid-stream — nothing to do
        pass

    return sresp


# ── management endpoints ─────────────────────────────────────────────────────

async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint."""
    _, _, provider = resolve_target()
    return web.json_response({
        "ok": True,
        "provider": provider,
        "uptime": time.time() - START_TIME,
    })


async def switch_provider(request: web.Request) -> web.Response:
    """Switch active provider."""
    try:
        data = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider_name = data.get("provider")

    if provider_name == "together":
        api_key = data.get("api_key", os.environ.get("TOGETHER_API_KEY", ""))
        base_url = data.get("base_url", "https://api.together.ai/v1")
        model_id = data.get("model_id", "meta-llama/Llama-3.1-8B-Instruct-Turbo")

        ep = {
            "provider": "together",
            "model_id": model_id,
            "base_url": base_url,
            "endpoint": f"{base_url}/chat/completions",
            "switched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (ROOT / ".active_endpoint").write_text(json.dumps(ep, indent=2))
        return web.json_response({"status": "ok", "provider": "together"})

    elif provider_name == "vast-gguf":
        ep_file = ROOT / ".active_endpoint"
        if ep_file.exists():
            ep_file.unlink()
        return web.json_response({"status": "ok", "provider": "vast-gguf"})

    elif provider_name == "local":
        name = data.get("name", "")
        if not name:
            return web.json_response({"error": "Missing 'name' for local provider"}, status=400)
        instances_dir = PROVIDER_DIR / "local_instances"
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
            "switched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        async with ClientSession(timeout=ClientTimeout(total=3)) as session:  # FIX C4
            async with session.get(f"http://127.0.0.1:{LOCAL_TUNNEL_PORT}/v1/models") as r:
                vast_ok = r.status == 200
    except Exception:
        pass

    # Check Together status (if configured)
    together_ok = False
    togeth_key = os.environ.get("TOGETHER_API_KEY", "")
    if togeth_key:
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:  # FIX C4
                async with session.get(
                    "https://api.together.ai/v1/models",
                    headers={"Authorization": f"Bearer {togeth_key}"},
                ) as r:
                    together_ok = r.status == 200
        except Exception:
            pass

    # Check local instances
    local_instances = []
    instances_dir = PROVIDER_DIR / "local_instances"
    if instances_dir.exists():
        for meta_file in instances_dir.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text())
                name = meta.get("name", "")
                port = meta.get("port", 0)
                pid_file = instances_dir / f"{name}.pid"
                running = False
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text().strip()), 0)
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
            "vast-gguf": {"available": vast_ok, "url": f"http://127.0.0.1:{LOCAL_TUNNEL_PORT}/v1"},
            "together": {"available": together_ok, "url": "https://api.together.ai/v1"},
        },
        "local_instances": local_instances,
    })


# ── server startup ───────────────────────────────────────────────────────────

def ensure_usage_dir():
    USAGE_DIR.mkdir(parents=True, exist_ok=True)


def create_app() -> web.Application:
    ensure_usage_dir()
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
