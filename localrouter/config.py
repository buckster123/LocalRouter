"""Shared configuration, constants, and recipe loader for localrouter."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

try:
    from questionary import Style
    from rich.console import Console
except ImportError as e:
    print(f"Missing dep: {e}")
    print("Run: pip3 install questionary rich --break-system-packages")
    sys.exit(1)


# ── paths ─────────────────────────────────────────────────────────────────────
# ROOT points to the *project* root (parent of the localrouter/ package dir).
ROOT       = Path(__file__).parent.parent.resolve()
LAST_INST  = ROOT / ".last_instance"
TUNNEL_PID = Path("/tmp/vastai-gguf-tunnel.pid")
HF_PIN     = ROOT / ".hf_pin"

# Provider config
PROVIDER_DIR   = Path.home() / ".vastai-gguf"
PROVIDER_CFG   = PROVIDER_DIR / "config.toml"

# Local endpoint management
LOCAL_INSTANCES  = PROVIDER_DIR / "local_instances"
LOCAL_LOGS       = PROVIDER_DIR / "local_logs"
LOCAL_PID_SUFFIX = ".pid"


# ── console & style ──────────────────────────────────────────────────────────
console    = Console()
LOCAL_PORT       = 8800     # SSH tunnel local port (remote 8000 → local 8800)
PROXY_PORT       = 8888     # Endpoint proxy listen port

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

def _load_toml(path: Path) -> dict:
    """Load a TOML file using stdlib tomllib (Python 3.11+)."""
    with open(path, "rb") as f:
        return tomllib.load(f)


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


def image_for_type(docker_cfg: dict, image_type: str) -> str:
    """Return the docker image string for a given image_type."""
    return docker_cfg.get(image_type, docker_cfg.get("prebuilt", "ghcr.io/buckster123/vastai-gguf:prebuilt"))


def cold_start_estimate(image_type: str) -> str:
    """Human-readable cold start estimate."""
    return {"prebuilt": "~2 min  (image pull only)",
            "builder":  "~12-18 min  (pull + SM compile)"}.get(image_type, "unknown")


# ── sampling presets ──────────────────────────────────────────────────────────

SAMPLING_PRESETS = {
    "thinking":    ["--temp", "1.0", "--top-p", "0.95", "--min-p", "0.0", "--presence-penalty", "1.5"],
    "coding":      ["--temp", "0.6", "--top-p", "0.95", "--min-p", "0.0", "--presence-penalty", "0.0"],
    "nonthinking": ["--temp", "0.7", "--top-p", "0.80", "--min-p", "0.0", "--presence-penalty", "1.5",
                    '--chat-template-kwargs', '{"enable_thinking":false}'],
}


# ── launch-wizard maps ───────────────────────────────────────────────────────

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
