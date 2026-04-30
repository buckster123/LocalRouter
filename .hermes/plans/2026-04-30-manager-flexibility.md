# vastai-gguf-launcher — Manager Flexibility Plan

**Date:** 2026-04-30  
**Status:** Draft  
**Scope:** Make the manager and image pipeline work across the full range of
Vast.ai GPU inventory — consumer (4090/5090), datacentre (H100/H200/B200/A100),
and Blackwell variants — without maintaining a separate codebase per card.

---

## Goal

Right now the launcher is effectively locked to SM89 (4090) + SM120 (5090)
because the Docker image is a single fat binary built for those two archs.
Adding new GPU families means either:
- Rebuilding the image for every new arch (slow, fragile)
- Or building the binary at runtime on the rented host (flexible, right approach)

At the same time, the `vast_up.sh` GPU filter and `recipes.toml` GPU tiers are
hardcoded to a small fixed set. We want the manager to surface H100s, B200s,
A100s etc. from the offer browser without needing code changes.

---

## Current state

```
Dockerfile
  - Base: nvidia/cuda:12.8.0-devel-ubuntu24.04
  - Builds llama-server fat binary: SM89 (4090) + SM120 (5090)
  - Ships the binary in the image — works great for those two cards
  - Falls over silently on anything else (wrong SM = garbage output or crash)

vast_up.sh
  - gpu_name filter: RTX_5090 | RTX_4090 | RTX_PRO_6000_WS/S
  - Case statement: dense | moe | moe-256k | moe-beast
  - No knowledge of H-series or Blackwell datacenter cards

recipes.toml
  - gpu_tiers: 5090 | 4090 | 6000pro only
  - No datacenter tier entries

vast_manager.py
  - browse_offers(): hard-coded gpu_filter strings
  - GPU tier list built from recipes.toml gpu_tiers section (already flexible)
  - No SM arch mapping
```

---

## Architecture: two-image strategy

Rather than one fat binary that tries to cover everything, use two images:

### Image A — `vastai-gguf:prebuilt` (current)
- Stays exactly as-is for 4090/5090
- Fast cold start (~2 min pull, binary already compiled)
- Used when `gpu` matches a known consumer tier

### Image B — `vastai-gguf:builder` (new)
- Ships only: CUDA dev toolchain, cmake, git, hf-transfer, Python deps
- No pre-compiled llama-server
- `launch.sh` detects SM arch at runtime, compiles llama.cpp for exact target
- Compilation adds ~5-8 min to cold start but works on ANY CUDA GPU
- Used when `gpu` is a datacenter tier or custom arch

The recipe in `recipes.toml` declares which image strategy to use:

```toml
[[recipes]]
name        = "llama33-70b-h100-sxm"
gpu         = "h100-sxm"
image_type  = "builder"           # ← new field: "prebuilt" | "builder"
...
```

`vast_up.sh` selects the image based on `image_type` env var.

---

## Step-by-step plan

### Phase 1 — Dynamic GPU tier + offer search (manager + recipes, no new image)

**Files:** `recipes.toml`, `vast_up.sh`, `vast_manager.py`

1. **Add datacentre GPU tiers to `recipes.toml`**

