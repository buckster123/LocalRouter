#!/usr/bin/env bash
# Spin up a Vast.ai instance running ghcr.io/buckster123/qwen36-llamacpp:latest
# Picks the cheapest reliable RTX_5090 (or RTX_4090 if GPU=4090) by default.
#
# Usage:
#   ./vast_up.sh                                      # 5090, Qwen3.6-27B Q6
#   GPU=4090 ./vast_up.sh                             # 4090, Qwen3.6-27B Q4
#   MODEL=moe ./vast_up.sh                            # 5090, Qwen3.6-35B-A3B Q5 128K
#   MODEL=moe-256k ./vast_up.sh                       # 5090, Q5 256K q4 KV (tight)
#   MODEL=moe-beast GPU=6000pro ./vast_up.sh          # 6000 Pro, Q8 256K q8 KV × 6 slots
#   MODEL=moe MODEL_QUANT=UD-Q6_K_XL ./vast_up.sh
#
# Env you can override:
#   GPU            5090 | 4090 | 6000pro             default 5090
#   MODEL          dense | moe | moe-256k | moe-beast  default dense
#   MODEL_REPO     HF repo                            default depends on MODEL+GPU
#   MODEL_QUANT    quant tag                          default depends on MODEL+GPU
#   CTX            context length                     default depends on MODEL+GPU
#   KV_TYPE        q8_0|bf16|q4_0                     default q8_0
#   PARALLEL       concurrent decoder slots           default 1 (beast=6)
#   MODE           thinking|coding|nonthinking        default thinking
#   MMPROJ         F16 to enable vision               default unset (text-only, saves ~2GB)
#   DOCKER_IMAGE   override image
#   MIN_DISK_GB    container disk                     default 60 (weights + cache)
#   MAX_PRICE      $/hr ceiling                       default 0.50 (6000pro=1.50)

set -euo pipefail

GPU="${GPU:-5090}"
MODEL="${MODEL:-dense}"
KV_TYPE="${KV_TYPE:-q8_0}"
MODE="${MODE:-thinking}"
MIN_DISK_GB="${MIN_DISK_GB:-60}"
PARALLEL="${PARALLEL:-}"
# 6000 Pro hosts are pricier ($0.45-$2.33/hr range, median ~$0.93). Bump ceiling.
if [ "${GPU}" = "6000pro" ]; then
    MAX_PRICE="${MAX_PRICE:-1.50}"
    # beast recipe needs more disk: ~40 GB weights (Q8_0) + cache
    MIN_DISK_GB="${MIN_DISK_GB:-80}"
else
    MAX_PRICE="${MAX_PRICE:-0.50}"
fi
DOCKER_IMAGE="${DOCKER_IMAGE:-ghcr.io/buckster123/qwen36-llamacpp:latest}"

# ---- defaults per (model, GPU) combo -----------------------------------------
case "${MODEL}_${GPU}" in
    dense_5090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q6_K_XL}"
        CTX="${CTX:-98304}"          # 96K
        PARALLEL="${PARALLEL:-1}"
        ;;
    dense_4090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q4_K_XL}"
        CTX="${CTX:-65536}"          # 64K
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe_5090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q5_K_XL}"
        CTX="${CTX:-131072}"         # 128K — MoE has way more KV headroom on 32GB
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe-256k_5090)
        # 5090 squeeze recipe: 256K ctx on 32GB by dropping KV quant to q4_0.
        # Hybrid-linear architecture keeps KV tiny — only 10 of 40 layers are full-attn.
        # ~27 GiB total (Q5 weights + q4 KV), tight but feasible for solo-slot use.
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q5_K_XL}"
        CTX="${CTX:-262144}"         # 256K
        KV_TYPE="${KV_TYPE:-q4_0}"   # override default q8_0 to q4_0
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe_4090)
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q4_K_XL}"
        CTX="${CTX:-49152}"          # 48K
        PARALLEL="${PARALLEL:-1}"
        ;;
    moe-beast_6000pro)
        # THE BEAST: RTX PRO 6000 Blackwell 96GB, Config "G" from sizing analysis.
        # UD-Q8_K_XL weights (~38 GiB) + q8 KV @ 6 slots × 256K (~16 GiB) + compute (~4 GiB)
        # = ~58 GiB used, ~34 GiB headroom. Main + 5 subagents all at 256K, max quality.
        # Aggregate throughput ~420-480 t/s across 6 streams.
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q8_K_XL}"
        # Total KV pool = ctx. Each of 6 slots gets ctx/parallel = 256K tokens.
        CTX="${CTX:-1572864}"        # 1.5M total pool = 6 × 256K
        KV_TYPE="${KV_TYPE:-q8_0}"
        PARALLEL="${PARALLEL:-6}"
        ;;
    moe_6000pro)
        # Conservative 6000 Pro: Q6 weights, 4 slots × 256K, q8 KV. Safer, faster per-stream.
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q6_K_XL}"
        CTX="${CTX:-1048576}"        # 1M total pool = 4 × 256K
        PARALLEL="${PARALLEL:-4}"
        ;;
    dense_6000pro)
        # Dense 27B at full quality on 6000 Pro: UD-Q8_K_XL weights, bigger slots possible
        # (dense has MUCH bigger KV/token — NOT hybrid linear — budget carefully).
        MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-27B-GGUF}"
        MODEL_QUANT="${MODEL_QUANT:-UD-Q8_K_XL}"
        CTX="${CTX:-524288}"         # 512K total pool = 4 × 128K (dense KV is fat)
        PARALLEL="${PARALLEL:-4}"
        ;;
    *)
        echo "Unknown MODEL=${MODEL} GPU=${GPU}" >&2
        echo "  valid GPU: 5090 | 4090 | 6000pro" >&2
        echo "  valid MODEL: dense | moe | moe-256k | moe-beast" >&2
        echo "  valid combos:" >&2
        echo "    dense_5090  dense_4090  dense_6000pro" >&2
        echo "    moe_5090    moe_4090    moe_6000pro" >&2
        echo "    moe-256k_5090  moe-beast_6000pro" >&2
        exit 2
        ;;
