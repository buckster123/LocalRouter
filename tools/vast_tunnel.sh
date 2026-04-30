#!/usr/bin/env bash
# SSH tunnel manager for the qwen36-vast llama-server endpoint.
#
# llama-server binds 127.0.0.1:8000 inside the container for security.
# This script forwards local port 8800 → container's 127.0.0.1:8000
# via Vast's SSH gateway. All your local tools (Hermes, harness, CC)
# connect to http://127.0.0.1:8800/v1 and never need to know the
# instance IP/port.
#
# Usage:
#   ./tools/vast_tunnel.sh up       # start tunnel (reads .last_instance)
#   ./tools/vast_tunnel.sh status   # print status + test health endpoint
#   ./tools/vast_tunnel.sh down     # kill tunnel
#   ./tools/vast_tunnel.sh logs     # tail launch.log from the container
#
# Env overrides:
#   LOCAL_PORT   default 8800
#   REMOTE_PORT  default 8000
#   INST_FILE    default .last_instance (relative to project root)
#
# ControlMaster is required for fast agentic tool loops.
# Without it: ~500ms per request. With it: ~RTT (20-130ms depending on geo).
# Add to ~/.ssh/config:
#   Host ssh*.vast.ai
#       ControlMaster auto
#       ControlPath ~/.ssh/cm-%r@%h:%p
#       ControlPersist 5m
#       ServerAliveInterval 30

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

LOCAL_PORT="${LOCAL_PORT:-8800}"
REMOTE_PORT="${REMOTE_PORT:-8000}"
INST_FILE="${INST_FILE:-${PROJECT_DIR}/.last_instance}"
PID_FILE="/tmp/vastai-gguf-tunnel.pid"
SSH_CFG_FILE="/tmp/vastai-gguf-tunnel.ssh"

die() { echo "ERROR: $*" >&2; exit 1; }

get_ssh_details() {
    local inst_id
    inst_id=$(cat "$INST_FILE" 2>/dev/null) || die "no .last_instance file at $INST_FILE — run vast_up.sh first"
    local raw
    raw=$(vastai show instance "$inst_id" --raw 2>/dev/null) || die "vastai CLI failed"
    local as ssh_host ssh_port
    as=$(echo "$raw" | jq -r '.actual_status // "unknown"')
    ssh_host=$(echo "$raw" | jq -r '.ssh_host // empty')
    ssh_port=$(echo "$raw" | jq -r '.ssh_port // empty')
    [ -n "$ssh_host" ] && [ -n "$ssh_port" ] || die "instance $inst_id has no SSH info yet (status=$as) — wait for it to start"
    echo "$inst_id $ssh_host $ssh_port $as"
}

cmd_up() {
    # Kill any existing tunnel first
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "existing tunnel (pid $OLD_PID) found — killing before re-opening..."
            kill "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PID_FILE" "$SSH_CFG_FILE"
    fi

    read -r INST_ID SSH_HOST SSH_PORT STATUS <<< "$(get_ssh_details)"
    echo "==> instance $INST_ID  status=$STATUS"
    echo "    $SSH_HOST:$SSH_PORT  → local 127.0.0.1:$LOCAL_PORT"

    if [ "$STATUS" != "running" ]; then
        echo "WARNING: instance is $STATUS, not running. Tunnel may fail until it's ready."
    fi

    # Write ephemeral SSH config for this session so we don't pollute ~/.ssh/config
    cat > "$SSH_CFG_FILE" <<EOF
Host vast-qwen36
    HostName $SSH_HOST
    Port $SSH_PORT
    User root
    StrictHostKeyChecking no
    ControlMaster auto
    ControlPath /tmp/qwen36-vast-cm
    ControlPersist 5m
    ServerAliveInterval 30
    ExitOnForwardFailure yes
EOF

    # -f: background after auth  -N: no remote command  -F: use our ephemeral config
    ssh -f -N -F "$SSH_CFG_FILE" \
        -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
        vast-qwen36

    # Find the PID of our backgrounded SSH process
    sleep 0.5
    TPID=$(pgrep -f "ssh.*${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}.*vast-qwen36" | head -1 || true)
    if [ -z "$TPID" ]; then
        # fallback: grab newest ssh process
        TPID=$(pgrep -n ssh || true)
    fi
    echo "$TPID" > "$PID_FILE"
    echo "    tunnel pid=$TPID  saved to $PID_FILE"
    echo ""
    echo "    endpoint ready at: http://127.0.0.1:${LOCAL_PORT}/v1"
    echo "    test with:  curl -s http://127.0.0.1:${LOCAL_PORT}/health"
    echo "    close with: $0 down"
    echo ""

    # Quick health check with retry (model may still be loading)
    for i in 1 2 3; do
        H=$(curl -s --max-time 3 "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null || true)
        if echo "$H" | grep -q '"ok"'; then
            MODEL=$(curl -s --max-time 3 "http://127.0.0.1:${LOCAL_PORT}/v1/models" 2>/dev/null | jq -r '.data[0].id // "loading"')
            echo "    health=OK  model=$MODEL"
            return 0
        fi
        [ $i -lt 3 ] && echo "    health check $i/3 not ready yet, retrying..." && sleep 3
    done
    echo "    tunnel is up but llama-server not responding yet — model may still be loading."
    echo "    run '$0 status' in a minute to check."
}

cmd_status() {
    echo "=== tunnel status ==="
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "  SSH tunnel: running (pid $PID)"
        else
            echo "  SSH tunnel: DEAD (pid $PID no longer exists)"
            rm -f "$PID_FILE"
        fi
    else
        echo "  SSH tunnel: not running (no pidfile)"
    fi

    echo ""
    echo "=== instance ==="
    read -r INST_ID SSH_HOST SSH_PORT STATUS <<< "$(get_ssh_details)" 2>/dev/null || { echo "  (no instance)"; return; }
    echo "  id=$INST_ID  status=$STATUS"
    echo "  ssh: $SSH_HOST:$SSH_PORT"

    echo ""
    echo "=== endpoint ==="
    H=$(curl -s --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null || true)
    if echo "$H" | grep -q '"ok"'; then
        MODELS=$(curl -s --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/v1/models" 2>/dev/null | jq -r '[.data[].id] | join(", ")' || echo "?")
        SLOTS=$(curl -s --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/slots" 2>/dev/null | jq 'length' 2>/dev/null || echo "?")
        echo "  health=OK  slots=$SLOTS  model=$MODELS"
    else
        echo "  health=unreachable (tunnel down or server still loading)"
    fi
}

cmd_down() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "killing tunnel (pid $PID)..."
            kill "$PID"
        else
            echo "tunnel pid $PID already gone"
        fi
        rm -f "$PID_FILE"
    else
        echo "no tunnel pidfile found"
    fi
    # Also kill any lingering ControlMaster socket
    ssh -F "$SSH_CFG_FILE" -O exit vast-qwen36 2>/dev/null || true
    rm -f "$SSH_CFG_FILE" /tmp/qwen36-vast-cm 2>/dev/null || true
    echo "tunnel down."
}

cmd_logs() {
    read -r INST_ID SSH_HOST SSH_PORT STATUS <<< "$(get_ssh_details)"
    echo "==> tailing /var/log/launch.log on instance $INST_ID ($STATUS)..."
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        root@"$SSH_HOST" 'tail -f /var/log/launch.log' 2>&1
}

CMD="${1:-status}"
case "$CMD" in
    up)     cmd_up ;;
    status) cmd_status ;;
    down)   cmd_down ;;
    logs)   cmd_logs ;;
    *) echo "usage: $0 up|status|down|logs" >&2; exit 1 ;;
esac
