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

# Quiet noisy warnings and pip notices globally
export PYTHONWARNINGS="ignore"
export PIP_DISABLE_PIP_VERSION_CHECK="1"
export HF_HUB_DISABLE_TELEMETRY="1"
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
  COMFY_SYNC_ROOT="$root" COMFY_SYNC_MANIFEST="$(comfy_image_sync_manifest_path "$root")" python - <<'PY'
from pathlib import Path
import os
import subprocess

root = Path(os.environ["COMFY_SYNC_ROOT"])
manifest_path = Path(os.environ["COMFY_SYNC_MANIFEST"])
output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
tracked = sorted(
    entry.decode("utf-8")
    for entry in output.split(b"\x00")
    if entry
)
manifest_path.write_text("".join(f"{entry}\n" for entry in tracked), encoding="utf-8")
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
    comfyui|ai-toolkit)
      printf '%s\n' "$app_name"
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

placeholder_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

stop_placeholder_process() {
  local pid_var_name="$1"
  local -n placeholder_pid_ref="$pid_var_name"
  if placeholder_running "${placeholder_pid_ref:-}"; then
    kill "${placeholder_pid_ref}" 2>/dev/null || true
    sleep 0.5
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
    --source-app ai-toolkit \
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

# --- AI Toolkit isolated install/update + UI ---
AITK_DIR="${AITK_DIR:-/opt/ai-toolkit}"
AITK_REPO_DIR="${AITK_REPO_DIR:-$AITK_DIR/repo}"
AITK_VENV="${AITK_VENV:-$AITK_DIR/venv}"
AITK_DATA_DIR="${AITK_DATA_DIR:-/workspace/ai-toolkit}"
AITK_REPO_URL="${AITK_REPO_URL:-https://github.com/ostris/ai-toolkit.git}"
AITK_UI_PORT="${AITK_UI_PORT:-8675}"
AITK_UPDATE="${AITK_UPDATE:-1}"

# Temporary log page on AI Toolkit port until UI starts
start_ai_toolkit_startup_placeholder() {
  if placeholder_running "${AITK_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/ai_toolkit_wait_page.py \
    --port "$AITK_UI_PORT" --refresh 2 \
    --files /ai_toolkit_setup.log \
    >> /aitk_placeholder.log 2>&1 &
  export AITK_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! placeholder_running "${AITK_PLACEHOLDER_PID:-}"; then
    echo "[aitk placeholder][error] failed to start on :$AITK_UI_PORT" >> /ai_toolkit_setup.log
    return 1
  fi
  echo "[aitk placeholder] started on :$AITK_UI_PORT (pid=$AITK_PLACEHOLDER_PID)" >> /ai_toolkit_setup.log
}

start_ai_toolkit_switch_placeholder() {
  if placeholder_running "${AITK_PLACEHOLDER_PID:-}"; then
    return 0
  fi
  /usr/bin/env python3 /scripts/app_switch_page.py \
    --port "$AITK_UI_PORT" \
    --state-dir "$APP_STATE_DIR" \
    --target-app ai-toolkit \
    --source-app comfyui \
    >> /aitk_placeholder.log 2>&1 &
  export AITK_PLACEHOLDER_PID=$!
  sleep 0.2
  if ! placeholder_running "${AITK_PLACEHOLDER_PID:-}"; then
    echo "[aitk placeholder][error] failed to start switch page on :$AITK_UI_PORT" >> /ai_toolkit_setup.log
    return 1
  fi
  echo "[aitk placeholder] switch page started on :$AITK_UI_PORT (pid=$AITK_PLACEHOLDER_PID)" >> /ai_toolkit_setup.log
}

start_ai_toolkit_placeholder() {
  start_ai_toolkit_startup_placeholder "$@"
}

prepare_ai_toolkit_data_dirs() {
  mkdir -p "$AITK_DATA_DIR" "$AITK_DATA_DIR/datasets" "$AITK_DATA_DIR/output" "$AITK_DATA_DIR/data"
  if [[ -d "$AITK_REPO_DIR" ]]; then
    for name in output datasets data; do
      target="$AITK_REPO_DIR/$name"
      link="$AITK_DATA_DIR/$name"
      if [[ -e "$target" && ! -L "$target" ]]; then
        rm -rf "$target"
      fi
      ln -sfn "$link" "$target"
    done
    db_target="$AITK_DATA_DIR/aitk_db.db"
    db_link="$AITK_REPO_DIR/aitk_db.db"
    if [[ -f "$db_link" && ! -L "$db_link" ]]; then
      mv "$db_link" "$db_target" 2>/dev/null || true
    fi
    ln -sfn "$db_target" "$db_link"
  fi
}

ensure_ai_toolkit_settings() {
  local db="$AITK_DATA_DIR/aitk_db.db"
  if [[ ! -f "$db" ]]; then
    return 0
  fi
  AITK_DB="$db" \
  AITK_TRAINING_FOLDER="$AITK_DATA_DIR/output" \
  AITK_DATASETS_FOLDER="$AITK_DATA_DIR/datasets" \
  AITK_DATA_ROOT="$AITK_DATA_DIR/data" \
  python3 - <<'PY'
import os
import sqlite3

db = os.environ["AITK_DB"]
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Settings'")
if not cur.fetchone():
    conn.close()
    raise SystemExit(0)

settings = [
    ("TRAINING_FOLDER", os.environ["AITK_TRAINING_FOLDER"]),
    ("DATASETS_FOLDER", os.environ["AITK_DATASETS_FOLDER"]),
    ("DATA_ROOT", os.environ["AITK_DATA_ROOT"]),
]
for key, value in settings:
    cur.execute("INSERT OR REPLACE INTO Settings(key, value) VALUES (?, ?)", (key, value))
conn.commit()
conn.close()
PY
}

prepare_ai_toolkit_runtime() {
  set +e
  local pip_constraint="/etc/pip/constraints.txt"
  echo "[ai-toolkit] setting up in $AITK_DIR" > /ai_toolkit_setup.log
  mkdir -p "$AITK_DIR"

  if [[ ! -d "$AITK_REPO_DIR/.git" ]]; then
    echo "[ai-toolkit] cloning repo." >> /ai_toolkit_setup.log
    git clone "$AITK_REPO_URL" "$AITK_REPO_DIR" >> /ai_toolkit_setup.log 2>&1 || true
    latest_tag=$(git -C "$AITK_REPO_DIR" tag -l 'v[0-9]*' | sort -V | tail -n 1)
    if [[ -n "$latest_tag" ]]; then
      echo "[ai-toolkit] checking out stable tag: $latest_tag" >> /ai_toolkit_setup.log
      git -C "$AITK_REPO_DIR" checkout -f "$latest_tag" >> /ai_toolkit_setup.log 2>&1 || true
    fi
  elif [[ "${AITK_UPDATE:-0}" == "1" ]]; then
    echo "[ai-toolkit] updating repo to latest stable tag." >> /ai_toolkit_setup.log
    git -C "$AITK_REPO_DIR" fetch --tags >> /ai_toolkit_setup.log 2>&1 || true
    latest_tag=$(git -C "$AITK_REPO_DIR" tag -l 'v[0-9]*' | sort -V | tail -n 1)
    if [[ -n "$latest_tag" ]]; then
      git -C "$AITK_REPO_DIR" checkout -f "$latest_tag" >> /ai_toolkit_setup.log 2>&1 || true
    else
      git -C "$AITK_REPO_DIR" pull --ff-only >> /ai_toolkit_setup.log 2>&1 || true
    fi
  fi

  prepare_ai_toolkit_data_dirs

  deps_marker="$AITK_DIR/.deps_installed"
  if [[ ! -d "$AITK_VENV" ]]; then
    echo "[ai-toolkit] creating venv at $AITK_VENV" >> /ai_toolkit_setup.log
    python3 -m venv "$AITK_VENV" >> /ai_toolkit_setup.log 2>&1 || true
    rm -f "$deps_marker"
  fi

  if [[ ! -f "$deps_marker" ]]; then
    if [[ -f "$pip_constraint" ]]; then
      PIP_CONSTRAINT="$pip_constraint" pip_install "$AITK_VENV/bin/python" --upgrade pip wheel setuptools >> /ai_toolkit_setup.log 2>&1 || true
    else
      pip_install "$AITK_VENV/bin/python" --upgrade pip wheel setuptools >> /ai_toolkit_setup.log 2>&1 || true
    fi
    torch_marker="$AITK_DIR/.torch_installed"
    AITK_TORCH_INDEX_URL="${AITK_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"
    torch_ok=0
    if "$AITK_VENV/bin/python" -c "import torch, torchvision, torchaudio" >/dev/null 2>&1; then
      torch_ok=1
    fi
    sys_torch_versions="$(python3 -c "import torch, torchvision, torchaudio; print(torch.__version__); print(torchvision.__version__); print(torchaudio.__version__)" 2>/dev/null)" || sys_torch_versions=""
    sys_torch_ver=""
    sys_torchvision_ver=""
    sys_torchaudio_ver=""
    if [[ -n "$sys_torch_versions" ]]; then
      IFS=$'\n' read -r sys_torch_ver sys_torchvision_ver sys_torchaudio_ver <<< "$sys_torch_versions"
    fi
    desired_torch_spec="${sys_torch_ver}|${sys_torchvision_ver}|${sys_torchaudio_ver}"
    if [[ "$torch_ok" -ne 1 ]] || [[ -z "$sys_torch_ver" ]] || [[ -z "$sys_torchvision_ver" ]] || [[ -z "$sys_torchaudio_ver" ]] || [[ ! -f "$torch_marker" ]] || [[ "$(cat "$torch_marker")" != "$desired_torch_spec" ]]; then
      if [[ -n "$sys_torch_ver" && -n "$sys_torchvision_ver" && -n "$sys_torchaudio_ver" ]]; then
        echo "[ai-toolkit] installing torch ${desired_torch_spec}" >> /ai_toolkit_setup.log
        pip_install "$AITK_VENV/bin/python" --index-url "$AITK_TORCH_INDEX_URL" \
          "torch==${sys_torch_ver}" "torchvision==${sys_torchvision_ver}" "torchaudio==${sys_torchaudio_ver}" \
          >> /ai_toolkit_setup.log 2>&1 || true
        echo "$desired_torch_spec" > "$torch_marker"
      else
        echo "[ai-toolkit][warn] system torch versions unknown; installing latest torch from index" >> /ai_toolkit_setup.log
        pip_install "$AITK_VENV/bin/python" --index-url "$AITK_TORCH_INDEX_URL" torch torchvision torchaudio \
          >> /ai_toolkit_setup.log 2>&1 || true
      fi
    fi

    if [[ -f "$AITK_REPO_DIR/requirements.txt" ]]; then
      echo "[ai-toolkit] installing requirements (excluding torch/vision/audio to keep your version)." >> /ai_toolkit_setup.log
      tmp_req="$AITK_DIR/requirements.no-torch.txt"
      grep -Ev '^(torch|torchvision|torchaudio)($|[<>=])' "$AITK_REPO_DIR/requirements.txt" > "$tmp_req" || cp "$AITK_REPO_DIR/requirements.txt" "$tmp_req"
      if [[ -f "$pip_constraint" ]]; then
        PIP_CONSTRAINT="$pip_constraint" pip_install_requirements "$AITK_VENV/bin/python" "$tmp_req" >> /ai_toolkit_setup.log 2>&1 || true
        PIP_CONSTRAINT="$pip_constraint" pip_install "$AITK_VENV/bin/python" torchsde trampoline >> /ai_toolkit_setup.log 2>&1 || true
      else
        pip_install_requirements "$AITK_VENV/bin/python" "$tmp_req" >> /ai_toolkit_setup.log 2>&1 || true
        pip_install "$AITK_VENV/bin/python" torchsde trampoline >> /ai_toolkit_setup.log 2>&1 || true
      fi
    fi

    touch "$deps_marker"
  else
    echo "[ai-toolkit] deps already installed; skipping" >> /ai_toolkit_setup.log
  fi
  if ! "$AITK_VENV/bin/python" -c "import mediapipe as mp; raise SystemExit(0 if hasattr(mp, 'solutions') else 1)" >/dev/null 2>&1; then
    echo "[ai-toolkit][warn] mediapipe missing solutions; patching controlnet_aux" >> /ai_toolkit_setup.log
    "$AITK_VENV/bin/python" -c $'import site\nfrom pathlib import Path\nfor base in site.getsitepackages():\n    path = Path(base) / "controlnet_aux" / "__init__.py"\n    if not path.is_file():\n        continue\n    text = path.read_text(encoding="utf-8")\n    if "skip mediapipe when unavailable" in text:\n        break\n    target = "from .mediapipe_face import MediapipeFaceDetector"\n    if target in text:\n        replacement = (\n            "try:\\n"\n            "    from .mediapipe_face import MediapipeFaceDetector\\n"\n            "except Exception:\\n"\n            "    MediapipeFaceDetector = None  # skip mediapipe when unavailable\\n"\n        )\n        text = text.replace(target, replacement)\n        path.write_text(text, encoding="utf-8")\n    break\n'
  fi
  # Build and run UI (Node)
  if [[ -f "$AITK_REPO_DIR/ui/package.json" ]]; then
    local ui_dir="$AITK_REPO_DIR/ui"
    if [[ ! -d "$ui_dir/node_modules" ]]; then
      echo "[ai-toolkit] installing UI dependencies (incl. dev) ..." >> /ai_toolkit_setup.log
      (cd "$ui_dir" && npm install --include=dev >> /ai_toolkit_setup.log 2>&1)
    fi
    local db_path="$AITK_DATA_DIR/aitk_db.db"
    if [[ ! -f "$db_path" || "${AITK_FORCE_DB_UPDATE:-0}" == "1" ]]; then
      echo "[ai-toolkit] updating DB (prisma) ..." >> /ai_toolkit_setup.log
      (cd "$ui_dir" && npm run update_db >> /ai_toolkit_setup.log 2>&1)
    fi
    if [[ ! -d "$ui_dir/.next" ]]; then
      echo "[ai-toolkit] building UI..." >> /ai_toolkit_setup.log
      (cd "$ui_dir" && npm run build >> /ai_toolkit_setup.log 2>&1)
    fi
    ensure_ai_toolkit_settings
  else
    echo "[ai-toolkit] ui/package.json not found; skipping UI start" >> /ai_toolkit_setup.log
    set -e
    return 1
  fi
  # restore -e for the rest of the script
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
    echo "[deps] Ensuring core requirements are up to date for ${target_dir}..."
    pip_install_requirements "$VIRTUAL_ENV/bin/python" requirements.txt --upgrade || true
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

if changed:
    with config_path.open("w", encoding="utf-8") as fh:
        config.write(fh)
    print(f"[manager] updated {config_path} (use_uv={desired_uv}, update_policy={desired_update_policy})")
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
  if [[ ! -f /Comfy/models/checkpoints/taew2_1.safetensors ]]; then
    mkdir -p /Comfy/models/vae_approx
    curl -L \
      https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/taew2_1.safetensors \
      -o /Comfy/models/vae_approx/taew2_1.safetensors
  fi
  echo "Persisting ComfyUI to /workspace (first boot)…"
  mv /Comfy /workspace/ComfyUI
  write_comfy_image_sync_marker /workspace/ComfyUI "${image_comfy_head:-}"
  refresh_comfy_image_sync_manifest /workspace/ComfyUI
else
  echo "ComfyUI already present in /workspace; skipping copy."
  if [[ "${SYNC_IMAGE_BAKED_COMFY_CORE:-0}" == "1" ]]; then
    sync_image_baked_comfy_core
  fi
fi

migrate_manager_settings
cleanup_legacy_manager

# --- Venv create/activate ---
if [[ ! -d "$VIRTUAL_ENV" ]]; then
  echo "Creating venv at $VIRTUAL_ENV…"
  python3 -m venv "$VIRTUAL_ENV" --system-site-packages
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
ARCH=$(python3 - <<'EOF'
import torch
if not torch.cuda.is_available():
    print("none")
    exit(0)
cap = torch.cuda.get_device_capability()[0] * 10 + torch.cuda.get_device_capability()[1]
arch_map = {120: "sm120", 86: "sm86", 89: "sm89"}
print(arch_map.get(cap, "none"))
EOF
)

if [[ "${MAKE_WHEELS:-0}" == "1" ]]; then
  echo "STAGE: Building sageattention wheel(s) (MAKE_WHEELS=1)"
  if [[ "$ARCH" == "none" ]]; then
    echo "[WARN] No supported GPU architecture detected; skipping sageattention wheel build."
  else
    mkdir -p "$SAGE_WHEEL_OUTPUT_DIR"
    TMP_SAGE_DIR=$(mktemp -d /tmp/sageattention-build.XXXXXX)
    echo "Building sageattention wheel from git repository into $TMP_SAGE_DIR"
    if pip wheel --no-deps --no-build-isolation --no-cache-dir "git+https://github.com/sageattention/SageAttention.git" -w "$TMP_SAGE_DIR"; then
      BUILT_WHEEL=$(find "$TMP_SAGE_DIR" -maxdepth 1 -type f -name "sageattention-*.whl" -print -quit)
      if [[ -n "$BUILT_WHEEL" ]]; then
        BASE_NAME=$(basename "$BUILT_WHEEL")
        VER_STR=$(echo "$BASE_NAME" | sed -E 's/sageattention-([0-9\.]+)-.*/\1/')
        RENAMED_NAME="${BASE_NAME/sageattention-${VER_STR}-/sageattention-${VER_STR}+${ARCH}-}"
        TARGET_PATH="${SAGE_WHEEL_OUTPUT_DIR}/${RENAMED_NAME}"
        mv "$BUILT_WHEEL" "$TARGET_PATH"
        echo "Saved architecture-specific wheel to $TARGET_PATH"
        echo "Installing sageattention from built wheel"
        pip_install "$VIRTUAL_ENV/bin/python" --no-deps "$TARGET_PATH"
      else
        echo "[WARN] Wheel build succeeded but artifact missing; installing from git directly"
        pip_install "$VIRTUAL_ENV/bin/python" "git+https://github.com/sageattention/SageAttention.git"
      fi
    else
      echo "[WARN] Failed to build sageattention wheel; installing from git directly"
      pip_install "$VIRTUAL_ENV/bin/python" "git+https://github.com/sageattention/SageAttention.git"
    fi
    rm -rf "${TMP_SAGE_DIR:-}"
  fi
else
  if [ "$ARCH" != "none" ]; then
      echo "STAGE: Installing sageattention"
      WHEEL_FILE=$(find /wheels /workspace/wheels -type f -name "sageattention-*+${ARCH}-*.whl" 2>/dev/null | head -n 1)
      if [ -n "$WHEEL_FILE" ]; then
          echo "Installing sageattention wheel for architecture $ARCH"
          pip_install "$VIRTUAL_ENV/bin/python" --no-deps "$WHEEL_FILE"
      else
          echo "No matching wheel found for architecture $ARCH, installing from git"
          pip_install "$VIRTUAL_ENV/bin/python" "git+https://github.com/sageattention/SageAttention.git"
      fi
  else
      echo "STAGE: Installing sageattention"
      echo "Installing sageattention from git"
      pip_install "$VIRTUAL_ENV/bin/python" "git+https://github.com/sageattention/SageAttention.git"
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
AITK_APP_PID=""

process_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
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

launch_ai_toolkit_runtime() {
  if ! prepare_ai_toolkit_runtime; then
    return 1
  fi
  if [[ -n "${AITK_PLACEHOLDER_PID:-}" ]]; then
    kill "${AITK_PLACEHOLDER_PID}" 2>/dev/null || true
    sleep 0.5
  fi
  local ui_dir="$AITK_REPO_DIR/ui"
  echo "[ai-toolkit] starting UI on :$AITK_UI_PORT (PATH prefixed with venv)" >> /ai_toolkit_setup.log
  cd "$ui_dir" || return 1
  # Use npm run start but override concurrently's infinite restart with --restart-tries 0
  # This prevents ai-toolkit from auto-respawning when killed
  env \
    PATH="$AITK_VENV/bin:$PATH" \
    VIRTUAL_ENV="$AITK_VENV" \
    PYTHON="$AITK_VENV/bin/python" \
    PORT="$AITK_UI_PORT" \
    npx concurrently --restart-tries 0 -n WORKER,UI "node dist/cron/worker.js" "next start --port $AITK_UI_PORT" >> /ai_toolkit_ui.log 2>&1
}

export -f pip_install
export -f pip_install_requirements
export -f prepare_ai_toolkit_data_dirs
export -f ensure_ai_toolkit_settings
export -f prepare_ai_toolkit_runtime
export -f launch_comfyui_runtime
export -f launch_ai_toolkit_runtime
export CPU_CONFIRM_REQUIRED
export CPU_CONTINUE_FILE
export CPU_WAIT_PORT
export CPU_PIN_CANDIDATE
export VIRTUAL_ENV
export NEW_MANAGER
export DISABLE_PM
export AITK_DIR
export AITK_REPO_DIR
export AITK_VENV
export AITK_DATA_DIR
export AITK_REPO_URL
export AITK_UI_PORT
export AITK_UPDATE
export APP_STATE_DIR
export NIGHTLY_COMFYUI

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

start_ai_toolkit_app_process() {
  stop_placeholder_process AITK_PLACEHOLDER_PID
  start_ai_toolkit_startup_placeholder || true
  ensure_nginx_started || true
  export AITK_PLACEHOLDER_PID
  spawn_app_session 'launch_ai_toolkit_runtime'
  AITK_APP_PID=$!
  echo "[ai-toolkit] supervisor child started (pid=$AITK_APP_PID)"
  # brief check: if the process exits immediately, sleep to avoid fast respawn loops
  sleep 2
  if ! process_running "${AITK_APP_PID:-}"; then
    echo "[ai-toolkit][warn] aitk process exited immediately; backing off before restart" >> /ai_toolkit_setup.log 2>&1 || true
    sleep 5
  fi
}

active_app_placeholder_running() {
  local app_name="$1"
  case "$app_name" in
    comfyui)
      placeholder_running "${COMFY_PLACEHOLDER_PID:-}"
      ;;
    ai-toolkit)
      placeholder_running "${AITK_PLACEHOLDER_PID:-}"
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
      start_ai_toolkit_switch_placeholder || true
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
    ai-toolkit)
      start_comfy_switch_placeholder || true
      if process_running "${AITK_APP_PID:-}"; then
        if active_app_placeholder_running "$selected_app"; then
          set_app_runtime_state "ai-toolkit" "ai-toolkit" "starting-ai-toolkit" "Starting AI Toolkit"
        else
          set_app_runtime_state "ai-toolkit" "ai-toolkit" "running" "AI Toolkit is the main app on :8675"
        fi
        return 0
      fi
      set_app_runtime_state "ai-toolkit" "ai-toolkit" "starting-ai-toolkit" "Starting AI Toolkit"
      start_ai_toolkit_app_process
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
      set_app_runtime_state "comfyui" "$target_app" "stopping-comfyui" "Stopping ComfyUI before starting AI Toolkit"
      stop_process_group COMFY_APP_PID "ComfyUI"
      set_app_runtime_state "comfyui" "$target_app" "clearing-vram" "Clearing GPU memory after ComfyUI shutdown"
      clear_gpu_runtime_state
      start_comfy_switch_placeholder || true
      ;;
    ai-toolkit)
      set_app_runtime_state "ai-toolkit" "$target_app" "stopping-ai-toolkit" "Stopping AI Toolkit before starting ComfyUI"
      stop_process_group AITK_APP_PID "AI Toolkit"
      set_app_runtime_state "ai-toolkit" "$target_app" "clearing-vram" "Clearing GPU memory after AI Toolkit shutdown"
      clear_gpu_runtime_state
      start_ai_toolkit_switch_placeholder || true
      ;;
  esac

  case "$target_app" in
    comfyui)
      set_app_runtime_state "comfyui" "comfyui" "starting-comfyui" "Starting ComfyUI"
      start_comfy_app_process
      ;;
    ai-toolkit)
      set_app_runtime_state "ai-toolkit" "ai-toolkit" "starting-ai-toolkit" "Starting AI Toolkit"
      start_ai_toolkit_app_process
      ;;
  esac
}

CURRENT_MAIN_APP="comfyui"
set_app_runtime_state "$CURRENT_MAIN_APP" "$CURRENT_MAIN_APP" "booting-comfyui" "ComfyUI is selected as the main app"
start_ai_toolkit_switch_placeholder || true
switch_to_app "$CURRENT_MAIN_APP" ""

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
