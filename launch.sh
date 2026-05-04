#!/usr/bin/env bash
# Container entrypoint for vastai-gguf-launcher.
# Env-driven so the same image serves any GGUF model without rebuilding.
#
# Required env:
#   MODEL_REPO    HF repo,  e.g. unsloth/Qwen3.6-27B-GGUF
#   MODEL_QUANT   quant tag, e.g. UD-Q6_K_XL  (matches *${MODEL_QUANT}*.gguf)
#
# Optional env:
#   IMAGE_TYPE       prebuilt | builder  (default prebuilt)
#                    builder: compiles llama.cpp for the host GPU's exact SM arch
#   CTX              context tokens (default 65536)
#   KV_TYPE          bf16 | q8_0 | q4_0   (default q8_0)
#   MODE             thinking | coding | nonthinking   (default thinking)
#   N_GPU_LAYERS     default 999 (all on GPU)
#   PARALLEL         concurrent decode slots (default 1)
#   EXTRA_ARGS       passthrough to llama-server
#   HF_TOKEN         for gated repos
#   MODELS_DIR       default /workspace/models
#   MMPROJ           F16 to enable vision
#   PORT, HOST       default 8000, 127.0.0.1
#   LLAMA_CPP_REPO   custom llama.cpp fork (default: ggml-org/llama.cpp)
#   LLAMA_CPP_REF    branch/tag/commit to build from (default: master)
#                    Use for models needing unmerged PRs, e.g. DeepSeek V4

set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

: "${MODEL_REPO:?MODEL_REPO required (e.g. unsloth/Qwen3.6-27B-GGUF)}"
: "${MODEL_QUANT:?MODEL_QUANT required (e.g. UD-Q6_K_XL)}"

IMAGE_TYPE="${IMAGE_TYPE:-prebuilt}"
CTX="${CTX:-65536}"
KV_TYPE="${KV_TYPE:-q8_0}"
MODE="${MODE:-thinking}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"
PARALLEL="${PARALLEL:-1}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
MODELS_DIR="${MODELS_DIR:-/workspace/models}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# ── builder path: compile llama.cpp for exact SM arch ─────────────────────────
if [ "${IMAGE_TYPE}" = "builder" ] && [ ! -x /usr/local/bin/llama-server ]; then
    log "==> builder image: no pre-compiled llama-server — detecting GPU arch..."

    # Custom repo/branch support (for models needing unmerged PRs)
    LLAMA_CPP_REPO="${LLAMA_CPP_REPO:-ggml-org/llama.cpp}"
    LLAMA_CPP_REF="${LLAMA_CPP_REF:-master}"

    # Get compute capability from nvidia-smi, strip the dot: "9.0" → "90"
    RAW_CAP="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
    if [ -z "${RAW_CAP}" ]; then
        die "nvidia-smi returned no compute_cap — is the GPU visible?"
    fi
    SM="${RAW_CAP//./}"   # "9.0" → "90", "8.0" → "80", "10.0" → "100"
    log "    detected SM arch: ${SM}  (compute_cap ${RAW_CAP})"

    # SM100 (B200) needs a recent llama.cpp; warn if it might not be supported yet
    if [ "${SM}" = "100" ]; then
        log "    note: SM100 (Blackwell B200) support in llama.cpp is bleeding edge"
        log "    if compile fails, try pinning LLAMA_CPP_REF to a known-good commit"
    fi

    SRC_DIR="/opt/llama.cpp"
    BUILD_DIR="${SRC_DIR}/build"

    # Source may be cached in image — re-clone if repo/branch differs or missing
    if [ ! -d "${SRC_DIR}" ]; then
        log "    cloning https://github.com/${LLAMA_CPP_REPO}.git (ref: ${LLAMA_CPP_REF})..."
        git clone --depth 1 --branch "${LLAMA_CPP_REF}" \
            "https://github.com/${LLAMA_CPP_REPO}.git" "${SRC_DIR}"
    elif [ "${LLAMA_CPP_REPO}" != "ggml-org/llama.cpp" ] || [ "${LLAMA_CPP_REF}" != "master" ]; then
        # Custom repo/branch requested but image has default source — re-clone
        log "    custom repo/branch requested — re-cloning..."
        log "    repo: ${LLAMA_CPP_REPO}  ref: ${LLAMA_CPP_REF}"
        rm -rf "${SRC_DIR}"
        git clone --depth 1 --branch "${LLAMA_CPP_REF}" \
            "https://github.com/${LLAMA_CPP_REPO}.git" "${SRC_DIR}"
    fi

    log "    configuring for SM${SM}..."
    cmake -B "${BUILD_DIR}" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DGGML_NATIVE=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${SM}-real" \
        -DLLAMA_CURL=ON \
        -DBUILD_SHARED_LIBS=OFF \
        "${SRC_DIR}" 2>&1 | tail -5

    log "    compiling llama-server (this takes ~8-12 min on first boot)..."
    cmake --build "${BUILD_DIR}" --config Release \
        -j"$(nproc)" \
        --target llama-server llama-bench 2>&1 | grep -E '^\[|error:|warning:' | tail -20

    install -m755 "${BUILD_DIR}/bin/llama-server" /usr/local/bin/llama-server
    install -m755 "${BUILD_DIR}/bin/llama-bench"  /usr/local/bin/llama-bench 2>/dev/null || true
    log "    compile done — llama-server installed"
