#!/usr/bin/env bash
# 00-setup-control-plane.sh — install/manage the ruyici control plane (venv + systemd).
#
# Combines the Python venv (dependencies) and the systemd unit into one script:
#   - writes /etc/ruyici/control-plane.env  (secrets from the caller, not the script)
#   - installs systemd/ruyici-control-plane.service with $USER / real paths
#   - enables + starts the service
#
# SUBCOMMANDS (destructive ones require --yes):   <-- off by default
#
#   status                 Show service + env status (safe, default no-op if no subcommand)
#   install                First-time/update install of the service
#   env-set NAME=value     Update ONE env var in /etc/ruyici/control-plane.env and restart
#   restart                systemctl restart the service
#   remove --yes           Stop, disable, delete unit + env file (destructive, needs --yes)
#
# Secrets are passed in via env (or the env file), nothing sensitive is hardcoded:
#   RUIYICI_GH_APP_ID, RUIYICI_GH_WEBHOOK_SECRET, RUIYICI_GH_ORG,
#   RUIYICI_GH_APP_PRIVATE_KEY_FILE, RUIYICI_KUBECONFIG,
#   RUIYICI_RUNNER_LABEL (optional), RUIYICI_RUNNER_IMAGE (optional)
#
# Examples:
#   sudo -E ./00-setup-control-plane.sh install \
#       RUIYICI_GH_APP_ID=4966074 RUIYICI_GH_ORG=ruyici \
#       RUIYICI_GH_WEBHOOK_SECRET=... RUIYICI_GH_APP_PRIVATE_KEY_FILE=/etc/ruyici/github-app-private-key.pem
#   sudo -E ./00-setup-control-plane.sh env-set RUIYICI_RUNNER_LABEL=ubuntu-24.04-riscv
#   sudo ./00-setup-control-plane.sh status
#   sudo ./00-setup-control-plane.sh remove --yes
#
set -euo pipefail

ENV_FILE="/etc/ruyici/control-plane.env"
UNIT_NAME="ruyici-control-plane"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}.service"
UNIT_SRC="systemd/ruyici-control-plane.service"
PORT="${RUIYICI_PORT:-8080}"
HOST="0.0.0.0"

# --- resolve paths for $USER ------------------------------------------------
REAL_USER="${USER:-$(id -un)}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
: "${REPO_DIR:=/home/${REAL_USER}/github-app/ruyici-github-app}"

# Prefer a pre-existing uvicorn if one was already created (e.g. ${REAL_HOME}/venv).
# Otherwise use (and if need be, create) the repo-local venv so the unit's
# ExecStart always points at an executable that exists.
if [ -x "${REAL_HOME}/venv/bin/uvicorn" ]; then
  UVICORN="${REAL_HOME}/venv/bin/uvicorn"
  VENV_DIR=""
else
  VENV_DIR="${REPO_DIR}/control-plane/venv"
  UVICORN="${VENV_DIR}/bin/uvicorn"
fi

# Create the repo-local venv + install deps if uvicorn isn't present yet.
create_venv() {
  if [ -x "${UVICORN}" ]; then
    return 0
  fi
  if [ -z "${VENV_DIR}" ]; then
    printf 'error: uvicorn missing at %s and no repo-local venv dir configured\n' "${UVICORN}" >&2
    exit 1
  fi
  command -v python3 >/dev/null || { printf 'error: python3 not found\n' >&2; exit 1; }
  printf 'creating venv: %s\n' "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/control-plane/requirements.txt"
}

info() { printf 'run: %s %s\n' "${REAL_USER}" "$*"; }

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

require_yes() {
  case "${1:-}" in
    --yes|-y|yes) return 0 ;;
    *) printf 'error: destructive. pass --yes to confirm: %s\n' "$*"; exit 2 ;;
  esac
}

# --- read the current env file into a temp file -----------------------------
_require_envfile() {
  [[ -f "${ENV_FILE}" ]] || { printf 'error: %s not found (run install first)\n' "${ENV_FILE}"; exit 1; }
}

status_cmd() {
  if systemctl is-active --quiet "${UNIT_NAME}" 2>/dev/null; then
    printf 'service %s: ACTIVE\n' "${UNIT_NAME}"
  else
    printf 'service %s: INACTIVE/unknown\n' "${UNIT_NAME}"
  fi
  echo "env file: ${ENV_FILE}"
  if [[ -f "${ENV_FILE}" ]]; then
    sed -E 's/=(.*)$/=<redacted>/' "${ENV_FILE}"
  else
    echo "(none)"
  fi
  echo
  systemctl --no-pager --full status "${UNIT_NAME}" 2>/dev/null | head -n 12 || true
}

