#!/bin/bash
# Fail fast & sane IFS
set -Eeuo pipefail
IFS=$'\n\t'

# Capture all script output to a log file for the placeholder page
LOG_FILE="/server.log"
: > "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

# Default venv (must be at the very top)
: "${VIRTUAL_ENV:=/workspace/ComfyUI/venv}"
export VIRTUAL_ENV

# Container script version marker
SCRIPT_VERSION="v2.2.1-build-patch-cuda130"
echo "========================================================"
echo "[init] Starting container script version: $SCRIPT_VERSION"
echo "========================================================"

# Quiet noisy warnings and pip notices globally
export PYTHONWARNINGS="ignore"
export PIP_DISABLE_PIP_VERSION_CHECK="1"
export HF_HUB_DISABLE_TELEMETRY="1"
ENABLE_RUNTIME_REPO_UPDATES="${ENABLE_RUNTIME_REPO_UPDATES:-1}"
# Persist pip cache across runs to speed installs
export PIP_CACHE_DIR="/workspace/.cache/pip"
PYTORCH_WHL_INDEX="${PYTORCH_WHL_INDEX:-https://download.pytorch.org/whl/cu130}"
if [[ -z "${PIP_EXTRA_INDEX_URL:-}" ]]; then
  export PIP_EXTRA_INDEX_URL="$PYTORCH_WHL_INDEX"
fi
if [[ -z "${UV_EXTRA_INDEX_URL:-}" ]]; then
  export UV_EXTRA_INDEX_URL="$PYTORCH_WHL_INDEX"
fi
# Keep pip and uv aligned with the image's runtime foundation during custom node installs.
DEFAULT_PYTHON_CONSTRAINTS="/etc/pip/constraints.txt"
if [[ -z "${PIP_CONSTRAINT:-}" && -f "$DEFAULT_PYTHON_CONSTRAINTS" ]]; then
  export PIP_CONSTRAINT="$DEFAULT_PYTHON_CONSTRAINTS"
fi
if [[ -n "${PIP_CONSTRAINT:-}" ]]; then
  export UV_CONSTRAINT="${UV_CONSTRAINT:-$PIP_CONSTRAINT}"
  export UV_BUILD_CONSTRAINT="${UV_BUILD_CONSTRAINT:-$PIP_CONSTRAINT}"
  echo "[deps] python install constraints: $PIP_CONSTRAINT"
fi
echo "[deps] extra package index: $PYTORCH_WHL_INDEX"

# --- Optional uv (fast installer) ---
USE_UV="${USE_UV:-}"
if [[ -z "${USE_UV}" ]]; then
  for cfg in /workspace/ComfyUI/user/__manager/config.ini /Comfy/user/__manager/config.ini; do
    if [[ -f "$cfg" ]]; then
      uv_val="$(awk -F= '/^\s*use_uv\s*=/{gsub(/[[:space:]]/,"",$2); print tolower($2); exit}' "$cfg")"
      case "$uv_val" in
        true|1|yes) USE_UV=1 ;;
      esac
      break
    fi
  done
fi
USE_UV="${USE_UV:-0}"
if [[ "$USE_UV" == "1" ]] && ! command -v uv >/dev/null 2>&1; then
  echo "[warn] USE_UV=1 but uv not found; falling back to pip"
  USE_UV=0
fi
export USE_UV

# --- Nightly ComfyUI update policy (disabled by default -> stable) ---
NIGHTLY_COMFYUI="${NIGHTLY_COMFYUI:-${COMFYUI_NIGHTLY:-${USE_NIGHTLY:-0}}}"
case "$(echo "${NIGHTLY_COMFYUI}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) NIGHTLY_COMFYUI=1 ;;
  *) NIGHTLY_COMFYUI=0 ;;
esac
export NIGHTLY_COMFYUI
if [[ "$NIGHTLY_COMFYUI" == "1" ]]; then
  echo "[comfyui] nightly update policy enabled (NIGHTLY_COMFYUI=1)"
else
  echo "[comfyui] stable update policy enabled by default (NIGHTLY_COMFYUI=0)"
fi

pip_install() {
  local py="$1"
  shift
  local constraint_args=()
  if [[ -n "${PIP_CONSTRAINT:-}" ]]; then
    constraint_args=(--constraint "$PIP_CONSTRAINT")
  fi
  if [[ "$USE_UV" == "1" ]]; then
    uv pip install --python "$py" "${constraint_args[@]}" "$@"
  else
    "$py" -m pip install "${constraint_args[@]}" "$@"
  fi
}

pip_install_requirements() {
  local py="$1"
  local req="$2"
  shift 2
  local constraint_args=()
  if [[ -n "${PIP_CONSTRAINT:-}" ]]; then
    constraint_args=(--constraint "$PIP_CONSTRAINT")
  fi
  if [[ "$USE_UV" == "1" ]]; then
    uv pip install --python "$py" "${constraint_args[@]}" -r "$req" "$@"
  else
    "$py" -m pip install "${constraint_args[@]}" -r "$req" "$@"
  fi
}

run_with_manager_pip_env() {
  PIP_NO_COMPILE=1 "$@"
}

COMFY_IMAGE_SYNC_MARKER_NAME=".image_baked_core_head"
COMFY_IMAGE_SYNC_MANIFEST_NAME=".image_baked_core_files.txt"

current_image_comfy_head() {
  git -C /Comfy rev-parse HEAD 2>/dev/null || true
}

comfy_image_sync_marker_path() {
  printf '%s\n' "$1/$COMFY_IMAGE_SYNC_MARKER_NAME"
}

comfy_image_sync_manifest_path() {
  printf '%s\n' "$1/$COMFY_IMAGE_SYNC_MANIFEST_NAME"
}

write_comfy_image_sync_marker() {
  local root="$1"
  local head="$2"
  [[ -n "$head" ]] || return 0
  printf '%s\n' "$head" > "$(comfy_image_sync_marker_path "$root")"
}

refresh_comfy_image_sync_manifest() {
  local root="$1"
  [[ -d "$root/.git" ]] || return 0
  COMFY_SYNC_ROOT="$root" COMFY_SYNC_MANIFEST="$(comfy_image_sync_manifest_path "$root")" python3 - <<'PY' || true
from pathlib import Path
import os
import subprocess

root = Path(os.environ["COMFY_SYNC_ROOT"])
manifest_path = Path(os.environ["COMFY_SYNC_MANIFEST"])
try:
    output = subprocess.check_output(
        ["git", "-c", "safe.directory=*", "-C", str(root), "ls-files", "-z"],
        stderr=subprocess.DEVNULL
    )
    tracked = sorted(
        entry.decode("utf-8")
        for entry in output.split(b"\x00")
        if entry
    )
    manifest_path.write_text("".join(f"{entry}\n" for entry in tracked), encoding="utf-8")
except Exception:
    pass
PY
}

sync_image_baked_comfy_core() {
  local source_root="/Comfy"
  local target_root="/workspace/ComfyUI"
  local image_head=""
  local synced_head=""

  if [[ "${SYNC_IMAGE_BAKED_COMFY_CORE:-0}" != "1" ]]; then
    echo "[core] image-baked ComfyUI core sync disabled; keeping persisted workspace copy"
    return 0
  fi

  if [[ ! -d "$source_root/.git" || ! -d "$target_root" ]]; then
    return 0
  fi

  image_head="$(current_image_comfy_head)"
  if [[ -z "$image_head" ]]; then
    echo "[core][warn] failed to determine image-baked ComfyUI head; skipping core sync"
    return 0
  fi

  synced_head="$(tr -d '\r\n' < "$(comfy_image_sync_marker_path "$target_root")" 2>/dev/null || true)"
  if [[ "$synced_head" == "$image_head" ]]; then
    echo "[core] image-baked ComfyUI core already synced ($image_head)"
    return 0
  fi

  echo "STAGE: Syncing image-baked ComfyUI core"
  echo "[core] syncing tracked ComfyUI files from image head $image_head into persisted workspace"
  if IMAGE_COMFY_SOURCE="$source_root" WORKSPACE_COMFY_TARGET="$target_root" COMFY_SYNC_MANIFEST="$(comfy_image_sync_manifest_path "$target_root")" python - <<'PY'
from pathlib import Path
import os
import shutil
import subprocess

source_root = Path(os.environ["IMAGE_COMFY_SOURCE"])
target_root = Path(os.environ["WORKSPACE_COMFY_TARGET"])
manifest_path = Path(os.environ["COMFY_SYNC_MANIFEST"])


def tracked_files(root: Path) -> set[str]:
    output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    return {
        entry.decode("utf-8")
        for entry in output.split(b"\x00")
        if entry
    }


source_files = tracked_files(source_root)
copied = 0
for relative_path in sorted(source_files):
    source_path = source_root / relative_path
    target_path = target_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and target_path.is_dir():
        shutil.rmtree(target_path)
    shutil.copy2(source_path, target_path)
    copied += 1

removed = 0
if manifest_path.exists():
    target_files = {
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
else:
    try:
        target_files = tracked_files(target_root)
    except Exception:
        target_files = set()

for relative_path in sorted(target_files - source_files):
    target_path = target_root / relative_path
    try:
        target_path.unlink()
        removed += 1
    except FileNotFoundError:
        pass

manifest_path.write_text(
    "".join(f"{relative_path}\n" for relative_path in sorted(source_files)),
    encoding="utf-8",
)
print(f"[core] copied {copied} tracked files; removed {removed} stale tracked files")
PY
  then
    write_comfy_image_sync_marker "$target_root" "$image_head"
    echo "[core] synced image-baked ComfyUI core to $image_head"
  else
    echo "[core][warn] failed to sync image-baked ComfyUI core; keeping persisted workspace copy"
  fi
}

# --- Sanitize COMFYUI_BACKUP: must be exactly owner/repo; otherwise unset ---
if [[ -n "${COMFYUI_BACKUP-}" ]]; then
  _cb="${COMFYUI_BACKUP//[[:space:]]/}"
  if [[ "$_cb" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
    COMFYUI_BACKUP="$_cb"
  else
    echo "[WARN] COMFYUI_BACKUP invalid ('${COMFYUI_BACKUP}'); unsetting."
    unset COMFYUI_BACKUP
  fi
fi

# --- CPU preflight helpers ---
check_cpu_overload() {
  local idle=""
  if command -v mpstat >/dev/null 2>&1; then
    idle="$(mpstat 1 1 | awk '/Average/ {print $NF}' | tail -n 1 | tr ',' '.' | cut -d. -f1)"
  elif command -v vmstat >/dev/null 2>&1; then
    idle="$(vmstat 1 2 | tail -1 | awk '{print $15}')"
  fi
  if [[ -z "${idle:-}" || ! "$idle" =~ ^[0-9]+$ ]]; then
    idle=0
  fi
  [[ "$idle" -lt 5 ]]
}

compute_stable_pin_cores() {
  local max_stable="${CPU_STABLE_CORE_MAX:-71}"
  local cpu_count
  local max_idx
  local upper

  if [[ ! "$max_stable" =~ ^[0-9]+$ ]]; then
    max_stable=71
  fi

  cpu_count="$(nproc 2>/dev/null || echo 0)"
  if [[ ! "$cpu_count" =~ ^[0-9]+$ || "$cpu_count" -le 0 ]]; then
    echo "0-$max_stable"
    return
  fi

  max_idx=$((cpu_count - 1))
  upper=$max_idx
  if [[ "$max_stable" =~ ^[0-9]+$ && "$max_stable" -lt "$upper" ]]; then
    upper=$max_stable
  fi
  if [[ "$upper" -le 0 ]]; then
    echo "0"
  else
    echo "0-$upper"
  fi
}

# --- CUDA preflight helper (returns JSON; exit 0 only when CUDA is usable) ---
cuda_probe_json() {
  python - <<'PY'
import json

result = {
    "ok": False,
    "torch_version": None,
    "torch_cuda": None,
    "cuda_available": False,
    "device_index": None,
    "device_name": None,
    "error": None,
}

try:
    import torch
    result["torch_version"] = getattr(torch, "__version__", None)
    result["torch_cuda"] = getattr(torch.version, "cuda", None)
    result["cuda_available"] = bool(torch.cuda.is_available())
    if result["cuda_available"]:
        idx = torch.cuda.current_device()
        result["device_index"] = int(idx)
        result["device_name"] = str(torch.cuda.get_device_name(idx))
        result["ok"] = True
    else:
        result["error"] = "torch.cuda.is_available() returned False"
except Exception as exc:
    msg = str(exc).replace("\n", " ").strip()
    result["error"] = f"{type(exc).__name__}: {msg}"

print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["ok"] else 1)
PY
}

# --- CPU confirm state (used by wait page and launch gate) ---
PIN_CORES=""
CPU_CONFIRM_REQUIRED=0
CPU_CONTINUE_FILE=/tmp/continue_cpu
CPU_WAIT_PORT="${CPU_WAIT_PORT:-${COMFY_WAIT_PORT:-8188}}"
CPU_PIN_CANDIDATE="$(compute_stable_pin_cores)"
rm -f "$CPU_CONTINUE_FILE"

APP_STATE_DIR="${APP_STATE_DIR:-/tmp/app_state}"
APP_ACTIVE_FILE="$APP_STATE_DIR/active_app"
APP_DESIRED_FILE="$APP_STATE_DIR/desired_app"
APP_PHASE_FILE="$APP_STATE_DIR/phase"
APP_DETAIL_FILE="$APP_STATE_DIR/detail"
mkdir -p "$APP_STATE_DIR"

read_state_value() {
  local path="$1"
  local default_value="${2:-}"
  if [[ -f "$path" ]]; then
    local value
    value="$(tr -d '\r' < "$path" 2>/dev/null || true)"
    if [[ -n "$value" ]]; then
      printf '%s\n' "$value"
      return 0
    fi
  fi
  printf '%s\n' "$default_value"
}

write_state_value() {
  local path="$1"
  local value="${2:-}"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$value" > "$path"
}

normalize_app_name() {
  local app_name="${1:-}"
  case "$app_name" in
    comfyui|fizgig)
      printf '%s\n' "$app_name"
      ;;
    ai-toolkit)
      printf 'fizgig\n'
      ;;
    *)
      printf 'comfyui\n'
      ;;
  esac
}

