"""
localrouter.hf_browser — HuggingFace model browser and quant pinning.

Extracted from vast_manager.py: list GGUF files in a HF repo, browse them
interactively, and pin a quant for the next launch wizard.
"""

import json
import re
import urllib.request

import questionary
from rich.table import Table
from rich import box

from .config import console, ROOT, HF_PIN, MENU_STYLE
from .helpers import _hf_token, ask_back, hr, press_enter, _fmt_bytes


# ── HuggingFace file listing ─────────────────────────────────────────────────

def _hf_list_files(repo_id, token=None):
    """Fetch the file list for a HuggingFace model repo.

    Args:
        repo_id: HF repo identifier (e.g. 'unsloth/Qwen3.6-27B-GGUF').
        token:   Optional HF API token for gated repos.

    Returns:
        List of file dicts with 'rfilename' and 'size' keys, or [].
    """
    url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "LocalRouter/1.0"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("siblings", [])
    except Exception:
        return []


# ── interactive browser ───────────────────────────────────────────────────────

def menu_hf_browser(recipes):
    """Interactive HuggingFace GGUF browser.

    Lists repos from known recipes, lets the user browse .gguf files,
    and optionally pins a quant (MODEL_REPO + MODEL_QUANT) for the
    next launch wizard.

    Args:
        recipes: List of recipe dicts (each may have 'model_repo', 'label').
    """
    hr("HuggingFace model browser")

    # build repo list from recipes.toml + custom option
    known_repos = {}
    for r in recipes:
        repo = r.get("model_repo", "")
        if repo and repo not in known_repos:
            known_repos[repo] = f"{repo}  (used in recipe: {r.get('label','')})"

    repo_choices = list(known_repos.values()) + ["[custom] enter repo ID manually"]

    sel = questionary.select(
        "HF repo to browse:",
        choices=ask_back(repo_choices),
        style=MENU_STYLE,
    ).ask()
    if sel is None or sel == "← Back":
        return

    if sel.startswith("[custom]"):
        repo_id = questionary.text(
            "Enter HF repo ID (e.g. unsloth/Qwen3.6-27B-GGUF):",
            style=MENU_STYLE,
        ).ask()
        if not repo_id:
            return
    else:
        repo_id = sel.split()[0]

    console.print(f"\n[dim]Fetching file list for {repo_id} from HuggingFace...[/dim]")
    files = _hf_list_files(repo_id, _hf_token())

    if not files:
        console.print("[red]Could not fetch file list — check repo ID or network.[/red]")
        press_enter(); return

    gguf_files = [f for f in files if f.get("rfilename", "").endswith(".gguf")]
    if not gguf_files:
        console.print("[yellow]No .gguf files found in this repo.[/yellow]")
        press_enter(); return

    quant_re   = re.compile(r'(UD-Q\d+[^.\s_-]*|Q\d+_K_[A-Z]+|Q\d+_\d+)', re.IGNORECASE)

    tbl = Table(title=f"[bold]{repo_id}[/bold]", box=box.SIMPLE, show_lines=False)
    tbl.add_column("filename",  style="bold", min_width=42)
    tbl.add_column("size",      style="cyan",  width=10)
    tbl.add_column("quant",     style="green", width=18)

    choice_map  = {}
    file_labels = []
    for f in gguf_files:
        fname    = f.get("rfilename", "?")
        size     = f.get("size", 0)
        size_str = _fmt_bytes(size) if size else "?"
        m        = quant_re.search(fname)
        quant    = m.group(0) if m else "?"
        tbl.add_row(fname, size_str, quant)
        label = f"{fname:<54} {size_str:>8}  {quant}"
        choice_map[label] = (fname, quant, size_str, repo_id)
        file_labels.append(label)

    console.print(tbl)
    console.print(f"  [dim]{len(gguf_files)} .gguf file(s) shown — sizes are per-shard[/dim]\n")

    action = questionary.select(
        "Action:",
        choices=["Pin a quant for the next launch wizard", "← Back to main menu"],
        style=MENU_STYLE,
    ).ask()
    if action is None or action.startswith("←"):
        return

    file_sel = questionary.select(
        "Select file to pin as MODEL_QUANT:",
        choices=ask_back(file_labels),
        style=MENU_STYLE,
        use_shortcuts=False,
    ).ask()
    if file_sel is None or file_sel == "← Back":
        return

    fname, quant, size_str, repo_id = choice_map[file_sel]
    pin = {"MODEL_REPO": repo_id, "MODEL_QUANT": quant, "filename": fname, "size": size_str}
    HF_PIN.write_text(json.dumps(pin))
    console.print(f"\n[green]Pinned:[/green]  MODEL_REPO={repo_id}  MODEL_QUANT={quant}")
    console.print(f"[dim]Next Launch wizard will offer to use this quant.[/dim]")
    press_enter()
