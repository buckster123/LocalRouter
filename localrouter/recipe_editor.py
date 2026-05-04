"""Recipe editor — read, modify, and write back recipes.toml with proper TOML.

Uses tomllib (stdlib 3.11+) for reading and tomli_w for writing.
Falls back to the existing hand-rolled parser on Python 3.10.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore[assignment]

from .config import ROOT, console


# ── TOML read/write ──────────────────────────────────────────────────────────

RECIPES_PATH = ROOT / "recipes.toml"


def _read_toml(path: Path | None = None) -> dict[str, Any]:
    """Read recipes.toml using tomllib (proper parser)."""
    p = path or RECIPES_PATH
    if tomllib is None:
        # Fallback: use existing hand-rolled parser
        from .config import _load_toml
        return _load_toml(p)
    with open(p, "rb") as f:
        return tomllib.load(f)


def _write_toml(data: dict[str, Any], path: Path | None = None) -> None:
    """Write recipes.toml using tomli_w."""
    if tomli_w is None:
        raise RuntimeError("tomli_w not installed. Run: pip install tomli_w")
    p = path or RECIPES_PATH

    # Back up the original
    backup = p.with_suffix(".toml.bak")
    if p.exists():
        backup.write_bytes(p.read_bytes())

    with open(p, "wb") as f:
        tomli_w.dump(data, f)


def load_recipes() -> dict[str, Any]:
    """Load the full recipes.toml as a dict."""
    return _read_toml()


def save_recipes(data: dict[str, Any]) -> None:
    """Save the full dict back to recipes.toml."""
    _write_toml(data)


# ── Recipe CRUD ──────────────────────────────────────────────────────────────

def get_recipes(data: dict) -> list[dict]:
    """Get the recipes list from loaded data."""
    return data.get("recipes", [])


def get_gpu_tiers(data: dict) -> dict[str, dict]:
    """Get GPU tiers dict from loaded data."""
    return data.get("gpu_tiers", {})


def get_docker_cfg(data: dict) -> dict:
    """Get docker config from loaded data."""
    return data.get("docker", {})


def find_recipe(data: dict, name: str) -> dict | None:
    """Find a recipe by name slug."""
    for r in get_recipes(data):
        if r.get("name") == name:
            return r
    return None


def add_recipe(data: dict, recipe: dict) -> None:
    """Add a new recipe to the data."""
    if "recipes" not in data:
        data["recipes"] = []
    data["recipes"].append(recipe)


def remove_recipe(data: dict, name: str) -> bool:
    """Remove a recipe by name. Returns True if found."""
    recipes = get_recipes(data)
    for i, r in enumerate(recipes):
        if r.get("name") == name:
            recipes.pop(i)
            return True
    return False


def update_recipe(data: dict, name: str, updates: dict) -> bool:
    """Update fields of an existing recipe. Returns True if found."""
    recipe = find_recipe(data, name)
    if recipe is None:
        return False
    recipe.update(updates)
    return True


def duplicate_recipe(data: dict, name: str, new_name: str) -> dict | None:
    """Duplicate a recipe with a new name. Returns the new recipe or None."""
    orig = find_recipe(data, name)
    if orig is None:
        return None
    new = copy.deepcopy(orig)
    new["name"] = new_name
    add_recipe(data, new)
    return new


# ── GPU tier CRUD ────────────────────────────────────────────────────────────

def add_gpu_tier(data: dict, key: str, tier: dict) -> None:
    """Add or replace a GPU tier."""
    if "gpu_tiers" not in data:
        data["gpu_tiers"] = {}
    data["gpu_tiers"][key] = tier


def remove_gpu_tier(data: dict, key: str) -> bool:
    """Remove a GPU tier. Returns True if found."""
    tiers = get_gpu_tiers(data)
    if key in tiers:
        del tiers[key]
        return True
    return False


# ── Docker image management ──────────────────────────────────────────────────

def add_docker_image(data: dict, key: str, image: str) -> None:
    """Add or update a docker image entry."""
    if "docker" not in data:
        data["docker"] = {}
    data["docker"][key] = image


def list_docker_images(data: dict) -> dict[str, str]:
    """List all docker image entries."""
    return dict(get_docker_cfg(data))


# ── Validation ───────────────────────────────────────────────────────────────

REQUIRED_RECIPE_FIELDS_VAST = {"name", "label", "gpu", "model_repo", "model_quant", "ctx"}
REQUIRED_RECIPE_FIELDS_LOCAL = {"name", "label", "model_path", "port"}
REQUIRED_RECIPE_FIELDS_TOGETHER = {"name", "label", "model_id"}
REQUIRED_RECIPE_FIELDS_VLLM = {"name", "label", "gpu", "model_id", "ctx"}
REQUIRED_TIER_FIELDS = {"vast_names", "label", "max_price"}
# Optional recipe fields that the editor wizard should know about
OPTIONAL_RECIPE_FIELDS_VAST = {
    "parallel", "kv_type", "min_disk_gb", "image_type", "description",
    "llama_cpp_repo", "llama_cpp_ref",  # custom llama.cpp fork/branch
}


def validate_recipe(recipe: dict, gpu_tiers: dict) -> list[str]:
    """Validate a recipe. Returns list of error strings (empty = valid)."""
    errors = []
    provider = recipe.get("provider", "vast_gguf")

    if provider == "local":
        missing = REQUIRED_RECIPE_FIELDS_LOCAL - set(recipe.keys())
    elif provider == "together":
        missing = REQUIRED_RECIPE_FIELDS_TOGETHER - set(recipe.keys())
    elif provider == "vllm":
        missing = REQUIRED_RECIPE_FIELDS_VLLM - set(recipe.keys())
    else:
        missing = REQUIRED_RECIPE_FIELDS_VAST - set(recipe.keys())

    if missing:
        errors.append(f"Missing required fields: {', '.join(sorted(missing))}")

    # Check GPU tier exists (for vast recipes)
    if provider not in ("local", "together"):
        gpu = recipe.get("gpu", "")
        if gpu and gpu not in gpu_tiers:
            errors.append(f"GPU tier '{gpu}' not found in gpu_tiers")

    # Check name is unique-ish (slug format)
    name = recipe.get("name", "")
    if name and not all(c.isalnum() or c in "-_." for c in name):
        errors.append(f"Name '{name}' should be alphanumeric with hyphens/underscores")

    # Basic ctx sanity
    ctx = recipe.get("ctx")
    if ctx is not None:
        if not isinstance(ctx, int) or ctx < 1:
            errors.append(f"ctx must be a positive integer, got {ctx}")

    return errors


def validate_gpu_tier(tier: dict) -> list[str]:
    """Validate a GPU tier. Returns list of error strings."""
    errors = []
    missing = REQUIRED_TIER_FIELDS - set(tier.keys())
    if missing:
        errors.append(f"Missing required fields: {', '.join(sorted(missing))}")

    if "vast_names" in tier and not isinstance(tier["vast_names"], list):
        errors.append("vast_names must be a list of strings")

    return errors
