"""Local llama-server instance management.

Discovers local llama.cpp binaries and GGUF models, starts/stops
llama-server processes, and tracks their lifecycle via PID files.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import (
    LOCAL_INSTANCES,
    LOCAL_LOGS,
    LOCAL_PID_SUFFIX,
    PROVIDER_DIR,
    ROOT,
    SAMPLING_PRESETS,
    console,
)
from .helpers import _expand_tilde, capture

# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def _ensure_local_dirs() -> None:
    """Create local instance and log directories if they don't exist."""
    LOCAL_INSTANCES.mkdir(parents=True, exist_ok=True)
    LOCAL_LOGS.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_local(models_dir: str | None = None) -> dict:
    """Auto-discover local llama.cpp binaries, GGUF models, and backend support.

    Returns dict with keys: binaries[], models[], backends[].
    """
    result: dict = {"binaries": [], "models": [], "backends": []}

    # 1. Scan for llama-server binaries
    search_paths = [
        Path.home() / "llama.cpp",
        Path.home() / "Projects" / "llama.cpp",
        Path("/usr/local/bin"),
    ]

    # Also check PATH
    for p in os.environ.get("PATH", "").split(":"):
        search_paths.append(Path(p))

    found_bins: set[str] = set()
    for base in search_paths:
        if not base.exists():
            continue
        # Check common build dirs and bin dirs
        candidates = [
            base / "build" / "bin" / "llama-server",
            base / "build-vulkan" / "bin" / "llama-server",
            base / "build-rocm" / "bin" / "llama-server",
            base / "llama-server",
        ]
        for c in candidates:
            if c.exists() and c.is_file():
                resolved = str(c.resolve())
                if resolved not in found_bins:
                    found_bins.add(resolved)
                    result["binaries"].append({
                        "path": resolved,
                        "label": c.name,
                    })

    # 2. Detect backends by probing first binary
    if result["binaries"]:
        main_bin = result["binaries"][0]["path"]
        help_out, _, rc = capture(f"{main_bin} --help 2>&1", timeout=5)
        if rc == 0 and help_out:
            backends: list[str] = []
            if "vulkan" in help_out.lower() or "--gpu-vk" in help_out.lower():
                backends.append("vulkan")
            if "cuda" in help_out.lower() or "--gpu" in help_out.lower():
                backends.append("cuda")
            if "hip" in help_out.lower() or "rocm" in help_out.lower():
                backends.append("rocm")
            # Always have CPU fallback
            result["backends"] = backends if backends else ["cpu"]

    # 3. Scan for GGUF models
    scan_dirs: list[Path] = []
    if models_dir:
        scan_dirs.append(Path(models_dir).expanduser())
    scan_dirs.extend([
        Path.home() / "models",
        Path.home() / ".cache" / "huggingface" / "hub",
    ])

    found_models: set[str] = set()
    for d in scan_dirs:
        if not d.exists():
            continue
        try:
            for gguf in d.rglob("*.gguf"):
                # Skip mmproj and vocab files
                if "mmproj" in str(gguf).lower() or "vocab" in str(gguf).lower():
                    continue
                resolved = str(gguf.resolve())
                if resolved not in found_models:
                    found_models.add(resolved)
                    size_mb = gguf.stat().st_size / (1024 * 1024)
                    result["models"].append({
                        "path": resolved,
                        "name": gguf.name,
                        "size_mb": round(size_mb, 1),
                    })
        except PermissionError:
            pass

    # Sort models by size descending (larger = usually more interesting)
    result["models"].sort(key=lambda m: -m["size_mb"])
    return result


# ---------------------------------------------------------------------------
# Server argument builder
# ---------------------------------------------------------------------------