set_app_runtime_state() {
  local active_app
  active_app="$(normalize_app_name "${1:-$(read_state_value "$APP_ACTIVE_FILE" comfyui)}")"
  local desired_app
  desired_app="$(normalize_app_name "${2:-$(read_state_value "$APP_DESIRED_FILE" "$active_app")}")"
  local phase="${3:-idle}"
  local detail="${4:-}"
  write_state_value "$APP_ACTIVE_FILE" "$active_app"
  write_state_value "$APP_DESIRED_FILE" "$desired_app"
  write_state_value "$APP_PHASE_FILE" "$phase"
  write_state_value "$APP_DETAIL_FILE" "$detail"
}

is_pid_alive() {
  local p="${1:-}"
  [[ -n "$p" ]] || return 1
  if ! kill -0 "$p" 2>/dev/null; then
    return 1
  fi
  if [[ -f "/proc/$p/status" ]]; then
    if grep -qE '^State:[[:space:]]*Z' "/proc/$p/status" 2>/dev/null; then
      return 1
    fi
  else
    local st
    st="$(ps -o stat= -p "$p" 2>/dev/null || echo "")"
    if [[ "$st" == *Z* ]]; then
      return 1
    fi
  fi
  return 0
}

placeholder_running() {
  is_pid_alive "${1:-}"
}

stop_placeholder_process() {
  local pid_var_name="$1"
  local -n placeholder_pid_ref="$pid_var_name"
  if placeholder_running "${placeholder_pid_ref:-}"; then
    kill "${placeholder_pid_ref}" 2>/dev/null || true
    sleep 0.5
    if placeholder_running "${placeholder_pid_ref:-}"; then
      kill -9 "${placeholder_pid_ref}" 2>/dev/null || true
    fi
  fi
  placeholder_pid_ref=""
}

# --- Temporary log page on port 8188 until ComfyUI starts ---
start_comfy_startup_placeholder() {
  local port="${COMFY_WAIT_PORT:-8188}"
  if placeholder_running "${COMFY_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/comfy_wait_page.py \
    --port "$port" --refresh 2 \
    --files /server.log \
    --continue-file "$CPU_CONTINUE_FILE" \
    --cpu-pin-cores "$CPU_PIN_CANDIDATE" \
    >> /placeholder.log 2>&1 &
  export COMFY_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! kill -0 "$COMFY_PLACEHOLDER_PID" 2>/dev/null; then
    echo "[placeholder][error] failed to start on :$port (see /placeholder.log)"
    return 1
  fi
  echo "[placeholder] started on :$port (pid=$COMFY_PLACEHOLDER_PID)"
}

start_comfy_switch_placeholder() {
  local port="${COMFY_WAIT_PORT:-8188}"
  if placeholder_running "${COMFY_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/app_switch_page.py \
    --port "$port" \
    --state-dir "$APP_STATE_DIR" \
    --target-app comfyui \
    --source-app fizgig \
    >> /placeholder.log 2>&1 &
  export COMFY_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! placeholder_running "${COMFY_PLACEHOLDER_PID:-}"; then
    echo "[placeholder][error] failed to start ComfyUI switch page on :$port"
    return 1
  fi
  echo "[placeholder] comfy switch page started on :$port (pid=$COMFY_PLACEHOLDER_PID)"
}

start_comfy_placeholder() {
  start_comfy_startup_placeholder "$@"
}

# Ensure nginx is up even if startup later halts on preflight.
NGINX_STARTED=0
NGINX_WARNED=0
nginx_http_ready() {
  local code=""
  if ! command -v curl >/dev/null 2>&1; then
    return 1
  fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:7860/ || true)"
  [[ "$code" =~ ^[0-9]{3}$ && "$code" != "000" ]]
}

nginx_config_test() {
  if command -v nginx >/dev/null 2>&1; then
    if ! nginx -t -c /etc/nginx/nginx.conf >/tmp/nginx_test.out 2>&1; then
      echo "[nginx][error] config test failed; see /tmp/nginx_test.out"
      cat /tmp/nginx_test.out
      return 1
    fi
  fi
  return 0
}

ensure_nginx_started() {
  if [[ "${NGINX_STARTED:-0}" == "1" ]]; then
    if nginx_http_ready; then
      return 0
    fi
    NGINX_STARTED=0
  fi

  if nginx_http_ready; then
    NGINX_STARTED=1
    return 0
  fi

  nginx_config_test || true

  local tries=0
  while (( tries < 3 )); do
    tries=$((tries + 1))
    if pgrep -x nginx >/dev/null 2>&1 && nginx_http_ready; then
      NGINX_STARTED=1
      return 0
    fi

    service nginx start >/dev/null 2>&1 || true
    if pgrep -x nginx >/dev/null 2>&1 && nginx_http_ready; then
      NGINX_STARTED=1
      echo "[nginx] started."
      return 0
    fi

    if [[ -x /etc/init.d/nginx ]]; then
      /etc/init.d/nginx start >/dev/null 2>&1 || true
      if pgrep -x nginx >/dev/null 2>&1 && nginx_http_ready; then
        NGINX_STARTED=1
        echo "[nginx] started."
        return 0
      fi
    fi

    if command -v nginx >/dev/null 2>&1; then
      nginx >/dev/null 2>&1 || true
      if pgrep -x nginx >/dev/null 2>&1 && nginx_http_ready; then
        NGINX_STARTED=1
        echo "[nginx] started."
        return 0
      fi
    fi

    sleep 0.5
  done

  if [[ "${NGINX_WARNED:-0}" == "0" ]]; then
    echo "[nginx][warn] failed to start."
    NGINX_WARNED=1
  fi
  return 1
}

keep_comfy_wait_stack_alive_forever() {
  echo "[fatal][wait] Keeping wait page stack alive on proxy (placeholder + nginx)."
  while true; do
    if [[ -z "${COMFY_PLACEHOLDER_PID:-}" ]] || ! kill -0 "${COMFY_PLACEHOLDER_PID}" 2>/dev/null; then
      start_comfy_placeholder || true
    fi
    ensure_nginx_started || true
    sleep 5
  done
}

# Start placeholder before any preflight checks so failures are visible in UI.
start_comfy_placeholder || true
ensure_nginx_started || true

# --- CUDA preflight (bail early) ---
echo "STAGE: Checking CUDA initialization"
CUDA_PREFLIGHT_ATTEMPTS="${CUDA_PREFLIGHT_ATTEMPTS:-6}"
CUDA_PREFLIGHT_DELAY="${CUDA_PREFLIGHT_DELAY:-2}"
CUDA_OK=0
CUDA_DIAG='{"ok":false,"error":"preflight did not run"}'
for ((cuda_try=1; cuda_try<=CUDA_PREFLIGHT_ATTEMPTS; cuda_try++)); do
  if CUDA_DIAG="$(cuda_probe_json 2>&1)"; then
    CUDA_OK=1
    echo "[cuda] preflight passed: ${CUDA_DIAG}"
    break
  fi
  echo "[cuda][warn] preflight attempt ${cuda_try}/${CUDA_PREFLIGHT_ATTEMPTS} failed: ${CUDA_DIAG}"
  if (( cuda_try < CUDA_PREFLIGHT_ATTEMPTS )); then
    sleep "$CUDA_PREFLIGHT_DELAY"
  fi
done

if [[ "$CUDA_OK" != "1" ]]; then
  cat << 'EOF'
  ███████╗██╗  ██╗██╗████████╗    ██████╗  ██████╗ ██████╗
  ██╔════╝██║  ██║██║╚══██╔══╝    ██╔══██╗██╔═══██╗██╔══██╗
  ███████╗███████║██║   ██║       ██████╔╝██║   ██║██║  ██║
  ╚════██║██╔══██║██║   ██║       ██╔═══╝ ██║   ██║██║  ██║
  ███████║██║  ██║██║   ██║       ██║     ╚██████╔╝██████╔╝
  ╚══════╝╚═╝  ╚═╝╚═╝   ╚═╝       ╚═╝      ╚═════╝ ╚═════╝

      this pod failed CUDA runtime initialization
          deploy a new pod on a different host

EOF
  echo "CUDA initialization failed: ${CUDA_DIAG}" >> ~/comfyui_error.log
  echo "STAGE: Host driver failure"
  echo "[fatal][gpu] CUDA runtime initialization failed on this host."
  echo "[fatal][gpu_diag] ${CUDA_DIAG}"
  echo "[fatal][action] Startup halted. Redeploy this pod on a healthy host."
  start_comfy_placeholder || true
  ensure_nginx_started || true
  keep_comfy_wait_stack_alive_forever
fi

# --- CPU preflight (warn + confirm on overload) ---
if check_cpu_overload; then
  CPU_CONFIRM_REQUIRED=1
  echo "STAGE: Host CPU overload confirmation"
  cat <<'EOF'
███████╗ ██████╗  ██████╗ ██████╗     ███╗   ██╗███╗   ███╗
██╔════╝██╔═══██╗██╔════╝██╔═══██╗    ████╗  ██║████╗ ████║
█████╗  ██║   ██║██║     ██║   ██║    ██╔██╗ ██║██╔████╔██║
██╔══╝  ██║   ██║██║     ██║   ██║    ██║╚██╗██║██║╚██╔╝██║
██║     ╚██████╔╝╚██████╗╚██████╔╝    ██║ ╚████║██║ ╚═╝ ██║
╚═╝      ╚═════╝  ╚═════╝ ╚═════╝     ╚═╝  ╚═══╝╚═╝     ╚═╝

