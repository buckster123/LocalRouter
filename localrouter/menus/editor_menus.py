"""Editor menus — interactive recipe, GPU tier, and docker image management."""
from __future__ import annotations

import questionary
from rich.panel import Panel
from rich.table import Table
from rich import box

from ..config import console, MENU_STYLE
from ..helpers import ask_back, hr, press_enter
from ..recipe_editor import (
    load_recipes, save_recipes,
    get_recipes, get_gpu_tiers, get_docker_cfg,
    find_recipe, add_recipe, remove_recipe, update_recipe, duplicate_recipe,
    add_gpu_tier, remove_gpu_tier,
    add_docker_image, list_docker_images,
    validate_recipe, validate_gpu_tier,
)


# ── Formatting helpers ───────────────────────────────────────────────────────

def _provider_color(provider: str) -> str:
    return {"local": "green", "together": "cyan", "vast_gguf": "#7c6af7"}.get(provider, "#7c6af7")


def _recipe_table(recipes: list[dict], gpu_tiers: dict) -> Table:
    """Build a rich Table of recipes."""
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, pad_edge=False)
    t.add_column("#", style="dim", width=3)
    t.add_column("Name", style="bold", max_width=30)
    t.add_column("Label", max_width=40)
    t.add_column("Provider", width=10)
    t.add_column("GPU", width=14)
    t.add_column("Ctx", width=8, justify="right")

    for i, r in enumerate(recipes, 1):
        provider = r.get("provider", "vast_gguf")
        color = _provider_color(provider)
        gpu_key = r.get("gpu", "")
        tier = gpu_tiers.get(gpu_key, {})
        gpu_label = tier.get("label", gpu_key)[:14] if gpu_key else "—"
        ctx = f"{r['ctx']//1024}K" if r.get("ctx") else "—"
        t.add_row(
            str(i), r.get("name", "?"),
            r.get("label", "?"),
            f"[{color}]{provider}[/{color}]",
            gpu_label, ctx,
        )
    return t


def _tier_table(tiers: dict) -> Table:
    """Build a rich Table of GPU tiers."""
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, pad_edge=False)
    t.add_column("Key", style="bold #7c6af7", width=14)
    t.add_column("Label", max_width=35)
    t.add_column("VRAM", width=8, justify="right")
    t.add_column("GPUs", width=5, justify="right")
    t.add_column("Max $/hr", width=9, justify="right")
    t.add_column("Image type", width=11)
    t.add_column("Vast names", max_width=30)

    for key, tier in sorted(tiers.items()):
        num_gpus = tier.get("num_gpus", 1)
        vram = tier.get("vram_gb", "?")
        total_vram = f"{vram}GB" if num_gpus == 1 else f"{num_gpus}×{vram}GB"
        t.add_row(
            key, tier.get("label", "?"),
            total_vram, str(num_gpus),
            f"${tier.get('max_price', '?')}", tier.get("image_type", "builder"),
            ", ".join(tier.get("vast_names", [])),
        )
    return t


# ── Recipe editor submenu ────────────────────────────────────────────────────

def _pick_provider() -> str | None:
    """Ask user to pick a provider type."""
    choice = questionary.select(
        "Provider type:",
        choices=[
            "vast_gguf  — GGUF on rented Vast.ai GPU (llama.cpp)",
            "vllm       — vLLM tensor-parallel on multi-GPU cluster",
            "local      — llama.cpp on your own hardware",
            "together   — Together AI managed endpoint",
            "← Back",
        ],
        style=MENU_STYLE,
    ).ask()
    if choice is None or choice == "← Back":
        return None
    return choice.split()[0]


