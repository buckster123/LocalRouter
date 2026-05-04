---
name: localrouter
description: Launch and manage private LLM endpoints via LocalRouter TUI — local GPU, rented Vast.ai, vLLM multi-GPU clusters, or Together AI managed inference.
version: 0.3.0
trigger: user wants to launch, manage, or configure private LLM endpoints — local, rented GPU (Vast.ai), vLLM multi-GPU, or Together AI managed
source: https://github.com/buckster123/LocalRouter
---

# LocalRouter — Private LLM Endpoint Manager

Interactive TUI + CLI for spinning up private LLM inference across 4 providers:
- **local** — llama.cpp on your own GPU (Vulkan/ROCm/CUDA/CPU)
- **vast_gguf** — GGUF models on rented Vast.ai GPUs via llama.cpp
- **vllm** — tensor-parallel serving on multi-GPU clusters (DeepSeek V4 Pro, etc.)
- **together** — Together AI managed endpoints (229+ models)

All providers route through a unified OpenAI-compatible proxy at `localhost:8888`.

## Install

```bash
git clone https://github.com/buckster123/LocalRouter.git
cd LocalRouter
pip install -e .
```

Dependencies: `questionary`, `rich`, `tomli_w` (auto-installed).
For Vast.ai: `pip install vastai && vastai set api-key <key>`

## Launch the TUI

```bash
localrouter
# or: python3 -m localrouter
# or: cd ~/Projects/qwen36-vast && python3 vast_manager.py
```

## Key Workflows

### 1. Spin up a GGUF model on a rented GPU

```
localrouter → Launch → Vast GGUF → pick GPU tier → pick recipe → GEO → go
```

After launch:
- **Watch** — follow boot progress until healthy
- **Tunnel → up** — SSH tunnel to forward the endpoint locally
- **Smoke** — run provider-aware smoke tests
- **Proxy** — start the local proxy on localhost:8888

### 2. Spin up a large MoE model via vLLM

```
localrouter → Launch → vLLM → pick cluster tier (e.g. 5×H200) → pick recipe → GEO → go
```

vLLM auto-configures tensor parallelism, FlashInfer attention, FP8 KV cache.
Pre-configured for DeepSeek V4 Flash (284B) and Pro (1.6T).

### 3. Run locally (no cloud needed)

```
localrouter → Local → Launch → pick a local recipe
```

Requires: compiled llama.cpp + GGUF models in `~/models/`.

### 4. Edit recipes without touching TOML

```
localrouter → Editor → Recipes / GPU Tiers / Docker Images
```

Full CRUD with validation. Saves to `recipes.toml` with auto-backup.

## Recipe System

Everything is in `recipes.toml` at the project root. 70 pre-configured recipes across 19 GPU tiers.

### Recipe fields (Vast GGUF — llama.cpp)

```toml
[[recipes]]
name        = "qwen36-27b-q6-5090"       # unique slug
label       = "Qwen3.6-27B  Q6_K  96K"   # display name
gpu         = "5090"                       # must match a gpu_tiers key
model_repo  = "unsloth/Qwen3.6-27B-GGUF" # HuggingFace repo
model_quant = "UD-Q6_K_XL"               # substring match against filenames
ctx         = 98304                        # context length in tokens
parallel    = 1                            # concurrent decode slots
kv_type     = "q8_0"                       # q8_0 | q4_0 | bf16
# Optional: custom llama.cpp fork for unmerged model support
llama_cpp_repo = "fairydreaming/llama.cpp"
llama_cpp_ref  = "deepseek-dsa"
```

### Recipe fields (vLLM — tensor parallel)

```toml
[[recipes]]
name             = "dsv4-pro-5xh200"
provider         = "vllm"
label            = "DSv4-Pro 1.6T  FP4+FP8  384K ctx  (5×H200)"
gpu              = "h200-sxm-5x"
model_id         = "deepseek-ai/DeepSeek-V4-Pro"
ctx              = 393216
image_type       = "vllm"
kv_cache_dtype   = "fp8"
reasoning_parser = "deepseek_r1"
```

