# vastai-gguf-launcher

Rental compute & endpoint manager for private LLM inference. Spin up GGUF models
on rented Vast.ai GPUs, or connect managed endpoints (Together AI) — all through
one interactive TUI with a transparent local proxy server.

```
vastai-gguf-launcher/
  vast_manager.py        ← interactive TUI (the main thing)
  recipes.toml           ← model + GPU recipe catalogue (edit this)
  endpoint_proxy.py      ← local proxy server (localhost:8888)
  usage_tracker.py       ← cost tracking, rate limits, usage summaries
  launch.sh              ← runs inside container: downloads weights + starts server
  vast_up.sh             ← launch script (reads env set by TUI)
  vast_down.sh           ← tear down
  smoke.sh               ← endpoint smoke test (provider-aware validation)
  Dockerfile             ← prebuilt llama.cpp container image
  tools/
    vast_tunnel.sh       ← SSH tunnel manager (up/status/down/logs)
```

## What it does

| Feature | Description |
|---------|-------------|
| **Vast.ai GGUF** | Launch private model endpoints on rented GPUs (RTX 4090/5090, etc.) |
| **Together AI** | Connect managed inference endpoints (229+ models, $0.18-$3.50/M tokens) |
| **Local proxy** | Unified `localhost:8888` endpoint — clients don't care which provider is active |
| **Usage tracking** | Per-provider cost logging (JSONL), session aggregation, rate limit monitoring |
| **Batch compare** | Send the same prompt to multiple providers side-by-side with metrics |
| **Launch wizard** | Guided workflow: GPU → recipe → geo → offer → spin up |
| **Deep diagnostics** | SSH probes, download speed measurement, stall detection & recovery |

## Quick start

```bash
git clone https://github.com/buckster123/vastai-gguf-launcher
cd vastai-gguf-launcher

pip install questionary rich          # TUI deps (one-time)
pip install vastai                    # Vast.ai CLI (one-time)
vastai set api-key <your-key>         # from console.vast.ai

# (Optional) Together AI — store your key:
mkdir -p ~/.vastai-gguf
cat > ~/.vastai-gguf/config.toml << 'EOF'
[provider.together]
api_key = "sk-xxxxx"
base_url = "https://api.together.ai/v1"
EOF

./vast_manager.py                     # that's it
```

The TUI walks you through **Launch** (Vast GGUF) or **Provider → Switch** (Together
managed endpoints). While instances boot, use **Watch** to follow progress. Once
healthy, start the **Proxy Server** to get a unified `http://127.0.0.1:8888/v1`
endpoint regardless of provider.

## Provider system

### Vast.ai (GGUF on rented GPU)

The TUI guides you through GPU tier → model recipe → geo → offer selection → launch.
While it boots, use **Watch** to follow progress. Once healthy, use **Tunnel → up**
to forward the endpoint locally, then start the proxy server for transparent access.

### Together AI (managed inference)

Configure once in `~/.vastai-gguf/config.toml`:

```toml
[provider.together]
api_key = "sk-xxxxx"
base_url = "https://api.together.ai/v1"
```

The TUI detects this and enables:
- **Browse Together Models** — search 229+ models, pin your choice
- **Provider → Switch to Together** — hotswap active provider
- **Rate limit monitoring** — visible in Diagnose screen

### Local proxy server

The bundled `endpoint_proxy.py` runs on `localhost:8888` and forwards all requests
to whichever provider is currently active (Vast GGUF or Together AI). This means:
- Clients always point at the same URL — switch providers without code changes
- OpenAI-compatible API format (`/v1/chat/completions`, `/v1/completions`, etc.)
- Built-in health check at `localhost:8888/health`

```bash
# Via TUI: Proxy Server → Start
# Or directly:
python3 endpoint_proxy.py --provider vast-gguf --upstream 127.0.0.1:8000
python3 endpoint_proxy.py --provider together     # uses config.toml
```

## Usage tracking

Every completion is logged to `~/.vastai-gguf/usage.log` (JSONL format):

```json
{"ts":"2026-05-02T20:15:32","provider":"together","model":"Qwen/Qwen2.5-Coder-32B-Instruct",
 "prompt_tokens":42,"completion_tokens":128,"cost":0.00042}
```

**View usage in the Diagnose screen**, or run:

```bash
python3 -c "from usage_tracker import format_summary; print(format_summary(24))"
```