[fatal][cpu] Host CPU resources are in a degraded state (CPU overload), making this host run ~3× slower than expected. Move this pod to a different host.
[fatal][cpu_action] If you keep landing on the same host, change your placement: switch to Secure, or (if you're on Community) choose a different GPU model and/or tweak your filters so you hit a different pool of machines. You can message RunPod support, but in practice they're usually not helpful for Community host problem.
[cpu][action] Confirm on the waiting page to continue startup anyway.
EOF
  echo "[cpu] Startup will continue, but ComfyUI launch is gated on web confirmation."
fi

mkdir -p /workspace

# --- Fizgig LoRA & Fine-tune Studio ---
FIZGIG_DIR="${FIZGIG_DIR:-/workspace/Fizgig}"
FIZGIG_REPO_URL="${FIZGIG_REPO_URL:-https://github.com/shootthesound/Fizgig.git}"
FIZGIG_REPO_REF="${FIZGIG_REPO_REF:-master}"
FIZGIG_VENV="${FIZGIG_VENV:-$FIZGIG_DIR/venv}"
FIZGIG_UI_PORT="${FIZGIG_UI_PORT:-8081}"
FIZGIG_UPDATE="${FIZGIG_UPDATE:-1}"
FIZGIG_APP_PID=""

# Temporary log page on Fizgig port until UI starts
start_fizgig_startup_placeholder() {
  if placeholder_running "${FIZGIG_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/fizgig_wait_page.py \
    --port "$FIZGIG_UI_PORT" \
    --files /fizgig_setup.log \
    >> /fizgig_placeholder.log 2>&1 &
  export FIZGIG_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! placeholder_running "${FIZGIG_PLACEHOLDER_PID:-}"; then
    echo "[fizgig placeholder][error] failed to start on :$FIZGIG_UI_PORT" >> /fizgig_setup.log
    return 1
  fi
  echo "[fizgig placeholder] started on :$FIZGIG_UI_PORT (pid=$FIZGIG_PLACEHOLDER_PID)" >> /fizgig_setup.log
}

start_fizgig_switch_placeholder() {
  if placeholder_running "${FIZGIG_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/app_switch_page.py \
    --port "$FIZGIG_UI_PORT" \
    --state-dir "$APP_STATE_DIR" \
    --target-app fizgig \
    --source-app comfyui \
    >> /fizgig_placeholder.log 2>&1 &
  export FIZGIG_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! placeholder_running "${FIZGIG_PLACEHOLDER_PID:-}"; then
    echo "[fizgig placeholder][error] failed to start switch page on :$FIZGIG_UI_PORT" >> /fizgig_setup.log
    return 1
  fi
  echo "[fizgig placeholder] switch page started on :$FIZGIG_UI_PORT (pid=$FIZGIG_PLACEHOLDER_PID)" >> /fizgig_setup.log
}

start_fizgig_placeholder() {
  start_fizgig_startup_placeholder "$@"
}

is_fizgig_venv_healthy() {
  [[ -f "$FIZGIG_DIR/fizgig.py" && -f "$FIZGIG_VENV/bin/python" ]] || return 1
  "$FIZGIG_VENV/bin/python" -c "import torch, torchvision, diffusers, transformers" >/dev/null 2>&1
}

prepare_fizgig_runtime() {
  set +e
  if [[ -f "/tmp/fizgig_prep.done" ]]; then
    if is_fizgig_venv_healthy; then
      echo "[fizgig] runtime already prepared in this session; skipping redundant prep" >> /fizgig_setup.log
      return 0
    else
      echo "[fizgig][warn] previous prep marker found but venv health check failed; re-running preparation" >> /fizgig_setup.log
      rm -f "/tmp/fizgig_prep.done" "$FIZGIG_DIR/.prepared" "$FIZGIG_VENV/.isolated_torch_linked" 2>/dev/null || true
    fi
  fi

  local lock_file="/tmp/fizgig_prep.lock"
  if [[ -f "$lock_file" ]]; then
    local lock_pid
    lock_pid="$(cat "$lock_file" 2>/dev/null || echo)"
    if [[ -n "$lock_pid" && "$lock_pid" != "$$" ]] && is_pid_alive "$lock_pid"; then
      echo "[fizgig] background preparation is currently running (pid=$lock_pid); waiting for it..." >> /fizgig_setup.log
      while is_pid_alive "$lock_pid" && [[ ! -f "/tmp/fizgig_prep.done" ]]; do
        sleep 1
      done
      echo "[fizgig] background preparation finished." >> /fizgig_setup.log
    fi
    rm -f "$lock_file" 2>/dev/null || true
  fi

  if [[ -f "/tmp/fizgig_prep.done" ]]; then
    if is_fizgig_venv_healthy; then
      return 0
    else
      rm -f "/tmp/fizgig_prep.done" "$FIZGIG_DIR/.prepared" "$FIZGIG_VENV/.isolated_torch_linked" 2>/dev/null || true
    fi
  fi

  echo "$$" > "$lock_file"
  echo "[fizgig] setting up in $FIZGIG_DIR" >> /fizgig_setup.log
  mkdir -p "$FIZGIG_DIR" /workspace/datasets /workspace/models /workspace/output_loras /workspace/cache /workspace/profiles

  # 1. Clone or update repository from git
  local old_commit="none"
  if [[ -d "$FIZGIG_DIR/.git" ]]; then
    old_commit="$(git -C "$FIZGIG_DIR" rev-parse HEAD 2>/dev/null || echo "none")"
    if [[ "${FIZGIG_UPDATE:-0}" == "1" ]]; then
      echo "[fizgig] updating repo to $FIZGIG_REPO_REF from $FIZGIG_REPO_URL" >> /fizgig_setup.log
      git -C "$FIZGIG_DIR" remote set-url origin "$FIZGIG_REPO_URL" >> /fizgig_setup.log 2>&1 || true
      if ! git -C "$FIZGIG_DIR" fetch --depth 1 origin "$FIZGIG_REPO_REF" >> /fizgig_setup.log 2>&1 \
         || ! git -C "$FIZGIG_DIR" reset --hard FETCH_HEAD >> /fizgig_setup.log 2>&1; then
        echo "[fizgig][warn] git update failed (possible volume corruption); attempting clean re-clone" >> /fizgig_setup.log
        rm -rf "$FIZGIG_DIR.broken" 2>/dev/null || true
        mv "$FIZGIG_DIR" "$FIZGIG_DIR.broken" 2>/dev/null || true
        git clone --depth 1 --branch "$FIZGIG_REPO_REF" "$FIZGIG_REPO_URL" "$FIZGIG_DIR" >> /fizgig_setup.log 2>&1 || true
      fi
    else
      echo "[fizgig] update disabled; keeping current checkout" >> /fizgig_setup.log
    fi
  else
    echo "[fizgig] cloning repo ($FIZGIG_REPO_REF) from $FIZGIG_REPO_URL" >> /fizgig_setup.log
    git clone --depth 1 --branch "$FIZGIG_REPO_REF" "$FIZGIG_REPO_URL" "$FIZGIG_DIR" >> /fizgig_setup.log 2>&1 || true
  fi

  if [[ ! -f "$FIZGIG_DIR/fizgig.py" ]]; then
    echo "[fizgig][error] fizgig.py not found in $FIZGIG_DIR" >> /fizgig_setup.log
    rm -f "$lock_file" 2>/dev/null || true
    set -e
    return 1
  fi
  local new_commit="$(git -C "$FIZGIG_DIR" rev-parse HEAD 2>/dev/null || echo "none")"

  # 2. Virtual environment setup (isolated venv reusing system CUDA 13 torch without re-downloading)
  local venv_needed=0
  if [[ ! -d "$FIZGIG_VENV" || ! -f "$FIZGIG_VENV/bin/python" ]]; then
    echo "[fizgig] creating isolated venv at $FIZGIG_VENV" >> /fizgig_setup.log
    python3 -m venv "$FIZGIG_VENV" >> /fizgig_setup.log 2>&1 || true
    venv_needed=1
  else
    echo "[fizgig] reusing existing venv at $FIZGIG_VENV" >> /fizgig_setup.log
    if [[ -f "$FIZGIG_VENV/pyvenv.cfg" ]]; then
      sed -i 's/include-system-site-packages = true/include-system-site-packages = false/' "$FIZGIG_VENV/pyvenv.cfg" 2>/dev/null || true
    fi
  fi

  # Symlink system PyTorch & CUDA packages into isolated venv (zero download, CUDA 13 retained)
  # NOTE: .dist-info directories MUST be copied (not symlinked) so Rust-based uv detects
  # packages as already installed and skips re-resolving/re-downloading 3GB+ of PyTorch wheels.
  echo "[fizgig] linking system CUDA 13 PyTorch packages into isolated venv..." >> /fizgig_setup.log
  FIZGIG_VENV="$FIZGIG_VENV" python3 - <<'PY' >> /fizgig_setup.log 2>&1
import os, sys, glob, shutil
from pathlib import Path

target_site = None
venv_dir = os.environ.get("FIZGIG_VENV", "/workspace/Fizgig/venv")
for d in Path(venv_dir).glob("lib/python*/site-packages"):
    if d.is_dir():
        target_site = d
        break

if not target_site:
    print("[fizgig][error] could not locate venv site-packages directory")
    sys.exit(0)

target_site.mkdir(parents=True, exist_ok=True)

# Find system site-packages containing torch
sys_site = None
for c in ["/usr/local/lib/python3.12/dist-packages", "/usr/local/lib/python3.11/dist-packages", "/usr/lib/python3/dist-packages"]:
    if os.path.isdir(os.path.join(c, "torch")):
        sys_site = c
        break

if not sys_site:
    try:
        import torch
        sys_site = str(Path(torch.__file__).parent.parent)
    except Exception:
        sys_site = "/usr/local/lib/python3.12/dist-packages"

patterns = [
    "torch*", "functorch*", "torchvision*", "torchaudio*",
    "nvidia*", "triton*", "cuda*", "cublas*", "cudnn*", "caffe2*"
]

# Clean up legacy non-torch symlinks and dist-infos so uv has full control over them
for legacy_pat in ["hf_transfer*", "hf-transfer*", "huggingface_hub*"]:
    for dst in target_site.glob(legacy_pat):
        if dst.is_symlink() or dst.is_file():
            dst.unlink(missing_ok=True)
        elif dst.is_dir() and dst.name.endswith((".dist-info", ".egg-info")):
            shutil.rmtree(dst, ignore_errors=True)

# Also ensure any other non-torch symlinks are removed
for item in target_site.iterdir():
    if item.is_symlink():
        if not any(item.match(p) for p in patterns):
            item.unlink(missing_ok=True)

# 1. Clean stale/broken packages or symlinks in target_site that do not match sys_site
for pat in patterns:
    for dst in target_site.glob(pat):
        if dst.name.endswith((".dist-info", ".egg-info")):
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
        else:
            if dst.is_symlink():
                try:
                    target = os.readlink(str(dst))
                    if not target.startswith(sys_site):
                        dst.unlink(missing_ok=True)
                except OSError:
                    dst.unlink(missing_ok=True)
            elif dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            elif dst.is_file():
                dst.unlink(missing_ok=True)

# 2. Copy .dist-info so uv & pip detect packages as already installed; symlink code directories/files
copied_dist_info = 0
linked_pkgs = 0
top_levels = set()

for pat in patterns:
    for src in glob.glob(os.path.join(sys_site, pat)):
        name = os.path.basename(src)
        dst = target_site / name
        try:
            if name.endswith((".dist-info", ".egg-info")):
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    copied_dist_info += 1
                record = Path(src) / "RECORD"
                if record.exists():
                    for line in record.read_text(encoding="utf-8", errors="ignore").splitlines():
                        part = line.split(",", 1)[0].split("/")[0].strip()
                        if part and not part.endswith((".dist-info", ".egg-info")) and part != "..":
                            top_levels.add(part)
            else:
                top_levels.add(name)
        except Exception as e:
            print(f"[fizgig][warn] could not link/copy {name}: {e}")

for name in sorted(top_levels):
    src = Path(sys_site) / name
    dst = target_site / name
    if src.exists():
        if dst.is_symlink():
            try:
                target = os.readlink(str(dst))
                if not target.startswith(sys_site):
                    dst.unlink(missing_ok=True)
            except OSError:
                dst.unlink(missing_ok=True)
        if not dst.exists() and not dst.is_symlink():
            try:
                dst.symlink_to(src)
                linked_pkgs += 1
            except Exception as e:
                print(f"[fizgig][warn] could not link {name}: {e}")

print(f"[fizgig] linked {linked_pkgs} packages and copied {copied_dist_info} dist-info entries from system into isolated venv")
PY

  # 3. Dependencies installation (only if venv created or repo updated or not marked prepared)
  local functorch_present=0
  for d in "$FIZGIG_VENV"/lib/python*/site-packages/functorch; do
    if [[ -d "$d" ]]; then functorch_present=1; break; fi
  done
  local hf_transfer_present=0
  if "$FIZGIG_VENV/bin/python" -c "import hf_transfer" 2>/dev/null; then
    hf_transfer_present=1
  fi
  local core_deps_present=0
  if "$FIZGIG_VENV/bin/python" -c "import diffusers, transformers" 2>/dev/null; then
    core_deps_present=1
  fi

  if [[ "$venv_needed" == "1" || "$old_commit" != "$new_commit" || ! -f "$FIZGIG_DIR/.prepared" || ! -f "$FIZGIG_VENV/.isolated_torch_linked" || "$functorch_present" != "1" || "$hf_transfer_present" != "1" || "$core_deps_present" != "1" ]]; then
    echo "[fizgig] ensuring pip, uv, wheel, and setuptools in venv..." >> /fizgig_setup.log
    "$FIZGIG_VENV/bin/python" -m pip install --upgrade pip uv wheel setuptools >> /fizgig_setup.log 2>&1 || true

    if [[ -f "$FIZGIG_DIR/requirements.txt" ]]; then
      echo "[fizgig] installing requirements into isolated venv..." >> /fizgig_setup.log
      local tmp_req="$FIZGIG_DIR/requirements.no-torch.txt"
      # Strip --extra-index-url (which hardcodes cu128 in Fizgig repo) so we retain CUDA 13.0
      grep -Ev '^\s*--extra-index-url' "$FIZGIG_DIR/requirements.txt" > "$tmp_req" 2>/dev/null || cp "$FIZGIG_DIR/requirements.txt" "$tmp_req"
      # Ensure hf_transfer is installed for fast downloads and HF_HUB_ENABLE_HF_TRANSFER compatibility
      grep -q -E "^hf[-_]transfer" "$tmp_req" 2>/dev/null || echo "hf_transfer" >> "$tmp_req"

      local constraint_args=()
      if [[ -n "${PIP_CONSTRAINT:-}" && -f "${PIP_CONSTRAINT}" ]]; then
        constraint_args=(--constraint "$PIP_CONSTRAINT")
      fi

      export UV_LINK_MODE="copy"

      local install_ok=0
      if env DISABLE_CUDA=1 \
          "$FIZGIG_VENV/bin/python" -m uv pip install \
            --link-mode=copy \
            --index-strategy unsafe-best-match \
            "${constraint_args[@]}" \
            -r "$tmp_req" >> /fizgig_setup.log 2>&1; then
        echo "[fizgig] dependencies installed successfully via uv pip" >> /fizgig_setup.log
        install_ok=1
      else
        echo "[fizgig][warn] uv pip install failed, attempting pip fallback..." >> /fizgig_setup.log
        if env DISABLE_CUDA=1 \
          "$FIZGIG_VENV/bin/python" -m pip install \
            --no-build-isolation \
            "${constraint_args[@]}" \
            -r "$tmp_req" >> /fizgig_setup.log 2>&1; then
          echo "[fizgig] dependencies installed successfully via pip fallback" >> /fizgig_setup.log
          install_ok=1
        else
          echo "[fizgig][error] both uv and pip dependency installation failed!" >> /fizgig_setup.log
        fi
      fi

      if [[ "$install_ok" == "1" ]]; then
        touch "$FIZGIG_VENV/.isolated_torch_linked"
      else
        echo "[fizgig][error] dependency installation failed; aborting preparation" >> /fizgig_setup.log
        rm -f "$lock_file" 2>/dev/null || true
        set -e
        return 1
      fi
    fi
  else
    echo "[fizgig] isolated venv and requirements already up to date (${new_commit:0:7})" >> /fizgig_setup.log
  fi

  # 4. Patch Fizgig to disable password authentication and allow web access via proxy
  FIZGIG_DIR="$FIZGIG_DIR" python3 - <<'PY' >> /fizgig_setup.log 2>&1
import os, re
from pathlib import Path

fizgig_dir = os.environ.get("FIZGIG_DIR", "/workspace/Fizgig")
server_file = Path(fizgig_dir) / "src" / "fizgig" / "web" / "server.py"
if server_file.is_file():
    text = server_file.read_text(encoding="utf-8")
    # Patch _logged_in to always return True
    text = re.sub(
        r'def _logged_in\(self\):.*?(?=\n    def )',
        'def _logged_in(self):\n        return True\n',
        text,
        flags=re.DOTALL
    )
    # Patch _host_ok to always return True
    text = re.sub(
        r'def _host_ok\(self\):.*?(?=\n    def )',
        'def _host_ok(self):\n        return True\n',
        text,
        flags=re.DOTALL
    )
    # Patch run_server to ensure password is None and local_only is False
    text = text.replace('Handler.password = password or None', 'Handler.password = None')
    text = text.replace('Handler.local_only = not password', 'Handler.local_only = False')
    if 'a.password = None' not in text:
        text = text.replace('a = ap.parse_args(argv)', 'a = ap.parse_args(argv)\n    a.password = None')
    server_file.write_text(text, encoding="utf-8")
    print("[fizgig] password authentication and host check disabled in server.py")
PY

  # 5. Shared model directories & preferences configuration
  echo "[fizgig] configuring shared models with ComfyUI..." >> /fizgig_setup.log
  mkdir -p "$FIZGIG_DIR/user_data"
  # Symlink user_data/models to /workspace/models
  if [[ ! -L "$FIZGIG_DIR/user_data/models" ]]; then
    rm -rf "$FIZGIG_DIR/user_data/models" 2>/dev/null || true
    ln -s /workspace/models "$FIZGIG_DIR/user_data/models" 2>/dev/null || true
  fi

  # Configure prefs.json to link ComfyUI model directories and output LoRAs
  FIZGIG_DIR="$FIZGIG_DIR" python3 - <<'PY' >> /fizgig_setup.log 2>&1
import json, os
from pathlib import Path

fizgig_dir = Path(os.environ.get("FIZGIG_DIR", "/workspace/Fizgig"))
prefs_file = fizgig_dir / "user_data" / "prefs.json"
prefs_file.parent.mkdir(parents=True, exist_ok=True)

prefs = {}
if prefs_file.is_file():
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
    except Exception:
        prefs = {}

# Set shared output directory to ComfyUI LoRAs
prefs["lora_output_dir"] = "/workspace/ComfyUI/models/loras"
prefs["input_dataset_dir"] = "/workspace/datasets"
prefs["profiles_dir"] = "/workspace/profiles"
prefs["cache_dir"] = "/workspace/cache"
prefs["web_password"] = ""

# Model file mappings between Fizgig keys and ComfyUI folders/filenames
model_map = {
    "minimax_dit": ("diffusion_models", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
    "minimax_ref_dit": ("diffusion_models", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
    "minimax_text_encoder": ("text_encoders", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    "minimax_vae": ("vae", "minimax_h3_video_vae_fp16.safetensors"),
    "minimax_audio_vae": ("vae", "minimax_h3_audio_vae_fp32.safetensors"),
    "minimax_turbo_lora": ("loras", "minimax_h3_turbo_v4_step600.safetensors"),
    "minimax_circlestone_adapter": ("loras", "minimax_h3_image_training_adapter.safetensors"),
    "minimax_training_adapter": ("loras", "minimax_h3_training_adapter_v1.safetensors"),
    "minimax_ref_training_adapter": ("loras", "minimax_h3_ref2va_training_adapter_v1.safetensors"),
    "krea2_text_encoder": ("text_encoders", "qwen3vl_4b_fp8_scaled.safetensors"),
    "base_dit": ("diffusion_models", "flux-2-klein-base-9b-fp8.safetensors"),
    "distilled_dit": ("diffusion_models", "flux-2-klein-9b-fp8.safetensors"),
    "text_encoder": ("text_encoders", "qwen_3_8b.safetensors"),
    "vae": ("vae", "ae.safetensors"),
}

search_roots = [
    Path("/workspace/ComfyUI/models"),
    Path("/Comfy/models"),
    Path("/workspace/models"),
]

for key, (subfolder, filename) in model_map.items():
    current_val = prefs.get(key)
    if current_val and os.path.isfile(current_val):
        continue
    found_path = None
    for root in search_roots:
        candidate = root / subfolder / filename
        if candidate.is_file():
            found_path = str(candidate)
            break
        candidate_flat = root / filename
        if candidate_flat.is_file():
            found_path = str(candidate_flat)
            break
    if found_path:
        prefs[key] = found_path
    elif not prefs.get(key):
        prefs[key] = f"/workspace/ComfyUI/models/{subfolder}/{filename}"

prefs_file.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
print("[fizgig] updated prefs.json with ComfyUI shared models and loras output dir")
PY

  if is_fizgig_venv_healthy; then
    touch "$FIZGIG_VENV/.isolated_torch_linked"
    touch "$FIZGIG_DIR/.prepared"
    touch /tmp/fizgig_prep.done
    echo "[fizgig] preparation complete and verified." >> /fizgig_setup.log
  else
    echo "[fizgig][error] preparation finished but venv health check failed (missing dependencies)." >> /fizgig_setup.log
    rm -f "/tmp/fizgig_prep.done" "$FIZGIG_DIR/.prepared" "$FIZGIG_VENV/.isolated_torch_linked" "$lock_file" 2>/dev/null || true
    set -e
    return 1
  fi
  rm -f "$lock_file" 2>/dev/null || true
  set -e
  return 0
}

# --- Start JupyterLab and Filebrowser early ---
(
  cd /
  jupyter lab \
    --ip=0.0.0.0 \
    --port=8888 \
    --no-browser \
    --allow-root \
    --NotebookApp.allow_origin='*' \
    --ServerApp.token='' \
    --ServerApp.password='' \
    --FileContentsManager.preferred_dir=/workspace \
    --FileContentsManager.delete_to_trash=False \
    --ServerApp.terminado_settings='{"shell_command":["/bin/bash","-l"]}' \
    &> /jupyter.log &
)

FILEBROWSER_BRANDING_DIR="${FILEBROWSER_BRANDING_DIR:-/scripts/filebrowser_branding}"
if [[ -d "$FILEBROWSER_BRANDING_DIR" ]]; then
  echo "[filebrowser] custom branding enabled from $FILEBROWSER_BRANDING_DIR"
  FB_BRANDING_FILES="$FILEBROWSER_BRANDING_DIR" filebrowser --address=0.0.0.0 --port=8080 --root=/workspace/ --noauth &
else
  echo "[filebrowser][warn] branding directory not found: $FILEBROWSER_BRANDING_DIR"
  filebrowser --address=0.0.0.0 --port=8080 --root=/workspace/ --noauth &
fi
ensure_nginx_started || true

if [[ "${NGINX_STARTED:-0}" == "1" ]]; then
  echo "JupyterLab and Filebrowser started; nginx up."
else
  echo "JupyterLab and Filebrowser started; nginx failed to start."
fi

# --- Helper: update repo + pip if changed ---
update_and_install_requirements() {
  cd "$1" || return
  local old="$(git rev-parse HEAD 2>/dev/null || echo)"
  git pull --ff-only --quiet 2>/dev/null || git pull --ff-only 2>/dev/null || true
  local new="$(git rev-parse HEAD 2>/dev/null || echo)"
  if [[ "$old" != "$new" && -f requirements.txt ]]; then
    if git diff --quiet "$old" "$new" -- requirements.txt 2>/dev/null; then
      echo "[deps] requirements.txt unchanged in $(basename "$1"); skipping reinstall"
    else
      echo "[deps] requirements.txt updated in $(basename "$1"); reinstalling packages"
      pip_install_requirements "$VIRTUAL_ENV/bin/python" requirements.txt
    fi
  fi
}

update_comfyui_core() {
  local target_dir="$1"
  cd "$target_dir" || return
  local old="$(git rev-parse HEAD 2>/dev/null || echo)"

  echo "[updates] Fetching latest refs and tags for ComfyUI core..."
  git fetch --all --tags origin 2>/dev/null || git fetch --all --tags 2>/dev/null || true

  if [[ "${NIGHTLY_COMFYUI:-0}" == "1" ]]; then
    echo "[updates] NIGHTLY_COMFYUI=1; updating ComfyUI core to latest master commit"
    git checkout -B master origin/master 2>/dev/null || git checkout master 2>/dev/null || true
    git pull origin master --ff-only --quiet 2>/dev/null || git reset --hard origin/master 2>/dev/null || true
  else
    echo "[updates] NIGHTLY_COMFYUI=0 (default); ensuring ComfyUI core is on latest stable release tag"
    local latest_tag
    latest_tag="$(git tag -l 'v[0-9]*' | sort -V | tail -n 1 2>/dev/null || true)"
    if [[ -z "$latest_tag" ]]; then
      latest_tag="$(git tag -l 'v*' | sort -V | tail -n 1 2>/dev/null || true)"
    fi
    if [[ -n "$latest_tag" ]]; then
      echo "[updates] Checking out latest stable ComfyUI tag: ${latest_tag}"
      git checkout -f "$latest_tag" 2>/dev/null || true
    else
      echo "[updates][warn] Could not determine latest tag; keeping current version"
    fi
  fi

  local new="$(git rev-parse HEAD 2>/dev/null || echo)"
  if [[ -f requirements.txt ]]; then
    if [[ "$old" != "$new" ]]; then
      if git diff --quiet "$old" "$new" -- requirements.txt 2>/dev/null; then
        echo "[deps] requirements.txt unchanged in ComfyUI core; skipping reinstall"
      else
        echo "[deps] requirements.txt updated in ComfyUI core; installing packages"
        pip_install_requirements "$VIRTUAL_ENV/bin/python" requirements.txt || true
      fi
    else
      echo "[deps] ComfyUI core is already at commit ${new:0:7}; requirements check skipped"
    fi
  fi
}

migrate_manager_settings() {
  # Move legacy ComfyUI-Manager settings into the new shared __manager directory.
  local roots=("/workspace/ComfyUI" "/Comfy")
  for root in "${roots[@]}"; do
    local legacy="$root/user/default/ComfyUI-Manager"
    local target="$root/user/__manager"
    if [[ -d "$legacy" ]]; then
      mkdir -p "$target"
      rsync -a "$legacy"/ "$target"/
      rm -rf "$legacy"
      echo "[manager] migrated settings from $legacy to $target"
    fi
  done
}

cleanup_legacy_manager() {
  if [[ "${NEW_MANAGER:-0}" != "1" ]]; then
    echo "[manager] NEW_MANAGER!=1; keeping legacy manager nodes"
    return 0
  fi
  local roots=("/workspace/ComfyUI" "/Comfy")
  for root in "${roots[@]}"; do
    for name in "comfyui-manager" "ComfyUI-Manager"; do
      local target="$root/custom_nodes/$name"
      if [[ -d "$target" ]]; then
        rm -rf "$target"
        echo "[manager] removed legacy custom_nodes/$name"
      fi
    done
  done
}

ensure_manager_policy_files() {
  local roots=("/workspace/ComfyUI" "/Comfy")
  local rel_paths=("user/__manager")

  for root in "${roots[@]}"; do
    [[ -d "$root" ]] || continue
    for rel in "${rel_paths[@]}"; do
      local manager_dir="$root/$rel"
      local blacklist_file="$manager_dir/pip_blacklist.list"
      local auto_fix_file="$manager_dir/pip_auto_fix.list"
      local config_file="$manager_dir/config.ini"
      mkdir -p "$manager_dir"
      if [[ ! -f "$config_file" && -f "/Comfy/user/__manager/config.ini" ]]; then
        cp "/Comfy/user/__manager/config.ini" "$config_file"
      elif [[ ! -f "$config_file" && -f "/config.ini" ]]; then
        cp "/config.ini" "$config_file"
      fi

      MANAGER_BLACKLIST_FILE="$blacklist_file" python - <<'PY'
from pathlib import Path
import os

blacklist_path = Path(os.environ["MANAGER_BLACKLIST_FILE"])
required = [
    "torch",
    "torchvision",
    "torchaudio",
    "torchsde",
    "onnxruntime-gpu",
    "onnxruntime_gpu",
    "torchcodec",
    "nunchaku",
]

existing = []
seen = set()
if blacklist_path.exists():
    existing = blacklist_path.read_text(encoding="utf-8").splitlines()
    seen = {line.strip().lower() for line in existing if line.strip()}

for pkg in required:
    if pkg.lower() not in seen:
        existing.append(pkg)
        seen.add(pkg.lower())

blacklist_path.write_text(
    "".join(f"{line.rstrip()}\n" for line in existing if line.strip()),
    encoding="utf-8",
)
PY
      MANAGER_CONFIG_FILE="$config_file" MANAGER_USE_UV="${MANAGER_USE_UV:-0}" MANAGER_NIGHTLY_COMFYUI="${NIGHTLY_COMFYUI:-0}" python - <<'PY'
from pathlib import Path
import configparser
import os

config_path = Path(os.environ["MANAGER_CONFIG_FILE"])
desired_uv_raw = os.environ.get("MANAGER_USE_UV", "0").strip().lower()
desired_uv = "true" if desired_uv_raw in {"1", "true", "yes", "on"} else "false"

nightly_raw = os.environ.get("MANAGER_NIGHTLY_COMFYUI", "0").strip().lower()
desired_update_policy = "nightly-comfyui" if nightly_raw in {"1", "true", "yes", "on"} else "stable-comfyui"

config = configparser.ConfigParser(strict=False)
if config_path.exists():
    config.read(config_path, encoding="utf-8")

if "default" not in config:
    config["default"] = {}

changed = False
current_uv = config["default"].get("use_uv", "").strip().lower()
if current_uv != desired_uv:
    config["default"]["use_uv"] = desired_uv
    changed = True

current_policy = config["default"].get("update_policy", "").strip().lower()
if current_policy != desired_update_policy:
    config["default"]["update_policy"] = desired_update_policy
    changed = True

for key, val in [
    ("allow_flagged_nodepack_install", "true"),
    ("allow_git_url_install", "true"),
    ("allow_pip_install", "true"),
]:
    if config["default"].get(key, "").strip().lower() != val:
        config["default"][key] = val
        changed = True

if changed:
    with config_path.open("w", encoding="utf-8") as fh:
        config.write(fh)
    print(f"[manager] updated {config_path} (use_uv={desired_uv}, update_policy={desired_update_policy}, allow_flagged_nodepack_install=true)")
PY
      MANAGER_AUTO_FIX_FILE="$auto_fix_file" python - <<'PY'
from pathlib import Path
import os
import re

auto_fix_path = Path(os.environ["MANAGER_AUTO_FIX_FILE"])
if not auto_fix_path.exists():
    raise SystemExit(0)

protected = {
    "torch",
    "torchvision",
    "torchaudio",
    "torchsde",
    "onnxruntime-gpu",
    "onnxruntime_gpu",
    "torchcodec",
    "nunchaku",
}

pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
kept = []
removed = []

for raw in auto_fix_path.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if not stripped:
        continue
    if stripped.startswith("#"):
        kept.append(stripped)
        continue

    match = pattern.match(stripped)
    normalized = match.group(1).lower().replace("-", "_") if match else ""
    if "+" in stripped or normalized in protected:
        removed.append(stripped)
        continue

    kept.append(stripped)

if removed:
    auto_fix_path.write_text(
        "".join(f"{line}\n" for line in kept),
        encoding="utf-8",
    )
    print(f"[manager] removed incompatible pip_auto_fix entries from {auto_fix_path}")
PY
    done
  done
}

patch_legacy_manager_blacklist_guard() {
  local roots=("/workspace/ComfyUI" "/Comfy")
  for root in "${roots[@]}"; do
    [[ -d "$root" ]] || continue
    for manager_dir in "$root/custom_nodes/ComfyUI-Manager" "$root/custom_nodes/comfyui-manager"; do
      local prestartup_target="$manager_dir/prestartup_script.py"
      if [[ -f "$prestartup_target" ]]; then
        MANAGER_PRESTARTUP_SCRIPT="$prestartup_target" python - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["MANAGER_PRESTARTUP_SCRIPT"])
text = path.read_text(encoding="utf-8")

if "skip protected forced pip installation" in text:
    raise SystemExit(0)

needle = """                    if script[1] == \"#FORCE\":\n                        del script[1]\n                    else:\n                        if 'pip' in script[1:] and 'install' in script[1:] and is_installed(script[-1]):\n                            continue\n"""
replacement = """                    if script[1] == \"#FORCE\":\n                        del script[1]\n                        if 'pip' in script[1:] and 'install' in script[1:] and is_installed(script[-1]):\n                            print(f\"[ComfyUI-Manager] skip protected forced pip installation: '{script[-1]}'\")\n                            continue\n                    else:\n                        if 'pip' in script[1:] and 'install' in script[1:] and is_installed(script[-1]):\n                            continue\n"""

if needle not in text:
    raise SystemExit(0)

path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
print(f"[manager] patched protected forced pip installs in {path}")
PY
      fi

      local manager_core_target="$manager_dir/glob/manager_core.py"
      if [[ -f "$manager_core_target" ]]; then
        MANAGER_CORE_SCRIPT="$manager_core_target" python - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["MANAGER_CORE_SCRIPT"])
text = path.read_text(encoding="utf-8")
patched = False

needle_class = """                        clean_package_name = package_name.split('#')[0].strip()\n                        install_cmd = manager_util.make_pip_cmd([\"install\", clean_package_name])\n                        if clean_package_name != \"\" and not clean_package_name.startswith('#'):\n                            res = res and try_install_script(url, repo_path, install_cmd, instant_execution=instant_execution)\n"""
replacement_class = """                        clean_package_name = package_name.split('#')[0].strip()\n                        if clean_package_name != \"\" and not clean_package_name.startswith('#'):\n                            if is_blacklisted(clean_package_name):\n                                print(f\"[ComfyUI-Manager] skip blacklisted instant pip installation: '{clean_package_name}'\")\n                                continue\n                            install_cmd = manager_util.make_pip_cmd([\"install\", clean_package_name])\n                            res = res and try_install_script(url, repo_path, install_cmd, instant_execution=instant_execution)\n"""
if needle_class in text:
    text = text.replace(needle_class, replacement_class, 1)
    patched = True

needle_module = """                    if package_name and not package_name.startswith('#'):\n                        if '--index-url' in package_name:\n                            s = package_name.split('--index-url')\n                            install_cmd = manager_util.make_pip_cmd([\"install\", s[0].strip(), '--index-url', s[1].strip()])\n                        else:\n                            install_cmd = manager_util.make_pip_cmd([\"install\", package_name])\n\n                        if package_name.strip() != \"\" and not package_name.startswith('#'):\n                            try_install_script(url, repo_path, install_cmd, instant_execution=instant_execution)\n"""
replacement_module = """                    if package_name and not package_name.startswith('#'):\n                        clean_package_name = package_name.strip()\n                        if clean_package_name != \"\":\n                            if is_blacklisted(clean_package_name):\n                                print(f\"[ComfyUI-Manager] skip blacklisted instant pip installation: '{clean_package_name}'\")\n                                continue\n                            if '--index-url' in package_name:\n                                s = package_name.split('--index-url')\n                                install_cmd = manager_util.make_pip_cmd([\"install\", s[0].strip(), '--index-url', s[1].strip()])\n                            else:\n                                install_cmd = manager_util.make_pip_cmd([\"install\", package_name])\n\n                            try_install_script(url, repo_path, install_cmd, instant_execution=instant_execution)\n"""
if needle_module in text:
    text = text.replace(needle_module, replacement_module, 1)
    patched = True

if not patched:
    raise SystemExit(0)

path.write_text(text, encoding="utf-8")
print(f"[manager] patched instant pip blacklist guard in {path}")
PY
      fi
    done
  done
}

cleanup_legacy_manager_settings_dir() {
  local roots=("/workspace/ComfyUI" "/Comfy")
  for root in "${roots[@]}"; do
    [[ -d "$root" ]] || continue
    local new_dir="$root/user/__manager"
    local legacy_dir="$root/user/default/ComfyUI-Manager"
    if [[ -d "$new_dir" && -d "$legacy_dir" ]]; then
      rm -rf "$legacy_dir"
      echo "[manager] removed legacy manager settings dir at $legacy_dir"
    fi
  done
}

# # --- One-shot: pre_start (only if /Comfy exists) ---
# if [[ -d /Comfy ]]; then
#   if [[ -n "${COMFYUI_BACKUP-}" ]]; then
#     jq --arg v "$COMFYUI_BACKUP" \
#       '."downloaderbackup.repo_name" = $v' \
#       /Comfy/user/default/comfy.settings.json \
#       > /Comfy/user/default/tmp.settings.json
#     mv /Comfy/user/default/tmp.settings.json /Comfy/user/default/comfy.settings.json

#     rm -rf /tmp/comfy_course_tmp
#     git clone https://github.com/jnxmx/comfycourse_json.git /tmp/comfy_course_tmp
#     mkdir -p /Comfy/user/default/workflows
#     rm -rf /Comfy/user/default/workflows/comfy_course
#     mv /tmp/comfy_course_tmp /Comfy/user/default/workflows/comfy_course
#     rm -rf /Comfy/user/default/workflows/comfy_course/.git
#     echo "✅ comfy_course restored to /Comfy/user/default/workflows/comfy_course"
#   else
#     echo "COMFYUI_BACKUP not set/valid; skipping comfy_course restore."
#   fi
# else
#   echo "Second start (no /Comfy)."
# fi

## moved below backup restore to avoid being overwritten

# --- First boot: persist ComfyUI into /workspace and fetch taesd ---
if [[ ! -d /workspace/ComfyUI && -d /Comfy ]]; then
  image_comfy_head="$(current_image_comfy_head)"
  echo "Persisting ComfyUI to /workspace (first boot)…"
  mv /Comfy /workspace/ComfyUI
  write_comfy_image_sync_marker /workspace/ComfyUI "${image_comfy_head:-}"
  refresh_comfy_image_sync_manifest /workspace/ComfyUI || true
else
  echo "ComfyUI already present in /workspace; skipping copy."
  if [[ "${SYNC_IMAGE_BAKED_COMFY_CORE:-0}" == "1" ]]; then
    sync_image_baked_comfy_core
  fi
fi

if [[ -d /workspace/ComfyUI/models ]]; then
  if [[ ! -f /workspace/ComfyUI/models/vae_approx/taew2_1.safetensors && ! -f /workspace/ComfyUI/models/checkpoints/taew2_1.safetensors ]]; then
    mkdir -p /workspace/ComfyUI/models/vae_approx
    echo "Downloading TAE-W2.1 VAE approx model..."
    curl -fL --retry 3 \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/taew2_1.safetensors \
      -o /workspace/ComfyUI/models/vae_approx/taew2_1.safetensors || true
  fi
  if [[ ! -f /workspace/ComfyUI/models/vae_approx/taeh3.safetensors ]]; then
    mkdir -p /workspace/ComfyUI/models/vae_approx
    echo "Downloading TAE-H3 VAE approx model..."
    curl -fL --retry 3 \
      https://raw.githubusercontent.com/madebyollin/taehv/62f7591f59dfbb4c3c02b7a621d180a9eeaba26c/safetensors/taeh3.safetensors \
      -o /workspace/ComfyUI/models/vae_approx/taeh3.safetensors || true
  fi
  if [[ ! -f /workspace/ComfyUI/models/latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors ]]; then
    mkdir -p /workspace/ComfyUI/models/latent_upscale_models
    echo "Downloading Minimax H3 Latent Upscaler weights..."
    curl -fL --retry 3 \
      https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors \
      -o /workspace/ComfyUI/models/latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors || true
  fi
fi

migrate_manager_settings
cleanup_legacy_manager

# --- Venv create/activate ---
if [[ ! -d "$VIRTUAL_ENV" ]]; then
  echo "Creating venv at $VIRTUAL_ENV…"
  python3 -m venv "$VIRTUAL_ENV" --system-site-packages
else
  # Sanity check: if venv has a broken torch (e.g. CPU torch or mismatched cuDNN from a bad install on persisted volume),
  # clean shadowed packages in venv site-packages so it cleanly falls through to the base image CUDA 13 PyTorch.
  if ! "$VIRTUAL_ENV/bin/python" -c "import torch; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "[comfyui][warn] venv torch is broken or missing CUDA; cleaning shadowed venv packages to restore system CUDA 13 PyTorch..."
    for sp in "$VIRTUAL_ENV"/lib/python*/site-packages; do
      if [[ -d "$sp" ]]; then
        rm -rf "$sp"/torch* "$sp"/torchvision* "$sp"/torchaudio* "$sp"/nvidia* "$sp"/triton* 2>/dev/null || true
      fi
    done
  fi
fi
if [[ -f "$VIRTUAL_ENV/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VIRTUAL_ENV/bin/activate"
else
  echo "Warning: $VIRTUAL_ENV/bin/activate not found; continuing without activation."
fi
echo "VIRTUAL_ENV: ${VIRTUAL_ENV:-}"
echo "PATH: $PATH"

if [[ "${NEW_MANAGER:-0}" == "1" ]]; then
  echo "STAGE: Ensuring ComfyUI Manager (pip)"
  pip_install "$VIRTUAL_ENV/bin/python" --upgrade comfyui-manager
else
  echo "[manager] NEW_MANAGER!=1; skipping pip-installed Manager"
fi

ensure_manager_policy_files
cleanup_legacy_manager_settings_dir
patch_legacy_manager_blacklist_guard

# --- Install/build sageattention based on GPU architecture ---
SAGEATTENTION_VERSION="2.2.0"
SAGE_WHEEL_OUTPUT_DIR="/workspace/wheels"
ARCH=$(python3 - <<'EOF' || echo "none"
import torch
if not torch.cuda.is_available():
    print("none")
    exit(0)
cap = torch.cuda.get_device_capability()[0] * 10 + torch.cuda.get_device_capability()[1]
arch_map = {120: "sm120", 86: "sm86", 89: "sm89"}
print(arch_map.get(cap, "none"))
EOF
)

# Helper: download SageAttention source into a directory
# Usage: _sage_fetch_source <dest_dir>
_sage_fetch_source() {
  local DEST="$1"
  echo "Fetching SageAttention source repository..."

  # Method 1: Direct git clone
  if GIT_TERMINAL_PROMPT=0 git clone --depth 1 https://github.com/thu-ml/SageAttention.git "$DEST" 2>/dev/null; then
    echo "Successfully cloned SageAttention via git"
    return 0
  fi

  echo "[WARN] git clone failed, falling back to HTTPS archive download"

  # Method 2: Codeload tarball download
  rm -rf "${DEST:?}"/*
  local TGZ
  TGZ=$(mktemp /tmp/sageattention-XXXXXX.tar.gz)
  local URL="https://codeload.github.com/thu-ml/SageAttention/tar.gz/refs/heads/main"
  if curl -fsSL --max-time 300 -o "$TGZ" "$URL" && gzip -t "$TGZ" 2>/dev/null; then
    tar -xz -C "$DEST" --strip-components=1 -f "$TGZ"
    rm -f "$TGZ"
    echo "Successfully downloaded SageAttention via codeload tarball"
    return 0
  fi
  rm -f "$TGZ"

  # Method 3: GitHub archive tarball download
  rm -rf "${DEST:?}"/*
  TGZ=$(mktemp /tmp/sageattention-XXXXXX.tar.gz)
  URL="https://github.com/thu-ml/SageAttention/archive/refs/heads/main.tar.gz"
  if curl -fsSL --max-time 300 -L -o "$TGZ" "$URL" && gzip -t "$TGZ" 2>/dev/null; then
    tar -xz -C "$DEST" --strip-components=1 -f "$TGZ"
    rm -f "$TGZ"
    echo "Successfully downloaded SageAttention via github archive tarball"
    return 0
  fi
  rm -f "$TGZ"

  echo "[ERROR] All methods to download SageAttention source failed."
  return 1
}

_sage_patch_and_build_env() {
  local DEST="$1"
  local ARCH_TARGET="$2"

  # 1. Setup CUDA_HOME & PATHs based on PyTorch's CUDA version
  local PYTORCH_CUDA_VER=""
  PYTORCH_CUDA_VER="$("$VIRTUAL_ENV/bin/python" -c "import torch; print(torch.version.cuda if torch.cuda.is_available() else '')" 2>/dev/null || true)"
  
  local DETECTED_CUDA_HOME=""
  if [[ -n "$PYTORCH_CUDA_VER" && -d "/usr/local/cuda-${PYTORCH_CUDA_VER}" ]]; then
    DETECTED_CUDA_HOME="/usr/local/cuda-${PYTORCH_CUDA_VER}"
  elif [[ -d "/usr/local/cuda" ]]; then
    DETECTED_CUDA_HOME="/usr/local/cuda"
  elif command -v nvcc >/dev/null 2>&1; then
    DETECTED_CUDA_HOME="$(dirname "$(dirname "$(which nvcc)")")"
  fi

  if [[ -n "$DETECTED_CUDA_HOME" ]]; then
    export CUDA_HOME="$DETECTED_CUDA_HOME"
    export PATH="$CUDA_HOME/bin:$PATH"
    export LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${CUDA_HOME}/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"
    export CFLAGS="-I${CUDA_HOME}/include -I${CUDA_HOME}/targets/x86_64-linux/include ${CFLAGS:-}"
    export CXXFLAGS="-I${CUDA_HOME}/include -I${CUDA_HOME}/targets/x86_64-linux/include ${CXXFLAGS:-}"
    echo "[sage] Using CUDA_HOME: $CUDA_HOME"
  fi

  # 2. Patch source headers and setup.py (from Easy-Install reference scripts)
  "$VIRTUAL_ENV/bin/python" - "$DEST" <<'PY'
import os, sys

dest = sys.argv[1]
print(f"[sage] Patching SageAttention source files in {dest}...")

# Add missing C++ std headers
patch_files = [
    os.path.join(dest, 'csrc/fused/fused.cu'),
    os.path.join(dest, 'csrc/qattn/qk_int_sv_f16_cuda_sm80.cu'),
    os.path.join(dest, 'csrc/qattn/qk_int_sv_f8_cuda_sm89.cuh'),
]
for fpath in patch_files:
    if os.path.exists(fpath):
        with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        if '#include <vector>' not in content:
            new_content = '#include <vector>\n#include <typeindex>\n' + content
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"[sage] Added vector/typeindex headers to {os.path.basename(fpath)}")

# Patch setup.py C++ flags to use std=c++20 & ENABLE_BF16
setup_path = os.path.join(dest, 'setup.py')
if os.path.exists(setup_path):
    with open(setup_path, 'r', encoding='utf-8', errors='ignore') as f:
        setup_content = f.read()
    setup_content = setup_content.replace(
        'CXX_FLAGS = ["/O2", "/openmp"]',
        'CXX_FLAGS = ["/O2", "/openmp", "-std=c++20", "-DENABLE_BF16"]'
    )
    setup_content = setup_content.replace('-std=c++17', '-std=c++20')
    if '-I/usr/include' not in setup_content:
        setup_content = setup_content.replace(
            "'-O3',",
            "'-O3', '-I/usr/include', '-I/usr/local/cuda/include',"
        )
    with open(setup_path, 'w', encoding='utf-8') as f:
        f.write(setup_content)
    print("[sage] Patched setup.py flags to C++20")
PY

  # 3. Environment compilation flags
  CUDA_ARCH_VER=""
  case "$ARCH_TARGET" in
    sm120) CUDA_ARCH_VER="12.0" ;;
    sm89)  CUDA_ARCH_VER="8.9" ;;
    sm86)  CUDA_ARCH_VER="8.6" ;;
    *)     CUDA_ARCH_VER="" ;;
  esac

  export MAX_JOBS=4
  export NINJA_MAX_JOBS=4
  export EXT_PARALLEL=4
  export NVCC_APPEND_FLAGS="--threads 4"
  if [[ -n "$CUDA_ARCH_VER" ]]; then
    export TORCH_CUDA_ARCH_LIST="$CUDA_ARCH_VER"
  fi
}

if [[ "${MAKE_WHEELS:-0}" == "1" ]]; then
  echo "STAGE: Building sageattention wheel(s) (MAKE_WHEELS=1) [$SCRIPT_VERSION]"
  if [[ "$ARCH" == "none" ]]; then
    echo "[WARN] No supported GPU architecture detected; skipping sageattention wheel build."
  else
    echo "[deps] Ensuring build toolchain (build-essential, python3-dev, CUDA headers) is installed..."
    apt-get update -qq && (apt-get install -y --no-install-recommends build-essential python3-dev libcublas-dev libcusparse-dev -qq || apt-get install -y --no-install-recommends build-essential python3-dev -qq) 2>/dev/null || true
    mkdir -p "$SAGE_WHEEL_OUTPUT_DIR"
    TMP_SAGE_BUILD_DIR=$(mktemp -d /tmp/sageattention-src.XXXXXX)
    TMP_SAGE_WHEEL_DIR=$(mktemp -d /tmp/sageattention-out.XXXXXX)
    if _sage_fetch_source "$TMP_SAGE_BUILD_DIR"; then
      echo "Building sageattention wheel for $ARCH in $TMP_SAGE_WHEEL_DIR..."
      _sage_patch_and_build_env "$TMP_SAGE_BUILD_DIR" "$ARCH"

      if "$VIRTUAL_ENV/bin/python" -m pip wheel --no-deps --no-build-isolation --no-cache-dir "$TMP_SAGE_BUILD_DIR" -w "$TMP_SAGE_WHEEL_DIR"; then
        BUILT_WHEEL=$(find "$TMP_SAGE_WHEEL_DIR" -maxdepth 1 -type f -name "sageattention-*.whl" -print -quit 2>/dev/null || true)
        if [[ -n "$BUILT_WHEEL" ]]; then
          BASE_NAME=$(basename "$BUILT_WHEEL")
          VER_STR=$(echo "$BASE_NAME" | sed -E 's/^sageattention-([^-]+)-.*/\1/' || true)
          RENAMED_NAME="${BASE_NAME/sageattention-${VER_STR}-/sageattention-${VER_STR}+${ARCH}-}"
          TARGET_PATH="${SAGE_WHEEL_OUTPUT_DIR}/${RENAMED_NAME}"
          mv "$BUILT_WHEEL" "$TARGET_PATH"
          echo "Saved architecture-specific wheel to $TARGET_PATH"
          echo "Installing sageattention from built wheel"
          pip_install "$VIRTUAL_ENV/bin/python" --no-deps "$TARGET_PATH"
        else
          echo "[WARN] Wheel build succeeded but artifact missing; installing from source directly"
          pip_install "$VIRTUAL_ENV/bin/python" --no-build-isolation "$TMP_SAGE_BUILD_DIR"
        fi
      else
        echo "[WARN] Failed to build sageattention wheel; installing from source directly"
        pip_install "$VIRTUAL_ENV/bin/python" --no-build-isolation "$TMP_SAGE_BUILD_DIR"
      fi
      unset MAX_JOBS NINJA_MAX_JOBS EXT_PARALLEL NVCC_APPEND_FLAGS TORCH_CUDA_ARCH_LIST CUDA_HOME 2>/dev/null || true
    else
      echo "[WARN] Failed to download SageAttention source archive"
    fi
    rm -rf "${TMP_SAGE_BUILD_DIR:-}" "${TMP_SAGE_WHEEL_DIR:-}"
  fi
else
  if [ "$ARCH" != "none" ]; then
      echo "STAGE: Installing sageattention"
      WHEEL_FILE=$(find /wheels /workspace/wheels -type f -name "sageattention-*+${ARCH}-*.whl" 2>/dev/null | head -n 1 || true)
      if [ -n "$WHEEL_FILE" ]; then
          echo "Installing sageattention wheel for architecture $ARCH"
          pip_install "$VIRTUAL_ENV/bin/python" --no-deps "$WHEEL_FILE"
      else
          echo "No matching wheel found for architecture $ARCH, downloading and installing from source"
          if ! dpkg -s libcusparse-dev >/dev/null 2>&1; then
            echo "Installing libcusparse-dev dependency for CUDA extension compilation..."
            apt-get update -qq && apt-get install -y --no-install-recommends libcusparse-dev -qq || true
          fi
          TMP_CLONE_DIR=$(mktemp -d /tmp/sage-dl.XXXXXX)
          if _sage_fetch_source "$TMP_CLONE_DIR"; then
            _sage_patch_and_build_env "$TMP_CLONE_DIR" "$ARCH"
            pip_install "$VIRTUAL_ENV/bin/python" --no-build-isolation "$TMP_CLONE_DIR"
            unset MAX_JOBS NINJA_MAX_JOBS EXT_PARALLEL NVCC_APPEND_FLAGS TORCH_CUDA_ARCH_LIST CUDA_HOME 2>/dev/null || true
          fi
          rm -rf "$TMP_CLONE_DIR"
      fi
  else
      echo "STAGE: Installing sageattention"
      echo "Installing sageattention from source tarball"
      if ! dpkg -s libcusparse-dev >/dev/null 2>&1; then
        echo "Installing libcusparse-dev dependency for CUDA extension compilation..."
        apt-get update -qq && apt-get install -y --no-install-recommends libcusparse-dev -qq || true
      fi
      TMP_CLONE_DIR=$(mktemp -d /tmp/sage-dl.XXXXXX)
      if _sage_fetch_source "$TMP_CLONE_DIR"; then
        _sage_patch_and_build_env "$TMP_CLONE_DIR" "none"
        pip_install "$VIRTUAL_ENV/bin/python" --no-build-isolation "$TMP_CLONE_DIR"
        unset MAX_JOBS NINJA_MAX_JOBS EXT_PARALLEL NVCC_APPEND_FLAGS TORCH_CUDA_ARCH_LIST CUDA_HOME 2>/dev/null || true
      fi
      rm -rf "$TMP_CLONE_DIR"
  fi
fi

# --- Optional first-boot restore (external scripts) ---
echo "RESTORE_BACKUP: ${RESTORE_BACKUP:-}"
echo "COMFYUI_BACKUP: ${COMFYUI_BACKUP:-}"
RESTORE_JUST_RAN=0
if [[ "${RESTORE_BACKUP:-0}" == "1" && -n "${COMFYUI_BACKUP:-}" && -d "/workspace/ComfyUI" && ! -f "/workspace/.restore_done" ]]; then
  echo "[restore] first-boot restore enabled"
  echo "[INFO] First-boot restore from ${COMFYUI_BACKUP}…"
  pip_install "$VIRTUAL_ENV/bin/python" -q huggingface_hub PyYAML hf_transfer || true
  
  NEW_RESTORE_SCRIPT=""
  if [[ -d "/workspace/ComfyUI/custom_nodes" ]]; then
    NEW_RESTORE_SCRIPT="$(find /workspace/ComfyUI/custom_nodes -maxdepth 3 -name restore_backup.py -print -quit 2>/dev/null || true)"
  fi

  if [[ -n "$NEW_RESTORE_SCRIPT" && -f "$NEW_RESTORE_SCRIPT" ]]; then
    echo "[restore] Running background model restoration via restore_backup.py..."
    "$VIRTUAL_ENV/bin/python" "$NEW_RESTORE_SCRIPT" --only-models &> /restore.log &
    echo "[restore] Running foreground nodes & settings restoration via restore_backup.py..."
    "$VIRTUAL_ENV/bin/python" "$NEW_RESTORE_SCRIPT" --skip-models
  else
    echo "[restore][warn] Restore script (restore_backup.py) not found. Skipping restore."
  fi
  RESTORE_JUST_RAN=1
  touch /workspace/.restore_done
else
  echo "Backup restore skipped."
fi

# --- Restore comfy_course workflows when NOT in student mode ---
# Supports both pre-persist (/Comfy) and post-persist (/workspace/ComfyUI) layouts.
if [[ "${STUDENT_MODE:-0}" != "1" ]]; then
  target_user_dir=""
  if [[ -d "/workspace/ComfyUI/user/default" ]]; then
    target_user_dir="/workspace/ComfyUI/user/default"
  elif [[ -d "/Comfy/user/default" ]]; then
    target_user_dir="/Comfy/user/default"
  fi

  if [[ -n "$target_user_dir" ]]; then
    rm -rf /tmp/comfy_course_tmp
    # Avoid exiting the whole script if network is flaky; check directory before moving
    if git clone --depth=1 https://github.com/jnxmx/comfycourse_json.git /tmp/comfy_course_tmp 2>/dev/null; then
      mkdir -p "$target_user_dir/workflows"
      if [[ -d "$target_user_dir/workflows/comfy_course" ]]; then
        echo "[course] comfy_course present; replacing with latest"
        rm -rf "$target_user_dir/workflows/comfy_course"
      fi
      mv /tmp/comfy_course_tmp "$target_user_dir/workflows/comfy_course"
      rm -rf "$target_user_dir/workflows/comfy_course/.git"
      echo "[course] comfy_course restored to $target_user_dir/workflows/comfy_course"
    else
      echo "[course][warn] failed to clone comfycourse_json; skipping restore"
    fi
  else
    echo "[course] no Comfy user dir found; skipping comfy_course restore"
  fi
fi

# --- Always set downloaderbackup repo in comfy.settings.json when COMFYUI_BACKUP is provided ---
if [[ -n "${COMFYUI_BACKUP-}" ]]; then
  for settings in \
      /workspace/ComfyUI/user/default/comfy.settings.json \
      /Comfy/user/default/comfy.settings.json; do
    if [[ -f "$settings" ]]; then
      tmp="${settings}.tmp"
      jq --arg v "$COMFYUI_BACKUP" '."downloaderbackup.repo_name" = $v' "$settings" > "$tmp" && mv "$tmp" "$settings"
      echo "[settings] downloaderbackup.repo_name set in $settings"
    fi
  done
fi

# --- Set runpod shutdown timer setting in comfy.settings.json when env variable provided ---
if [[ "${DISABLE_SHUTDOWN_TIMER:-0}" == "1" || "${RUNPOD_DISABLE_SHUTDOWN_TIMER:-0}" == "1" || "${DISABLE_TIMER:-0}" == "1" || "${RUNPOD_TIMER_ENABLED:-1}" == "0" ]]; then
  export DISABLE_SHUTDOWN_TIMER=1
  export RUNPOD_DISABLE_SHUTDOWN_TIMER=1
  export RUNPOD_TIMER_ENABLED=0
  for settings in \
      /workspace/ComfyUI/user/default/comfy.settings.json \
      /Comfy/user/default/comfy.settings.json; do
    if [[ -f "$settings" ]]; then
      tmp="${settings}.tmp"
      jq '."runpod.enable_shutdown_timer" = false' "$settings" > "$tmp" && mv "$tmp" "$settings"
      echo "[settings] runpod.enable_shutdown_timer disabled in $settings"
    fi
  done
fi


# --- Update core + custom nodes when repos change (idempotent) ---
if [[ -d /workspace/ComfyUI ]]; then
  if [[ "${ENABLE_RUNTIME_REPO_UPDATES:-1}" != "1" ]]; then
    echo "[updates] runtime git/custom-node updates disabled; keeping persisted workspace versions"
  else
    # Even after restore, refresh preinstalled and runtime custom nodes to the latest version.
    echo "STAGE: Updating ComfyUI and custom nodes"
    update_comfyui_core /workspace/ComfyUI || true

    if command -v comfy >/dev/null 2>&1; then
      # Use cache mode to reduce remote registry fetches; hide noisy cm-cli banners
      tmp_log="$(mktemp)"
      if (cd /workspace/ComfyUI && run_with_manager_pip_env comfy --here --skip-prompt node update all --mode cache) >"$tmp_log" 2>&1; then
        comfy_status=0
      else
        comfy_status=$?
      fi
      grep -vE '^(Command: \[|Execute from: )' "$tmp_log" || true
      rm -f "$tmp_log"
      if [[ "$comfy_status" -ne 0 ]]; then
        echo "[nodes][warn] comfy CLI update failed; falling back to git updates"
        for d in /workspace/ComfyUI/custom_nodes/*/; do
          if [[ -d "$d/.git" ]]; then
            node_name="$(basename "$d")"
            echo "[nodes] refreshing ${node_name}"
            update_and_install_requirements "$d" || true
          fi
        done
      fi
    else
      for d in /workspace/ComfyUI/custom_nodes/*/; do
        if [[ -d "$d/.git" ]]; then
          node_name="$(basename "$d")"
          echo "[nodes] refreshing ${node_name}"
          update_and_install_requirements "$d" || true
        fi
      done
    fi
  fi
fi

echo "pod started"

# --- SSH key (optional) ---
if [[ -n "${PUBLIC_KEY:-}" ]]; then
  mkdir -p ~/.ssh
  chmod 700 ~/.ssh
  echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
  service ssh start
fi

# --- Main app supervision / switching ---
COMFY_APP_PID=""
FIZGIG_APP_PID=""

process_running() {
  is_pid_alive "${1:-}"
}

collect_descendants() {
  local root_pid="$1"
  local out=""
  local queue="$root_pid"
  while [[ -n "$queue" ]]; do
    local pid
    pid="$(echo "$queue" | awk '{print $1}')"
    queue="$(echo "$queue" | cut -d' ' -f2- )"
    local children
    children="$(ps -o pid= --ppid "$pid" 2>/dev/null | xargs)"
    if [[ -n "$children" ]]; then
      out="$out $children"
      queue="$queue $children"
    fi
  done
  echo "$out" | xargs
}

stop_process_group() {
  local pid_var_name="$1"
  local label="$2"
  local -n process_pid_ref="$pid_var_name"
  local pid="${process_pid_ref:-}"
  if ! process_running "$pid"; then
    process_pid_ref=""
    return 0
  fi
  echo "[switch] stopping $label (pid=$pid)"
  local pgid=""
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [[ -n "$pgid" && "$pgid" != "$pid" ]]; then
    echo "[switch] killing group pgid=$pgid for $label"
    kill -TERM -- "-$pgid" 2>/dev/null || true
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! process_running "$pid"; then
      break
    fi
    sleep 0.5
  done
  if process_running "$pid"; then
    local desc
    desc="$(collect_descendants "$pid" 2>/dev/null || true)"
    if [[ -n "$desc" ]]; then
      echo "[switch] also killing descendants of $pid: $desc"
      kill -TERM $desc 2>/dev/null || true
      sleep 0.5
    fi
  fi
  if process_running "$pid"; then
    echo "[switch][warn] forcing $label to exit"
    if [[ -n "$pgid" ]]; then
      kill -KILL -- "-$pgid" 2>/dev/null || true
    fi
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    local descf
    descf="$(collect_descendants "$pid" 2>/dev/null || true)"
    if [[ -n "$descf" ]]; then
      kill -KILL $descf 2>/dev/null || true
    fi
    sleep 0.5
  fi
  wait "$pid" 2>/dev/null || true
  process_pid_ref=""
}

clear_gpu_runtime_state() {
  echo "[switch] clearing GPU memory caches"
  python3 - <<'PY'
import gc
try:
    import torch
except Exception as exc:
    print(f"[switch][warn] torch cleanup skipped: {exc}")
    raise SystemExit(0)

gc.collect()
try:
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        try:
            torch.cuda.ipc_collect()
        except Exception:
            pass
        print("[switch] torch CUDA cache cleared")
except Exception as exc:
    print(f"[switch][warn] CUDA cache cleanup failed: {exc}")
PY
  sleep 1
}

launch_comfyui_runtime() {
  local pin_cores=""
  cd /workspace/ComfyUI || return 1
  # Ensure ComfyUI allows all hosts and origins for proxy compatibility
  export COMFYUI_ALLOWED_HOSTS="*"
  export COMFYUI_CORS_ALLOWED_ORIGINS="*"
  if [[ "${CPU_CONFIRM_REQUIRED:-0}" == "1" && ! -f "${CPU_CONTINUE_FILE:-/tmp/continue_cpu}" ]]; then
    echo "STAGE: Waiting for CPU confirmation"
    echo "[cpu] Waiting for user confirmation on :${CPU_WAIT_PORT:-8188} before launching ComfyUI..."
    while [[ ! -f "${CPU_CONTINUE_FILE:-/tmp/continue_cpu}" ]]; do
      sleep 1
    done
    echo "[cpu] User confirmation received; continuing startup."
  fi
  if [[ -f "${CPU_CONTINUE_FILE:-/tmp/continue_cpu}" ]]; then
    pin_cores="${CPU_PIN_CANDIDATE:-}"
  fi
  if [[ -n "${COMFY_PLACEHOLDER_PID:-}" ]]; then
    kill "${COMFY_PLACEHOLDER_PID}" 2>/dev/null || true
    sleep 0.5
  fi
  echo "STAGE: Starting ComfyUI"
  local comfy_args=(--listen 0.0.0.0 --port 8188 --enable-cors-header "*")
  if [[ "${NEW_MANAGER:-0}" == "1" ]]; then
    comfy_args+=(--enable-manager)
  fi
  if [[ "${DISABLE_PM:-0}" == "1" ]]; then
    comfy_args+=(--disable-pinned-memory)
  fi
  if [[ -n "$pin_cores" ]] && command -v taskset >/dev/null 2>&1; then
    if taskset -c "$pin_cores" true >/dev/null 2>&1; then
      echo "[cpu] Applying CPU affinity mask: $pin_cores"
      PIP_NO_COMPILE=1 COMFYUI_ALLOWED_HOSTS="*" COMFYUI_CORS_ALLOWED_ORIGINS="*" taskset -c "$pin_cores" "$VIRTUAL_ENV/bin/python" main.py "${comfy_args[@]}"
      return $?
    fi
    echo "[cpu][warn] Invalid CPU affinity mask '$pin_cores'; launching without taskset."
  fi
  PIP_NO_COMPILE=1 COMFYUI_ALLOWED_HOSTS="*" COMFYUI_CORS_ALLOWED_ORIGINS="*" "$VIRTUAL_ENV/bin/python" main.py "${comfy_args[@]}"
}

launch_fizgig_runtime() {
  if ! prepare_fizgig_runtime; then
    echo "[fizgig][error] prepare_fizgig_runtime failed; cannot start UI" >> /fizgig_setup.log
    return 1
  fi
  if [[ -n "${FIZGIG_PLACEHOLDER_PID:-}" ]]; then
    kill "${FIZGIG_PLACEHOLDER_PID}" 2>/dev/null || true
    sleep 0.5
    kill -9 "${FIZGIG_PLACEHOLDER_PID}" 2>/dev/null || true
  fi
  pkill -f "fizgig_wait_page.py.*${FIZGIG_UI_PORT}" 2>/dev/null || true
  pkill -f "app_switch_page.py.*${FIZGIG_UI_PORT}" 2>/dev/null || true
  sleep 0.5
  echo "[fizgig] starting web UI on :$FIZGIG_UI_PORT (PATH prefixed with venv)" >> /fizgig_setup.log
  cd "$FIZGIG_DIR" || return 1
  local hf_env=()
  if ! "$FIZGIG_VENV/bin/python" -c "import hf_transfer" 2>/dev/null; then
    hf_env=(-u HF_HUB_ENABLE_HF_TRANSFER)
  fi
  env -u VNC_PASSWORD -u FIZGIG_PASSWORD "${hf_env[@]}" \
    PATH="$FIZGIG_VENV/bin:$PATH" \
    VIRTUAL_ENV="$FIZGIG_VENV" \
    PYTHON="$FIZGIG_VENV/bin/python" \
    "$FIZGIG_VENV/bin/python" fizgig.py --web --port "$FIZGIG_UI_PORT" --host 127.0.0.1 >> /fizgig_ui.log 2>&1
}

export -f is_pid_alive
export -f is_fizgig_venv_healthy
export -f placeholder_running
export -f stop_placeholder_process
export -f pip_install
export -f pip_install_requirements
export -f prepare_fizgig_runtime
export -f launch_comfyui_runtime
export -f launch_fizgig_runtime
export CPU_CONFIRM_REQUIRED
export CPU_CONTINUE_FILE
export CPU_WAIT_PORT
export CPU_PIN_CANDIDATE
export VIRTUAL_ENV
export NEW_MANAGER
export DISABLE_PM
export FIZGIG_DIR
export FIZGIG_REPO_URL
export FIZGIG_REPO_REF
export FIZGIG_VENV
export FIZGIG_UI_PORT
export FIZGIG_UPDATE
export APP_STATE_DIR
export NIGHTLY_COMFYUI
export DISABLE_SHUTDOWN_TIMER
export RUNPOD_DISABLE_SHUTDOWN_TIMER
export RUNPOD_TIMER_ENABLED

spawn_app_session() {
  local command_text="$1"
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -lc "$command_text" &
  else
    bash -lc "$command_text" &
  fi
}

## detect_child_pid removed: rely on supervisor shell PID as group leader

start_comfy_app_process() {
  stop_placeholder_process COMFY_PLACEHOLDER_PID
  start_comfy_startup_placeholder || true
  ensure_nginx_started || true
  export COMFY_PLACEHOLDER_PID
  spawn_app_session 'launch_comfyui_runtime'
  COMFY_APP_PID=$!
  echo "[comfyui] supervisor child started (pid=$COMFY_APP_PID)"
  # brief check: if the process exits immediately, sleep to avoid fast respawn loops
  sleep 2
  if ! process_running "${COMFY_APP_PID:-}"; then
    echo "[comfyui][warn] comfy process exited immediately; backing off before restart" >> /placeholder.log 2>&1 || true
    sleep 5
  fi
}

start_fizgig_app_process() {
  stop_placeholder_process FIZGIG_PLACEHOLDER_PID
  start_fizgig_startup_placeholder || true
  ensure_nginx_started || true
  export FIZGIG_PLACEHOLDER_PID
  spawn_app_session 'launch_fizgig_runtime'
  FIZGIG_APP_PID=$!
  echo "[fizgig] supervisor child started (pid=$FIZGIG_APP_PID)"
  # brief check: if the process exits immediately, sleep to avoid fast respawn loops
  sleep 2
  if ! process_running "${FIZGIG_APP_PID:-}"; then
    echo "[fizgig][warn] fizgig process exited immediately; backing off before restart" >> /fizgig_setup.log 2>&1 || true
    if [[ -f /fizgig_ui.log ]]; then
      echo "[fizgig][error] last /fizgig_ui.log lines:" >> /fizgig_setup.log
      tail -n 25 /fizgig_ui.log >> /fizgig_setup.log 2>&1 || true
    fi
    sleep 5
  fi
}

active_app_placeholder_running() {
  local app_name="$1"
  case "$app_name" in
    comfyui)
      placeholder_running "${COMFY_PLACEHOLDER_PID:-}"
      ;;
    fizgig)
      placeholder_running "${FIZGIG_PLACEHOLDER_PID:-}"
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_selected_app_running() {
  local selected_app
  selected_app="$(normalize_app_name "$1")"
  case "$selected_app" in
    comfyui)
      start_fizgig_switch_placeholder || true
      if process_running "${COMFY_APP_PID:-}"; then
        if active_app_placeholder_running "$selected_app"; then
          set_app_runtime_state "comfyui" "comfyui" "starting-comfyui" "Starting ComfyUI"
        else
          set_app_runtime_state "comfyui" "comfyui" "running" "ComfyUI is the main app on :8188"
        fi
        return 0
      fi
      set_app_runtime_state "comfyui" "comfyui" "starting-comfyui" "Starting ComfyUI"
      start_comfy_app_process
      ;;
    fizgig)
      start_comfy_switch_placeholder || true
      if process_running "${FIZGIG_APP_PID:-}"; then
        if active_app_placeholder_running "$selected_app"; then
          set_app_runtime_state "fizgig" "fizgig" "starting-fizgig" "Starting Fizgig"
        else
          set_app_runtime_state "fizgig" "fizgig" "running" "Fizgig is the main app on :8081"
        fi
        return 0
      fi
      set_app_runtime_state "fizgig" "fizgig" "starting-fizgig" "Starting Fizgig"
      start_fizgig_app_process
      ;;
  esac
}

switch_to_app() {
  local target_app
  target_app="$(normalize_app_name "$1")"
  local current_app
  current_app="$(normalize_app_name "${2:-}")"

  if [[ "$current_app" == "$target_app" ]]; then
    ensure_selected_app_running "$target_app"
    return 0
  fi

  case "$current_app" in
    comfyui)
      set_app_runtime_state "comfyui" "$target_app" "stopping-comfyui" "Stopping ComfyUI before starting Fizgig"
      stop_process_group COMFY_APP_PID "ComfyUI"
      set_app_runtime_state "comfyui" "$target_app" "clearing-vram" "Clearing GPU memory after ComfyUI shutdown"
      clear_gpu_runtime_state
      start_comfy_switch_placeholder || true
      ;;
    fizgig)
      set_app_runtime_state "fizgig" "$target_app" "stopping-fizgig" "Stopping Fizgig before starting ComfyUI"
      stop_process_group FIZGIG_APP_PID "Fizgig"
      set_app_runtime_state "fizgig" "$target_app" "clearing-vram" "Clearing GPU memory after Fizgig shutdown"
      clear_gpu_runtime_state
      start_fizgig_switch_placeholder || true
      ;;
  esac

  case "$target_app" in
    comfyui)
      set_app_runtime_state "comfyui" "comfyui" "starting-comfyui" "Starting ComfyUI"
      start_comfy_app_process
      ;;
    fizgig)
      set_app_runtime_state "fizgig" "fizgig" "starting-fizgig" "Starting Fizgig"
      start_fizgig_app_process
      ;;
  esac
}

CURRENT_MAIN_APP="comfyui"
set_app_runtime_state "$CURRENT_MAIN_APP" "$CURRENT_MAIN_APP" "booting-comfyui" "ComfyUI is selected as the main app"
start_fizgig_switch_placeholder || true
switch_to_app "$CURRENT_MAIN_APP" ""

# If ComfyUI is the active app, prepare Fizgig runtime in the background
# so it is ready for instantaneous launch when switched.
if [[ "$CURRENT_MAIN_APP" != "fizgig" ]]; then
  echo "[fizgig] Triggering background preparation of Fizgig runtime..."
  prepare_fizgig_runtime &
fi

while true; do
  desired_app="$(normalize_app_name "$(read_state_value "$APP_DESIRED_FILE" "$CURRENT_MAIN_APP")")"
  if [[ "$desired_app" != "$CURRENT_MAIN_APP" ]]; then
    switch_to_app "$desired_app" "$CURRENT_MAIN_APP"
    CURRENT_MAIN_APP="$desired_app"
  else
    ensure_selected_app_running "$CURRENT_MAIN_APP"
  fi
  ensure_nginx_started || true
  sleep 2
done
