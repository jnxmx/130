# RunPod Docker Template: Comprehensive Architecture & Bug Resolve Hooks

## Executive Summary

This repository contains a high-performance, resilient **RunPod Docker Template** designed for running **ComfyUI** and **Ostris AI Toolkit** side-by-side in cloud GPU container environments. 

Rather than serving as a basic container, this template acts as an autonomous operating environment. It solves major operational challenges inherent to cloud GPU providers (such as broken host GPU drivers, CPU steal/overload, VRAM fragmentation between apps, custom node dependency conflicts, and long silent startup times) through automated preflight probes, dependency constraints, process group supervision, and interactive UI status placeholders.

---

## 1. High-Level Architecture Overview

```mermaid
flowchart TD
    User([User Browser]) -->|Port 7860| NginxComfy[Nginx Port 7860: ComfyUI Proxy]
    User -->|Port 7861| NginxFB[Nginx Port 7861: Filebrowser Proxy]
    User -->|Port 7862| NginxAITK[Nginx Port 7862: AI Toolkit Proxy]
    User -->|Port 7863| NginxJup[Nginx Port 7863: JupyterLab Proxy]

    NginxComfy -->|Reverse Proxy| ComfyPort{Port 8188}
    NginxFB -->|Reverse Proxy| FBPort[Filebrowser :8080]
    NginxAITK -->|Reverse Proxy| AITKPort{Port 8675}
    NginxJup -->|Reverse Proxy| JupPort[JupyterLab :8888]

    ComfyPort -->|Active App| ComfyApp[ComfyUI Core / PyTorch]
    ComfyPort -.->|Booting/Switching| ComfyWait[comfy_wait_page.py / app_switch_page.py]

    AITKPort -->|Active App| AITKApp[AI Toolkit Next.js UI + Worker]
    AITKPort -.->|Booting/Switching| AITKWait[ai_toolkit_wait_page.py / app_switch_page.py]

    subgraph Supervisor ["start.sh Supervisor Loop"]
        CUDA_Probe[1. CUDA Preflight Probe] --> CPU_Probe[2. CPU Overload Guard]
        CPU_Probe --> Early_Services[3. Jupyter & Filebrowser Launch]
        Early_Services --> Core_Sync[4. Workspace / Image Core Sync]
        Core_Sync --> Wheel_Select[5. SageAttention Wheel Selector]
        Wheel_Select --> Switch_Loop[6. Dynamic App Switcher Loop]
    end

    subgraph Storage ["Persistent Volume (/workspace)"]
        WS_Comfy[/workspace/ComfyUI]
        WS_AITK[/workspace/ai-toolkit]
        WS_Cache[/workspace/.cache]
    end

    ComfyApp --> WS_Comfy
    AITKApp --> WS_AITK
    ComfyApp --> WS_Cache
```

---

## 2. Dynamic Dual-App Supervision & VRAM Isolation

Running multiple heavy PyTorch workloads simultaneously in a single GPU container causes Out-Of-Memory (OOM) crashes. To avoid this, the container implements a **single-active-app supervisor loop** in [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh).

### Key Mechanics:
1. **Single App Execution**: Only one main GPU application—either **ComfyUI** (port 8188) or **AI Toolkit** (port 8675)—runs at any given moment.
2. **State Directory (`/tmp/app_state`)**:
   - `active_app`: Indicates the app currently initialized and assigned GPU access.
   - `desired_app`: Set by user interaction on the UI switch page.
   - `phase` & `detail`: Human-readable status details consumed by proxy placeholders.
3. **Graceful App Switching Routine (`switch_to_app`)**:
   When switching from ComfyUI to AI Toolkit (or vice versa):
   - **Process Termination**: Sends SIGTERM to the process group, followed by recursive descendant child PID cleanup and SIGKILL fallback if unresponded after 10 seconds.
   - **VRAM Cache Flush (`clear_gpu_runtime_state`)**: Executes a Python snippet calling `gc.collect()`, `torch.cuda.empty_cache()`, and `torch.cuda.ipc_collect()` to liberate VRAM.
   - **Placeholder Server Activation**: Spins up [app_switch_page.py](file:///Users/jnx/Documents/GitHub/130/scripts/app_switch_page.py) on the targeted port so the user sees a smooth status screen instead of a broken connection page.
   - **Target App Launch**: Launches the newly requested application.

---

## 3. System Bug Resolve Hooks & Resilience Guards

The template includes multiple defensive hooks designed to mitigate common failure modes in cloud container environments.

```
+-----------------------------------------------------------------------------------+
|                           SYSTEM BUG RESOLVE HOOKS                               |
+-----------------------------------+-----------------------------------------------+
| Problem / Failure Mode            | Automated Resolve Hook                        |
+-----------------------------------+-----------------------------------------------+
| Broken Host CUDA Driver / PyTorch | cuda_probe_json (6-attempt preflight probe)   |
| Degraded Host / CPU Steal (>95%)  | check_cpu_overload + taskset affinity pin     |
| Custom Node Dependency Overwrite  | /etc/pip/constraints.txt (Package Locking)    |
| GPU Arch Incompatibility (SageAttn)| Capability Probe -> SM86 / SM89 / SM120 Whl   |
| Missing Mediapipe in Python 3.12  | Runtime AST Patching in controlnet_aux        |
| Proxy CORS & Host Header Rejection| Nginx Dynamic Host/Origin Header Rewriting    |
| Unresponsive Child Processes      | Process Group (PGID) + Descendant Killing     |
| Image Core Updates vs Persisted WS| Core File Sync Manifest (.image_baked_core)   |
+-----------------------------------+-----------------------------------------------+
```

### Hook A: CUDA Preflight Probe (`cuda_probe_json`)
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L284-L317)
- **Problem**: Host machines on cloud providers occasionally supply broken NVIDIA drivers or mismatched kernel modules, leading to silent PyTorch CUDA crashes.
- **Mechanism**:
  - Runs a standalone Python probe test up to 6 times (with exponential sleep).
  - Tests `torch.cuda.is_available()`, retrieves `device_index` and `device_name`.