def _pick_gpu_tier(tiers: dict) -> str | None:
    """Ask user to pick a GPU tier."""
    choices = []
    for key, tier in sorted(tiers.items()):
        num_gpus = tier.get("num_gpus", 1)
        vram = tier.get("vram_gb", "?")
        gpu_str = f"{num_gpus}×" if num_gpus > 1 else ""
        choices.append(f"{key:16s} — {gpu_str}{vram}GB  {tier.get('label', '')}")
    choices.append("← Back")

    sel = questionary.select("GPU tier:", choices=choices, style=MENU_STYLE).ask()
    if sel is None or sel == "← Back":
        return None
    return sel.split()[0]


def _edit_recipe_fields(recipe: dict, gpu_tiers: dict) -> dict | None:
    """Interactive field editor. Returns updated recipe or None on cancel."""
    import copy
    edited = copy.deepcopy(recipe)
    provider = edited.get("provider", "vast_gguf")

    while True:
        hr("Edit Recipe Fields")

        # Show current values
        t = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False)
        t.add_column("field", style="dim", width=14)
        t.add_column("value", style="bold")
        for k, v in edited.items():
            t.add_row(k, str(v))
        console.print(Panel(t, title=f"[bold]{edited.get('name', '?')}[/bold]", border_style="#3d3d5c"))

        # Build field choices
        editable = list(edited.keys())
        field_choices = [f"{k:16s} = {str(edited[k])[:50]}" for k in editable]
        field_choices += ["+ Add field", "✓ Done — save", "✗ Cancel"]

        sel = questionary.select("Edit field:", choices=field_choices, style=MENU_STYLE).ask()
        if sel is None or sel.startswith("✗"):
            return None
        if sel.startswith("✓"):
            return edited

        if sel.startswith("+"):
            # Add new field
            key = questionary.text("Field name:", style=MENU_STYLE).ask()
            if not key:
                continue
            val = questionary.text(f"Value for '{key}':", style=MENU_STYLE).ask()
            if val is None:
                continue
            edited[key] = _coerce_value(val)
        else:
            # Edit existing field
            field_name = sel.split("=")[0].strip()
            if field_name == "name":
                console.print("[yellow]Tip: changing name creates a new identity. Use Duplicate instead.[/yellow]")
            if field_name == "provider":
                new_provider = _pick_provider()
                if new_provider:
                    edited["provider"] = new_provider
                continue
            if field_name == "gpu":
                new_gpu = _pick_gpu_tier(gpu_tiers)
                if new_gpu:
                    edited["gpu"] = new_gpu
                continue

            current = str(edited.get(field_name, ""))
            new_val = questionary.text(
                f"{field_name}:", default=current, style=MENU_STYLE
            ).ask()
            if new_val is None:
                continue

            # Option to delete the field
            if new_val == "" and field_name not in ("name", "label"):
                if questionary.confirm(f"Remove field '{field_name}'?", default=False, style=MENU_STYLE).ask():
                    del edited[field_name]
                    continue

            edited[field_name] = _coerce_value(new_val)


def _safe_int(raw: str | None, default: int, label: str = "value") -> int:
    """Convert user input to int with validation. Returns default on bad input."""
    val = raw or str(default)
    try:
        return int(val)
    except ValueError:
        console.print(f"[red]Invalid number for {label}: '{val}' — using default {default}[/red]")
        return default


def _coerce_value(val: str):
    """Coerce a string value to int/float/list if appropriate."""
    if val.isdigit():
        return int(val)
    try:
        return float(val)
    except ValueError:
        pass
    # Simple list detection: ["a", "b"]
    if val.startswith("[") and val.endswith("]"):
        import re
        items = re.findall(r'"([^"]+)"', val)
        if items:
            return items
    return val