```toml
[gpu_tiers.h100-sxm]
vast_names  = ["H100_SXM", "H100_SXM5"]
max_price   = "3.50"
min_disk_gb = 100
label       = "H100 SXM 80GB  (~$2.50/hr)"
image_type  = "builder"

[gpu_tiers.h100-pcie]
vast_names  = ["H100_PCIE"]
max_price   = "2.50"
min_disk_gb = 100
label       = "H100 PCIe 80GB  (~$1.80/hr)"
image_type  = "builder"

[gpu_tiers.a100-sxm]
vast_names  = ["A100_SXM4_80GB", "A100_SXM4_40GB"]
max_price   = "2.00"
min_disk_gb = 80
label       = "A100 SXM 80/40GB  (~$1.20/hr)"
image_type  = "builder"

[gpu_tiers.h200-sxm]
vast_names  = ["H200_SXM"]
max_price   = "5.00"
min_disk_gb = 120
label       = "H200 SXM 141GB  (~$3.50/hr)"
image_type  = "builder"

[gpu_tiers.b200-sxm]
vast_names  = ["B200_SXM"]
max_price   = "8.00"
min_disk_gb = 200
label       = "B200 SXM 192GB  (~$5+/hr)"
image_type  = "builder"

# Blackwell datacenter 5090 variant (shows up under datacenter tab)
[gpu_tiers.5090-dc]
vast_names  = ["RTX_5090"]
max_price   = "0.60"
min_disk_gb = 60
label       = "RTX 5090 32GB  DC  (~$0.40/hr)"
image_type  = "prebuilt"
```

   Key change: `vast_names` is a list — allows multiple Vast gpu_name strings
   per tier (e.g. SXM5 and SXM are both H100). The offer search ORs them.

2. **Update `vast_up.sh` GPU filter generation**

   Replace the hardcoded `case "${GPU}"` for gpu_name with a helper that reads
   `VAST_NAMES` env var (set by manager from recipes.toml):

```bash
# Set by vast_manager.py from recipes.toml vast_names list
if [ -n "${VAST_NAMES:-}" ]; then
    # e.g. VAST_NAMES="H100_SXM H100_SXM5"
    # vastai DSL: gpu_name in [H100_SXM,H100_SXM5]
    NAMES_CSV=$(echo "${VAST_NAMES}" | tr ' ' ',')
    GPU_NAME="gpu_name in [${NAMES_CSV}]"   # used directly in search string
else
    # fallback: old behaviour for direct CLI use
    case "${GPU}" in
        5090|4090) GPU_NAME="gpu_name=RTX_${GPU}" ;;
        6000pro)   GPU_NAME="gpu_name in [RTX_PRO_6000_WS,RTX_PRO_6000_S]" ;;
        *)         GPU_NAME="gpu_name=RTX_${GPU}" ;;
    esac
fi
```

3. **Update `vast_manager.py` `browse_offers()`**

   Pass `vast_names` list from tier config instead of deriving gpu_filter
   from a hardcoded string. Manager already reads gpu_tiers from recipes.toml —
   just forward `vast_names` into the search.

4. **Add `cuda_vers` guard per tier**

   H100/A100 work fine with CUDA 12.8. B200 needs CUDA 12.9+. Add optional
   `min_cuda` field to gpu_tiers and use it in the offer search filter:

```toml
[gpu_tiers.b200-sxm]
min_cuda = "12.9"
```

**Deliverables:** Updated `recipes.toml`, `vast_up.sh`, `vast_manager.py`.  
**Effort:** ~2-3 hours. No new Docker image needed.  
**Risk:** Low — purely additive, old tiers unchanged.

---

### Phase 2 — SM arch detection + runtime-compiled image

**Files:** `Dockerfile.builder` (new), `launch.sh`, `vast_up.sh`

The builder image skips the pre-compiled binary. `launch.sh` detects the GPU's
SM arch via `nvidia-smi` at boot and compiles llama.cpp for the exact target.

1. **`Dockerfile.builder`** — stripped-down image, no llama-server binary:

```dockerfile
FROM nvidia/cuda:12.8.0-devel-ubuntu24.04
# CUDA dev tools, cmake, ninja, git, Python, hf-transfer
# NO llama.cpp clone or compile step
# launch.sh handles compile at boot
```

   For B200 (Blackwell 100 series, SM=100), need CUDA 12.9+ base image. 
   Add a second builder variant: `Dockerfile.builder-b200` with
   `FROM nvidia/cuda:12.9.0-devel-ubuntu24.04`.

2. **`launch.sh` — arch detection + conditional compile**:

