# LocalRouter

Local compute & endpoint manager for private LLM inference. Run GGUF models on your own hardware, rent Vast.ai GPUs, or connect managed providers — all through one interactive TUI with a transparent local proxy server.

```
qwen36-vast/                  # renamed → LocalRouter
  vast_manager.py             ← interactive TUI (the main thing)
  recipes.toml                ← model + GPU recipe catalogue (edit this)
  endpoint_proxy.py           ← local proxy server (localhost:8888)
  usage_tracker.py            ← cost tracking, rate limits, usage summaries
  launch.sh                   ← runs inside container: downloads weights + starts server
  vast_up.sh                  ← Vast.ai instance launcher (reads env set by TUI)
  vast_down.sh                ← tear down Vast instance
  smoke.sh                    ← endpoint smoke test (provider-aware validation)
  Dockerfile                  ← prebuilt llama.cpp container image
  tools/
    vast_tunnel.sh            ← SSH tunnel manager (up/status/down/logs)
```

## What it does

| Feature | Description |
|---------|-------------|
| **Local LLM** | Run llama-server on your own hardware — Vulkan, ROCm, CUDA, CPU |
| **Vast.ai GGUF** | Launch private model endpoints on rented GPUs (RTX 4090/5090, H100, etc.) |
| **Together AI** | Connect managed inference endpoints (229+ models, $0.18-$3.50/M tokens) |
| **Local proxy** | Unified `localhost:8888` endpoint — clients don't care which provider is active |
| **Usage tracking** | Per-provider cost logging (JSONL), session aggregation, rate limit monitoring |
| **Batch compare** | Send the same prompt to multiple providers side-by-side with metrics |
| **Launch wizard** | Guided workflow: GPU → recipe → geo → offer → spin up |
| **Deep diagnostics** | SSH probes, download speed measurement, stall detection & recovery |

## Quick start

```bash
git clone https://github.com/buckster123/LocalRouter.git
cd LocalRouter

pip install questionary rich          # TUI deps (one-time)

# For Vast.ai rental mode:
pip install vastai                    # Vast.ai CLI (one-time)
vastai set api-key <your-key>         # from console.vast.ai
```

### Local mode (no API keys needed)

Point recipes at your GGUF models and run llama-server on your own GPU:

```bash
# Make sure llama.cpp is compiled somewhere findable (~llama.cpp/build*/bin/)
# And your .gguf files are in ~/models/ (auto-discovered)

python3 vast_manager.py               # TUI → Local → Launch → pick recipe
```

### Vast.ai mode

Configure your Vast API key and launch from the TUI:

```bash
vastai set api-key <your-key>
python3 vast_manager.py               # TUI → Launch → Vast GGUF
```

### Together AI (managed)

Optional — store your key for managed inference:

```bash
mkdir -p ~/.vastai-gguf
cat > ~/.vastai-gguf/config.toml << 'EOF'
[provider.together]
api_key = "sk-xxxxx"
base_url = "https://api.together.ai/v1"
EOF
```

The TUI detects this and enables **Browse Together Models** and hot-swapping.

## Provider system

### Local (llama.cpp on your hardware)

Run models directly without Docker or rental compute. The TUI auto-discovers:
- llama-server binaries from PATH, `~/llama.cpp/build*/bin/`
- GGUF models in `~/models/` (configurable)
- Available backends (Vulkan, ROCm/HIP, CUDA, CPU)

```toml
[[recipes]]
name       = "local-qwen35-9b"
provider   = "local"
label      = "Qwen3.5-9B  Q4_K_M  (local Vulkan)"
model_path = "~/models/Qwen3.5-9B-Q4_K_M.gguf"
port       = 8100
ctx        = 32768
backend    = "vulkan"     # vulkan / rocm / cuda / cpu
mode       = "thinking"   # thinking / coding / nonthinking
```

TUI flow: **Local → Launch** → pick recipe → confirm config → start llama-server.  
PID tracking, health checks, log viewing, and graceful shutdown built in.

