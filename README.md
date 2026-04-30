# vastai-gguf-launcher

Spin up a private, OpenAI-compatible GGUF model endpoint on a rented Vast.ai
GPU in ~10 minutes. Comes with an interactive TUI to launch, monitor, diagnose,
and tear down instances without remembering any CLI flags.

```
vastai-gguf-launcher/
  vast_manager.py   ← interactive TUI (the main thing)
  recipes.toml      ← model + GPU recipe catalogue (edit this)
  vast_up.sh        ← launch script (reads env set by the TUI)
  vast_down.sh      ← tear down
  launch.sh         ← runs inside the container: downloads weights + starts llama-server
  smoke.sh          ← endpoint smoke test (health, completion, tool call, throughput)
  tools/
    vast_tunnel.sh  ← SSH tunnel manager (up/status/down/logs)
```

## Quick start

```bash
git clone https://github.com/buckster123/vastai-gguf-launcher
cd vastai-gguf-launcher

pip install questionary rich          # TUI deps (one-time)
pip install vastai                    # Vast.ai CLI (one-time)
vastai set api-key <your-key>         # from console.vast.ai

./vast_manager.py                     # that's it
```

The TUI walks you through GPU tier → model recipe → geo → offer selection →
launch. While it boots, use **Watch** to follow progress. Once healthy, use
**Tunnel → up** to forward the endpoint to `http://127.0.0.1:8800/v1`.

## The container image

The prebuilt image (`ghcr.io/buckster123/qwen36-llamacpp:latest`) ships:
- llama.cpp `llama-server` built for SM89 (RTX 4090) and SM120 (RTX 5090)
- CUDA 12.8 runtime (avoids Unsloth's CUDA 13.2 quality warning)
- `hf-transfer` + `huggingface_hub` for fast weight downloads

**Build your own** (change the image in `recipes.toml`):

```bash
docker build -t ghcr.io/<you>/vastai-gguf:latest .
echo "$(gh auth token)" | docker login ghcr.io -u <you> --password-stdin
docker push ghcr.io/<you>/vastai-gguf:latest
# make the package public on GitHub so Vast can pull it
```

## Configuring recipes

Edit `recipes.toml` to add any GGUF model. The TUI reads it at startup — no
Python editing needed.

```toml
[[recipes]]
name        = "mistral-nemo-q6-5090"
label       = "Mistral Nemo 12B  Q6_K  64K ctx"
gpu         = "5090"
model_repo  = "bartowski/Mistral-Nemo-Instruct-2407-GGUF"
model_quant = "Q6_K"
ctx         = 65536
parallel    = 2
kv_type     = "q8_0"
description = "Mistral Nemo at near-lossless quality, 2 concurrent slots."
```

`model_quant` is matched as a substring against filenames in the repo, so
`Q6_K` matches `Mistral-Nemo-Instruct-2407-Q6_K.gguf`. Use the **HF Browse**
menu to inspect available files and quants before adding a recipe — it fetches
the file list from the HuggingFace API and lets you pin a quant directly into
the launch wizard.

## Supported GPUs

| `gpu` value | Card             | Default price ceiling |
|-------------|------------------|-----------------------|
| `5090`      | RTX 5090 32GB    | $0.55/hr              |
| `4090`      | RTX 4090 24GB    | $0.45/hr              |
| `6000pro`   | RTX PRO 6000 96GB| $1.60/hr              |

Add more GPU tiers in the `[gpu_tiers]` section of `recipes.toml`.

## TUI menu overview

| Menu item | What it does |
|-----------|--------------|
| **Launch** | Guided wizard: GPU → recipe → mode → geo → offer → spin up |
| **Watch** | Live boot watcher — polls status + log every 10s until healthy |
| **Diagnose** | SSH in, show processes/disk/model files, measure download speed, detect + fix stalls |
| **Instances** | List all your active Vast instances, reattach `.last_instance` |
| **HF Browse** | Fetch file list from HuggingFace, pin a quant for the next launch |
| **Tunnel** | Manage SSH tunnel: up / status / down / logs |
| **Smoke** | Run `smoke.sh` against the endpoint (completions, tool calls, throughput) |
| **Destroy** | Tear down the current instance (stops tunnel first) |

## Security

By default `launch.sh` binds llama-server to `127.0.0.1:8000` — the public
port mapping exists on the host but nothing listens externally. Access is
exclusively through the SSH tunnel (`tools/vast_tunnel.sh up` → `127.0.0.1:8800`).

This prevents anyone scanning Vast's IP range from hitting your endpoint and
burning your rented GPU time.

## Tunnel

```bash
./tools/vast_tunnel.sh up       # start (reads .last_instance)
./tools/vast_tunnel.sh status   # pid + instance + health + model + slots
./tools/vast_tunnel.sh logs     # tail -f /var/log/launch.log on container
./tools/vast_tunnel.sh down     # kill tunnel
```

For low-latency agentic loops, add ControlMaster to `~/.ssh/config`:

```
Host ssh*.vast.ai
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 5m
```

This drops per-request SSH overhead from ~500ms to ~RTT (20-50ms for EU hosts).

## Inference modes

Set by `MODE` env / selectable in the TUI wizard:

| Mode | temp | top-p | thinking |
|------|------|-------|----------|
| `thinking` | 1.0 | 0.95 | on (default) |
| `coding` | 0.6 | 0.95 | on |
| `nonthinking` | 0.7 | 0.80 | off |

Set `max_tokens >= 1024` for thinking-mode — the model emits `<think>...</think>`
before answering and will silently truncate if the budget is too small.

## Diagnosing a stuck instance

The **Diagnose** menu SSHes in and shows:
- Process list (is `hf download` running? is `llama-server` up?)
- Disk usage on `/workspace`
- Model file status (`.incomplete` = still downloading, `.gguf` = done)
- Live download speed measured over a 4-second sample on `eth0`
- Last 30 lines of `/var/log/launch.log`

If download speed < 1 KB/s it flags the stall and offers to kill the hung
process and restart `launch.sh`. HF hub resumes from the `.incomplete` file
so no data is lost.

## Cost reference (Apr 2026 snapshot)

| GPU | cheapest reliable | cold start | decode speed |
|-----|-------------------|------------|--------------|
| RTX 5090 32GB | ~$0.34/hr | ~8 min | ~110 t/s (MoE Q5) |
| RTX 4090 24GB | ~$0.28/hr | ~8 min | ~60 t/s (MoE Q4) |
| RTX PRO 6000 96GB | ~$0.93/hr | ~15 min | ~70 t/s × 6 slots |

5090 ≈ 75-80% faster decode than 4090 on these models due to GDDR7 bandwidth.
6000 Pro advantage is capacity (96GB) for multi-slot or high-quant configs.

## Requirements

- Python 3.10+
- `pip install questionary rich vastai`
- `vastai set api-key <key>` (get from console.vast.ai)
- `jq` (usually pre-installed)
- Optional: `huggingface_hub` token at `~/.cache/huggingface/token` for gated models

## License

MIT