```bash
if [ ! -f /usr/local/bin/llama-server ]; then
    log "No pre-built llama-server — detecting GPU arch..."
    SM=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
    # SM=89 (4090), SM=120 (5090), SM=90 (H100), SM=100 (B200), SM=80 (A100)
    log "Compiling llama.cpp for SM${SM}..."
    git clone --depth 1 https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp
    cmake -B /opt/llama.cpp/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${SM}-real" \
        /opt/llama.cpp
    cmake --build /opt/llama.cpp/build --target llama-server -j$(nproc)
    cp /opt/llama.cpp/build/bin/llama-server /usr/local/bin/
    log "Compile done."
fi
```

   Cold start time impact:
   - Prebuilt image (consumer): +0 min
   - Builder image (datacenter): +6-10 min compile on H100 (fast CPU + NVMe)

3. **`vast_up.sh` — image selection**:

```bash
IMAGE_TYPE="${IMAGE_TYPE:-prebuilt}"
case "${IMAGE_TYPE}" in
    prebuilt) DOCKER_IMAGE="${DOCKER_IMAGE:-ghcr.io/buckster123/vastai-gguf:prebuilt}" ;;
    builder)  DOCKER_IMAGE="${DOCKER_IMAGE:-ghcr.io/buckster123/vastai-gguf:builder}" ;;
esac
```

4. **GitHub Actions CI** — build and push both images on merge to main.
   Matrix: `[prebuilt, builder, builder-b200]`.

**Deliverables:** `Dockerfile.builder`, `Dockerfile.builder-b200`, updated
`launch.sh` and `vast_up.sh`, CI workflow.  
**Effort:** ~1 day.  
**Risk:** Medium — runtime compile adds complexity; compile failure = dead
instance. Mitigate: log everything, fail loudly, test on A100 first.

---

### Phase 3 — Multi-GPU VRAM recipes for datacenter cards

**Files:** `recipes.toml`

Datacenter cards open up configs the consumer cards can't touch:

```toml
# H100 SXM 80GB — Qwen3.6 35B MoE full quality, generous slots

[[recipes]]
name        = "qwen36-35b-moe-q8-h100"
label       = "Qwen3.6-35B-A3B MoE  Q8_K  4×256K"
gpu         = "h100-sxm"
model_repo  = "unsloth/Qwen3.6-35B-A3B-GGUF"
model_quant = "UD-Q8_K_XL"
ctx         = 1048576     # 4 × 256K
parallel    = 4
kv_type     = "q8_0"
image_type  = "builder"
description = "Max quality on H100 80GB. ~55GiB weights + KV. Fast decode ~180 t/s."

# H200 SXM 141GB — Qwen3 72B or similar large dense
[[recipes]]
name        = "qwen3-72b-q6-h200"
label       = "Qwen3-72B  Q6_K  2×128K"
gpu         = "h200-sxm"
model_repo  = "bartowski/Qwen3-72B-GGUF"
model_quant = "Q6_K_L"
ctx         = 262144
parallel    = 2
kv_type     = "q8_0"
image_type  = "builder"
description = "72B dense at Q6 on H200 141GB. ~55GB weights, fits with room."

# B200 SXM 192GB — multiple large models or massive context
[[recipes]]
name        = "qwen3-72b-q8-b200"
label       = "Qwen3-72B  Q8_K  4×256K  (beast)"
gpu         = "b200-sxm"
model_repo  = "bartowski/Qwen3-72B-GGUF"
model_quant = "Q8_0"
ctx         = 1048576
parallel    = 4
kv_type     = "q8_0"
image_type  = "builder"
description = "Full quality 72B dense on 192GB. ~72GB weights, 4 slots × 256K."
```

VRAM estimates for datacenter cards:
- H100 SXM 80GB: ~73 GB usable
- H200 SXM 141GB: ~135 GB usable  
- A100 SXM 80GB: ~77 GB usable
- B200 SXM 192GB: ~185 GB usable

**Effort:** Config only — just TOML editing.  
**Risk:** Low (if Phase 1+2 done). Pure data.

---

### Phase 4 — Manager UX polish for multi-tier

**Files:** `vast_manager.py`