### Vast.ai (GGUF on rented GPU)

The TUI guides you through GPU tier → model recipe → geo → offer selection → launch.
While it boots, use **Watch** to follow progress. Once healthy, use **Tunnel → up**
to forward the endpoint locally, then start the proxy server for transparent access.

### Together AI (managed inference)

Configure once in `~/.vastai-gguf/config.toml`. The TUI enables:
- **Browse Together Models** — search 229+ models, pin your choice
- **Provider → Switch to Together** — hotswap active provider
- **Rate limit monitoring** — visible in Diagnose screen

### Local proxy server

The bundled `endpoint_proxy.py` runs on `localhost:8888` and forwards all requests
to whichever provider is currently active (Local, Vast GGUF, or Together AI). This means:
- Clients always point at the same URL — switch providers without code changes
- OpenAI-compatible API format (`/v1/chat/completions`, `/v1/completions`, etc.)
- Built-in health check at `localhost:8888/health`

## Usage tracking

Every completion is logged to `~/.vastai-gguf/usage.log` (JSONL format):

```json
{"ts":"2026-05-02T20:15:32","provider":"local","model":"Qwen3.5-9B-Q4_K_M.gguf",
 "prompt_tokens":42,"completion_tokens":128,"cost":0.0}
```

Local inference is tracked at $0 cost. View usage in the **Diagnose** screen, or:

```bash
python3 -c "from usage_tracker import format_summary; print(format_summary(24))"
```

## Configuring recipes

Edit `recipes.toml` to add models. The TUI reads it at startup — no Python editing needed.

### Local endpoint recipes:

```toml
[[recipes]]
name       = "local-mistral-nemo"
provider   = "local"
label      = "Mistral Nemo 12B  Q6_K  (local)"
model_path = "~/models/Mistral-Nemo-Instruct-Q6_K.gguf"
port       = 8100
ctx        = 32768
parallel   = 2
kv_type    = "q8_0"
backend    = "vulkan"
mode       = "thinking"
```

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
name        = "together-qwen3-32b"
provider    = "together"
label       = "Qwen3-32B (managed)"
model_id    = "Qwen/Qwen3-32B"
description = "Fast, efficient Qwen3 for general tasks."
```

## TUI menu overview

| Menu item | What it does |
|-----------|--------------|
| **Launch** | Guided wizard: Local / Vast GGUF / Together AI → spin up |
| **Local** | Manage local llama.cpp endpoints (launch/status/configure) |
| **Watch** | Live boot watcher — polls status + log every 10s until healthy |
| **Diagnose** | Deep diagnostics: usage stats, rate limits, SSH probes, stall detection |
| **Instances** | List all active Vast instances, reattach `.last_instance` |
| **Providers** | Configure API keys and base URLs |
| **Together** | Browse Together AI models, pin a choice |
| **Batch Compare** | Send same prompt to multiple providers side-by-side |
| **HF Browse** | Browse HuggingFace model files, pin a quant |
| **Tunnel** | Manage SSH tunnel: up / status / down / logs |
| **Proxy** | Start/stop the local transparent proxy on localhost:8888 |
| **Smoke** | Run provider-aware smoke tests (health, completion, tool call, throughput) |
| **Destroy** | Tear down current Vast instance (stops tunnel first) |

## Security

By default `launch.sh` binds llama-server to `127.0.0.1:8000` — the public port mapping
exists on the host but nothing listens externally. Access is exclusively through the SSH
tunnel (`tools/vast_tunnel.sh up`) or the local proxy server.

For local endpoints, the process binds to `127.0.0.1` by default — never exposed to the network.

## Requirements

- Python 3.10+
- `pip install questionary rich`
- For Vast.ai: `vastai` CLI + API key from console.vast.ai
- For local mode: compiled llama.cpp (Vulkan/ROCm/CUDA build) + GGUF models
- Optional: Together AI API key from https://api.together.ai/settings

## License

MIT