### GPU tier fields

```toml
[gpu_tiers.h100-sxm-2x]
vast_names  = ["H100_SXM", "H100_SXM5"]
label       = "2× H100 SXM 160GB"
max_price   = "7.00"
vram_gb     = 80
num_gpus    = 2
image_type  = "builder"    # prebuilt | builder | vllm
```

## Agentic Usage (Hermes / OpenAI-compatible clients)

Once an endpoint is running and tunneled, point your agent at the proxy:

```bash
# The proxy auto-routes to whichever provider is active
export OPENAI_BASE_URL=http://localhost:8888/v1
export OPENAI_API_KEY=not-needed  # local proxy, no auth

# Or configure Hermes directly
hermes config set providers.localrouter.base_url http://localhost:8888/v1
hermes config set providers.localrouter.api_key not-needed
```

Health check: `curl http://localhost:8888/health`

## Docker Images

| Image | Use case |
|-------|----------|
| `ghcr.io/buckster123/vastai-gguf:prebuilt` | Fat binary for 4090/5090 (SM89+SM120) |
| `ghcr.io/buckster123/vastai-gguf:builder` | Compiles llama.cpp at boot for any GPU |
| `ghcr.io/buckster123/vastai-gguf:vllm` | vLLM serving for multi-GPU tensor parallel |

## Available GPU Tiers (v0.3.0)

Consumer: RTX 4090 (24GB), RTX 5090 (32GB), RTX PRO 6000 (96GB)
Datacenter single: H100 SXM/PCIe (80GB), A100 SXM/PCIe (80GB), H200 (141GB), B200 (192GB)
Multi-GPU GGUF: 2×H100, 4×H100, 2×H200, 2×B200, 4×B200
Multi-GPU vLLM: 4×H200, 5×H200, 8×H100, 8×A100

## File Layout

```
LocalRouter/
├── localrouter/           # Python package (18 modules, ~5000 LOC)
│   ├── menus/             # TUI menus (main, local, vast, provider, editor, tool)
│   ├── config.py          # Recipe/tier loading, constants
│   ├── recipe_editor.py   # TOML CRUD, validation (tomllib + tomli_w)
│   └── ...
├── recipes.toml           # 70 recipes, 19 GPU tiers, 4 docker images
├── launch.sh              # llama.cpp entrypoint (GGUF + custom branch support)
├── launch_vllm.sh         # vLLM entrypoint (tensor parallel, FlashInfer)
├── vast_up.sh             # Vast.ai instance launcher
├── vast_down.sh           # Tear down instance
├── smoke.sh               # Endpoint smoke tests
├── tools/vast_tunnel.sh   # SSH tunnel manager
├── endpoint_proxy.py      # Transparent proxy on localhost:8888
├── Dockerfile             # Prebuilt llama.cpp (SM89+SM120)
├── Dockerfile.builder     # Build-at-boot llama.cpp (any SM)
└── Dockerfile.vllm        # vLLM serving image
```

## Pitfalls

1. **DeepSeek V4 on llama.cpp**: Not in upstream yet. Recipes use `llama_cpp_repo`/`llama_cpp_ref` to build from fairydreaming's PR branch. When it merges to master, remove those fields.
2. **vLLM image must be built first**: `docker build -f Dockerfile.vllm -t ghcr.io/buckster123/vastai-gguf:vllm .` then push to GHCR.
3. **Multi-GPU needs NVLink**: Tensor parallel across GPUs without NVLink is painfully slow. Vast.ai datacenter offers usually have it.
4. **Builder cold start**: Builder images compile llama.cpp at boot (~1 min on H100, longer on consumer GPUs). Prebuilt is faster but only supports SM89+SM120.
5. **Split GGUFs**: launch.sh uses `find` with maxdepth 2 to handle repos with subdirectory shards.
6. **Proxy auth**: The proxy on localhost:8888 has no auth — it's designed for local use behind an SSH tunnel, not public exposure.
