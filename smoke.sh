#!/usr/bin/env bash
# Smoke test for a running Qwen3.6 endpoint. Pass the base URL as arg or set BASE.
#   ./smoke.sh http://HOST:PORT
set -euo pipefail
BASE="${1:-${BASE:-}}"
[ -n "${BASE}" ] || { echo "usage: $0 http://HOST:PORT  (no /v1 suffix)"; exit 2; }
URL="${BASE%/}/v1"

hr() { printf '\n=== %s ===\n' "$*"; }

hr "models"
curl -fsS "${URL}/models" | jq '.data[] | {id, owned_by}'

hr "warm-up: short completion"
time curl -fsS "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"In one sentence, what is a hash table?"}],
  "max_tokens":80
}' | jq -r '.choices[0].message.content'

hr "tool calling: get_weather"
curl -fsS "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"What is the weather in Reykjavik right now? Use the tool."}],
  "tools":[{
    "type":"function",
    "function":{
      "name":"get_weather",
      "description":"Get current weather for a city.",
      "parameters":{
        "type":"object",
        "properties":{
          "city":{"type":"string","description":"City name"},
          "unit":{"type":"string","enum":["c","f"],"description":"Temp unit"}
        },
        "required":["city"]
      }
    }
  }],
  "tool_choice":"auto",
  "max_tokens":256
}' | jq '.choices[0].message | {content, tool_calls}'

hr "throughput: 200-token sustained generation"
curl -fsS "${URL}/chat/completions" -H 'content-type: application/json' -d '{
  "model":"x",
  "messages":[{"role":"user","content":"Write a 200-word explanation of how a B-tree differs from a hash table for database indexing."}],
  "max_tokens":300,
  "stream":false
}' | jq '{
    completion_tokens: .usage.completion_tokens,
    prompt_tokens: .usage.prompt_tokens,
    timings: .timings
}'

hr "metrics endpoint"
curl -fsS "${BASE%/}/metrics" | grep -E '^(llamacpp:|# HELP llamacpp:)' | head -25 || echo "(no /metrics — server may need --metrics)"

hr "long-context probe: ~8K-token prompt"
LONG=$(python3 -c "print(('The quick brown fox jumps over the lazy dog. ' * 800)[:32000])")
time curl -fsS "${URL}/chat/completions" -H 'content-type: application/json' -d "$(jq -nc --arg p "$LONG" '{
  model:"x",
  messages:[{role:"user", content:($p + "\n\nHow many times did the fox jump? Just give a rough count.")}],
  max_tokens:60
}')" | jq '{content: .choices[0].message.content, prompt_tokens: .usage.prompt_tokens, timings: .timings}'

hr "DONE"