1. **Offer browser enhancement** — show SM arch in offer table (query
   `cuda_max_good` as proxy, or `compute_cap` if vastai exposes it). Flag
   CUDA ≥ 13.0 hosts with a yellow warning in the table.

2. **Image type indicator in launch summary** — show whether it'll use
   prebuilt or builder image, and estimated cold start time:
   ```
   Image     ghcr.io/.../vastai-gguf:builder  (~8-12 min compile)
   ```

3. **"What fits here?" helper** — given a selected GPU tier, show a VRAM
   budget breakdown for every compatible recipe: weights + KV + overhead,
   flagging anything that's marginal (< 2GB headroom).

4. **Multi-GPU support in offer search** — some recipes will want 2× or 4×
   GPUs (e.g. 2× H100 for 160GB effective VRAM). Add optional `num_gpus`
   field to recipes, pass through to offer search filter.

**Effort:** ~4-6 hours.  
**Risk:** Low — pure UI.

---

## Implementation order

```
Phase 1  ←  do this next session (2-3 hrs, high value, no new infra)
  └─ recipes.toml: add h100/a100/h200/b200/5090-dc tiers + vast_names lists
  └─ vast_up.sh: VAST_NAMES-driven gpu_filter
  └─ vast_manager.py: forward vast_names from tier config to browse_offers()

Phase 2  ←  after Phase 1 is tested (1 day, need a datacenter instance to test)
  └─ Dockerfile.builder
  └─ launch.sh: SM detection + conditional compile
  └─ GitHub Actions CI: build matrix

Phase 3  ←  alongside Phase 2 (config only)
  └─ recipes.toml: datacenter recipes

Phase 4  ←  ongoing polish, low urgency
  └─ VRAM budget display
  └─ image type + cold start estimate in UI
  └─ multi-GPU recipe support
```

---

## Open questions

1. **Does vastai expose `compute_cap` in offer search JSON?** If yes, we can
   show actual SM arch in the offer table rather than inferring from cuda_max_good.
   Worth checking: `vastai search offers "..." --raw | jq '.[0] | keys'`

2. **B200 CUDA requirement** — Blackwell 100-series (SM=100) needs CUDA 12.9+
   and a patched llama.cpp. As of Apr 2026 this is bleeding edge. Verify
   llama.cpp SM100 support before adding B200 recipes.

3. **Compile cache** — for builder image, could mount a Vast volume as ccache
   directory so repeat spin-ups on the same host skip recompilation. Worth it
   only if renting the same host repeatedly. Probably over-engineering for now.

4. **`cpu_name` filter** — Poland was wonky partly due to host CPU quality.
   Vast exposes `cpu_name` in offer JSON. Could add optional `min_cpu_score`
   or a known-bad CPU blocklist. EPYC and Xeon Scalable are generally solid;
   some older Xeons or no-name chips are not.

5. **Vast datacenter vs consumer tab** — the manager currently doesn't
   distinguish. Vast's API uses `hosting_type` or similar. Worth checking if
   datacenter instances have different SLA or billing behaviour.

---

## Files changed summary

| Phase | File | Change |
|-------|------|--------|
| 1 | `recipes.toml` | Add 5+ GPU tiers, `vast_names` + `image_type` + `min_cuda` fields |
| 1 | `vast_up.sh` | `VAST_NAMES`-driven gpu_filter, `IMAGE_TYPE`-driven image selection |
| 1 | `vast_manager.py` | Forward `vast_names` to `browse_offers()`, show `image_type` in summary |
| 2 | `Dockerfile.builder` | New: CUDA dev toolchain only, no binary |
| 2 | `Dockerfile.builder-b200` | New: CUDA 12.9+ base |
| 2 | `launch.sh` | SM detection + conditional compile block |
| 2 | `.github/workflows/build.yml` | New: CI build matrix for all image variants |
| 3 | `recipes.toml` | Add H100/H200/A100/B200 model recipes |
| 4 | `vast_manager.py` | VRAM budget display, image type indicator, multi-GPU |
