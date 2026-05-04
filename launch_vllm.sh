#!/usr/bin/env bash
# Container entrypoint for vLLM-served models on Vast.ai.
# Designed for large MoE models (DeepSeek V4 Pro/Flash) that need
# tensor parallelism across multiple GPUs — where llama.cpp can't go.
#
# Uses the official vllm/vllm-openai image, which includes vLLM + all deps.
# The model is fetched from HuggingFace at boot (same as launch.sh).
#
# Required env:
#   MODEL_ID         HF model ID, e.g. deepseek-ai/DeepSeek-V4-Pro
#
# Optional env:
#   TP               tensor parallel size (default: auto-detect GPU count)
#   CTX              max model length / context (default: 131072)
#   QUANTIZATION     vLLM quantization method: fp8 | awq | gptq | None (default: None)
#   KV_CACHE_DTYPE   auto | fp8 | fp8_e5m2 | fp8_e4m3 (default: auto)
#   GPU_UTIL         GPU memory utilization 0.0-1.0 (default: 0.95)
#   EXTRA_ARGS       passthrough to vllm serve
#   HF_TOKEN         for gated repos
#   PORT, HOST       default 8000, 0.0.0.0
#   DTYPE            bfloat16 | float16 | auto (default: auto)
#   MAX_NUM_SEQS     max concurrent sequences (default: 64)
#   TRUST_REMOTE     trust remote code (default: true)
#   ENFORCE_EAGER    disable CUDA graphs, saves memory (default: false)
#   CHUNKED_PREFILL  enable chunked prefill for long prompts (default: true)
#   REASONING_PARSER deepseek_r1 for thinking models (default: empty)

set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

: "${MODEL_ID:?MODEL_ID required (e.g. deepseek-ai/DeepSeek-V4-Pro)}"

# ── defaults ──────────────────────────────────────────────────────────────────
CTX="${CTX:-131072}"
GPU_UTIL="${GPU_UTIL:-0.95}"
DTYPE="${DTYPE:-auto}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
TRUST_REMOTE="${TRUST_REMOTE:-true}"
ENFORCE_EAGER="${ENFORCE_EAGER:-false}"
CHUNKED_PREFILL="${CHUNKED_PREFILL:-true}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

# ── auto-detect tensor parallel size ──────────────────────────────────────────
if [ -z "${TP:-}" ]; then
    TP="$(nvidia-smi -L 2>/dev/null | wc -l)"
    [ "${TP}" -gt 0 ] || die "No GPUs detected — is the NVIDIA runtime available?"
fi
log "tensor parallel size: ${TP} GPUs"

# ── GPU info ──────────────────────────────────────────────────────────────────
log "==> GPU inventory:"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null || true
echo

# ── build vllm serve command ──────────────────────────────────────────────────
ARGS=(
    --model "${MODEL_ID}"
    --tensor-parallel-size "${TP}"
    --max-model-len "${CTX}"
    --gpu-memory-utilization "${GPU_UTIL}"
    --dtype "${DTYPE}"
    --max-num-seqs "${MAX_NUM_SEQS}"
    --host "${HOST}"
    --port "${PORT}"
)

# Trust remote code (required for DeepSeek, Qwen, etc.)
if [ "${TRUST_REMOTE}" = "true" ]; then
    ARGS+=(--trust-remote-code)
fi

# Quantization
if [ -n "${QUANTIZATION:-}" ] && [ "${QUANTIZATION}" != "None" ]; then
    ARGS+=(--quantization "${QUANTIZATION}")
    log "quantization: ${QUANTIZATION}"
fi

# KV cache dtype
if [ -n "${KV_CACHE_DTYPE:-}" ] && [ "${KV_CACHE_DTYPE}" != "auto" ]; then
    ARGS+=(--kv-cache-dtype "${KV_CACHE_DTYPE}")
    log "KV cache dtype: ${KV_CACHE_DTYPE}"
fi

# Enforce eager (disable CUDA graphs — saves memory, slightly slower)
if [ "${ENFORCE_EAGER}" = "true" ]; then
    ARGS+=(--enforce-eager)
    log "CUDA graphs disabled (enforce-eager mode)"
fi

# Chunked prefill (handles long prompts without OOM)
if [ "${CHUNKED_PREFILL}" = "true" ]; then
    ARGS+=(--enable-chunked-prefill)
fi

# Reasoning parser (for thinking/reasoning models)
if [ -n "${REASONING_PARSER:-}" ]; then
    ARGS+=(--enable-reasoning --reasoning-parser "${REASONING_PARSER}")
    log "reasoning parser: ${REASONING_PARSER}"
fi

# HF token
if [ -n "${HF_TOKEN:-}" ]; then
    export HF_TOKEN
fi

# ── environment tuning ────────────────────────────────────────────────────────
# FlashInfer is the best attention backend for MoE models
export VLLM_ATTENTION_BACKEND="${VLLM_ATTENTION_BACKEND:-FLASHINFER}"

# ── launch ────────────────────────────────────────────────────────────────────
log "==> launching vLLM"
log "    model   : ${MODEL_ID}"
log "    TP      : ${TP} GPUs"
log "    ctx     : ${CTX}"
log "    dtype   : ${DTYPE}"
log "    gpu_util: ${GPU_UTIL}"
log "    quant   : ${QUANTIZATION:-none}"
log "    listen  : ${HOST}:${PORT}"
log "    backend : ${VLLM_ATTENTION_BACKEND}"

exec vllm serve "${ARGS[@]}" ${EXTRA_ARGS}