fi

# Sanity check
[ -x /usr/local/bin/llama-server ] || die "llama-server not found — check image or build log"

# ── sampling presets ───────────────────────────────────────────────────────────
case "${MODE}" in
    thinking)
        SAMPLE_ARGS="--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 1.5"
        TPL_KW=""
        TPL_KW_JSON=""
        ;;
    coding)
        SAMPLE_ARGS="--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0"
        TPL_KW=""
        TPL_KW_JSON=""
        ;;
    nonthinking)
        SAMPLE_ARGS="--temp 0.7 --top-p 0.80 --top-k 20 --min-p 0.0 --presence-penalty 1.5"
        TPL_KW='--chat-template-kwargs' 
TPL_KW_JSON='{"enable_thinking":false}'
        ;;
    *)
        die "MODE must be thinking|coding|nonthinking, got: ${MODE}"
        ;;
esac

# ── model fetch (idempotent) ───────────────────────────────────────────────────
mkdir -p "${MODELS_DIR}"
TARGET_DIR="${MODELS_DIR}/$(basename "${MODEL_REPO}")"

need_fetch=0
if [ ! -d "${TARGET_DIR}" ] || \
   [ -z "$(ls -1 "${TARGET_DIR}" 2>/dev/null | grep -i "${MODEL_QUANT}" || true)" ]; then
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

# ── locate weights ─────────────────────────────────────────────────────────
# Handles both single-file and split GGUFs.
# Split files: llama-server needs the first shard (e.g. -00001-of-00023.gguf)
# Some repos put shards in subdirectories (e.g. Q2_K/model-Q2_K.gguf-00001-of-N)
MODEL_FILE="$(find "${TARGET_DIR}" -maxdepth 2 -name "*.gguf" \
    | grep -iE "${MODEL_QUANT}" | grep -v 'mmproj' | sort | head -n1 || true)"
[ -n "${MODEL_FILE}" ] || die "no .gguf matching '${MODEL_QUANT}' in ${TARGET_DIR}"
MODEL_PATH="${MODEL_FILE}"   # find returns full path

MMPROJ_ARGS=""
if [ -n "${MMPROJ:-}" ]; then
    MMPROJ_FILE="$(find "${TARGET_DIR}" -maxdepth 2 -name "*.gguf" \
        | grep -iE "mmproj-${MMPROJ}" | head -n1 || true)"
    [ -n "${MMPROJ_FILE}" ] || die "MMPROJ requested but no mmproj file found"
    MMPROJ_ARGS="--mmproj ${MMPROJ_FILE}"
fi

# ── launch ─────────────────────────────────────────────────────────────────────
log "==> launching llama-server"
log "    model   : ${MODEL_PATH}"
log "    mmproj  : ${MMPROJ_ARGS:-<none>}"
log "    ctx     : ${CTX}    kv-cache: ${KV_TYPE}    n-gpu-layers: ${N_GPU_LAYERS}"
log "    mode    : ${MODE}   parallel: ${PARALLEL}"
log "    listen  : ${HOST}:${PORT}"
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader 2>/dev/null || true

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
    --flash-attn \
    ${SAMPLE_ARGS} ${TPL_KW} ${TPL_KW_JSON:+"$TPL_KW_JSON"} ${EXTRA_ARGS}