esac

# vast uses "RTX_5090", "RTX_4090", "RTX_PRO_6000" as gpu_name strings
case "${GPU}" in
    5090|4090) GPU_NAME="RTX_${GPU}" ;;
    6000pro)   GPU_NAME="RTX_PRO_6000" ;;
    *)         GPU_NAME="RTX_${GPU}" ;;
esac

# Geo filter: vast doesn't have a `region` field but `geolocation` is a string
# like "Sweden, SE". We post-filter via jq on the country code suffix.
# GEO env can be:  EU | EU_NORDIC | US | ANY      (default EU_NORDIC)
GEO="${GEO:-EU_NORDIC}"
case "${GEO}" in
    EU_NORDIC) GEO_RE='SE|NO|FI|DK|IS' ;;
    EU)        GEO_RE='SE|NO|FI|DK|IS|DE|NL|FR|BE|UK|IE|EE|LV|LT|PL|CZ|AT|CH|ES|PT|IT' ;;
    US)        GEO_RE='US' ;;
    ANY)       GEO_RE='.*' ;;
    *)         GEO_RE="${GEO}" ;;  # raw regex passthrough
esac

# Allow overriding the offer entirely (e.g. OFFER_ID=30940768 ./vast_up.sh)
if [ -n "${OFFER_ID:-}" ]; then
    echo "==> using user-supplied OFFER_ID=${OFFER_ID}"
else
    echo "==> finding cheapest ${GPU_NAME} (geo=${GEO}, rel>0.99, ↓>500 Mbps, max \$${MAX_PRICE}/hr)..."
    OFFER_ID="$(vastai search offers \
        "gpu_name=${GPU_NAME} num_gpus=1 reliability>0.99 inet_down>500 \
         dph_total<${MAX_PRICE} disk_space>${MIN_DISK_GB} cuda_vers>=12.8 \
         rentable=true" \
        --order 'dph_total' --raw 2>/dev/null \
        | jq -r --arg re "${GEO_RE}" '[.[] | select((.geolocation // "") | test(", (" + $re + ")$"))] | .[0].id // empty')"

    if [ -z "${OFFER_ID}" ]; then
        echo "no offers matched geo=${GEO}; widening to EU..." >&2
        OFFER_ID="$(vastai search offers \
            "gpu_name=${GPU_NAME} num_gpus=1 reliability>0.97 inet_down>300 \
             dph_total<${MAX_PRICE} disk_space>${MIN_DISK_GB} rentable=true" \
            --order 'dph_total' --raw 2>/dev/null \
            | jq -r '[.[] | select((.geolocation // "") | test(", (SE|NO|FI|DK|IS|DE|NL|FR|BE|UK|IE|EE|LV|LT|PL|CZ|AT|CH|ES|PT|IT)$"))] | .[0].id // empty')"
    fi
fi
[ -n "${OFFER_ID}" ] || { echo "FATAL: no matching offers"; exit 1; }

echo "    selected offer ${OFFER_ID}"
vastai search offers \
    "gpu_name=${GPU_NAME} num_gpus=1 reliability>0.97 rentable=true" \
    --raw 2>/dev/null \
    | jq -r --arg id "${OFFER_ID}" '.[] | select((.id|tostring) == $id) | "    $\(.dph_total)/hr  rel=\(.reliability2)  vram=\(.gpu_ram/1024 | floor)GB  ↓\(.inet_down|floor)Mbps  cuda=\(.cuda_max_good)  geo=\(.geolocation)"' \
    || true

# Build env string for the container. NB: vast SSH-mode hijacks ENTRYPOINT, so
# duplicate the env into --onstart-cmd which IS process-env at launch time.
HF_TOKEN_VAL="$(cat ~/.cache/huggingface/token 2>/dev/null || echo "")"

ENV_ARGS=(
    -e "MODEL_REPO=${MODEL_REPO}"
    -e "MODEL_QUANT=${MODEL_QUANT}"
    -e "CTX=${CTX}"
    -e "KV_TYPE=${KV_TYPE}"
    -e "MODE=${MODE}"
    -e "PARALLEL=${PARALLEL}"
    -e "HOST=127.0.0.1"
    -p "8000:8000"
)
[ -n "${MMPROJ:-}" ]      && ENV_ARGS+=(-e "MMPROJ=${MMPROJ}")
[ -n "${HF_TOKEN_VAL}" ]  && ENV_ARGS+=(-e "HF_TOKEN=${HF_TOKEN_VAL}")

ONSTART_CMD="MODEL_REPO=${MODEL_REPO} MODEL_QUANT=${MODEL_QUANT} CTX=${CTX} KV_TYPE=${KV_TYPE} MODE=${MODE} PARALLEL=${PARALLEL} HOST=127.0.0.1"
[ -n "${MMPROJ:-}" ]     && ONSTART_CMD="${ONSTART_CMD} MMPROJ=${MMPROJ}"
[ -n "${HF_TOKEN_VAL}" ] && ONSTART_CMD="${ONSTART_CMD} HF_TOKEN=${HF_TOKEN_VAL}"
ONSTART_CMD="${ONSTART_CMD} bash /app/launch.sh > /var/log/launch.log 2>&1 &"

echo "==> creating instance..."
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
    echo "    poll with:    vastai show instance ${INST_ID}"
    echo "    ssh shortly:  vastai ssh-url ${INST_ID}"
    echo "    tear down:    ./vast_down.sh"
fi
