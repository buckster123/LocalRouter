#!/usr/bin/env bash
# Spin up a Vast.ai instance for vastai-gguf-launcher.
# Driven entirely by env vars — vast_manager.py sets these from recipes.toml.
# Can also be called directly; see usage below.
#
# Usage (direct):
#   ./vast_up.sh                                    # 5090, Qwen3.6-27B Q6, 96K
#   GPU=4090 MODEL=moe ./vast_up.sh                 # 4090, MoE Q4
#   GPU=h100-sxm MODEL_REPO=... MODEL_QUANT=... CTX=... ./vast_up.sh
#
# Key env vars (manager sets all of these from recipes.toml):
#   GPU            tier key: 5090 | 4090 | 6000pro | h100-sxm | a100-sxm | etc.
#   VAST_NAMES     space-separated Vast gpu_name strings (overrides GPU lookup)
#   MODEL          named preset: dense | moe | moe-256k | moe-beast (direct use only)
#   MODEL_REPO     HF repo  (required if MODEL not a known preset)
#   MODEL_QUANT    quant tag (required if MODEL not a known preset)
#   CTX            context tokens (required if MODEL not a known preset)
#   PARALLEL       decode slots (default 1)
#   KV_TYPE        q8_0 | q4_0 | bf16 (default q8_0)
#   MODE           thinking | coding | nonthinking (default thinking)
#   IMAGE_TYPE     prebuilt | builder (default prebuilt)
#   DOCKER_IMAGE   explicit image override (skips IMAGE_TYPE logic)
#   GEO            EU_NORDIC | EU | US | ANY | raw-regex (default EU_NORDIC)
#   MIN_CUDA       minimum cuda_vers filter (default 12.8)
#   NUM_GPUS       GPUs per instance (default 1)
#   MAX_PRICE      $/hr ceiling
#   MIN_DISK_GB    disk space floor (default 60)
#   OFFER_ID       pin a specific offer (skips search)
#   MMPROJ         F16 to enable vision

set -euo pipefail

GPU="${GPU:-5090}"
MODEL="${MODEL:-dense}"
KV_TYPE="${KV_TYPE:-q8_0}"
MODE="${MODE:-thinking}"
MIN_DISK_GB="${MIN_DISK_GB:-}"  # set per-GPU below if not overridden
PARALLEL="${PARALLEL:-}"
IMAGE_TYPE="${IMAGE_TYPE:-prebuilt}"
MIN_CUDA="${MIN_CUDA:-12.8}"
NUM_GPUS="${NUM_GPUS:-1}"

# ── image selection ────────────────────────────────────────────────────────────
if [ -z "${DOCKER_IMAGE:-}" ]; then
    case "${IMAGE_TYPE}" in
        builder)  DOCKER_IMAGE="ghcr.io/buckster123/vastai-gguf:builder" ;;
        prebuilt) DOCKER_IMAGE="ghcr.io/buckster123/vastai-gguf:prebuilt" ;;
        *)        DOCKER_IMAGE="ghcr.io/buckster123/vastai-gguf:prebuilt" ;;
    esac
fi

# ── price ceiling defaults ─────────────────────────────────────────────────────
if [ -z "${MAX_PRICE:-}" ]; then
    case "${GPU}" in
        6000pro)           MAX_PRICE="1.60" ; MIN_DISK_GB="${MIN_DISK_GB:-80}" ;;
        h100-sxm|h100-pcie) MAX_PRICE="3.50" ; MIN_DISK_GB="${MIN_DISK_GB:-100}" ;;
        a100-sxm|a100-pcie) MAX_PRICE="2.00" ; MIN_DISK_GB="${MIN_DISK_GB:-80}" ;;
        h200-sxm)          MAX_PRICE="5.50" ; MIN_DISK_GB="${MIN_DISK_GB:-150}" ;;
        b200-sxm)          MAX_PRICE="9.00" ; MIN_DISK_GB="${MIN_DISK_GB:-200}" ;;
        *)                 MAX_PRICE="0.55" ;;
    esac
fi

# Global disk default — applies if neither env nor GPU tier set it
MIN_DISK_GB="${MIN_DISK_GB:-60}"