def _get_local_server_args(recipe: dict, binary_path: str) -> tuple[list[str] | None, str | None]:
    """Convert a local recipe into llama-server CLI arguments.

    Mirrors launch.sh env-to-args mapping.
    Returns (args_list, error_message). On success error_message is None.
    """
    args = [binary_path]

    # Required
    model_path = _expand_tilde(recipe.get("model_path", ""))
    if not model_path or not Path(model_path).exists():
        return None, f"Model not found: {recipe.get('model_path', '')}"
    args.extend(["--model", model_path])

    # Network
    host = recipe.get("host", "127.0.0.1")
    port = recipe.get("port", 8100)
    args.extend(["--host", str(host), "--port", str(port)])

    # Context & parallelism
    ctx = int(recipe.get("ctx", 32768))
    parallel = int(recipe.get("parallel", 1))
    args.extend(["--ctx-size", str(ctx), "--parallel", str(parallel)])

    # KV cache type
    kv_type = recipe.get("kv_type", "q8_0")
    args.extend(["--cache-type-k", kv_type, "--cache-type-v", kv_type])

    # GPU offload
    n_gpu_layers = int(recipe.get("n_gpu_layers", 999))
    args.extend(["--n-gpu-layers", str(n_gpu_layers)])

    # Standard flags (same as launch.sh)
    args.extend(["--jinja", "--metrics"])
    args.extend(["--flash-attn", "on"])

    # Sampling preset
    mode = recipe.get("mode", "thinking")
    if mode in SAMPLING_PRESETS:
        args.extend(SAMPLING_PRESETS[mode])

    # Vision projector (optional)
    mmproj = recipe.get("mmproj", "")
    if mmproj:
        mmproj_path = _expand_tilde(mmproj)
        if Path(mmproj_path).exists():
            args.extend(["--mmproj", mmproj_path])

    return args, None


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------