# --- write the env file (install) -------------------------------------------
_apply_env_from_args() {
  # Accept NAME=value pairs as positional args; merge with existing file first.
  local env_final="${REAL_USER:-?}.ruyici-env.tmp"
  env_final="${TMPDIR:-/tmp}/${env_final}"
  : > "${env_final}"
  if [[ -f "${ENV_FILE}" ]]; then
    cp "${ENV_FILE}" "${env_final}"
  fi
  for kv in "$@"; do
    case "${kv}" in
      *=*) ;;
      *) printf 'error: expected NAME=value, got %q\n' "${kv}"; exit 1 ;;
    esac
    local key="${kv%%=*}" val="${kv#*=}"
    if grep -q "^${key}=" "${env_final}"; then
      sed -i "s|^${key}=.*|${key}=${val}|" "${env_final}"
    else
      printf '%s=%s\n' "${key}" "${val}" >> "${env_final}"
    fi
  done
  # append defaults only if the key is absent
  grep -q '^RUIYICI_RUNNER_LABEL=' "${env_final}" || printf 'RUIYICI_RUNNER_LABEL=ubuntu-24.04-riscv\n' >> "${env_final}"
  grep -q '^RUIYICI_RUNNER_IMAGE=' "${env_final}" || printf 'RUIYICI_RUNNER_IMAGE=%s\n' "community-ci.openruyi.cn/ruyici-runner:ubuntu-24.04" >> "${env_final}"
  grep -q '^RUIYICI_KUBECONFIG=' "${env_final}" || printf 'RUIYICI_KUBECONFIG=%s\n' "${REAL_HOME}/.kube/config" >> "${env_final}"

  # validate required keys exist
  for k in RUIYICI_GH_APP_ID RUIYICI_GH_APP_PRIVATE_KEY_FILE RUIYICI_GH_WEBHOOK_SECRET RUIYICI_GH_ORG; do
    grep -q "^${k}=." "${env_final}" || { printf 'error: %s is required (pass %s=...)\n' "$k" "$k"; exit 1; }
  done
  local priv_key
  priv_key=$(grep '^RUIYICI_GH_APP_PRIVATE_KEY_FILE=' "${env_final}" | cut -d= -f2-)
  [[ -f "${priv_key}" ]] || { printf 'error: private key not found: %s\n' "${priv_key}"; exit 1; }

  sudo install -d -m 0700 /etc/ruyici
  sudo install -m 600 "${env_final}" "${ENV_FILE}"
  rm -f "${env_final}"
}

install_unit() {
  sudo install -d -m 0755 /etc/systemd/system
  sed -e "s|__USER__|${REAL_USER}|g" \
      -e "s|__REPO_DIR__|${REPO_DIR}|g" \
      -e "s|__UVICORN__|${UVICORN}|g" \
      "${UNIT_SRC}" | sudo tee "${UNIT_DEST}" >/dev/null
}

install_cmd() {
  _apply_env_from_args "$@"
  create_venv
  install_unit
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${UNIT_NAME}"
  echo
  echo "installed. verify:" 
  echo "  sudo -E $0 status"
  echo "  curl http://${HOST}:${PORT}/health"
  echo "  journalctl -u ${UNIT_NAME} -f"
}

env_set_cmd() {
  _require_envfile
  local tmp="${REAL_HOME}/.ruyici-env.tmp"
  cp "${ENV_FILE}" "${tmp}"
  local done=0
  for kv in "$@"; do
    case "${kv}" in
      *=*) ;;
      *) printf 'error: expected NAME=value, got %q\n' "${kv}"; exit 1 ;;
    esac
    local key="${kv%%=*}" val="${kv#*=}"
    if grep -q "^${key}=" "${tmp}"; then
      sed -i "s|^${key}=.*|${key}=${val}|" "${tmp}"
    else
      printf '%s=%s\n' "${key}" "${val}" >> "${tmp}"
    fi
    done=1
  done
  [[ "$done" == 1 ]] || { printf 'error: no NAME=value provided\n'; exit 1; }
  sudo install -m 600 "${tmp}" "${ENV_FILE}"
  rm -f "${tmp}"
  sudo systemctl restart "${UNIT_NAME}"
  echo "updated and restarted ${UNIT_NAME}."
}

restart_cmd() { _require_envfile; sudo systemctl restart "${UNIT_NAME}"; echo "restarted ${UNIT_NAME}."; }

remove_cmd() {
  require_yes "${1:-}"
  sudo systemctl stop "${UNIT_NAME}" 2>/dev/null || true
  sudo systemctl disable "${UNIT_NAME}" 2>/dev/null || true
  sudo rm -f "${UNIT_DEST}"
  sudo rmdir --ignore-fail-on-non-empty /etc/ruyici 2>/dev/null || true
  sudo systemctl daemon-reload
  echo "removed ${UNIT_NAME} (unit + env)."
}

main() {
  local cmd="${1:-}"
  shift || true
  case "${cmd}" in
    status)   status_cmd ;;
    install)  install_cmd "$@" ;;
    env-set)  env_set_cmd "$@" ;;
    restart)  restart_cmd ;;
    remove)   remove_cmd "${1:-}" ;;
    -h|--help|help) usage ;;
    *) printf 'error: unknown or missing subcommand: %q\n' "${cmd:-<none>}" >&2; usage ;;
  esac
}

main "$@"