- **Failure Action**:
  - If CUDA fails, the container **does not exit or enter a restart loop**.
  - It prints an ASCII warning banner, logs the exact JSON diagnostic to `~/comfyui_error.log`, and enters `keep_comfy_wait_stack_alive_forever()`.
  - The Nginx proxy stays alive so the user can inspect `/ready` and error diagnostics via the browser UI instead of receiving an unhelpful container exit code.

### Hook B: Host CPU Overload Guard & Affinity Pinning
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L242-L281)
- **Problem**: Multi-tenant GPU hosts often suffer from CPU resource starvation (CPU steal), causing node startup and model execution to run up to 3x slower.
- **Mechanism**:
  - Measures CPU idle time via `mpstat` or `vmstat`.
  - If CPU idle is `< 5%` (severe load), triggers `CPU_CONFIRM_REQUIRED=1`.
  - Displays a warning banner and gates ComfyUI launch behind user explicit confirmation on the wait page (`/tmp/continue_cpu`).
  - **Core Pinning (`compute_stable_pin_cores`)**: Calculates a stable core range (e.g., `0-71`) and executes PyTorch processes via `taskset -c` to protect worker threads from erratic core migration.

### Hook C: Python Dependency Protection (`/etc/pip/constraints.txt`)
- **Target Files**: [dockerfile](file:///Users/jnx/Documents/GitHub/130/dockerfile#L178-L198) & [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L28-L37)
- **Problem**: `ComfyUI-Manager` or custom nodes installing their own `requirements.txt` frequently downgrade PyTorch, torchvision, or CUDA libraries, breaking binary C++ extension modules.
- **Mechanism**:
  - During Docker build, the exact versions of core packages (`torch`, `torchvision`, `torchaudio`, `bitsandbytes`, `onnxruntime-gpu`, `torchcodec`, `nunchaku`) are frozen into `/etc/pip/constraints.txt`.
  - At runtime, `PIP_CONSTRAINT`, `UV_CONSTRAINT`, and `UV_BUILD_CONSTRAINT` are exported globally. Any `pip install` or `uv pip` command executed by nodes or managers is forced to respect these pinned limits.

### Hook D: Hardware-Specific SageAttention Architecture Selector
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L1150-L1208)
- **Problem**: `SageAttention` requires C++ extensions compiled specifically for the GPU microarchitecture (Ampere, Ada Lovelace, Blackwell). Using an mismatched wheel results in invalid instruction crashes.
- **Mechanism**:
  - Queries `torch.cuda.get_device_capability()` at startup.
  - Maps device capabilities: `86` -> `sm86` (RTX 30xx), `89` -> `sm89` (RTX 40xx), `120` -> `sm120` (RTX 50xx).
  - Installs the pre-compiled wheel matching the detected architecture from `/wheels/`.
  - If `MAKE_WHEELS=1` is passed, automatically builds and tags an architecture-specific wheel on boot and saves it to `/workspace/wheels`.

### Hook E: Mediapipe Incompatibility Fallback Patch
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L773-L776)
- **Problem**: In Python 3.12 environments, `mediapipe` sometimes lacks `solutions` attributes, causing imports in `controlnet_aux` to throw fatal `AttributeError` exceptions.
- **Mechanism**:
  - `prepare_ai_toolkit_runtime` inspects `controlnet_aux/__init__.py`.
  - If `mediapipe` fails, it dynamically patches `from .mediapipe_face import MediapipeFaceDetector` with a safe `try...except` block, setting `MediapipeFaceDetector = None` so AI Toolkit operations can proceed without crashing.

