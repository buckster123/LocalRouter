#!/usr/bin/env bash
# Smoke test for any OpenAI-compatible endpoint (Vast GGUF or managed provider).
#
# Usage:
#   ./smoke.sh http://HOST:PORT                # localhost / tunnel endpoint
#   ./smoke.sh --provider together              # active Together endpoint from config
#   ./smoke.sh https://api.example.com/v1 -k sk-...  # any URL with explicit key
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ACTIVE_EP="$ROOT/.active_endpoint"
PROVIDER_DIR="${HOME}/.vastai-gguf"
CONFIG_TOML="$PROVIDER_DIR/config.toml"

# Parse args
BASE=""
API_KEY=""
USE_ACTIVE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)
            USE_ACTIVE=1
            shift ;;
        -k|--key)
            API_KEY="$2"; shift 2 ;;
        *)
            BASE="${BASE:-$1}"; shift ;;
    esac
done

# Resolve active endpoint if requested
if [[ "$USE_ACTIVE" -eq 1 ]]; then
    if [[ ! -f "$ACTIVE_EP" ]]; then
        echo "No active endpoint. Run 'Launch → Together AI' first."
        exit 2
    fi
    EP_JSON=$(cat "$ACTIVE_EP")
    PROVIDER=$(echo "$EP_JSON" | jq -r '.provider // empty')

    if [[ "$PROVIDER" == "together" ]]; then
        BASE=$(echo "$EP_JSON" | jq -r '.endpoint // empty' | sed 's|/chat/completions$||')
        # Get API key from config if not provided
        if [[ -z "$API_KEY" ]]; then
            # Parse TOML for together api_key (simple grep approach)
            if [[ -f "$CONFIG_TOML" ]]; then
                in_together=0
                while IFS= read -r line; do
                    if [[ "$line" == "[providers.together]"* ]]; then
                        in_together=1; continue
                    elif [[ "$line" =~ ^\[.*\] ]] && [[ $in_together -eq 1 ]]; then
                        break
                    fi
                    if [[ $in_together -eq 1 ]]; then
                        key_match=$(echo "$line" | grep -oP 'api_key\s*=\s*"\K[^"]+' || true)
                        if [[ -n "$key_match" ]]; then
                            API_KEY="$key_match"; break
                        fi
                    fi
                done < "$CONFIG_TOML"
            fi
        fi
        if [[ -z "$API_KEY" ]]; then
            API_KEY="${TOGETHER_API_KEY:-}"
        fi
    fi

    if [[ -z "$BASE" ]]; then
        echo "Could not resolve active endpoint base URL."
        exit 2
    fi
fi

[ -n "${BASE}" ] || { echo "usage: $0 http://HOST:PORT  (no /v1 suffix)"; exit 2; }
URL="${BASE%/}/v1"

hr() { printf '\n=== %s ===\n' "$*"; }

# Build auth header if API key is set
AUTH_HEADER=""
if [ -n "${API_KEY}" ]; then
    AUTH_HEADER="-H 'Authorization: Bearer ${API_KEY}'"
fi

hr "endpoint info"
echo "  Base URL : $URL"
if [[ "$BASE" == *"api.together"* ]]; then
    echo "  Provider : Together AI (managed)"
else
    echo "  Provider : Self-hosted (tunnel/localhost)"
fi

hr "models"
eval curl -fsS "${AUTH_HEADER}" "${URL}/models" | jq '.data // . | type | if . == "array" then .[0:3] else .data[0:3] end' 2>/dev/null || \
     echo "(models endpoint not available)"

hr "warm-up: short completion"
time curl -fsS "${AUTH_HEADER}" "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"In one sentence, what is a hash table?"}],
  "max_tokens":80
}' | jq -r '.choices[0].message.content // empty'

hr "tool calling: get_weather"
curl -fsS "${AUTH_HEADER}" "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"What is the weather in Reykjavik right now? Use the tool."}],
  "tools":[{"type":"function","function":{"name":"get_weather","description":"Get current weather for a city.","parameters":{"type":"object","properties":{"city":{"type":"string","description":"City name"},"unit":{"type":"string","enum":["c","f"],"description":"Temp unit"}},"required":["city"]}}}],
  "tool_choice":"auto",
  "max_tokens":256
}' | jq '.choices[0].message | {content, tool_calls}' || echo "(tool calling not supported or timed out)"

hr "throughput: 200-token sustained generation"
curl -fsS "${AUTH_HEADER}" "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"Write a 200-word explanation of how a B-tree differs from a hash table for database indexing."}],
  "max_tokens":300,
  "stream":false
}' | jq '{
    completion_tokens: .usage.completion_tokens,
    prompt_tokens: .usage.prompt_tokens,
    model: .model
}' || echo "(throughput test timed out)"

hr "DONE"