#!/usr/bin/env bash
set -euo pipefail

RUNNER_HOME="${RUNNER_HOME:-/home/runner}"
RUNNER_WAIT_FOR_DOCKER_IN_SECONDS="${RUNNER_WAIT_FOR_DOCKER_IN_SECONDS:-60}"

: "${RUNNER_JITCONFIG:?RUNNER_JITCONFIG must be set}"

LOG_DIR="${LOG_DIR:-/home/runner/logs}"
mkdir -p "$LOG_DIR"

echo "[ruyici] starting containerd"
containerd >"$LOG_DIR/ruyici-containerd.log" 2>&1 &

echo "[ruyici] starting dockerd"
dockerd --mtu=1450 >"$LOG_DIR/ruyici-dockerd.log" 2>&1 &

docker_ready=0
for _ in $(seq 1 "$RUNNER_WAIT_FOR_DOCKER_IN_SECONDS"); do
    if [ -S /var/run/docker.sock ]; then
        docker_ready=1
        break
    fi
    sleep 1
done
if [ "$docker_ready" -ne 1 ]; then
    echo "[ruyici] dockerd did not become ready in time" >&2
fi

runner_pid=0
cleanup() {
    echo "[ruyici] shutting down"
    if [ "$runner_pid" -gt 0 ]; then
        kill "$runner_pid" 2>/dev/null || true
    fi
    pkill -TERM dockerd 2>/dev/null || true
    pkill -TERM containerd 2>/dev/null || true
    sleep 3
    pkill -KILL dockerd 2>/dev/null || true
    pkill -KILL containerd 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT EXIT

cd "$RUNNER_HOME"
./run.sh --jitconfig "$RUNNER_JITCONFIG" &
runner_pid=$!
wait "$runner_pid"
