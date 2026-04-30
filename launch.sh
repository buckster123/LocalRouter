#!/usr/bin/env bash
# Qwen3.6 launcher for llama-server. Env-driven so the same image serves
# Qwen3.6-27B (dense) and Qwen3.6-35B-A3B (MoE) without a rebuild.
#
# Required env:
#   MODEL_REPO    HF repo,  e.g. unsloth/Qwen3.6-27B-GGUF
#   MODEL_QUANT   quant tag, e.g. UD-Q6_K_XL  (matches *${MODEL_QUANT}*.gguf)
#
# Optional env:
#   MMPROJ           if set (e.g. F16), also pulls *mmproj-${MMPROJ}*.gguf
#                    (vision support for Qwen3.6 — both 27B and 35B-A3B are VLMs)
#   CTX              default 65536 (64K). 5090/Q6 27B can do ~96K w/ q8 KV.
#   KV_TYPE          bf16 | q8_0 | q4_0   (default q8_0 — halves KV memory)
#   MODE             thinking | coding | nonthinking   (default thinking)
#                    Sets temp/top_p/min_p/presence_penalty per Unsloth recipe.
#   N_GPU_LAYERS     default 999 (everything on GPU). Set lower to spill to CPU.
#   PARALLEL         default 1. Concurrent decoder slots.
#   EXTRA_ARGS       passthrough flags to llama-server (e.g. --metrics)
#   HF_TOKEN         optional, for gated repos (Qwen3.6 unsloth GGUFs are public)
#   MODELS_DIR       default /workspace/models
#   PORT, HOST       default 8000, 0.0.0.0

set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

: "${MODEL_REPO:?MODEL_REPO env var required (e.g. unsloth/Qwen3.6-27B-GGUF)}"
: "${MODEL_QUANT:?MODEL_QUANT env var required (e.g. UD-Q6_K_XL)}"

CTX="${CTX:-65536}"
KV_TYPE="${KV_TYPE:-q8_0}"
MODE="${MODE:-thinking}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"
PARALLEL="${PARALLEL:-1}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
MODELS_DIR="${MODELS_DIR:-/workspace/models}"
PORT="${PORT:-8000}"
# Default to 127.0.0.1 — locked-down by design. Endpoint is reachable only
# via SSH tunnel (see qwen36-harness/tools/vast_tunnel.sh). Override with
# HOST=0.0.0.0 only if you understand you're exposing the GPU to the internet.
HOST="${HOST:-127.0.0.1}"

# Sampling presets from the Unsloth + Qwen3.6 model-card recipe.
case "${MODE}" in
    thinking)
        SAMPLE_ARGS="--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 1.5"
        TPL_KW=""
        ;;
    coding)
        SAMPLE_ARGS="--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0"
        TPL_KW=""
        ;;
    nonthinking)
        SAMPLE_ARGS="--temp 0.7 --top-p 0.80 --top-k 20 --min-p 0.0 --presence-penalty 1.5"
        TPL_KW='--chat-template-kwargs {"enable_thinking":false}'
        ;;
    *)
        die "MODE must be thinking|coding|nonthinking, got: ${MODE}"
        ;;
esac

mkdir -p "${MODELS_DIR}"
TARGET_DIR="${MODELS_DIR}/$(basename "${MODEL_REPO}")"

# ---- model fetch (idempotent) -------------------------------------------------
need_fetch=0
if [ ! -d "${TARGET_DIR}" ] || [ -z "$(ls -1 "${TARGET_DIR}" 2>/dev/null | grep -i "${MODEL_QUANT}" || true)" ]; then
    need_fetch=1
fi

if [ "${need_fetch}" = "1" ]; then
    log "fetching ${MODEL_REPO} (quant=${MODEL_QUANT})  ->  ${TARGET_DIR}"
    INCLUDE_ARGS=(--include "*${MODEL_QUANT}*.gguf")
    if [ -n "${MMPROJ:-}" ]; then
        log "  + mmproj-${MMPROJ} (vision)"
        INCLUDE_ARGS+=(--include "*mmproj-${MMPROJ}*.gguf")
    fi
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    hf download "${MODEL_REPO}" --local-dir "${TARGET_DIR}" "${INCLUDE_ARGS[@]}"
else
    log "model already present in ${TARGET_DIR}, skipping fetch"
fi

# ---- locate weights -----------------------------------------------------------
# UD quants for the bigger sizes are sharded; pass the FIRST shard, llama.cpp
# auto-discovers the rest.
MODEL_FILE="$(ls -1 "${TARGET_DIR}" | grep -iE "${MODEL_QUANT}.*\.gguf$" | grep -v 'mmproj' | sort | head -n1 || true)"
[ -n "${MODEL_FILE}" ] || die "no .gguf matching '${MODEL_QUANT}' in ${TARGET_DIR}"
MODEL_PATH="${TARGET_DIR}/${MODEL_FILE}"

MMPROJ_ARGS=""
if [ -n "${MMPROJ:-}" ]; then
    MMPROJ_FILE="$(ls -1 "${TARGET_DIR}" | grep -iE "mmproj-${MMPROJ}.*\.gguf$" | head -n1 || true)"
    [ -n "${MMPROJ_FILE}" ] || die "MMPROJ requested but no mmproj file found"
    MMPROJ_ARGS="--mmproj ${TARGET_DIR}/${MMPROJ_FILE}"
fi

# ---- launch -------------------------------------------------------------------
log "==> launching llama-server"
log "    model   : ${MODEL_PATH}"
log "    mmproj  : ${MMPROJ_ARGS:-<none>}"
log "    ctx     : ${CTX}    kv-cache: ${KV_TYPE}    n-gpu-layers: ${N_GPU_LAYERS}"
log "    mode    : ${MODE}   parallel: ${PARALLEL}"
log "    listen  : ${HOST}:${PORT}"
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader 2>/dev/null || true

# --jinja: use the GGUF's embedded chat template (required for Qwen3.6 tool use)
# --metrics: prometheus on /metrics
# Tool-call parsing for Qwen3.6 uses the qwen3_coder grammar/parser internally
# via the chat template, no separate flag needed in llama-server.
exec llama-server \
    --model "${MODEL_PATH}" \
    ${MMPROJ_ARGS} \
    --host "${HOST}" --port "${PORT}" \
    --ctx-size "${CTX}" \
    --cache-type-k "${KV_TYPE}" --cache-type-v "${KV_TYPE}" \
    --n-gpu-layers "${N_GPU_LAYERS}" \
    --parallel "${PARALLEL}" \
    --jinja \
    --metrics \
    --flash-attn on \
    ${SAMPLE_ARGS} ${TPL_KW} ${EXTRA_ARGS}