def _create_recipe_wizard(data: dict) -> dict | None:
    """Guided wizard for creating a new recipe."""
    hr("Create New Recipe")
    gpu_tiers = get_gpu_tiers(data)

    provider = _pick_provider()
    if not provider:
        return None

    recipe: dict = {}

    if provider == "local":
        recipe["provider"] = "local"
        recipe["name"] = questionary.text("Recipe name (slug):", style=MENU_STYLE).ask() or ""
        recipe["label"] = questionary.text("Display label:", style=MENU_STYLE).ask() or ""
        recipe["model_path"] = questionary.text("Model path (e.g. ~/models/model.gguf):", style=MENU_STYLE).ask() or ""
        recipe["port"] = _safe_int(questionary.text("Port:", default="8100", style=MENU_STYLE).ask(), 8100, "port")
        recipe["ctx"] = _safe_int(questionary.text("Context length:", default="32768", style=MENU_STYLE).ask(), 32768, "context length")
        recipe["backend"] = questionary.select(
            "Backend:", choices=["vulkan", "rocm", "cuda", "cpu"], style=MENU_STYLE
        ).ask() or "vulkan"
        recipe["mode"] = questionary.select(
            "Sampling mode:", choices=["thinking", "coding", "nonthinking"], style=MENU_STYLE
        ).ask() or "thinking"

    elif provider == "together":
        recipe["provider"] = "together"
        recipe["name"] = questionary.text("Recipe name (slug):", style=MENU_STYLE).ask() or ""
        recipe["label"] = questionary.text("Display label:", style=MENU_STYLE).ask() or ""
        recipe["model_id"] = questionary.text("Together model ID (e.g. Qwen/Qwen3-32B):", style=MENU_STYLE).ask() or ""
        recipe["ctx"] = _safe_int(questionary.text("Context length:", default="131072", style=MENU_STYLE).ask(), 131072, "context length")
        price = questionary.text("Price per 1M tokens (input):", default="0.50", style=MENU_STYLE).ask() or "0.50"
        recipe["price_input"] = float(price)
        recipe["price_output"] = float(
            questionary.text("Price per 1M tokens (output):", default=price, style=MENU_STYLE).ask() or price
        )

    elif provider == "vllm":
        recipe["provider"] = "vllm"
        recipe["name"] = questionary.text("Recipe name (slug):", style=MENU_STYLE).ask() or ""
        recipe["label"] = questionary.text("Display label:", style=MENU_STYLE).ask() or ""

        gpu = _pick_gpu_tier(gpu_tiers)
        if not gpu:
            return None
        recipe["gpu"] = gpu

        recipe["model_id"] = questionary.text(
            "HF model ID (e.g. deepseek-ai/DeepSeek-V4-Pro):", style=MENU_STYLE
        ).ask() or ""
        recipe["ctx"] = _safe_int(questionary.text("Context length:", default="393216", style=MENU_STYLE).ask(), 393216, "context length")
        recipe["image_type"] = "vllm"

        kv_dtype = questionary.select(
            "KV cache dtype:", choices=["auto", "fp8", "fp8_e5m2", "fp8_e4m3"], style=MENU_STYLE
        ).ask()
        if kv_dtype and kv_dtype != "auto":
            recipe["kv_cache_dtype"] = kv_dtype

        enforce = questionary.confirm("Enforce eager (disable CUDA graphs, saves VRAM)?", default=False, style=MENU_STYLE).ask()
        if enforce:
            recipe["enforce_eager"] = "true"

        reasoning = questionary.confirm("Enable reasoning parser (deepseek_r1)?", default=True, style=MENU_STYLE).ask()
        if reasoning:
            recipe["reasoning_parser"] = "deepseek_r1"

    else:  # vast_gguf
        recipe["name"] = questionary.text("Recipe name (slug):", style=MENU_STYLE).ask() or ""
        recipe["label"] = questionary.text("Display label:", style=MENU_STYLE).ask() or ""

        gpu = _pick_gpu_tier(gpu_tiers)
        if not gpu:
            return None
        recipe["gpu"] = gpu

        recipe["model_repo"] = questionary.text(
            "HF model repo (e.g. unsloth/Qwen3.6-27B-GGUF):", style=MENU_STYLE
        ).ask() or ""
        recipe["model_quant"] = questionary.text(
            "Quant tag (substring match, e.g. Q6_K, UD-Q6_K_XL):", style=MENU_STYLE
        ).ask() or ""
        recipe["ctx"] = _safe_int(questionary.text("Context length:", default="98304", style=MENU_STYLE).ask(), 98304, "context length")

        # Optional fields
        parallel = questionary.text("Parallel slots:", default="1", style=MENU_STYLE).ask()
        if parallel and parallel != "1":
            recipe["parallel"] = _safe_int(parallel, 1, "parallel slots")

        kv = questionary.select(
            "KV cache type:", choices=["q8_0", "q4_0", "bf16"], style=MENU_STYLE
        ).ask()
        if kv and kv != "q8_0":
            recipe["kv_type"] = kv

        image_override = questionary.confirm(
            "Override image type from tier default?", default=False, style=MENU_STYLE
        ).ask()
        if image_override:
            recipe["image_type"] = questionary.select(
                "Image type:", choices=["prebuilt", "builder"], style=MENU_STYLE
            ).ask()

        # Custom llama.cpp (for models needing unmerged PRs)
        custom_llama = questionary.confirm(
            "Custom llama.cpp fork/branch? (for unmerged model support, e.g. DSv4)",
            default=False, style=MENU_STYLE
        ).ask()
        if custom_llama:
            recipe["image_type"] = "builder"  # must compile from source
            recipe["llama_cpp_repo"] = questionary.text(
                "GitHub repo (user/repo):",
                default="fairydreaming/llama.cpp",
                style=MENU_STYLE,
            ).ask() or "fairydreaming/llama.cpp"
            recipe["llama_cpp_ref"] = questionary.text(
                "Branch/tag/commit:",
                default="deepseek-dsa",
                style=MENU_STYLE,
            ).ask() or "deepseek-dsa"

    # Description (optional for all)
    desc = questionary.text("Description (optional):", style=MENU_STYLE).ask()
    if desc:
        recipe["description"] = desc

    if not recipe.get("name"):
        console.print("[red]Recipe name is required.[/red]")
        return None

    return recipe