# ── model defaults (for direct invocation with named presets) ──────────────────
# Manager always passes MODEL_REPO/MODEL_QUANT/CTX explicitly so this block
# is a fallback for direct CLI use only.
case "${MODEL}_${GPU}" in
    dense_5090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q6_K_XL}"
        CTX="${CTX:-98304}"
        PARALLEL="${PARALLEL:-1}"
        ;;
    dense_4090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q4_K_XL}"
        CTX="${CTX:-65536}"
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe_5090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q5_K_XL}"
        CTX="${CTX:-131072}"
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe-256k_5090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q5_K_XL}"
        CTX="${CTX:-262144}"
        KV_TYPE="${KV_TYPE:-q4_0}"
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe_4090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q4_K_XL}"
        CTX="${CTX:-49152}"
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe-beast_6000pro)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q8_K_XL}"
        CTX="${CTX:-1572864}"
        KV_TYPE="${KV_TYPE:-q8_0}"
        PARALLEL="${PARALLEL:-6}"
        MIN_DISK_GB="${MIN_DISK_GB:-80}"
        ;;
    moe_6000pro)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q6_K_XL}"
        CTX="${CTX:-1048576}"
        PARALLEL="${PARALLEL:-4}"
        ;;
    dense_6000pro)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q8_K_XL}"
        CTX="${CTX:-524288}"
        PARALLEL="${PARALLEL:-4}"
        ;;
    *)
        # Manager path: MODEL_REPO/MODEL_QUANT/CTX set explicitly — just validate.
        if [ -z "${MODEL_REPO:-}" ] || [ -z "${MODEL_QUANT:-}" ] || [ -z "${CTX:-}" ]; then
            echo "ERROR: MODEL=${MODEL} GPU=${GPU} is not a known preset." >&2
            echo "  Set MODEL_REPO + MODEL_QUANT + CTX explicitly, or use a named preset." >&2
            exit 2
        fi
        PARALLEL="${PARALLEL:-1}"
        ;;
esac

# Global KV_TYPE default — applies if neither env nor preset set it
KV_TYPE="${KV_TYPE:-q8_0}"

# ── GPU name filter for offer search ──────────────────────────────────────────
# VAST_NAMES env (space-separated) takes priority — set by manager from recipes.toml.
# Falls back to deriving from GPU tier name for direct CLI use.
if [ -n "${VAST_NAMES:-}" ]; then
    # Convert "H100_SXM H100_SXM5" → "gpu_name in [H100_SXM,H100_SXM5]"
    NAMES_CSV=$(echo "${VAST_NAMES}" | tr ' ' ',')
    if echo "${NAMES_CSV}" | grep -q ','; then
        GPU_FILTER="gpu_name in [${NAMES_CSV}]"
    else
        GPU_FILTER="gpu_name=${NAMES_CSV}"
    fi
else
    case "${GPU}" in
        5090|5090-dc) GPU_FILTER="gpu_name=RTX_5090" ;;
        4090)         GPU_FILTER="gpu_name=RTX_4090" ;;
        6000pro)      GPU_FILTER="gpu_name in [RTX_PRO_6000_WS,RTX_PRO_6000_S]" ;;
        h100-sxm)     GPU_FILTER="gpu_name in [H100_SXM,H100_SXM5,H100X]" ;;
        h100-pcie)    GPU_FILTER="gpu_name=H100_PCIE" ;;
        a100-sxm)     GPU_FILTER="gpu_name in [A100_SXM4_80GB,A100_SXM,A100X]" ;;
        a100-pcie)    GPU_FILTER="gpu_name in [A100_PCIE,A100_PCIE_80GB]" ;;
        h200-sxm)     GPU_FILTER="gpu_name in [H200_SXM,H200]" ;;
        b200-sxm)     GPU_FILTER="gpu_name in [B200_SXM,B200]" ;;
        *)            GPU_FILTER="gpu_name=RTX_${GPU}" ;;
    esac
fi

# ── geo filter ─────────────────────────────────────────────────────────────────
GEO="${GEO:-EU_NORDIC}"
case "${GEO}" in
    EU_NORDIC) GEO_RE='SE|NO|FI|DK|IS' ;;
    EU)        GEO_RE='SE|NO|FI|DK|IS|DE|NL|FR|BE|UK|IE|EE|LV|LT|PL|CZ|AT|CH|ES|PT|IT' ;;
    US)        GEO_RE='US' ;;
    ANY)       GEO_RE='.*' ;;
    *)         GEO_RE="${GEO}" ;;
esac

# ── offer search ───────────────────────────────────────────────────────────────
if [ -n "${OFFER_ID:-}" ]; then
    echo "==> using user-supplied OFFER_ID=${OFFER_ID}"
else
    echo "==> searching: ${GPU_FILTER}  num_gpus=${NUM_GPUS}  geo=${GEO}  cuda>=${MIN_CUDA}  max \$${MAX_PRICE}/hr ..."
    SEARCH_FILTER="${GPU_FILTER} num_gpus=${NUM_GPUS} reliability>0.99 inet_down>500 dph_total<${MAX_PRICE} disk_space>${MIN_DISK_GB} cuda_vers>=${MIN_CUDA} rentable=true"
    OFFER_ID="$(vastai search offers "${SEARCH_FILTER}" \
        --order 'dph_total' --raw 2>/dev/null \
        | jq -r --arg re "${GEO_RE}" \
            '[.[] | select((.geolocation // "") | test(", (" + $re + ")$"))] | .[0].id // empty')"

    if [ -z "${OFFER_ID}" ]; then
        echo "  no offers in ${GEO}; widening search (lower thresholds, any geo)..." >&2
        SEARCH_FILTER_WIDE="${GPU_FILTER} num_gpus=${NUM_GPUS} reliability>0.97 inet_down>300 dph_total<${MAX_PRICE} disk_space>${MIN_DISK_GB} cuda_vers>=${MIN_CUDA} rentable=true"
        OFFER_ID="$(vastai search offers "${SEARCH_FILTER_WIDE}" \
            --order 'dph_total' --raw 2>/dev/null \
            | jq -r '.[0].id // empty')"
    fi