### Hook F: Image Core Sync (`SYNC_IMAGE_BAKED_COMFY_CORE`)
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L135-L220)
- **Problem**: When a user updates their Docker container image, persistent volume storage (`/workspace/ComfyUI`) retains old core framework files, preventing new image updates from taking effect.
- **Mechanism**:
  - Maintains `.image_baked_core_head` (git commit SHA) and `.image_baked_core_files.txt` (manifest of tracked core files).
  - If `SYNC_IMAGE_BAKED_COMFY_CORE=1`, compares the image head against the workspace marker.
  - Safely copies updated core files from `/Comfy` into `/workspace/ComfyUI` while preserving user custom nodes, workflows, output files, and settings.

### Hook G: Robust Process Group Termination (`stop_process_group`)
- **Target File**: [start.sh](file:///Users/jnx/Documents/GitHub/130/start.sh#L1360-L1407)
- **Problem**: Child processes spawned by ComfyUI nodes or AI Toolkit workers (e.g. sub-processes, dataloader workers) often become orphaned when parent processes receive SIGTERM, holding onto VRAM.
- **Mechanism**:
  - Retrieves the process group ID (`pgid`) via `ps -o pgid=`.
  - Collects all descendant PIDs recursively using `collect_descendants`.
  - Issues SIGTERM to the process group, polls status, and issues SIGKILL to remaining process groups and child processes.

---

## 4. Reverse Proxy & Port Strategy

All external communication is funneled through **Nginx** ([nginx.conf](file:///Users/jnx/Documents/GitHub/130/nginx.conf)).

| Public Port | Internal Service | Internal Port | Special Features & Optimization |
|---|---|---|---|
| **7860** | ComfyUI | 8188 | WebSockets (`/ws`), CORS & Host Origin rewriting, Ready status endpoint (`/ready`), 502 error interceptor |
| **7861** | Filebrowser | 8080 | Custom branding theme (`/scripts/filebrowser_branding`), `--noauth`, root `/workspace` |
| **7862** | AI Toolkit | 8675 | Next.js frontend proxy, unbuffered long-polling support |
| **7863** | JupyterLab | 8888 | Tokenless root access, unbuffered SSE (`/api/events/`), terminal WebSocket (`/terminals/`) |

### Origin & CORS Handling in Nginx:
ComfyUI strict Security origin policy rejects requests when Host/Origin headers mismatch public RunPod proxy URLs. Nginx handles this seamlessly:
```nginx
map $http_origin $comfy_origin {
  default $forwarded_proto://$forwarded_host;
  ''      '';
}
```
This forces outgoing proxy requests to match the host expected by ComfyUI, preventing `403 Forbidden` / `WebSocket connection failed` errors.

---

## 5. Interactive Status & Wait Page System

Instead of serving generic static 502 pages, the system includes three Python HTTP servers:

1. **[comfy_wait_page.py](file:///Users/jnx/Documents/GitHub/130/scripts/comfy_wait_page.py)**:
   - Runs on port 8188 during ComfyUI startup and backup restoration.
   - Streams live logs from `/server.log`.
   - Parses regex triggers for git clones, pip installs, custom node snapshots, and restoration progress.
   - Displays CPU overload warnings and provides an interactive "Continue Startup" button.
2. **[ai_toolkit_wait_page.py](file:///Users/jnx/Documents/GitHub/130/scripts/ai_toolkit_wait_page.py)**:
   - Runs on port 8675 during AI Toolkit venv build, Node dependency installation, DB schema creation (`prisma update_db`), and Next.js bundle building.
   - Parses multi-stage build definitions (`repo`, `venv`, `deps`, `ui-build`, `start`).
3. **[app_switch_page.py](file:///Users/jnx/Documents/GitHub/130/scripts/app_switch_page.py)**:
   - Renders a clean transition interface when switching between ComfyUI and AI Toolkit.
   - Allows one-click switching directly from the browser interface.

---

## 6. Environment Variables Reference

| Variable | Default | Purpose / Description |
|---|---|---|
| `COMFYUI_BACKUP` | *(unset)* | GitHub repository (`owner/repo`) containing model & workflow backup snapshots. |
| `RESTORE_BACKUP` | `0` | Set to `1` to enable automatic model and node restoration on first boot. |
| `SYNC_IMAGE_BAKED_COMFY_CORE` | `0` | Set to `1` to sync image core updates into persisted `/workspace/ComfyUI`. |
| `USE_UV` | `0` | Set to `1` to use fast `uv` installer for python packages instead of `pip`. |
| `MAKE_WHEELS` | `0` | Set to `1` to build `sageattention` architecture wheels on boot if missing. |
| `STUDENT_MODE` | `0` | Disables automated restoration of `comfy_course` default workflows. |
| `ENABLE_RUNTIME_REPO_UPDATES` | `1` | Automatically updates ComfyUI and custom nodes on startup via git/cli. |
| `NIGHTLY_COMFYUI` | `0` | Set to `1` to switch ComfyUI core & Manager to nightly builds. Defaults to `0` (latest stable release). |
| `PUBLIC_KEY` | *(unset)* | SSH public key to inject into `~/.ssh/authorized_keys` for remote terminal access. |
| `NEW_MANAGER` | `0` | Installs modern `comfyui-manager` via pip instead of legacy git node. |