def menu_edit_recipes(data: dict) -> bool:
    """Recipe list + CRUD menu. Returns True if data was modified."""
    modified = False

    while True:
        hr("Recipe Editor")
        recipes = get_recipes(data)
        gpu_tiers = get_gpu_tiers(data)

        # Filter options
        providers = sorted(set(r.get("provider", "vast_gguf") for r in recipes))
        provider_counts = {p: sum(1 for r in recipes if r.get("provider", "vast_gguf") == p) for p in providers}

        console.print(f"  [dim]{len(recipes)} recipes across {len(providers)} providers: "
                       f"{', '.join(f'{p} ({c})' for p, c in provider_counts.items())}[/dim]\n")

        choice = questionary.select(
            "Action:",
            choices=[
                f"Browse     — view all {len(recipes)} recipes",
                "Browse by provider  — filter by provider type",
                "Browse by GPU tier  — filter by GPU tier",
                "Create     — new recipe wizard",
                "Edit       — modify an existing recipe",
                "Duplicate  — clone + rename a recipe",
                "Delete     — remove a recipe",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            return modified

        if choice.startswith("Browse "):
            if "provider" in choice:
                prov = questionary.select(
                    "Provider:", choices=ask_back(providers), style=MENU_STYLE
                ).ask()
                if prov and prov != "← Back":
                    filtered = [r for r in recipes if r.get("provider", "vast_gguf") == prov]
                    console.print(Panel(_recipe_table(filtered, gpu_tiers),
                                        title=f"[bold]{prov} recipes ({len(filtered)})[/bold]",
                                        border_style="#3d3d5c"))
                    press_enter()
            elif "GPU" in choice:
                tier_keys = sorted(gpu_tiers.keys())
                tier = questionary.select(
                    "GPU tier:", choices=ask_back(tier_keys), style=MENU_STYLE
                ).ask()
                if tier and tier != "← Back":
                    filtered = [r for r in recipes if r.get("gpu") == tier]
                    console.print(Panel(_recipe_table(filtered, gpu_tiers),
                                        title=f"[bold]{tier} recipes ({len(filtered)})[/bold]",
                                        border_style="#3d3d5c"))
                    press_enter()
            else:
                console.print(Panel(_recipe_table(recipes, gpu_tiers),
                                    title=f"[bold]All recipes ({len(recipes)})[/bold]",
                                    border_style="#3d3d5c"))
                press_enter()

        elif choice.startswith("Create"):
            new = _create_recipe_wizard(data)
            if new:
                errors = validate_recipe(new, gpu_tiers)
                if errors:
                    console.print("[red]Validation errors:[/red]")
                    for e in errors:
                        console.print(f"  [red]• {e}[/red]")
                    if not questionary.confirm("Add anyway?", default=False, style=MENU_STYLE).ask():
                        continue
                if find_recipe(data, new["name"]):
                    console.print(f"[red]Recipe '{new['name']}' already exists.[/red]")
                    continue
                add_recipe(data, new)
                modified = True
                console.print(f"[green]✓ Recipe '{new['name']}' created.[/green]")
                press_enter()

        elif choice.startswith("Edit"):
            names = [r.get("name", "?") for r in recipes]
            sel = questionary.select("Recipe to edit:", choices=ask_back(names), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                recipe = find_recipe(data, sel)
                if recipe:
                    result = _edit_recipe_fields(recipe, gpu_tiers)
                    if result:
                        recipe.clear()
                        recipe.update(result)
                        modified = True
                        console.print(f"[green]✓ Recipe '{sel}' updated.[/green]")
                        press_enter()

        elif choice.startswith("Duplicate"):
            names = [r.get("name", "?") for r in recipes]
            sel = questionary.select("Recipe to duplicate:", choices=ask_back(names), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                new_name = questionary.text("New name:", default=f"{sel}-copy", style=MENU_STYLE).ask()
                if new_name:
                    dup = duplicate_recipe(data, sel, new_name)
                    if dup:
                        modified = True
                        console.print(f"[green]✓ Duplicated '{sel}' → '{new_name}'.[/green]")
                    press_enter()

        elif choice.startswith("Delete"):
            names = [r.get("name", "?") for r in recipes]
            sel = questionary.select("Recipe to delete:", choices=ask_back(names), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                if questionary.confirm(f"Delete '{sel}'?", default=False, style=MENU_STYLE).ask():
                    remove_recipe(data, sel)
                    modified = True
                    console.print(f"[green]✓ Recipe '{sel}' deleted.[/green]")
                    press_enter()


# ── GPU tier editor submenu ──────────────────────────────────────────────────

def _create_tier_wizard() -> tuple[str, dict] | None:
    """Create a new GPU tier interactively."""
    hr("Create GPU Tier")

    key = questionary.text("Tier key (e.g. h100-sxm-2x):", style=MENU_STYLE).ask()
    if not key:
        return None

    label = questionary.text("Display label:", style=MENU_STYLE).ask() or key
    vast_names_raw = questionary.text(
        "Vast.ai GPU names (comma-separated, e.g. H100_SXM,H100_SXM5):", style=MENU_STYLE
    ).ask()
    vast_names = [n.strip() for n in (vast_names_raw or "").split(",") if n.strip()]
    max_price = questionary.text("Max $/hr:", default="3.50", style=MENU_STYLE).ask() or "3.50"
    vram_gb = _safe_int(questionary.text("VRAM per GPU (GB):", default="80", style=MENU_STYLE).ask(), 80, "VRAM")
    num_gpus = _safe_int(questionary.text("Number of GPUs:", default="1", style=MENU_STYLE).ask(), 1, "GPU count")
    min_disk = _safe_int(questionary.text("Min disk (GB):", default="100", style=MENU_STYLE).ask(), 100, "min disk")
    image_type = questionary.select(
        "Default image type:", choices=["prebuilt", "builder"], style=MENU_STYLE
    ).ask() or "builder"
    min_cuda = questionary.text("Min CUDA version:", default="12.8", style=MENU_STYLE).ask() or "12.8"

    tier = {
        "vast_names": vast_names,
        "label": label,
        "max_price": max_price,
        "vram_gb": vram_gb,
        "num_gpus": num_gpus,
        "min_disk_gb": min_disk,
        "image_type": image_type,
        "min_cuda": min_cuda,
    }

    return key, tier


def menu_edit_gpu_tiers(data: dict) -> bool:
    """GPU tier list + CRUD menu. Returns True if data was modified."""
    modified = False

    while True:
        hr("GPU Tier Editor")
        tiers = get_gpu_tiers(data)

        console.print(Panel(_tier_table(tiers),
                            title=f"[bold]GPU Tiers ({len(tiers)})[/bold]",
                            border_style="#3d3d5c"))

        choice = questionary.select(
            "Action:",
            choices=[
                "Create  — add a new GPU tier",
                "Edit    — modify an existing tier",
                "Delete  — remove a tier",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            return modified

        if choice.startswith("Create"):
            result = _create_tier_wizard()
            if result:
                key, tier = result
                errors = validate_gpu_tier(tier)
                if errors:
                    console.print("[red]Validation errors:[/red]")
                    for e in errors:
                        console.print(f"  [red]• {e}[/red]")
                    if not questionary.confirm("Add anyway?", default=False, style=MENU_STYLE).ask():
                        continue
                add_gpu_tier(data, key, tier)
                modified = True
                console.print(f"[green]✓ GPU tier '{key}' created.[/green]")
                press_enter()

        elif choice.startswith("Edit"):
            keys = sorted(tiers.keys())
            sel = questionary.select("Tier to edit:", choices=ask_back(keys), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                tier = tiers[sel]
                # Simple field editor
                while True:
                    fields = [f"{k:16s} = {str(v)[:40]}" for k, v in tier.items()]
                    fields += ["✓ Done", "← Back"]
                    f_sel = questionary.select("Edit field:", choices=fields, style=MENU_STYLE).ask()
                    if f_sel is None or f_sel.startswith("✓") or f_sel == "← Back":
                        break
                    fname = f_sel.split("=")[0].strip()
                    if fname == "vast_names":
                        current = ", ".join(tier.get("vast_names", []))
                        new = questionary.text(f"{fname} (comma-separated):", default=current, style=MENU_STYLE).ask()
                        if new is not None:
                            tier["vast_names"] = [n.strip() for n in new.split(",") if n.strip()]
                            modified = True
                    else:
                        current = str(tier.get(fname, ""))
                        new = questionary.text(f"{fname}:", default=current, style=MENU_STYLE).ask()
                        if new is not None:
                            tier[fname] = _coerce_value(new)
                            modified = True

        elif choice.startswith("Delete"):
            keys = sorted(tiers.keys())
            # Check which tiers are in use
            recipes = get_recipes(data)
            in_use = {r.get("gpu") for r in recipes}
            labels = []
            for k in keys:
                used = " [yellow](in use)[/yellow]" if k in in_use else ""
                labels.append(f"{k}{used}")
            sel = questionary.select("Tier to delete:", choices=ask_back(keys), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                if sel in in_use:
                    count = sum(1 for r in recipes if r.get("gpu") == sel)
                    console.print(f"[yellow]Warning: {count} recipe(s) use this tier.[/yellow]")
                if questionary.confirm(f"Delete tier '{sel}'?", default=False, style=MENU_STYLE).ask():
                    remove_gpu_tier(data, sel)
                    modified = True
                    console.print(f"[green]✓ Tier '{sel}' deleted.[/green]")
                    press_enter()


# ── Docker image editor ──────────────────────────────────────────────────────

def menu_edit_docker(data: dict) -> bool:
    """Docker image management menu. Returns True if modified."""
    modified = False

    while True:
        hr("Docker Images")
        images = list_docker_images(data)

        t = Table(box=box.SIMPLE_HEAVY, show_header=True, pad_edge=False)
        t.add_column("Key", style="bold #7c6af7", width=20)
        t.add_column("Image", style="bold")
        for k, v in images.items():
            t.add_row(k, v)
        console.print(Panel(t, title="[bold]Container Images[/bold]", border_style="#3d3d5c"))

        choice = questionary.select(
            "Action:",
            choices=[
                "Add / Update  — set an image entry",
                "Remove        — delete an image entry",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            return modified

        if choice.startswith("Add"):
            key = questionary.text(
                "Image key (e.g. prebuilt, builder, dsv4-flash):", style=MENU_STYLE
            ).ask()
            if not key:
                continue
            default_img = images.get(key, "ghcr.io/buckster123/")
            image = questionary.text(f"Image URI:", default=default_img, style=MENU_STYLE).ask()
            if image:
                add_docker_image(data, key, image)
                modified = True
                console.print(f"[green]✓ Image '{key}' → {image}[/green]")
                press_enter()

        elif choice.startswith("Remove"):
            keys = list(images.keys())
            sel = questionary.select("Image to remove:", choices=ask_back(keys), style=MENU_STYLE).ask()
            if sel and sel != "← Back":
                if questionary.confirm(f"Remove '{sel}'?", default=False, style=MENU_STYLE).ask():
                    docker = get_docker_cfg(data)
                    if sel in docker:
                        del docker[sel]
                    modified = True
                    console.print(f"[green]✓ Image '{sel}' removed.[/green]")
                    press_enter()


# ── Top-level editor dispatch ────────────────────────────────────────────────

def menu_editor() -> None:
    """Top-level editor menu — recipes, GPU tiers, docker images."""
    data = load_recipes()
    any_modified = False

    while True:
        hr("Configuration Editor")

        recipes = get_recipes(data)
        tiers = get_gpu_tiers(data)
        images = list_docker_images(data)
        status = "[green]modified — unsaved[/green]" if any_modified else "[dim]no changes[/dim]"

        console.print(f"  recipes: {len(recipes)}  |  GPU tiers: {len(tiers)}  |  images: {len(images)}  |  {status}\n")

        choice = questionary.select(
            "What to edit?",
            choices=[
                f"Recipes    — browse / create / edit / delete ({len(recipes)} recipes)",
                f"GPU Tiers  — manage GPU configurations ({len(tiers)} tiers)",
                f"Docker     — container images ({len(images)} images)",
                "Save       — write changes to recipes.toml" + (" [yellow]●[/yellow]" if any_modified else ""),
                "Reload     — discard changes, re-read from disk",
                "← Back",
            ],
            style=MENU_STYLE,
        ).ask()

        if choice is None or choice == "← Back":
            if any_modified:
                if questionary.confirm("Unsaved changes. Save before leaving?", default=True, style=MENU_STYLE).ask():
                    save_recipes(data)
                    console.print("[green]✓ Saved to recipes.toml[/green]")
            return

        if choice.startswith("Recipes"):
            if menu_edit_recipes(data):
                any_modified = True

        elif choice.startswith("GPU"):
            if menu_edit_gpu_tiers(data):
                any_modified = True

        elif choice.startswith("Docker"):
            if menu_edit_docker(data):
                any_modified = True

        elif choice.startswith("Save"):
            if any_modified:
                save_recipes(data)
                any_modified = False
                console.print("[green]✓ Saved to recipes.toml (backup at recipes.toml.bak)[/green]")
            else:
                console.print("[dim]No changes to save.[/dim]")
            press_enter()

        elif choice.startswith("Reload"):
            if any_modified:
                if not questionary.confirm("Discard unsaved changes?", default=False, style=MENU_STYLE).ask():
                    continue
            data = load_recipes()
            any_modified = False
            console.print("[green]✓ Reloaded from disk.[/green]")
            press_enter()