def start_local_instance(recipe: dict) -> tuple[bool, str]:
    """Start a local llama-server instance from a recipe.

    Returns (success: bool, message: str).
    """
    _ensure_local_dirs()

    name = recipe.get("name", "local-default")

    # Check if already running
    pid_file = LOCAL_INSTANCES / f"{name}{LOCAL_PID_SUFFIX}"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check alive
            console.print(f"[yellow]Instance '{name}' already running (PID {pid}).[/yellow]")
            return False, "Already running"
        except (ProcessLookupError, ValueError):
            pid_file.unlink()  # Stale PID

    # Build command
    binary = recipe.get("binary", "")
    if not binary:
        # Auto-discover, preferring the right backend
        disc = discover_local()
        if disc["binaries"]:
            target_backend = recipe.get("backend", "").lower()
            # Find a matching binary by path heuristic
            preferred = None
            for b in disc["binaries"]:
                bp = b["path"].lower()
                if "vulkan" in target_backend and "vulkan" in bp:
                    preferred = b["path"]
                    break
                elif "rocm" in target_backend or "hip" in target_backend:
                    if "rocm" in bp or (preferred is None):
                        preferred = b["path"]
            binary = preferred or disc["binaries"][0]["path"]
        else:
            return False, "No llama-server binary found. Run 'Local → Configure' to scan."

    args, err = _get_local_server_args(recipe, binary)
    if err:
        return False, err

    # Backend env vars
    env = os.environ.copy()
    backend = recipe.get("backend", "").lower()
    if "vulkan" in backend:
        env["GGML_VK_VISIBLE_DEVICES"] = "0"
    elif "rocm" in backend or "hip" in backend:
        env["HIP_VISIBLE_DEVICES"] = "0"

    # API key header (optional)
    api_key = recipe.get("api_key", "")

    # Start process
    log_file = LOCAL_LOGS / f"{name}.log"
    try:
        proc = subprocess.Popen(
            args,
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            env=env,
        )
    except FileNotFoundError:
        return False, f"Binary not found: {binary}"
    except Exception as e:
        return False, str(e)

    # Write PID and metadata
    pid_file.write_text(str(proc.pid))
    port = recipe.get("port", 8100)
    host = recipe.get("host", "127.0.0.1")

    instance_meta = {
        "name": name,
        "pid": proc.pid,
        "port": int(port),
        "host": str(host),
        "binary": binary,
        "model_path": recipe.get("model_path", ""),
        "backend": backend or "auto",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "starting",
    }

    # Write instance metadata
    meta_file = LOCAL_INSTANCES / f"{name}.json"
    meta_file.write_text(json.dumps(instance_meta, indent=2))

    # Update .active_endpoint
    endpoint_info: dict = {
        "provider": "local",
        "name": name,
        "host": str(host),
        "port": int(port),
        "pid": proc.pid,
        "model_path": recipe.get("model_path", ""),
        "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if api_key:
        endpoint_info["api_key"] = api_key

    (ROOT / ".active_endpoint").write_text(json.dumps(endpoint_info, indent=2))

    # Health check loop — poll until healthy or timeout
    console.print(f"[dim]Starting llama-server (PID {proc.pid})...[/dim]")
    base_url = f"http://{host}:{port}/v1"
    for i in range(60):  # 60 seconds max
        time.sleep(1)
        if proc.poll() is not None:
            # Process died — check log for error
            try:
                log_content = log_file.read_text()[-500:]
            except Exception:
                log_content = "(could not read log)"
            return False, f"Process exited immediately. Last log: {log_content.strip()}"

        # Health probe
        try:
            req = urllib.request.Request(f"{base_url}/models")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    meta_file.write_text(
                        json.dumps({**instance_meta, "status": "running"}, indent=2)
                    )

                    model_label = Path(recipe.get("model_path", "")).name[:40]
                    console.print(f"\n[green]✓ Local endpoint ready![/green]")
                    console.print(f"  Instance: {name}")
                    console.print(f"  Model:    {model_label}")
                    console.print(f"  Endpoint: {base_url}")
                    console.print(f"  PID:      {proc.pid} (port {port})")
                    return True, "Started successfully"
        except Exception:
            continue

    return False, f"Health check timed out after 60s. Check log: {log_file}"


def stop_local_instance(name: str) -> tuple[bool, str]:
    """Stop a local llama-server instance.

    Returns (success: bool, message: str).
    """
    pid_file = LOCAL_INSTANCES / f"{name}{LOCAL_PID_SUFFIX}"
    if not pid_file.exists():
        return False, f"No PID file for '{name}'. Not running?"

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)

        # Wait up to 5 seconds for graceful shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            # Force kill if still alive
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        pid_file.unlink()

        # Update metadata
        meta_file = LOCAL_INSTANCES / f"{name}.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                meta["status"] = "stopped"
                meta["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                meta_file.write_text(json.dumps(meta, indent=2))
            except Exception:
                pass

        # Clear .active_endpoint if this was the active one
        ep_file = ROOT / ".active_endpoint"
        if ep_file.exists():
            try:
                ep = json.loads(ep_file.read_text())
                if ep.get("provider") == "local" and ep.get("name") == name:
                    ep_file.unlink()
            except Exception:
                pass

        return True, f"Stopped '{name}' (PID {pid})"

    except ValueError:
        pid_file.unlink()
        return False, "Invalid PID file"
    except ProcessLookupError:
        pid_file.unlink()
        return True, f"'{name}' was already stopped"


# ---------------------------------------------------------------------------
# Instance queries
# ---------------------------------------------------------------------------


def is_local_running(name: str) -> bool:
    """Check if a named local instance is running."""
    pid_file = LOCAL_INSTANCES / f"{name}{LOCAL_PID_SUFFIX}"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        pid_file.unlink()  # Clean stale PID
        return False


def list_local_instances() -> list[dict]:
    """List all local instance metadata."""
    _ensure_local_dirs()
    instances: list[dict] = []
    for meta in sorted(LOCAL_INSTANCES.glob("*.json")):
        try:
            data = json.loads(meta.read_text())
            name = data.get("name", meta.stem)
            running = is_local_running(name)
            data["running"] = running
            if not running and data.get("status") == "starting":
                data["status"] = "stopped"
            instances.append(data)
        except Exception:
            pass
    return instances
