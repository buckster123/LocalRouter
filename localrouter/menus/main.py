"""Main menu loop and banner for LocalRouter."""
from __future__ import annotations

import questionary
from rich.panel import Panel

from ..config import console, MENU_STYLE, load_config, image_for_type
from ..providers import load_provider_config
from .tool_menus import show_status, menu_diagnose, menu_batch_compare, menu_smoke, menu_proxy
from .vast_menus import menu_launch, menu_tunnel, menu_destroy, menu_instances, menu_watch_boot
from .local_menus import menu_local_dispatch
from .provider_menus import menu_providers, menu_together_models
from ..hf_browser import menu_hf_browser
from .editor_menus import menu_editor


def banner(docker_img: str) -> None:
    """Print the app banner."""
    console.print(Panel(
        "[bold #7c6af7]LocalRouter[/bold #7c6af7]  \n"
        "[dim]GGUF endpoint manager — local, Vast.ai & managed[/dim]\n"
        f"[dim]image: {docker_img}  |  recipes: recipes.toml[/dim]",
        border_style="#3d3d5c", padding=(0, 2),
    ))


def main() -> None:
    """Main menu loop."""
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
                "Local      — manage local llama.cpp endpoints",
                "Providers  — configure API keys and base URLs",
                "Together   — browse Together AI models",
                "Batch      — compare multiple providers/models side-by-side",
                "Watch      — live boot progress watcher",
                "Diagnose   — usage stats, rate limits, deep diagnostics",
                "Instances  — list / reattach to all active instances",
                "HF Browse  — browse model files on HuggingFace, pin a quant",
                "Editor     — recipes, GPU tiers, docker images",
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
        elif choice.startswith("Local"):      menu_local_dispatch(provider_cfg)
        elif choice.startswith("Providers"):  menu_providers(provider_cfg)
        elif choice.startswith("Together"):   menu_together_models(provider_cfg)
        elif choice.startswith("Batch"):      menu_batch_compare(provider_cfg)
        elif choice.startswith("Watch"):      menu_watch_boot()
        elif choice.startswith("Diagnose"):   menu_diagnose(provider_cfg)
        elif choice.startswith("Instances"):  menu_instances()
        elif choice.startswith("HF Browse"):  menu_hf_browser(recipes)
        elif choice.startswith("Editor"):
            menu_editor()
            # Reload config in case editor changed recipes.toml
            cfg, recipes, gpu_tiers, docker_cfg = load_config()
        elif choice.startswith("Tunnel"):     menu_tunnel()
        elif choice.startswith("Smoke"):      menu_smoke(provider_cfg)
        elif choice.startswith("Proxy"):      menu_proxy()
        elif choice.startswith("Destroy"):    menu_destroy()