Output shows per-provider breakdown with token counts and costs for the last 24 hours.

## Batch comparison mode

Send the same prompt to multiple providers simultaneously and compare results side-by-side:

```bash
python3 endpoint_proxy.py batch --prompt "What is 2+2?" \
    --endpoints "127.0.0.1:8000" "https://api.together.ai/v1" \
    --models "Qwen/Qwen3-32B" "meta-llama/Llama-3.3-70B-Instruct"
```

Output includes latency, token counts, cost comparison, and rendered responses from each provider.

## Configuring recipes

Edit `recipes.toml` to add models. The TUI reads it at startup — no Python editing needed.

### Vast.ai GGUF recipes:

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

### Together AI recipes:

```toml
[[recipes]]
name        = "qwen3-32b-together"
label       = "Qwen3-32B (managed)"
provider    = "together"
model_id    = "Qwen/Qwen3-32B"
description = "Fast, efficient Qwen3 for general tasks."
```

`model_quant` is matched as a substring against filenames in the HF repo. Use the
**HF Browse** menu to inspect available files and quants before adding a recipe.

## Supported GPUs (Vast.ai)

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
| **Diagnose** | Deep diagnostics: usage stats, rate limits, SSH probes, stall detection |
| **Instances** | List all active Vast instances, reattach `.last_instance` |
| **Provider** | Switch between Vast GGUF and Together AI endpoints |
| **Browse Models** | Fetch Together model catalog or HF repo files; pin a model/quant |
| **Tunnel** | Manage SSH tunnel: up / status / down / logs |
| **Proxy Server** | Start/stop the local transparent proxy on localhost:8888 |
| **Smoke** | Run provider-aware smoke tests (health, completion, tool call, throughput) |
| **Batch Compare** | Send same prompt to multiple providers side-by-side |
| **Destroy** | Tear down current instance (stops tunnel first) |

## Security

By default `launch.sh` binds llama-server to `127.0.0.1:8000` — the public port mapping
exists on the host but nothing listens externally. Access is exclusively through the SSH
tunnel (`tools/vast_tunnel.sh up`) or the local proxy server.

This prevents anyone scanning Vast's IP range from hitting your endpoint and burning your
rented GPU time.

For Together AI, all requests go directly to their API — no public-facing endpoints needed.

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

Set `max_tokens >= 1024` for thinking-mode — the model emits `<thinking>...</thinking>`
before answering and will silently truncate if the budget is too small.

## Diagnosing a stuck instance

The **Diagnose** menu SSHes in and shows:
- Process list (is `hf download` running? is `llama-server` up?)
- Disk usage on `/workspace`
- Model file status (`.incomplete` = still downloading, `.gguf` = done)
- Live download speed measured over a 4-second sample on `eth0`
- Last 30 lines of `/var/log/launch.log`
- **Usage summary** — token counts and costs for the last 24 hours
- **Rate limit status** — if Together AI is configured

If download speed < 1 KB/s it flags the stall and offers to kill the hung process and
restart `launch.sh`. HF hub resumes from the `.incomplete` file so no data is lost.

## Cost reference (May 2026 snapshot)

### Vast.ai (rented GPU)

| GPU | cheapest reliable | cold start | decode speed |
|-----|-------------------|------------|--------------|
| RTX 5090 32GB | ~$0.34/hr | ~8 min | ~110 t/s (MoE Q5) |
| RTX 4090 24GB | ~$0.28/hr | ~8 min | ~60 t/s (MoE Q4) |
| RTX PRO 6000 96GB | ~$0.93/hr | ~15 min | ~70 t/s × 6 slots |

### Together AI (managed per-token)

| Model tier | Price /M tokens | Latency |
|------------|-----------------|---------|
| Small (8B) | $0.18 | ~200ms |
| Medium (32B) | $0.65 | ~400ms |
| Large (72B) | $1.80 | ~800ms |
| Premium (400B+) | $3.50 | ~1200ms |

For short workloads (< 1M tokens), managed is usually cheaper. For long sessions with
heavy context windows, rented GPU wins on predictable pricing and zero per-token metering.

## Requirements

- Python 3.10+
- `pip install questionary rich vastai`
- `vastai set api-key <key>` (from console.vast.ai)
- `jq` (usually pre-installed)
- Optional: `huggingface_hub` token at `~/.cache/huggingface/token` for gated models
- Optional: Together AI API key from https://api.together.ai/settings

## License

MIT