fi
[ -n "${OFFER_ID}" ] || { echo "FATAL: no matching offers found"; exit 1; }

echo "    selected offer ${OFFER_ID}"
# Print offer summary (best-effort — may not find it in a fresh search)
vastai search offers "${GPU_FILTER} num_gpus=${NUM_GPUS} reliability>0.90 rentable=true" \
    --raw 2>/dev/null \
    | jq -r --arg id "${OFFER_ID}" \
        '.[] | select((.id|tostring) == $id)
         | "    $\(.dph_total)/hr  rel=\(.reliability2)  \(.gpu_name)  \(.gpu_ram/1024|floor)GB VRAM  ↓\(.inet_down|floor)Mbps  cuda=\(.cuda_max_good)  \(.geolocation)"' \
    || true

# ── build container env ────────────────────────────────────────────────────────
HF_TOKEN_VAL="$(cat ~/.cache/huggingface/token 2>/dev/null || echo "")"

ENV_ARGS=(
    -e "MODEL_REPO=${MODEL_REPO}"
    -e "MODEL_QUANT=${MODEL_QUANT}"
    -e "CTX=${CTX}"
    -e "KV_TYPE=${KV_TYPE}"
    -e "MODE=${MODE}"
    -e "PARALLEL=${PARALLEL}"
    -e "HOST=127.0.0.1"
    -e "IMAGE_TYPE=${IMAGE_TYPE}"
    -p "8000:8000"
)
[ -n "${MMPROJ:-}"        ] && ENV_ARGS+=(-e "MMPROJ=${MMPROJ}")
[ -n "${HF_TOKEN_VAL}"    ] && ENV_ARGS+=(-e "HF_TOKEN=${HF_TOKEN_VAL}")
[ -n "${LLAMA_CPP_REPO:-}" ] && ENV_ARGS+=(-e "LLAMA_CPP_REPO=${LLAMA_CPP_REPO}")
[ -n "${LLAMA_CPP_REF:-}"  ] && ENV_ARGS+=(-e "LLAMA_CPP_REF=${LLAMA_CPP_REF}")

ONSTART_CMD="MODEL_REPO=${MODEL_REPO} MODEL_QUANT=${MODEL_QUANT} CTX=${CTX} KV_TYPE=${KV_TYPE} MODE=${MODE} PARALLEL=${PARALLEL} HOST=127.0.0.1 IMAGE_TYPE=${IMAGE_TYPE}"
[ -n "${MMPROJ:-}"         ] && ONSTART_CMD="${ONSTART_CMD} MMPROJ=${MMPROJ}"
[ -n "${HF_TOKEN_VAL}"     ] && ONSTART_CMD="${ONSTART_CMD} HF_TOKEN=${HF_TOKEN_VAL}"
[ -n "${LLAMA_CPP_REPO:-}" ] && ONSTART_CMD="${ONSTART_CMD} LLAMA_CPP_REPO=${LLAMA_CPP_REPO}"
[ -n "${LLAMA_CPP_REF:-}"  ] && ONSTART_CMD="${ONSTART_CMD} LLAMA_CPP_REF=${LLAMA_CPP_REF}"
ONSTART_CMD="${ONSTART_CMD} bash /app/launch.sh > /var/log/launch.log 2>&1 &"

echo "==> creating instance..."
echo "    image: ${DOCKER_IMAGE}  (${IMAGE_TYPE})"
RESULT="$(vastai create instance "${OFFER_ID}" \
    --image "${DOCKER_IMAGE}" \
    --disk "${MIN_DISK_GB}" \
    --env "${ENV_ARGS[*]}" \
    --onstart-cmd "${ONSTART_CMD}" \
    --raw 2>&1)"
echo "${RESULT}"

INST_ID="$(echo "${RESULT}" | jq -r '.new_contract // empty' 2>/dev/null || true)"
if [ -n "${INST_ID}" ]; then
    echo "${INST_ID}" > .last_instance
    echo
    echo "==> instance ${INST_ID} created.  saved to .last_instance"
    echo "    image type : ${IMAGE_TYPE}"
    if [ "${IMAGE_TYPE}" = "builder" ]; then
        echo "    note       : builder image compiles llama.cpp at boot — add ~8-12 min to cold start"
    fi
    echo "    poll with  : vastai show instance ${INST_ID}"
    echo "    tunnel     : ./tools/vast_tunnel.sh up"
    echo "    tear down  : ./vast_down.sh"
fi
