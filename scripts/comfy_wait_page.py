#!/usr/bin/env python3
import argparse
import http.server
import importlib
import json
import mimetypes
import os
import re
import socketserver
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


LOG_TAIL_BYTES = _int_env("COMFY_WAIT_LOG_TAIL_BYTES", 1_000_000)
SNAPSHOT_CANDIDATES = [
    Path("/workspace/.backup_tmp/hf_pull/ComfyUI/custom_nodes_snapshot.yaml"),
    Path("/workspace/.backup_tmp/hf_pull/ComfyUI/custom_nodes_snapshot.json"),
    Path("/workspace/.backup_tmp/hf_pull/ComfyUI/user/__manager/snapshots"),
    Path("/workspace/.backup_tmp/hf_pull/ComfyUI/user/default/ComfyUI-Manager/snapshots"),
]

RESTORE_CLONE_RE = re.compile(r"\[restore\]\s+cloning\s+(.+)", re.I)
RESTORE_CNR_RE = re.compile(r"\[restore\]\s+cnr install\s+([A-Za-z0-9._-]+)", re.I)
RESTORE_SKIP_RE = re.compile(r"\[restore\]\s+([A-Za-z0-9._-]+)\s+already present", re.I)
RESTORE_ERR_RE = re.compile(r"\[restore]\[err[^\]]*\]\s*([^\s]+)", re.I)
RESTORE_DONE_RE = re.compile(r"\[restore\]\s+nodes\s+&\s+settings\s+done", re.I)
RESTORE_FAIL_LIST_RE = re.compile(r"\[restore]\[warn].*failed:\s*\[(.+)\]", re.I)
RESTORE_SUMMARY_RE = re.compile(
    r"\[\s*(INSTALLED|CHECKOUT|SWITCHED|ENABLED|DISABLED|SKIPPED|FAILED)\s*\]\s+(.+)",
    re.I,
)
RESTORE_BACKUP_MANIFEST_RE = re.compile(r"\[restore\]\s+backup snapshot manifest:\s+(.+)", re.I)
RESTORE_SAVE_CURRENT_RE = re.compile(r"\[restore\]\s+saving current snapshot", re.I)
RESTORE_BUILD_MERGED_RE = re.compile(r"\[restore\]\s+building merged restore snapshot", re.I)
RESTORE_NORMALIZED_WRITTEN_RE = re.compile(r"\[restore\]\s+normalized backup snapshot written", re.I)
RESTORE_MERGED_WRITTEN_RE = re.compile(r"\[restore\]\s+merged snapshot written", re.I)
RESTORE_APPLY_RE = re.compile(r"\[restore\]\s+restoring nodes from merged snapshot", re.I)

FATAL_GPU_RE = re.compile(r"\[fatal\]\[gpu\]\s*(.+)", re.I)
FATAL_GPU_DIAG_RE = re.compile(r"\[fatal\]\[gpu_diag\]\s*(.+)", re.I)
FATAL_ACTION_RE = re.compile(r"\[fatal\]\[action\]\s*(.+)", re.I)
FATAL_CPU_RE = re.compile(r"\[fatal\]\[cpu\]\s*(.+)", re.I)
FATAL_CPU_ACTION_RE = re.compile(r"\[fatal\]\[cpu_action\]\s*(.+)", re.I)
CPU_CONFIRM_RE = re.compile(r"\[cpu\]\s+User confirmation received", re.I)
CUDA_FAIL_RE = re.compile(r"CUDA initialization failed", re.I)
BROKEN_DRIVER_RE = re.compile(r"broken or outdated GPU drivers", re.I)

NODE_PROGRESS_RE = [
    re.compile(r"\[nodes\]\s+refreshing\s+([A-Za-z0-9._-]+)", re.I),
    re.compile(r"Updating:\s*([A-Za-z0-9._-]+?)(?:\d+/\d+)?(?:\s|\x1b|$)", re.I),
]

LOCAL_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR_CANDIDATES = [
    Path("/usr/share/nginx/html/assets"),
    LOCAL_PROJECT_ROOT / "assets",
]


def _bool_env(value: str, default: str = "0") -> bool:
    raw = str(value if value is not None else default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


ENV_INFO = {
    "backup_repo": (os.environ.get("COMFYUI_BACKUP") or "").strip() or None,
    "restore_enabled": _bool_env(os.environ.get("RESTORE_BACKUP", "0")),
    "hf_token": bool((os.environ.get("HF_TOKEN") or "").strip()),
    "civitai_api_key": bool((os.environ.get("CIVITAI_API_KEY") or "").strip()),
    "runpod_pod_id": (os.environ.get("RUNPOD_POD_ID") or "").strip() or None,
    "student_mode": _bool_env(os.environ.get("STUDENT_MODE", os.environ.get("student_mode", "0"))),
}

def _extract_owner_repo(value: str):
    cleaned = str(value or "").strip().rstrip("/")
    if not cleaned:
        return None
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    lower = cleaned.lower()
    marker = "github.com/"
    if marker in lower:
        idx = lower.find(marker)
        tail = cleaned[idx + len(marker):].strip("/")
        parts = [part for part in tail.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    match = re.match(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$", cleaned)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def _extract_repo_name(value: str) -> str:
    owner_repo = _extract_owner_repo(value)
    if owner_repo:
        return owner_repo.split("/", 1)[1]
    cleaned = str(value or "").strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if "/" in cleaned:
        cleaned = cleaned.split("/")[-1]
    return cleaned


def _read_custom_nodes_from_dockerfile() -> str:
    candidates = (
        LOCAL_PROJECT_ROOT / "dockerfile",
        Path("/dockerfile"),
        Path("/workspace/dockerfile"),
    )
    pattern = re.compile(r'^\s*ENV\s+CUSTOM_NODES\s*=\s*"([^"]*)"', re.I)
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            match = pattern.search(line)
            if match:
                return match.group(1).strip()
    return ""


def _repo_only_custom_nodes(raw: str) -> str:
    names = []
    seen = set()
    for token in str(raw or "").split():
        repo_name = _extract_repo_name(token)
        key = repo_name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(repo_name)
    return " ".join(names)


DEFAULT_PREINSTALLED_CUSTOM_NODES = _repo_only_custom_nodes(_read_custom_nodes_from_dockerfile())


def _parse_preinstalled_custom_nodes(raw: str):
    entries = []
    seen = set()
    for token in str(raw or "").split():
        owner_repo = _extract_owner_repo(token)
        repo_name = _extract_repo_name(token)
        owner_repo_norm = owner_repo.lower() if owner_repo else None
        key = repo_name.lower()
        signature = key
        if not signature or signature in seen:
            continue
        seen.add(signature)
        entries.append(
            {
                "name": repo_name,
                "owner_repo": owner_repo_norm,
                "key": key,
            }
        )
    return entries


_RAW_PREINSTALLED_CUSTOM_NODES = (
    os.environ.get("CUSTOM_NODES")
    or DEFAULT_PREINSTALLED_CUSTOM_NODES
)
PREINSTALLED_CUSTOM_NODES = _parse_preinstalled_custom_nodes(_RAW_PREINSTALLED_CUSTOM_NODES)
PREINSTALLED_OWNER_REPO_SET = {
    entry["owner_repo"] for entry in PREINSTALLED_CUSTOM_NODES if entry.get("owner_repo")
}
PREINSTALLED_KEY_SET = {
    entry["key"] for entry in PREINSTALLED_CUSTOM_NODES if entry.get("key")
}

SUPPORT_REQUEST_TEMPLATE = """On one specific Community host/cluster, my pod's CPU is effectively pinned at 100%, with load averages over 400, while nvidia-smi shows the GPU idle. This happens before any real work begins-software is at rest, yet the CPU stays pegged.

Diagnostics indicate the GPU and CUDA stack are fine: nvidia-smi looks normal, and torch.cuda.is_available() returns True. The slowdown isn't because the code is falling back to CPU-it's the host. The workload itself isn't burning CPU; something on that node is saturating the cores.

When the pod lands on a different Community host, everything behaves as expected: iterations per second are comparable to Secure Cloud (about ~35 it/s vs ~12 it/s on the problematic host), and CPU usage stays mostly idle. This points to a specific node/cluster issue, not the entire Community tier.

I understand Secure offers tighter resource control, but leaving broken machines in the pool while continuing to sell Community access feels unfair."""


def _build_support_request_text() -> str:
    pod_id = ENV_INFO.get("runpod_pod_id") or "(not available)"
    return f"RUNPOD_POD_ID: {pod_id}\n\n{SUPPORT_REQUEST_TEMPLATE}"


def _regex(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


def _backup_detail(_: dict, backup_state: dict) -> str:
    repo = ENV_INFO["backup_repo"]
    if not repo:
        return "Backup is not set"
    if not ENV_INFO["restore_enabled"]:
        return f"Backup {repo} configured but RESTORE_BACKUP=0"
    total = len(backup_state.get("nodes") or [])
    if total:
        return f"{total} nodes from {repo}"
    return f"Restoring backup from {repo}"


def _update_backup_install_detail(line: str, current: str) -> str:
    if current == "Backup restore complete":
        return current
    if RESTORE_BACKUP_MANIFEST_RE.search(line):
        return "Backup snapshot manifest loaded"
    if RESTORE_SAVE_CURRENT_RE.search(line):
        return "Saving current node snapshot"
    if RESTORE_BUILD_MERGED_RE.search(line):
        return "Building merged restore snapshot"
    if RESTORE_NORMALIZED_WRITTEN_RE.search(line):
        return "Backup snapshot converted to Comfy format"
    if RESTORE_MERGED_WRITTEN_RE.search(line):
        return "Merged restore snapshot ready"
    if "[restore] snapshot not found; nodes not changed" in line:
        return "No node snapshot found in backup"
    if RESTORE_APPLY_RE.search(line):
        return "Applying merged restore snapshot"
    if RESTORE_DONE_RE.search(line):
        return "Backup restore complete"
    if "PIPs restore mode:" in line:
        return "Applying snapshot with pip restore disabled"
    if "Install: pip packages" in line or "Installing collected packages:" in line:
        return "Installing node dependencies"
    if "[ComfyUI-Manager] The ComfyRegistry cache update is still in progress" in line:
        return "Restoring nodes while manager cache refreshes"
    clone = RESTORE_CLONE_RE.search(line)
    if clone:
        return f"Cloning {_repo_label(clone.group(1))}"
    cnr = RESTORE_CNR_RE.search(line)
    if cnr:
        return f"Installing {cnr.group(1)} from snapshot"
    summary = RESTORE_SUMMARY_RE.search(line)
    if summary:
        action = summary.group(1).upper()
        target = summary.group(2).strip().split("@", 1)[0]
        verb = {
            "INSTALLED": "Installed",
            "CHECKOUT": "Checked out",
            "SWITCHED": "Switched",
            "ENABLED": "Enabled",
            "DISABLED": "Disabled",
            "SKIPPED": "Skipped",
            "FAILED": "Failed",
        }.get(action, action.title())
        return f"{verb} {_repo_label(target) if target.startswith('http') else target}"
    return current


def _update_combined_update_detail(line: str, current: str) -> str:
    clean_line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    if "[updates] skipped combined update;" in line:
        return "Skipped (already updated during backup restore)"
    if "[restore] running comfy update all" in line:
        return "Running comfy update all"
    if "[restore][warn] 'comfy update all' failed; trying legacy node update command" in line:
        return "Primary update failed, using legacy node update"
    if "[restore][warn] update step failed; continuing with restore flow" in line:
        return "Update step failed; continuing restore flow"
    if "Already up to date." in clean_line or "ComfyUI is already up to date." in clean_line:
        return "Already up to date"
    if "Current snapshot is saved as" in clean_line:
        return "Refreshing custom nodes"
    if "[nodes][warn] comfy CLI update failed; falling back to git updates" in line:
        return "Comfy CLI failed, falling back to git updates"
    if "[nodes] refreshing " in line:
        node_name = clean_line.split("[nodes] refreshing ", 1)[-1].strip()
        return f"Updating {node_name}"
    for pattern in NODE_PROGRESS_RE:
        match = pattern.search(clean_line)
        if match:
            name = match.group(1)
            if name.lower().startswith("http"):
                name = _repo_label(name)
            return f"Updating {name}"
    if "[restore] nodes & settings done." in line:
        return "Skipped (already updated/restored during backup step)"
    return current


STAGE_DEFS = [
    {
        "id": "cuda",
        "label": "Checking pod health",
        "detail": "Checking CUDA initialization",
        "patterns": [
            _regex(r"STAGE:\s*Checking CUDA"),
            _regex(r"STAGE:\s*Host driver failure"),
        ],
    },
    {
        "id": "environment",
        "label": "Creating environment",
        "detail": "Preparing workspace and Python environment",
        "patterns": [
            _regex(r"Persisting ComfyUI"),
            _regex(r"ComfyUI already present"),
            _regex(r"Creating venv"),
            _regex(r"VIRTUAL_ENV:"),
        ],
    },
    {
        "id": "manager",
        "label": "Updating ComfyUI repo",
        "detail": "Updating ComfyUI Manager package",
        "patterns": [
            _regex(r"STAGE:\s*Ensuring ComfyUI Manager \(pip\)"),
        ],
    },
    {
        "id": "update-all",
        "label": "Updating ComfyUI and custom nodes",
        "detail": "Updating ComfyUI core and custom nodes",
        "patterns": [
            _regex(r"STAGE:\s*Updating ComfyUI and custom nodes"),
            _regex(r"STAGE:\s*Updating ComfyUI core"),
            _regex(r"STAGE:\s*Updating custom nodes"),
            _regex(r"STAGE:\s*Backup node update"),
        ],
        "detail_from_line": _update_combined_update_detail,
    },
    {
        "id": "backup-install",
        "label": "Installing node packs from the backup",
        "detail_factory": _backup_detail,
        "patterns": [
            _regex(r"STAGE:\s*Installing nodes from backup"),
            _regex(r"STAGE:\s*Backup snapshot merge"),
            _regex(r"STAGE:\s*Backup node restore"),
            _regex(r"\[restore\]\s+nodes\s+&\s+settings\s+done"),
            _regex(r"\[restore\]\s+restoring nodes from merged snapshot"),
        ],
        "detail_from_line": _update_backup_install_detail,
    },
    {
        "id": "launch",
        "label": "Starting ComfyUI",
        "detail": "Starting ComfyUI on :8188",
        "patterns": [
            _regex(r"STAGE:\s*Starting ComfyUI"),
        ],
    },
]


def tail_lines(path: str, max_bytes: int = LOG_TAIL_BYTES):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(size - max_bytes, 0))
            data = handle.read().decode("utf-8", errors="ignore")
            return data.splitlines()
    except OSError:
        return []


def _resolve_asset_file(request_path: str):
    marker = "/assets/"
    if marker not in request_path:
        return None
    relative = request_path.split(marker, 1)[1]
    if not relative:
        return None
    relative_path = Path(relative)
    for root in ASSET_DIR_CANDIDATES:
        try:
            if not root.exists():
                continue
            root_resolved = root.resolve()
            candidate = (root_resolved / relative_path).resolve()
            candidate.relative_to(root_resolved)
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


_SNAPSHOT_CACHE = {"path": None, "mtime": None, "nodes": []}


def _repo_label(repo_url: str) -> str:
    cleaned = repo_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.split("/")[-1] if "/" in cleaned else cleaned


def _normalize_repo(repo_url: str) -> str:
    cleaned = repo_url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.rstrip("/")


def _is_manager_repo(repo_url: str) -> bool:
    return "comfyui-manager" in repo_url.lower()


def _find_snapshot_path():
    for candidate in SNAPSHOT_CANDIDATES:
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            matches = []
            for ext in (".yaml", ".yml", ".json"):
                matches.extend(candidate.glob(f"*{ext}"))
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime)
    return None


def _load_backup_nodes():
    snapshot_path = _find_snapshot_path()
    if not snapshot_path:
        _SNAPSHOT_CACHE["path"] = None
        _SNAPSHOT_CACHE["mtime"] = None
        _SNAPSHOT_CACHE["nodes"] = []
        return []
    current_mtime = snapshot_path.stat().st_mtime
    if (
        _SNAPSHOT_CACHE["path"] == snapshot_path
        and _SNAPSHOT_CACHE["mtime"] == current_mtime
    ):
        return _SNAPSHOT_CACHE["nodes"]
    try:
        yaml = importlib.import_module("yaml")
    except Exception:
        yaml = None
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
    except Exception:
        return []
    data = None
    if yaml:
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:
            data = None
    if data is None:
        try:
            data = json.loads(raw) or {}
        except Exception:
            return []
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("custom_nodes"), dict):
        data = data["custom_nodes"]
    nodes = []
    for repo_url, node_data in (data.get("git_custom_nodes") or {}).items():
        if _is_manager_repo(repo_url):
            continue
        if isinstance(node_data, dict) and node_data.get("disabled"):
            continue
        label = None
        owner_repo = _extract_owner_repo(repo_url)
        if isinstance(node_data, dict):
            label = node_data.get("name")
        nodes.append(
            {
                "key": _repo_label(repo_url),
                "name": label or _repo_label(repo_url),
                "source": "git",
                "repo": _normalize_repo(repo_url),
                "owner_repo": owner_repo.lower() if owner_repo else None,
            }
        )
    for node_name, version in (data.get("cnr_custom_nodes") or {}).items():
        nodes.append(
            {
                "key": str(node_name),
                "name": str(node_name),
                "source": "cnr",
                "version": "" if version is None else str(version),
                "owner_repo": None,
            }
        )
    _SNAPSHOT_CACHE["path"] = snapshot_path
    _SNAPSHOT_CACHE["nodes"] = nodes
    _SNAPSHOT_CACHE["mtime"] = current_mtime
    return nodes


def _should_scan_backup_snapshot(lines):
    if not (ENV_INFO["restore_enabled"] and ENV_INFO["backup_repo"]):
        return False
    markers = (
        "[restore]",
        "STAGE: Installing nodes from backup",
        "STAGE: Backup snapshot merge",
        "STAGE: Backup node restore",
        "STAGE: Starting ComfyUI",
    )
    for line in lines:
        for marker in markers:
            if marker in line:
                return True
    return False


def _apply_backup_progress(nodes, lines):
    if not nodes:
        return nodes
    index_by_key = {node["key"]: i for i, node in enumerate(nodes)}
    index_by_repo = {
        node["repo"]: i for i, node in enumerate(nodes) if node.get("repo")
    }
    index_by_name = {str(node.get("name", "")).strip().lower(): i for i, node in enumerate(nodes)}

    def _lookup_idx(token: str):
        key = token.strip()
        if not key:
            return None
        idx = index_by_key.get(key)
        if idx is None:
            idx = index_by_repo.get(_normalize_repo(key))
        if idx is None:
            idx = index_by_name.get(key.lower())
        if idx is None:
            idx = index_by_name.get(_repo_label(key).lower())
        return idx

    for node in nodes:
        node["status"] = node.get("status") or "pending"
    active_idx = None
    for line in lines:
        clone = RESTORE_CLONE_RE.search(line)
        if clone:
            key = index_by_repo.get(_normalize_repo(clone.group(1)))
            if key is not None:
                if (
                    active_idx is not None
                    and nodes[active_idx]["status"] == "installing"
                    and active_idx != key
                ):
                    nodes[active_idx]["status"] = "done"
                active_idx = key
                nodes[key]["status"] = "installing"
            continue
        cnr = RESTORE_CNR_RE.search(line)
        if cnr:
            key = index_by_key.get(cnr.group(1).strip())
            if key is not None:
                if (
                    active_idx is not None
                    and nodes[active_idx]["status"] == "installing"
                    and active_idx != key
                ):
                    nodes[active_idx]["status"] = "done"
                active_idx = key
                nodes[key]["status"] = "installing"
            continue
        skip = RESTORE_SKIP_RE.search(line)
        if skip:
            idx = _lookup_idx(skip.group(1))
            if idx is not None:
                nodes[idx]["status"] = "done"
            continue
        err_match = RESTORE_ERR_RE.search(line)
        if err_match:
            idx = _lookup_idx(err_match.group(1))
            if idx is not None:
                nodes[idx]["status"] = "failed"
            continue
        fail_list = RESTORE_FAIL_LIST_RE.search(line)
        if fail_list:
            failed_nodes = fail_list.group(1).split(",")
            for key in failed_nodes:
                idx = _lookup_idx(key)
                if idx is not None:
                    nodes[idx]["status"] = "failed"
            continue
        summary = RESTORE_SUMMARY_RE.search(line)
        if summary:
            action = summary.group(1).upper()
            node_key = summary.group(2).strip().split("@", 1)[0]
            idx = _lookup_idx(node_key)
            if idx is not None:
                if action == "FAILED":
                    nodes[idx]["status"] = "failed"
                else:
                    nodes[idx]["status"] = "done"
            continue
        if RESTORE_DONE_RE.search(line):
            for node in nodes:
                if node["status"] in {"pending", "installing"}:
                    node["status"] = "done"
            active_idx = None
    if active_idx is not None and nodes[active_idx]["status"] == "installing":
        for idx in range(active_idx):
            if nodes[idx]["status"] == "pending":
                nodes[idx]["status"] = "done"
    return nodes


def _is_preinstalled_backup_node(node: dict) -> bool:
    owner_repo = str(node.get("owner_repo") or "").strip().lower()
    key = str(node.get("key") or "").strip().lower()
    if owner_repo and owner_repo in PREINSTALLED_OWNER_REPO_SET:
        return True
    if key and key in PREINSTALLED_KEY_SET:
        return True
    return False


def _build_custom_node_catalog(backup_nodes):
    catalog = []
    seen = set()

    for node in PREINSTALLED_CUSTOM_NODES:
        signature = node.get("owner_repo") or node.get("key")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        catalog.append({"name": node.get("name") or "custom-node", "source": "preinstalled"})

    for node in backup_nodes:
        signature = (
            str(node.get("owner_repo") or "").strip().lower()
            or str(node.get("key") or "").strip().lower()
            or str(node.get("name") or "").strip().lower()
        )
        if not signature or signature in seen:
            continue
        seen.add(signature)
        catalog.append({"name": node.get("name") or node.get("key") or "custom-node", "source": "backup"})

    return catalog


def build_backup_state(lines):
    enabled = ENV_INFO["restore_enabled"] and ENV_INFO["backup_repo"]
    nodes = []
    has_manifest = False
    should_scan = _should_scan_backup_snapshot(lines)
    if enabled and should_scan:
        nodes = [dict(node) for node in _load_backup_nodes()]
        has_manifest = bool(nodes)
    if not ENV_INFO["backup_repo"]:
        message = "Backup is not configured."
    elif not ENV_INFO["restore_enabled"]:
        message = "Backup restore is disabled."
    elif not should_scan:
        message = "Waiting for backup restore stage..."
    elif not nodes:
        message = "No custom nodes found in backup snapshot yet."
    else:
        message = "Custom nodes snapshot loaded."

    backup_only_count = 0
    for node in nodes:
        preinstalled = _is_preinstalled_backup_node(node)
        node["preinstalled"] = preinstalled
        if not preinstalled:
            backup_only_count += 1

    catalog_nodes = _build_custom_node_catalog(nodes)
    return {
        "enabled": bool(enabled),
        "repo": ENV_INFO["backup_repo"],
        "nodes": nodes,
        "has_manifest": has_manifest,
        "message": message,
        "backup_only_count": backup_only_count,
        "preinstalled_count": len(PREINSTALLED_CUSTOM_NODES),
        "total_count": len(catalog_nodes),
        "catalog_nodes": catalog_nodes,
    }


def detect_fatal_state(lines):
    fatal_message = None
    action_message = None
    diag_message = None
    saw_cuda_failure = False
    saw_broken_driver_text = False

    for line in lines:
        gpu_match = FATAL_GPU_RE.search(line)
        if gpu_match and gpu_match.group(1).strip():
            fatal_message = gpu_match.group(1).strip()

        diag_match = FATAL_GPU_DIAG_RE.search(line)
        if diag_match and diag_match.group(1).strip():
            diag_message = diag_match.group(1).strip()

        action_match = FATAL_ACTION_RE.search(line)
        if action_match and action_match.group(1).strip():
            action_message = action_match.group(1).strip()

        if CUDA_FAIL_RE.search(line):
            saw_cuda_failure = True
        if BROKEN_DRIVER_RE.search(line):
            saw_broken_driver_text = True

    active = bool(fatal_message or action_message or saw_cuda_failure or saw_broken_driver_text)
    if not active:
        return {"active": False}

    if not fatal_message:
        fatal_message = "Host GPU drivers look corrupt or outdated; CUDA initialization failed."
    if not action_message:
        action_message = "Startup halted. Redeploy this pod on a healthy host."

    return {
        "active": True,
        "code": "host_gpu_driver_corrupt",
        "title": "Startup Halted",
        "message": fatal_message,
        "action": action_message,
        "detail": diag_message,
    }


def compute_stage_states(lines, backup_state, fatal_state):
    stages = []
    skip_ids = set()
    if not backup_state["enabled"]:
        skip_ids.update({"backup-install"})
    for definition in STAGE_DEFS:
        base_detail = definition.get("detail")
        label = definition.get("label", "")
        if definition["id"] == "backup-install":
            backup_only_count = int(backup_state.get("backup_only_count") or 0)
            label = f"Installing {backup_only_count} node packs from the backup"
        if callable(definition.get("detail_factory")):
            base_detail = definition["detail_factory"](ENV_INFO, backup_state)
        stages.append(
            {
                "id": definition["id"],
                "label": label,
                "detail": base_detail or "",
                "status": "pending",
                "skipped": definition["id"] in skip_ids,
            }
        )
        if definition["id"] in skip_ids:
            stages[-1]["status"] = "done"
    current_idx = None
    for line in lines:
        matched_idx = None
        for idx, definition in enumerate(STAGE_DEFS):
            if stages[idx]["skipped"]:
                continue
            if any(pattern.search(line) for pattern in definition["patterns"]):
                matched_idx = idx
                break

        if matched_idx is not None and (current_idx is None or matched_idx >= current_idx):
            current_idx = matched_idx

        # Keep detail text moving while a stage is active, even when logs don't
        # emit another explicit "STAGE:" marker for a while.
        if current_idx is not None and not stages[current_idx]["skipped"]:
            detail_fn = STAGE_DEFS[current_idx].get("detail_from_line")
            if detail_fn:
                new_detail = detail_fn(line, stages[current_idx]["detail"])
                if new_detail:
                    stages[current_idx]["detail"] = new_detail
    if current_idx is None:
        for idx, stage in enumerate(stages):
            if stage["skipped"]:
                continue
            stage["status"] = "active"
            break
    else:
        for idx, stage in enumerate(stages):
            if stage["skipped"]:
                continue
            if idx < current_idx:
                stage["status"] = "done"
            elif idx == current_idx:
                stage["status"] = "active"
            else:
                stage["status"] = "pending"

    if fatal_state.get("active"):
        fail_idx = current_idx
        if fail_idx is None:
            for idx, stage in enumerate(stages):
                if not stage["skipped"]:
                    fail_idx = idx
                    break
        if fail_idx is not None:
            for idx, stage in enumerate(stages):
                if stage["skipped"]:
                    continue
                if idx < fail_idx:
                    stage["status"] = "done"
                elif idx == fail_idx:
                    stage["status"] = "failed"
                    fatal_msg = fatal_state.get("message")
                    fatal_action = fatal_state.get("action")
                    detail = fatal_msg or stage["detail"]
                    if fatal_action:
                        detail = f"{detail} {fatal_action}"
                    stage["detail"] = detail
                else:
                    stage["status"] = "pending"

    return stages


def detect_cpu_confirm_state(lines, continue_file: str, cpu_pin_cores: str):
    required = False
    confirmed = False
    message = "Host CPU resources are overloaded."
    action = "Confirm on this page to continue startup anyway."

    for line in lines:
        cpu_match = FATAL_CPU_RE.search(line)
        if cpu_match and cpu_match.group(1).strip():
            required = True
            message = cpu_match.group(1).strip()

        cpu_action_match = FATAL_CPU_ACTION_RE.search(line)
        if cpu_action_match and cpu_action_match.group(1).strip():
            action = cpu_action_match.group(1).strip()

        if CPU_CONFIRM_RE.search(line):
            confirmed = True

    if continue_file:
        try:
            if Path(continue_file).is_file():
                confirmed = True
        except OSError:
            pass

    return {
        "required": required,
        "confirmed": bool(confirmed),
        "active": bool(required and not confirmed),
        "title": "CPU Overload Detected",
        "message": message,
        "action": action,
        "pin_cores": cpu_pin_cores,
        "support_request": _build_support_request_text(),
    }


def build_state_payload(log_path, continue_file: str = "", cpu_pin_cores: str = ""):
    lines = tail_lines(log_path)
    backup_state = build_backup_state(lines)
    fatal_state = detect_fatal_state(lines)
    cpu_confirm_state = detect_cpu_confirm_state(lines, continue_file, cpu_pin_cores)
    return {
        "timestamp": time.time(),
        "stages": compute_stage_states(lines, backup_state, fatal_state),
        "backup": backup_state,
        "fatal": fatal_state,
        "cpu_confirm": cpu_confirm_state,
        "env": {
            "backup_repo": ENV_INFO["backup_repo"],
            "restore_enabled": ENV_INFO["restore_enabled"],
            "hf_token": ENV_INFO["hf_token"],
            "civitai_api_key": ENV_INFO["civitai_api_key"],
            "runpod_pod_id": ENV_INFO["runpod_pod_id"],
            "student_mode": ENV_INFO["student_mode"],
        },
    }


def build_cpu_confirm_payload(continue_file: str, cpu_pin_cores: str):
    confirmed = False
    if continue_file:
        try:
            confirmed = Path(continue_file).is_file()
        except OSError:
            confirmed = False
    return {
        "timestamp": time.time(),
        "cpu_confirm": {
            "required": True,
            "confirmed": bool(confirmed),
            "active": not confirmed,
            "title": "Host CPU resources are overloaded",
            "message": "Running ComfyUI on this pod may be at least three times slower than normal.",
            "action": "It is strongly recommended to redeploy this pod on a healthier host.",
            "continue_file": continue_file,
            "pin_cores": cpu_pin_cores,
            "support_request": _build_support_request_text(),
        },
    }


HTML_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preload" href="/assets/fonts/InterVariable.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="/assets/fonts/InterVariable-Italic.woff2" as="font" type="font/woff2" crossorigin>
  <title>ComfyUI is starting...</title>
  <style>
    @font-face { font-family: 'Inter'; font-style: normal; font-weight: 100 900; font-display: swap; src: url('/assets/fonts/InterVariable.woff2') format('woff2'); }
    @font-face { font-family: 'Inter'; font-style: italic; font-weight: 100 900; font-display: swap; src: url('/assets/fonts/InterVariable-Italic.woff2') format('woff2'); }
    :root {
      --bg: #161717;
      --text-main: #c4c7cf;
      --text-dim: #575c68;
      --accent: #f0ff41;
      --screen-gap: 27px;
      --small-font: 14px;
      --small-line: 23px;
      --micro-font: 11px;
      --stage-font-size: 26px;
      --stage-line-height: 1.2;
      --stage-line-size: calc(var(--stage-font-size) * var(--stage-line-height));
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      min-height: 100%;
      background: var(--bg);
      color: var(--text-main);
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .page {
      min-height: 100vh;
      padding: var(--screen-gap);
    }
    .layout {
      min-height: calc(100vh - (var(--screen-gap) * 2));
      display: grid;
      grid-template-columns: minmax(220px, 320px) 1fr minmax(220px, 320px);
      gap: 28px;
    }
    .left-column {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
    }
    .template-name {
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.01em;
    }
    .template-by {
      margin-top: 4px;
      font-size: var(--small-font);
      line-height: 18px;
      color: var(--text-main);
      font-weight: 400;
    }
    .template-by a {
      color: var(--accent);
      text-decoration: underline;
      text-underline-offset: 2px;
      text-decoration-thickness: 1px;
    }
    .left-bottom {
      font-size: var(--small-font);
      line-height: var(--small-line);
      color: var(--text-main);
    }
    .filebrowser-link {
      display: inline-block;
      color: var(--accent);
      text-decoration: underline;
      text-underline-offset: 2px;
      text-decoration-thickness: 1px;
      font-size: var(--small-font);
      line-height: var(--small-line);
      font-weight: 400;
    }
    .left-gap {
      height: 24px;
    }
    .mini-stage-list {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .mini-stage-item {
      font-size: var(--small-font);
      line-height: var(--small-line);
      color: var(--text-dim);
      font-weight: 400;
      font-family: Inter, "Segoe UI Symbol", "Noto Sans Symbols", sans-serif;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .mini-stage-item.stage-done,
    .mini-stage-item.stage-active {
      color: var(--text-main);
    }
    .env-line {
      display: flex;
      flex-direction: column;
      gap: 0;
      align-items: flex-start;
    }
    .env-item {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: var(--small-line);
      color: var(--text-dim);
    }
    .env-item.is-on {
      color: var(--text-main);
    }
    .env-bullet {
      width: 15px;
      text-align: center;
      font-size: var(--micro-font);
      line-height: var(--small-line);
      color: var(--text-dim);
      font-weight: 700;
    }
    .env-item.is-on .env-bullet {
      color: var(--accent);
    }
    .env-name {
      font-size: var(--micro-font);
      line-height: var(--small-line);
      font-weight: 500;
      letter-spacing: 0.05em;
      color: inherit;
      text-transform: uppercase;
    }
    .env-backup-link {
      font-size: var(--micro-font);
      line-height: var(--small-line);
      color: var(--accent);
      text-decoration: underline;
      text-underline-offset: 2px;
      text-decoration-thickness: 1px;
      font-weight: 500;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .center-column {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      min-width: 0;
    }
    .center-stack {
      width: min(460px, 92%);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      gap: 0;
    }
    .brand-wrap {
      height: 104px;
      display: flex;
      align-items: flex-end;
      justify-content: center;
      margin-bottom: calc(var(--stage-line-size) * 1.5);
    }
    .brand-mark {
      width: min(236px, 84vw);
      height: auto;
      user-select: none;
      pointer-events: none;
    }
    .progress-track {
      width: min(360px, 86vw);
      height: 8px;
      border-radius: 999px;
      background: #34394a;
      overflow: hidden;
      position: relative;
    }
    .progress-runner {
      position: absolute;
      top: 0;
      left: -30%;
      width: 30%;
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
      animation: loading-runner 1.25s linear infinite;
    }
    .error-focus .progress-runner {
      animation: none;
      left: 0;
      width: 100%;
      background: var(--text-dim);
    }
    @keyframes loading-runner {
      from { left: -30%; }
      to { left: 100%; }
    }
    .current-stage {
      margin-top: var(--stage-line-size);
      font-size: var(--stage-font-size);
      line-height: var(--stage-line-height);
      font-weight: 700;
      color: var(--text-main);
      white-space: nowrap;
      display: inline-block;
      max-width: none;
    }
    .hint-note {
      margin-top: calc(var(--stage-line-size) * 1.5);
      font-size: 19px;
      line-height: 1.35;
      font-weight: 400;
      color: var(--text-dim);
    }
    .right-column {
      min-width: 0;
      display: flex;
      justify-content: flex-end;
      align-items: flex-start;
    }
    .node-catalog {
      text-align: right;
      max-width: 100%;
    }
    .node-count {
      font-size: var(--small-font);
      line-height: var(--small-line);
      color: #c4c7cf;
      margin-bottom: 0;
      white-space: nowrap;
    }
    .node-list {
      list-style: none;
      margin: 0;
      padding: 0;
      max-height: calc(100vh - (var(--screen-gap) * 2) - 56px);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--text-dim) transparent;
    }
    .node-list::-webkit-scrollbar { width: 6px; }
    .node-list::-webkit-scrollbar-thumb { background: var(--text-dim); border-radius: 999px; }
    .node-item {
      font-size: var(--small-font);
      line-height: var(--small-line);
      color: #575c68;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 100%;
    }
    .fatal-panel, .cpu-panel {
      display: none;
      width: min(560px, 100%);
      min-height: min(560px, calc(100vh - (var(--screen-gap) * 2)));
      max-height: calc(100vh - (var(--screen-gap) * 2));
      border: 1px solid rgba(240, 255, 65, 0.72);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(82, 67, 23, 0.52), rgba(49, 39, 15, 0.62));
      padding: 24px;
      color: #f6e7a3;
      font-size: 14px;
      line-height: 1.45;
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.38);
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: #8a7a39 transparent;
    }
    .fatal-panel::-webkit-scrollbar, .cpu-panel::-webkit-scrollbar { width: 8px; }
    .fatal-panel::-webkit-scrollbar-thumb, .cpu-panel::-webkit-scrollbar-thumb { background: #8a7a39; border-radius: 999px; }
    .fatal-title, .cpu-title {
      font-size: 18px;
      line-height: 1.3;
      font-weight: 700;
      margin-bottom: 10px;
      color: #f0ff41;
    }
    .fatal-columns, .cpu-columns {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 12px;
    }
    .fatal-detail, .cpu-detail {
      margin: 0;
      color: #f6e7a3;
      font-size: 14px;
      line-height: 1.45;
    }
    .cpu-message { display: none; }
    .cpu-support-link {
      display: inline-block;
      margin-top: 12px;
      color: #f0dd8b;
      text-decoration: underline;
      text-underline-offset: 2px;
      text-decoration-thickness: 1px;
      line-height: 1.35;
    }
    .cpu-support-wrap {
      margin-top: 10px;
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 0;
    }
    .cpu-support-title {
      display: none;
    }
    .cpu-support-text {
      width: 100%;
      min-height: 180px;
      resize: vertical;
      border: 1px solid #444955;
      border-radius: 8px;
      padding: 10px;
      background: rgba(6, 7, 10, 0.62);
      color: var(--text-main);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 14px;
      line-height: 1.3;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--text-dim) transparent;
      scrollbar-gutter: stable;
    }
    .cpu-support-text::-webkit-scrollbar { width: 8px; }
    .cpu-support-text::-webkit-scrollbar-thumb { background: var(--text-dim); border-radius: 999px; }
    .cpu-actions {
      margin-top: 10px;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .cpu-button {
      border: 1px solid #454a56;
      border-radius: 8px;
      background: #2a2f39;
      color: var(--text-main);
      font-size: 14px;
      font-weight: 600;
      padding: 8px 12px;
      cursor: pointer;
    }
    .cpu-button[disabled] {
      opacity: 0.55;
      cursor: default;
    }
    .cpu-status,
    .cpu-copy-status {
      font-size: 14px;
      color: var(--text-main);
    }
    .is-hidden { display: none !important; }
    @media (max-width: 980px) {
      .layout {
        grid-template-columns: 1fr;
        gap: 24px;
      }
      .left-column,
      .right-column {
        justify-content: flex-start;
      }
      .right-column { order: 3; }
      .center-column { order: 2; min-height: 360px; }
      .node-catalog { text-align: left; }
      .node-list { max-height: 260px; }
      .fatal-columns, .cpu-columns { grid-template-columns: 1fr; }
      .fatal-panel, .cpu-panel { min-height: 0; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="layout">
      <aside class="left-column" data-left-column>
        <div class="left-top">
          <h1 class="template-name" data-template-title>comfy.work</h1>
          <div class="template-by">by <a href="https://course.yakushev.fr/" target="_blank" rel="noopener" data-template-link>course.yakushev.fr</a></div>
        </div>
        <div class="left-bottom" data-left-bottom>
          <a class="filebrowser-link" data-filebrowser-link href="#" target="_blank" rel="noopener">Open File Browser</a>
          <div class="left-gap"></div>
          <ul class="mini-stage-list" data-stage-list></ul>
          <div class="left-gap"></div>
          <div class="env-line" data-env-line></div>
        </div>
      </aside>
      <main class="center-column">
        <div class="center-stack" data-center-stack>
          <div class="brand-wrap">
            <img class="brand-mark" data-brand-mark src="/assets/images/comfy-brand-mark.svg" alt="Comfy" draggable="false">
          </div>
          <div class="progress-track" aria-hidden="true"><div class="progress-runner"></div></div>
          <div class="current-stage" data-current-stage>Checking pod health</div>
          <div class="hint-note" data-start-note>If it takes more than 5 minutes, redeploy the pod</div>
        </div>
        <div class="fatal-panel" data-fatal-panel>
          <div class="fatal-title" data-fatal-title></div>
          <div class="fatal-columns">
            <div class="fatal-detail" data-fatal-detail-en></div>
            <div class="fatal-detail fatal-detail-ru" data-fatal-detail-ru>
              Проверка CUDA/драйверов GPU не пройдена. На этом хосте GPU-драйверы выглядят сломанными или устаревшими. Пересоздайте под на другом хосте.
            </div>
          </div>
        </div>
        <div class="cpu-panel" data-cpu-panel>
          <div class="cpu-title" data-cpu-title></div>
          <div class="cpu-message" data-cpu-message></div>
          <div class="cpu-columns">
            <div class="cpu-detail" data-cpu-detail-en>
              Host CPU resources are in a degraded state (CPU overload), making this host run ~3&#215; slower than expected. Redeploy the pod this pod to a different host.<br>
              If you keep landing on the same host: switch to Secure, or (if you&rsquo;re on Community) choose a different GPU model and/or tweak your filters so you hit a different pool of machines.<br><br>
              You can message RunPod support, but they&rsquo;re usually not helpful for Community host problem.
            </div>
            <div class="cpu-detail cpu-detail-ru" data-cpu-detail-ru>
              CPU на этом хосте работает в аварийном режиме (перегрузка), из-за чего он работает примерно в 3 раза медленнее, чем должен. Передеплойте под на другом хосте.<br><br>
              Если вас постоянно размещает на этот же хост: перейдите на Secure, либо (если вы в Community) выберите другую модель GPU и/или поменяйте набор фильтров, чтобы попасть в другой пул машин.<br>
              Вы можете написать в поддержку RunPod, но на Community поддержка обычно бесполезна.
            </div>
          </div>
          <a class="cpu-support-link" data-cpu-support-link href="https://contact.runpod.io/hc/en-us/requests/new" target="_blank" rel="noopener">Prepared message for RunPod support<br>https://contact.runpod.io/hc/en-us/requests/new</a>
          <div class="cpu-support-wrap">
            <textarea class="cpu-support-text" data-cpu-support-text readonly></textarea>
          </div>
          <div class="cpu-actions">
            <button type="button" class="cpu-button" data-cpu-continue-btn>Continue anyway</button>
            <button type="button" class="cpu-button" data-cpu-copy-btn>Copy message</button>
            <span class="cpu-status" data-cpu-status></span>
            <span class="cpu-copy-status" data-cpu-copy-status></span>
          </div>
        </div>
      </main>
      <aside class="right-column" data-right-column>
        <div class="node-catalog is-hidden" data-node-catalog>
          <div class="node-count" data-node-count></div>
          <ul class="node-list" data-node-list></ul>
        </div>
      </aside>
    </div>
  </div>
  <script>
    const stageList = document.querySelector('[data-stage-list]');
    const envLine = document.querySelector('[data-env-line]');
    const nodeCatalog = document.querySelector('[data-node-catalog]');
    const nodeCount = document.querySelector('[data-node-count]');
    const nodeList = document.querySelector('[data-node-list]');
    const fatalPanel = document.querySelector('[data-fatal-panel]');
    const fatalTitle = document.querySelector('[data-fatal-title]');
    const fatalDetailEn = document.querySelector('[data-fatal-detail-en]');
    const fatalDetailRu = document.querySelector('[data-fatal-detail-ru]');
    const cpuPanel = document.querySelector('[data-cpu-panel]');
    const cpuTitle = document.querySelector('[data-cpu-title]');
    const cpuMessage = document.querySelector('[data-cpu-message]');
    const cpuContinueBtn = document.querySelector('[data-cpu-continue-btn]');
    const cpuCopyBtn = document.querySelector('[data-cpu-copy-btn]');
    const cpuSupportText = document.querySelector('[data-cpu-support-text]');
    const cpuStatus = document.querySelector('[data-cpu-status]');
    const cpuCopyStatus = document.querySelector('[data-cpu-copy-status]');
    const startNote = document.querySelector('[data-start-note]');
    const templateTitle = document.querySelector('[data-template-title]');
    const currentStage = document.querySelector('[data-current-stage]');
    const fileBrowserLink = document.querySelector('[data-filebrowser-link]');
    const brandMark = document.querySelector('[data-brand-mark]');
    const leftColumn = document.querySelector('[data-left-column]');
    const rightColumn = document.querySelector('[data-right-column]');
    const centerStack = document.querySelector('[data-center-stack]');
    const standardBlocks = [centerStack];
    const BRAND_MARK_FALLBACK = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjc4IiBoZWlnaHQ9Ijc4IiB2aWV3Qm94PSIwIDAgMjc4IDc4IiBmaWxsPSJub25lIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPgo8cGF0aCBkPSJNMjMyLjE1MSA3Ny4xNzYxQzIzMC42NDUgNzcuMTc2MSAyMjkuNDMgNzYuNjIwOCAyMjguNjM4IDc1LjU2OTdDMjI3LjgyMyA3NC40ODg5IDIyNy42MTEgNzIuOTgxMSAyMjguMDU1IDcxLjQzMzNMMjMwLjgwMSA2MS44NTc2QzIzMS43MDggNTguNjkxNCAyMzUuMDIzIDU2LjExNTcgMjM4LjE5IDU2LjExNTdIMjQ0LjM0NkMyNDUuMDc4IDU2LjExNTcgMjQ1LjcyMSA1NS42MzEgMjQ1LjkyMyA1NC45Mjc2TDI0Ni45ODUgNTEuMjI0NUMyNDcuMTI3IDUwLjcyODYgMjQ3LjAyOCA1MC4xOTU4IDI0Ni43MTggNDkuNzg0QzI0Ni40MDggNDkuMzczIDI0NS45MjMgNDkuMTMxIDI0NS40MDcgNDkuMTMxTDIzMS45MTcgNDkuMTMzNEMyMzEuODQxIDQ5LjEyMyAyMzEuNzcxIDQ5LjExODEgMjMxLjY5NCA0OS4xMTgxSDIyNy4xNDlDMjI1LjY0MyA0OS4xMTgxIDIyNC40MjggNDguNTYyOSAyMjMuNjM2IDQ3LjUxMThDMjIyLjgyMiA0Ni40MzEgMjIyLjYwOSA0NC45MjMyIDIyMy4wNTMgNDMuMzc2MkwyMjMuNTgzIDQxLjUyNzFDMjIzLjcyNSA0MS4wMzIgMjIzLjYyNiA0MC40OTkyIDIyMy4zMTYgNDAuMDg3M0MyMjMuMDA2IDM5LjY3NjMgMjIyLjUyMSAzOS40MzQ0IDIyMi4wMDYgMzkuNDM0NEgyMjAuMjY1QzIxOS41MzIgMzkuNDM0NCAyMTguODg5IDM5LjkxOTEgMjE4LjY4NyA0MC42MjMzTDIxNy44OTggNDMuMzc2MkMyMTYuOTkxIDQ2LjU0MjQgMjEzLjY3NiA0OS4xMTgxIDIxMC41MDkgNDkuMTE4MUgyMDQuMzY5QzIwMy42MzggNDkuMTE4MSAyMDIuOTk1IDQ5LjYwMTMgMjAyLjc5MiA1MC4zMDNMMjAxLjA0NyA1Ni4zNDE1QzIwMS4wNCA1Ni4zNjQ3IDIwMS4wMiA1Ni40MjY0IDIwMS4wMTMgNTYuNDQ5NkwxOTguNTIyIDY1LjA3ODNDMTk4LjUxMSA2NS4xMDc5IDE5OC40OTYgNjUuMTU1MiAxOTguNDg3IDY1LjE4NTdMMTk2LjY4NSA3MS40MzE2QzE5NS43NzcgNzQuNjAwMiAxOTIuNDYyIDc3LjE3NjEgMTg5LjI5NSA3Ny4xNzYxSDE3OS43MkMxNzguMjE0IDc3LjE3NjEgMTc2Ljk5OSA3Ni42MjA4IDE3Ni4yMDcgNzUuNTY5N0MxNzUuMzkzIDc0LjQ4OTcgMTc1LjE4IDcyLjk4MTkgMTc1LjYyNCA3MS40MzQxTDE4My42NDkgNDMuNTk2NkMxODMuNjU5IDQzLjU2NzcgMTgzLjY3OCA0My41MTAxIDE4My42ODcgNDMuNDgwNEwxODQuMjQ3IDQxLjUyODhDMTg0LjM5IDQxLjAzMzcgMTg0LjI5MiA0MC41MDAxIDE4My45ODIgNDAuMDg4M0MxODMuNjcxIDM5LjY3NjQgMTgzLjE4NiAzOS40MzQ1IDE4Mi42NzEgMzkuNDM0NUgxODAuOTQ3QzE4MC4yMTYgMzkuNDM0NSAxNzkuNTc0IDM5LjkxODMgMTc5LjM3MSA0MC42MjAyTDE3NS44NzggNTIuNzI1MUMxNzUuODY3IDUyLjc1NDcgMTc1Ljg1MiA1Mi44MDI4IDE3NS44NDQgNTIuODMyNUwxNzQuNTI1IDU3LjQwMTZDMTczLjYxNyA2MC41NzEgMTcwLjMwMiA2My4xNDc2IDE2Ny4xMzYgNjMuMTQ3NkgxNTcuNTZDMTU2LjA1NCA2My4xNDc2IDE1NC44MzkgNjIuNTkyMyAxNTQuMDQ3IDYxLjU0MTJDMTUzLjIzMyA2MC40NTk2IDE1My4wMiA1OC45NTE5IDE1My40NjQgNTcuNDA0OEwxNTkuMjgzIDM3LjIwMjRDMTU5LjQyNiAzNi43MDczIDE1OS4zMjcgMzYuMTczNyAxNTkuMDE3IDM1Ljc2MTFDMTU4LjcwNyAzNS4zNDkzIDE1OC4yMjIgMzUuMTA3NCAxNTcuNzA2IDM1LjEwNzRIMTU1Ljk3M0MxNTUuMjQxIDM1LjEwNzQgMTU0LjU5NyAzNS41OTIxIDE1NC4zOTUgMzYuMjk1NUwxNTEuOTE1IDQ0LjkzNDVDMTUxLjkwNSA0NC45NjM0IDE1MS44ODkgNDUuMDEwNiAxNTEuODgxIDQ1LjA0MDNMMTQ4LjMxMSA1Ny40MDE2QzE0Ny40MDEgNjAuNTcxIDE0NC4wODYgNjMuMTQ3NiAxNDAuOTIgNjMuMTQ3NkgxMzEuMzQ1QzEyOS44MzkgNjMuMTQ3NiAxMjguNjI0IDYyLjU5MjMgMTI3LjgzMiA2MS41NDEyQzEyNy4wMTcgNjAuNDYwNCAxMjYuODA1IDU4Ljk1MjYgMTI3LjI0OSA1Ny40MDQ4TDEyOS45OTUgNDcuODI5MUMxMzAuMDA1IDQ3LjgwMDMgMTMwLjAyIDQ3Ljc1NDYgMTMwLjAyOCA0Ny43MjQ5TDEzMy4wNzIgMzcuMTg1NUMxMzMuMjE1IDM2LjY5MDQgMTMzLjExNyAzNi4xNTYgMTMyLjgwNyAzNS43NDQyQzEzMi40OTcgMzUuMzMyNCAxMzIuMDEyIDM1LjA4OTYgMTMxLjQ5NiAzNS4wODk2SDEyOS43NzlDMTI5LjA0OCAzNS4wODk2IDEyOC40MDUgMzUuNTczNSAxMjguMjAyIDM2LjI3NTRMMTI3Ljk1NCAzNy4xMzc0QzEyNy45NDUgMzcuMTYyMyAxMjcuOTI2IDM3LjIyMDggMTI3LjkxOSAzNy4yNDU2TDEyMi4zMiA1Ni42MjE5QzEyMi4zMTIgNTYuNjQ3NSAxMjIuMjkxIDU2LjcxIDEyMi4yODQgNTYuNzM1N0wxMjIuMDkzIDU3LjQwNzlDMTIxLjE4NiA2MC41NzA5IDExNy44NzEgNjMuMTQ3NSAxMTQuNzA1IDYzLjE0NzVIMTA1LjEyOUMxMDMuNjIzIDYzLjE0NzUgMTAyLjQwOCA2Mi41OTIyIDEwMS42MTYgNjEuNTQxMUMxMDAuODAyIDYwLjQ2MDMgMTAwLjU5IDU4Ljk1MjUgMTAxLjAzMyA1Ny40MDQ3TDEwMS41NjEgNTUuNTYzNkMxMDEuNzAzIDU1LjA2ODUgMTAxLjYwNCA1NC41MzU3IDEwMS4yOTQgNTQuMTIzOUMxMDAuOTg0IDUzLjcxMjkgMTAwLjQ5OSA1My40NzA5IDk5Ljk4MzkgNTMuNDcwOUg5OC4yNDg2Qzk3LjUxOCA1My40NzA5IDk2Ljg3NSA1My45NTQxIDk2LjY3MjMgNTQuNjU2N0w5NS44Nzk1IDU3LjQwMTVDOTQuOTcwNiA2MC41NzA5IDkxLjY1NTggNjMuMTQ3NSA4OC40ODkxIDYzLjE0NzVIODIuMjgzNkM4MS41NTE4IDYzLjE0NzUgODAuOTA4IDYzLjYzMjIgODAuNzA2NSA2NC4zMzU2TDc4LjY3MTEgNzEuNDMzMkM3Ny43NjM3IDc0LjYwMDIgNzQuNDQ4OSA3Ny4xNzYgNzEuMjgxOSA3Ny4xNzZINjEuNzA2N0M2MC4yMDAxIDc3LjE3NiA1OC45ODUxIDc2LjYyMDcgNTguMTkzMiA3NS41Njk2QzU3LjM3ODggNzQuNDg4OCA1Ny4xNjY0IDcyLjk4MSA1Ny42MTA3IDcxLjQzMzJMNTkuMzg2MSA2NS4yNDAxQzU5LjUyODMgNjQuNzQ1IDU5LjQyOTMgNjQuMjEyMyA1OS4xMTkzIDYzLjgwMDRDNTguODA5MyA2My4zODk0IDU4LjMyNDIgNjMuMTQ3NSA1Ny44MDkgNjMuMTQ3NUg1Mi42OTg3QzUxLjE5MjkgNjMuMTQ3NSA0OS45Nzc5IDYyLjU5MjIgNDkuMTg1MiA2MS41NDExQzQ4LjM3MTIgNjAuNDYwMyA0OC4xNTg1IDU4Ljk1MjUgNDguNjAyNCA1Ny40MDU1TDQ5LjEzMDQgNTUuNTYzNkM0OS4yNzIyIDU1LjA2ODUgNDkuMTczMiA1NC41MzU3IDQ4Ljg2MzIgNTQuMTIzOUM0OC41NTMyIDUzLjcxMjkgNDguMDY4IDUzLjQ3MDkgNDcuNTUyOCA1My40NzA5SDQ1LjgxMjNDNDUuMDgwNCA1My40NzA5IDQ0LjQzNyA1My45NTU2IDQ0LjIzNTEgNTQuNjU5TDQzLjQ0NzYgNTcuNDA0N0M0Mi41Mzk5IDYwLjU3MTcgMzkuMjI1IDYzLjE0NzUgMzYuMDU4NCA2My4xNDc1TDIyLjk0MjggNjMuMTcwN0wxMy4zMzk1IDYzLjE3MTVDMTEuODMzNCA2My4xNzE1IDEwLjYxODQgNjIuNjE2MyA5LjgyNjQzIDYxLjU2NTJDOS4wMTI0MyA2MC40ODQ0IDguNzk5NyA1OC45NzY2IDkuMjQzNjIgNTcuNDI5NUwxMS4wMjY2IDUxLjIxMTZDMTEuMTY4OCA1MC43MTU3IDExLjA2OTggNTAuMTgyOSAxMC43NTk4IDQ5Ljc3MTFDMTAuNDQ5OCA0OS4zNjAxIDkuOTY0NjcgNDkuMTE4MSA5LjQ0OTQ2IDQ5LjExODFINC4zMjMxOUMyLjgxNzAyIDQ5LjExODEgMS42MDIwMSA0OC41NjI5IDAuODEwMDc2IDQ3LjUxMThDLTAuMDAzOTI0IDQ2LjQzMSAtMC4yMTY2NTQgNDQuOTIzMiAwLjIyNzI2NSA0My4zNzYyTDUuMDAwNjggMjYuNzg3OUM1LjAwOTQ2IDI2Ljc2MzEgNS4wMjkxNSAyNi43MDA2IDUuMDM2NjkgMjYuNjc1OEw2LjU3NzczIDIxLjMzMjhDNi41OTE3NiAyMS4yOTQzIDYuNjAzNzQgMjEuMjU2NiA2LjYxNDk4IDIxLjIxNzRMNy4wMjkyIDE5Ljc3MTNDNy45MzY4OSAxNi42MDUxIDExLjI1MTQgMTQuMDI4NSAxNC40MTggMTQuMDI4NUgyMC41NTE0QzIxLjI4MzIgMTQuMDI4NSAyMS45MjcgMTMuNTQzOCAyMi4xMjg1IDEyLjg0MDRMMjQuMTU2NyA1Ljc2NzY2QzI1LjA2NDUgMi42MDA1OCAyOC4zNzkzIDAuMDI0ODU4MSAzMS41NDYzIDAuMDI0ODU4MUw0NC42OTE1IDBINTQuMjY0N0M1NS43NzA5IDAgNTYuOTg1OSAwLjU1NTI0NiA1Ny43Nzc4IDEuNjA2MzRDNTguNTkyMiAyLjY4NzE0IDU4LjgwNDUgNC4xOTQ5NSA1OC4zNjA2IDUuNzQyOEw1NS42MTUgMTUuMzE5MkM1NC43MDY5IDE4LjQ4NTUgNTEuMzkyIDIxLjA2MTIgNDguMjI1NCAyMS4wNjEyTDM1LjA4MDIgMjEuMDg1M0gyOC45NDkzQzI4LjIxNzggMjEuMDg1MyAyNy41NzQ1IDIxLjU2OTkgMjcuMzcyMiAyMi4yNzI2TDI0LjY2NzggMzEuNjg0QzI0LjY1NzggMzEuNzEzNiAyNC42NDI1IDMxLjc2MDkgMjQuNjM0MiAzMS43OTA2TDIyLjI2MTUgNDAuMDA3NEMyMi4xMTgxIDQwLjUwMzMgMjIuMjE2NiA0MS4wMzc3IDIyLjUyNzQgNDEuNDUwM0MyMi44Mzc1IDQxLjg2MTMgMjMuMzIyNiA0Mi4xMDMyIDIzLjgzNzggNDIuMTAzMkMyMy44MzkgNDIuMTAzMiAzMi41MjQxIDQyLjA4NjQgMzIuNTI0MSA0Mi4wODY0SDQyLjA5NzNDNDMuNjAzNCA0Mi4wODY0IDQ0LjgxODQgNDIuNjQxNyA0NS42MTA0IDQzLjY5MjhDNDYuNDI0OCA0NC43NzM2IDQ2LjYzNzEgNDYuMjgxNCA0Ni4xOTMyIDQ3LjgyOTJMNDUuNjQ2NCA0OS43Mzc2QzQ1LjUwNDIgNTAuMjMyNyA0NS42MDMyIDUwLjc2NTUgNDUuOTEzMiA1MS4xNzczQzQ2LjIyMzIgNTEuNTg4NCA0Ni43MDgzIDUxLjgzMDMgNDcuMjIzNSA1MS44MzAzSDQ4Ljk2NDFDNDkuNjk1NiA1MS44MzAzIDUwLjMzODkgNTEuMzQ1NiA1MC41NDEyIDUwLjY0MjlMNTEuNTczNSA0Ny4wNTEzQzUxLjU4MzUgNDcuMDIxNyA1NS40MDMyIDMzLjgwMzIgNTUuNDAzMiAzMy44MDMyQzU2LjMxMTcgMzAuNjMzNyA1OS42MjY1IDI4LjA1OCA2Mi43OTM2IDI4LjA1OEg2OC45MDU3QzY5LjYzNzYgMjguMDU4IDcwLjI4MTMgMjcuNTczMyA3MC40ODI4IDI2Ljg2OTFMNzIuNTEyNiAxOS43ODkxQzczLjQyMTEgMTYuNjIyOSA3Ni43MzYzIDE0LjA0NjMgNzkuOTAyNiAxNC4wNDYzSDg5LjQ3NzRDOTAuOTgzNiAxNC4wNDYzIDkyLjE5ODYgMTQuNjAxNiA5Mi45OTA1IDE1LjY1MjZDOTMuODA0OSAxNi43MzM0IDk0LjAxNzYgMTguMjQxMyA5My41NzM3IDE5Ljc4ODNMOTEuODAyNyAyNS45NjUzQzkxLjY2MDYgMjYuNDYwNCA5MS43NTk1IDI2Ljk5MzIgOTIuMDY5NSAyNy40MDVDOTIuMzc5NSAyNy44MTYxIDkyLjg2NDcgMjguMDU4IDkzLjM3OTkgMjguMDU4SDk4LjU4NDNDMTAwLjA5IDI4LjA1OCAxMDEuMzA1IDI4LjYxMzIgMTAyLjA5NyAyOS42NjQzQzEwMi45MTIgMzAuNzQ1MSAxMDMuMTI0IDMyLjI1MjkgMTAyLjY4IDMzLjgwMDhMOTguMDk0NCA0OS43MzYxQzk3Ljk1MTggNTAuMjMxMiA5OC4wNTAzIDUwLjc2NDggOTguMzYwMyA1MS4xNzY2Qzk4LjY3IDUxLjU4ODQgOTkuMTU1NSA1MS44MzA0IDk5LjY3MTEgNTEuODMwNEgxMDEuMzk1QzEwMi4xMjYgNTEuODMwNCAxMDIuNzY5IDUxLjM0NjUgMTAyLjk3MiA1MC42NDM4TDEwNS4xMzEgNDMuMTU0NUMxMDUuMTM4IDQzLjEzMjEgMTA1LjE1OCA0My4wNzIgMTA1LjE2NCA0My4wNDk2TDExMC43NjMgMjMuNjcyNUMxMTAuNzc0IDIzLjY0MiAxMTAuNzg5IDIzLjU5MzEgMTEwLjc5OCAyMy41NjI3TDExMS44OSAxOS43NzM5QzExMi43OTkgMTYuNjA1MyAxMTYuMTE0IDE0LjAyODggMTE5LjI4IDE0LjAyODhIMTQxLjk1N0MxNDMuNDYzIDE0LjAyODggMTQ0LjY3OCAxNC41ODQ3IDE0NS40NyAxNS42MzU5QzE0Ni4yODUgMTYuNzE1OSAxNDYuNDk4IDE4LjIyMzcgMTQ2LjA1NCAxOS43NzA3TDE0NC4yNzggMjUuOTY0NkMxNDQuMTM1IDI2LjQ2MDUgMTQ0LjIzNCAyNi45OTMzIDE0NC41NDQgMjcuNDA0M0MxNDQuODU0IDI3LjgxNjEgMTQ1LjMzOSAyOC4wNTgxIDE0NS44NTUgMjguMDU4MUgxNDcuNjQzQzE0OC4zNzUgMjguMDU4MSAxNDkuMDE5IDI3LjU3MzQgMTQ5LjIyIDI2Ljg3TDE1MS4yNTEgMTkuNzg5MkMxNTIuMTU5IDE2LjYyMjkgMTU1LjQ3NCAxNC4wNDY0IDE1OC42NCAxNC4wNDY0SDE2OC4yMTVDMTY5LjcyMiAxNC4wNDY0IDE3MC45MzcgMTQuNjAxNiAxNzEuNzI5IDE1LjY1MjdDMTcyLjU0MyAxNi43MzM1IDE3Mi43NTUgMTguMjQxMyAxNzIuMzExIDE5Ljc4OTJMMTcwLjU0MSAyNS45NjU0QzE3MC4zOTkgMjYuNDYwNSAxNzAuNDk4IDI2Ljk5MzMgMTcwLjgwOCAyNy40MDUxQzE3MS4xMTggMjcuODE2MSAxNzEuNjAzIDI4LjA1ODEgMTcyLjExOCAyOC4wNTgxSDE3Ny4yMzFDMTc4LjczNyAyOC4wNTgxIDE3OS45NTIgMjguNjEzMyAxODAuNzQ0IDI5LjY2NDRDMTgxLjU1OCAzMC43NDUyIDE4MS43NyAzMi4yNTMgMTgxLjMyNiAzMy44MDAxTDE4MC43ODIgMzUuNzAxMkMxODAuNjM5IDM2LjE5NjQgMTgwLjczOCAzNi43MjkxIDE4MS4wNDggMzcuMTQxQzE4MS4zNTggMzcuNTUyIDE4MS44NDQgMzcuNzkzOSAxODIuMzU5IDM3Ljc5MzlIMTg0LjA5M0MxODQuODI0IDM3Ljc5MzkgMTg1LjQ2NyAzNy4zMTA4IDE4NS42NyAzNi42MDlMMTkwLjUzNiAxOS43NzQ4QzE5MS40NDUgMTYuNjA0NiAxOTQuNzYgMTQuMDI4OCAxOTcuOTI2IDE0LjAyODhIMjA0LjA4MkMyMDQuODE0IDE0LjAyODggMjA1LjQ1NyAxMy41NDQxIDIwNS42NTkgMTIuODQwN0wyMDcuNjk1IDUuNzQzMTNDMjA4LjYwMyAyLjU3NjA1IDIxMS45MTggMC4wMDAzMjgxNiAyMTUuMDg0IDAuMDAwMzI4MTZIMjI0LjY1OUMyMjYuMTY2IDAuMDAwMzI4MTYgMjI3LjM4MSAwLjU1NTU3NSAyMjguMTczIDEuNjA2NjdDMjI4Ljk4NyAyLjY4NzQ2IDIyOS4yIDQuMTk1MjggMjI4Ljc1NSA1Ljc0MzEzTDIyNi4wMSAxNS4zMTk2QzIyNS4xMDIgMTguNDg1OCAyMjEuNzg3IDIxLjA2MTUgMjE4LjYyMSAyMS4wNjE1SDIxMi40NjRDMjExLjczMyAyMS4wNjE1IDIxMS4wODkgMjEuNTQ2MiAyMTAuODg3IDIyLjI1MDVMMjA5LjgyMiAyNS45NjU1QzIwOS42OCAyNi40NjA2IDIwOS43NzkgMjYuOTkzNCAyMTAuMDg5IDI3LjQwNTJDMjEwLjM5OSAyNy44MTYyIDIxMC44ODQgMjguMDU4MiAyMTEuMzk5IDI4LjA1ODJIMjE2LjU0OEMyMTguMDU0IDI4LjA1ODIgMjE5LjI2OSAyOC42MTM0IDIyMC4wNjEgMjkuNjY0NUMyMjAuODc2IDMwLjc0NDUgMjIxLjA4OCAzMi4yNTIzIDIyMC42NDQgMzMuODAwMUwyMjAuMDk5IDM1LjcwMTNDMjE5Ljk1NyAzNi4xOTY0IDIyMC4wNTYgMzYuNzI5MiAyMjAuMzY2IDM3LjE0MUMyMjAuNjc2IDM3LjU1MjEgMjIxLjE2MSAzNy43OTQgMjIxLjY3NiAzNy43OTRIMjIzLjQxN0MyMjQuMTQ4IDM3Ljc5NCAyMjQuNzkxIDM3LjMxMDkgMjI0Ljk5NCAzNi42MDgzTDIyOS44NTMgMTkuNzc0OEMyMzAuNzYzIDE2LjYwNTQgMjM0LjA3OCAxNC4wMjg4IDIzNy4yNDQgMTQuMDI4OEgyNDYuODE5QzI0OC4zMjYgMTQuMDI4OCAyNDkuNTQxIDE0LjU4NDEgMjUwLjMzMyAxNS42MzUyQzI1MS4xNDcgMTYuNzE2IDI1MS4zNTkgMTguMjIzOCAyNTAuOTE1IDE5Ljc3MTZMMjQ2LjU5MyAzNC44MDE2QzI0Ni41ODIgMzQuODMxMiAyNDYuNTY3IDM0Ljg3ODYgMjQ2LjU1OCAzNC45MDlMMjQ2LjA5NSAzNi41MTdDMjQ1Ljk1MyAzNy4wMTI5IDI0Ni4wNTEgMzcuNTQ2NSAyNDYuMzYxIDM3Ljk1ODJDMjQ2LjY3MSAzOC4zNyAyNDcuMTU2IDM4LjYxMiAyNDcuNjcyIDM4LjYxMkgyNDkuMzg1QzI1MC4xMTYgMzguNjEyIDI1MC43NTkgMzguMTI4MiAyNTAuOTYxIDM3LjQyNjNMMjU2LjA1NCAxOS43NzQ5QzI1Ni45NjMgMTYuNjA1NSAyNjAuMjc3IDE0LjAyODkgMjYzLjQ0NCAxNC4wMjg5SDI3My4wMTlDMjc0LjUyNSAxNC4wMjg5IDI3NS43NCAxNC41ODQ5IDI3Ni41MzIgMTUuNjM2MUMyNzcuMzQ3IDE2LjcxNjEgMjc3LjU1OSAxOC4yMjM5IDI3Ny4xMTUgMTkuNzcxN0wyNjYuMjc0IDU3LjQwMzVDMjY1LjM2NSA2MC41NzIyIDI2Mi4wNSA2My4xNDggMjU4Ljg4NCA2My4xNDhIMjUyLjcyOEMyNTEuOTk2IDYzLjE0OCAyNTEuMzUyIDYzLjYzMjcgMjUxLjE1MSA2NC4zMzYxTDI0OS4xMTUgNzEuNDMzN0MyNDguMjA4IDc0LjYwMDcgMjQ0Ljg5MyA3Ny4xNzY1IDI0MS43MjYgNzcuMTc2NUwyMzIuMTUxIDc3LjE3NjFaTTc3LjMyNjQgMzUuMTA3NEM3Ni41OTU0IDM1LjEwNzQgNzUuOTUyNCAzNS41OTEyIDc1Ljc0OTcgMzYuMjkzOUw3MC42NDEgNTQuMDIwNkM3MC40OTg0IDU0LjUxNTcgNzAuNTk2NSA1NS4wNDkzIDcwLjkwNjYgNTUuNDYxMUM3MS4yMTY2IDU1Ljg3MzcgNzEuNzAyMSA1Ni4xMTU3IDcyLjIxNzcgNTYuMTE1N0g3My45NTFDNzQuNjgyMSA1Ni4xMTU3IDc1LjMyNSA1NS42MzE5IDc1LjUyNzcgNTQuOTI5Mkw4MC42MzY0IDM3LjIwMjVDODAuNzc5IDM2LjcwNzQgODAuNjgwOSAzNi4xNzM4IDgwLjM3MDkgMzUuNzYyQzgwLjA2MDggMzUuMzQ5NCA3OS41NzUzIDM1LjEwNzQgNzkuMDU5OCAzNS4xMDc0SDc3LjMyNjRaIiBmaWxsPSIjRjBGRjQxIj48L3BhdGg+Cjwvc3ZnPg==";
    const BASE_STAGE_FONT_SIZE = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--stage-font-size')) || 26;
    const MIN_STAGE_FONT_SIZE = 12;
    let supportMessageInitialized = false;
    let fatalActive = false;
    let cpuConfirmPending = false;
    let lastRedirectAt = 0;

    function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), timeoutMs);
      return fetch(url, { ...options, signal: controller.signal })
        .finally(() => window.clearTimeout(timer));
    }

    function redirectWithCooldown(url) {
      const now = Date.now();
      if (now - lastRedirectAt < 15000) {
        return;
      }
      lastRedirectAt = now;
      window.location.replace(url);
    }

    function updateStartNote() {
      if (!startNote) return;
      startNote.textContent = 'If it takes more than 5 minutes, redeploy the pod';
    }

    function fitCurrentStageText() {
      if (!currentStage || !centerStack) return;
      const available = Math.max(0, centerStack.clientWidth - 6);
      let size = BASE_STAGE_FONT_SIZE;
      currentStage.style.fontSize = `${size}px`;
      while (size > MIN_STAGE_FONT_SIZE && currentStage.scrollWidth > available) {
        size -= 1;
        currentStage.style.fontSize = `${size}px`;
      }
    }

    function setErrorVisualState() {
      const freezeProgress = fatalActive || cpuConfirmPending;
      document.body.classList.toggle('error-focus', freezeProgress);
    }

    function setBlockingMode() {
      const active = fatalActive || cpuConfirmPending;
      standardBlocks.forEach((block) => {
        if (block) {
          block.classList.toggle('is-hidden', active);
        }
      });
      setErrorVisualState();
    }

    function escapeHtml(value) {
      return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function detectFileBrowserUrl() {
      try {
        const target = new URL(window.location.href);
        const labels = target.hostname.split('.');
        const firstLabel = labels[0] || '';
        const runpodHost = /\\.runpod\\.net$/i.test(target.hostname);
        const hasTcpSuffix = /-[0-9]+$/.test(firstLabel);
        if (!runpodHost || !hasTcpSuffix) {
          return null;
        }

        let changed = false;
        if (/-7860$/.test(firstLabel)) {
          labels[0] = firstLabel.replace(/-7860$/, '-7861');
          target.hostname = labels.join('.');
          changed = true;
        } else {
          const match = firstLabel.match(/^(.*-)([0-9]+)$/);
          if (match) {
            const nextPort = Number(match[2]) + 1;
            if (Number.isFinite(nextPort) && nextPort > 0) {
              labels[0] = `${match[1]}${nextPort}`;
              target.hostname = labels.join('.');
              changed = true;
            }
          }
        }

        if (target.port) {
          const port = Number(target.port);
          if (!Number.isNaN(port) && port > 0) {
            target.port = String(port === 7860 ? 7861 : port + 1);
            changed = true;
          }
        }

        if (!changed) {
          return null;
        }
        return target.toString();
      } catch (err) {
        return null;
      }
    }

    function ensureFileBrowserLink() {
      if (!fileBrowserLink) return;
      const url = detectFileBrowserUrl();
      if (!url) {
        fileBrowserLink.style.display = 'none';
        fileBrowserLink.removeAttribute('href');
        return;
      }
      fileBrowserLink.style.display = 'inline-block';
      fileBrowserLink.href = url;
    }

    const DEFAULT_STAGES = [
      { label: 'Checking pod health', detail: 'Checking CUDA initialization', status: 'pending', skipped: false },
      { label: 'Creating environment', detail: 'Preparing workspace and Python environment', status: 'pending', skipped: false },
      { label: 'Updating ComfyUI repo', detail: 'Updating ComfyUI Manager package', status: 'pending', skipped: false },
      { label: 'Updating ComfyUI and custom nodes', detail: 'Updating ComfyUI core and custom nodes', status: 'pending', skipped: false },
      { label: 'Installing 0 node packs from the backup', detail: 'Waiting for backup restore stage...', status: 'pending', skipped: false },
      { label: 'Starting ComfyUI', detail: 'Starting ComfyUI on :8188', status: 'pending', skipped: false },
    ];

    function getCurrentStageLabel(stages) {
      if (!Array.isArray(stages) || !stages.length) {
        return 'Checking pod health';
      }
      const active = stages.find((stage) => stage.status === 'active' || stage.status === 'failed');
      if (active) {
        return active.label;
      }
      const pending = stages.find((stage) => stage.status === 'pending' && !stage.skipped);
      if (pending) {
        return pending.label;
      }
      const lastVisible = [...stages].reverse().find((stage) => !stage.skipped);
      return lastVisible ? lastVisible.label : 'Starting ComfyUI';
    }

    function renderStages(stages) {
      if (!Array.isArray(stages)) return;
      if (currentStage) {
        const stageLabel = getCurrentStageLabel(stages);
        currentStage.textContent = stageLabel;
        fitCurrentStageText();
        try {
          localStorage.setItem('comfy_wait_current_stage', stageLabel);
        } catch (err) {
          // ignore storage restrictions
        }
      }
      stageList.innerHTML = '';
      stages.forEach((stage) => {
        const li = document.createElement('li');
        const status = stage.status || 'pending';
        li.className = `mini-stage-item stage-${status}`;
        let suffix = '';
        if (status === 'done' || stage.skipped) {
          suffix = ' \\u2713';
          li.classList.add('stage-done');
        } else if (status === 'active' || status === 'failed') {
          suffix = '...';
          li.classList.add('stage-active');
        } else {
          li.classList.add('stage-pending');
        }
        li.textContent = `${stage.label}${suffix}`;
        stageList.appendChild(li);
      });
    }

    function renderCatalog(backup) {
      if (!backup || !nodeCatalog || !nodeCount || !nodeList) {
        return;
      }
      const totalCount = Number(backup.total_count || 0);
      const nodes = Array.isArray(backup.catalog_nodes) ? backup.catalog_nodes : [];
      if (totalCount <= 0 || !nodes.length) {
        nodeCatalog.classList.add('is-hidden');
        nodeList.innerHTML = '';
        return;
      }
      nodeCatalog.classList.remove('is-hidden');
      nodeCount.textContent = `${totalCount} custom node packs`;
      nodeList.innerHTML = '';
      nodes.forEach((node) => {
        const item = document.createElement('li');
        item.className = 'node-item';
        item.textContent = node.name || '';
        nodeList.appendChild(item);
      });
    }

    function renderFatal(fatal) {
      fatalActive = !!(fatal && fatal.active);
      if (!fatalActive) {
        fatalPanel.style.display = 'none';
        if (fatalDetailEn) {
          fatalDetailEn.textContent = '';
        }
        setBlockingMode();
        return;
      }
      fatalPanel.style.display = 'block';
      fatalTitle.textContent = fatal.title || 'Startup halted';
      const msg = fatal.message || 'Host GPU drivers look corrupt or outdated; CUDA initialization failed.';
      const action = fatal.action ? ` ${fatal.action}` : '';
      const detail = fatal.detail ? ` Diagnostics: ${fatal.detail}` : '';
      if (fatalDetailEn) {
        fatalDetailEn.textContent = `${msg}${action}${detail}`;
      }
      if (fatalDetailRu && !fatalDetailRu.textContent.trim()) {
        fatalDetailRu.textContent = 'Проверка CUDA/драйверов GPU не пройдена. На этом хосте GPU-драйверы выглядят сломанными или устаревшими. Пересоздайте под на другом хосте.';
      }
      setBlockingMode();
    }

    function renderCpuConfirm(cpuConfirm) {
      const required = !!(cpuConfirm && cpuConfirm.required);
      const confirmed = !!(cpuConfirm && cpuConfirm.confirmed);
      cpuConfirmPending = !fatalActive && required && !confirmed;
      setBlockingMode();

      if (fatalActive) {
        cpuPanel.style.display = 'none';
        cpuStatus.textContent = '';
        if (cpuCopyStatus) {
          cpuCopyStatus.textContent = '';
        }
        return;
      }

      if (!required || confirmed) {
        cpuPanel.style.display = 'none';
        cpuStatus.textContent = '';
        if (cpuCopyStatus) {
          cpuCopyStatus.textContent = '';
        }
        return;
      }

      cpuPanel.style.display = 'block';
      cpuTitle.textContent = cpuConfirm.title || 'CPU Overload Detected';
      cpuMessage.textContent = '';
      if (cpuSupportText && !supportMessageInitialized) {
        const supportText = cpuConfirm.support_request ? String(cpuConfirm.support_request) : '';
        if (supportText) {
          cpuSupportText.value = supportText;
          supportMessageInitialized = true;
        }
      }
      if (!cpuStatus.textContent || cpuStatus.textContent.startsWith('Confirmation received')) {
        cpuStatus.textContent = '';
      }
      cpuContinueBtn.disabled = false;
    }

    async function sendCpuContinue() {
      let res = await fetchWithTimeout('/continue?ts=' + Date.now(), { method: 'POST', cache: 'no-store' }, 4000);
      if (res.ok) {
        return true;
      }
      res = await fetchWithTimeout('/continue?ts=' + Date.now(), { method: 'GET', cache: 'no-store' }, 4000);
      return res.ok;
    }

    async function requestCpuContinue() {
      cpuContinueBtn.disabled = true;
      cpuStatus.textContent = 'Sending confirmation...';
      try {
        const ok = await sendCpuContinue();
        if (!ok) {
          throw new Error('bad status');
        }
        cpuStatus.textContent = 'Confirmation received. Waiting for startup to finish...';
      } catch (err) {
        cpuStatus.textContent = 'Failed to send confirmation. Please retry.';
        cpuContinueBtn.disabled = false;
      }
    }

    async function copySupportMessage() {
      if (!cpuSupportText) {
        return;
      }
      const text = cpuSupportText.value || cpuSupportText.textContent || '';
      if (!text.trim()) {
        cpuCopyStatus.textContent = 'Nothing to copy.';
        return;
      }
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          cpuSupportText.focus();
          cpuSupportText.select();
          document.execCommand('copy');
          cpuSupportText.setSelectionRange(0, 0);
          cpuSupportText.blur();
        }
        cpuCopyStatus.textContent = 'Copied.';
      } catch (err) {
        cpuCopyStatus.textContent = 'Copy failed. Please copy manually.';
      }
    }

    function renderEnv(env) {
      if (!env) return;
      const isStudentMode = !!env.student_mode;
      if (templateTitle) {
        templateTitle.textContent = isStudentMode ? 'comfy.course' : 'comfy.work';
      }
      try {
        localStorage.setItem('comfy_wait_template_mode', isStudentMode ? 'student' : 'work');
      } catch (err) {
        // ignore storage restrictions
      }
      const backupRepo = env.backup_repo ? String(env.backup_repo).trim() : '';
      const backupHref = backupRepo ? `https://huggingface.co/${encodeURI(backupRepo)}` : '';
      const rows = [
        { label: 'HF_TOKEN', on: !!env.hf_token, value: '', href: '' },
        { label: 'CIVITAI_API_TOKEN', on: !!env.civitai_api_key, value: '', href: '' },
        { label: 'COMFYUI_BACKUP', on: !!backupRepo, value: backupRepo, href: backupHref },
      ];
      envLine.innerHTML = rows.map((row) => {
        const rowClass = row.on ? 'env-item is-on' : 'env-item';
        const bullet = row.on ? '&#9679;' : '&#9675;';
        const value = row.on && row.value
          ? ` <a class="env-backup-link" href="${escapeHtml(row.href)}" target="_blank" rel="noopener">${escapeHtml(row.value)}</a>`
          : '';
        return `<div class="${rowClass}"><span class="env-bullet">${bullet}</span><span class="env-name">${escapeHtml(row.label)}</span>${value}</div>`;
      }).join('');
    }

    async function fetchState() {
      try {
        const res = await fetchWithTimeout('/state?ts=' + Date.now(), { cache: 'no-store' }, 4500);
        if (res.ok) {
          const data = await res.json();
          renderFatal(data.fatal);
          renderCpuConfirm(data.cpu_confirm);
          renderStages(data.stages);
          renderCatalog(data.backup);
          renderEnv(data.env);
        }
      } catch (err) {
        // swallow while backend warms up
      } finally {
        window.setTimeout(fetchState, 2000);
      }
    }

    async function probeReady() {
      if (fatalActive || cpuConfirmPending) {
        window.setTimeout(probeReady, 5000);
        return;
      }
      try {
        const res = await fetchWithTimeout('/status?ts=' + Date.now(), { cache: 'no-store' }, 3500);
        if (res.status === 200) {
          const text = (await res.text()).trim();
          if (text !== 'placeholder') {
            redirectWithCooldown('/');
            return;
          }
        } else if (res.status === 404) {
          redirectWithCooldown('/');
          return;
        } else if (res.status === 502) {
          redirectWithCooldown('/502.html');
          return;
        }
      } catch (err) {
        // ignore network hiccups
      }
      window.setTimeout(probeReady, 2000);
    }

    if (cpuContinueBtn) {
      cpuContinueBtn.addEventListener('click', requestCpuContinue);
    }
    if (cpuCopyBtn) {
      cpuCopyBtn.addEventListener('click', copySupportMessage);
    }
    function ensureBrandMark(imgEl) {
      if (!imgEl) return;
      const applyFallback = () => {
        if (imgEl.dataset.fallbackApplied === '1') {
          return;
        }
        imgEl.dataset.fallbackApplied = '1';
        imgEl.onerror = null;
        imgEl.src = BRAND_MARK_FALLBACK;
      };
      imgEl.onerror = applyFallback;
      if (imgEl.complete && imgEl.naturalWidth === 0) {
        applyFallback();
      }
    }
    ensureBrandMark(brandMark);
    ensureFileBrowserLink();
    updateStartNote();
    renderStages(DEFAULT_STAGES);
    window.addEventListener('resize', fitCurrentStageText);
    fetchState();
    probeReady();
  </script>
</body>
</html>
"""

CPU_CONFIRM_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CPU Overload Warning</title>
  <style>
    html, body { margin: 0; min-height: 100%; font-family: Arial, sans-serif; background: #f5efe9; color: #0c4eb9; }
    body { display: flex; align-items: center; justify-content: center; padding: 1rem; }
    .card { width: min(720px, 100%); background: #fffdfb; border: 1px solid rgba(12, 78, 185, 0.25); border-radius: 14px; padding: 1.4rem; box-shadow: 0 10px 28px rgba(12, 78, 185, 0.1); }
    h1 { margin: 0 0 0.7rem 0; font-size: 1.6rem; color: #9f2f1f; }
    p { margin: 0.5rem 0; line-height: 1.5; }
    .meta { margin-top: 0.9rem; font-size: 0.95rem; color: #0c4eb9; opacity: 0.9; }
    .actions { margin-top: 1.1rem; display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; }
    button {
      border: 0;
      border-radius: 8px;
      background: #0c4eb9;
      color: #fff;
      font-size: 0.96rem;
      font-weight: 600;
      padding: 0.6rem 1rem;
      cursor: pointer;
    }
    button[disabled] { opacity: 0.6; cursor: default; }
    .status { font-size: 0.9rem; color: #285ea8; min-height: 1.2rem; }
  </style>
</head>
<body>
  <main class="card">
    <h1>Host CPU resources are overloaded</h1>
    <p>Running ComfyUI on this pod may be at least three times slower than normal.</p>
    <p>It is strongly recommended to redeploy this pod on a healthier host.</p>
    <p class="meta" data-pin-cores></p>
    <div class="actions">
      <button type="button" data-continue-btn>Continue anyway</button>
      <span class="status" data-status></span>
    </div>
  </main>
  <script>
    const pinLine = document.querySelector('[data-pin-cores]');
    const statusLine = document.querySelector('[data-status]');
    const continueBtn = document.querySelector('[data-continue-btn]');

    function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), timeoutMs);
      return fetch(url, { ...options, signal: controller.signal })
        .finally(() => window.clearTimeout(timer));
    }

    async function refreshState() {
      try {
        const res = await fetchWithTimeout('/state?ts=' + Date.now(), { cache: 'no-store' }, 4500);
        if (!res.ok) return;
        const data = await res.json();
        const pin = data && data.cpu_confirm ? data.cpu_confirm.pin_cores : '';
        pinLine.textContent = pin ? `CPU pinning on continue: ${pin}` : 'CPU pinning is disabled.';
      } catch (err) {
        // ignore temporary fetch issues
      }
    }

    async function continueStartup() {
      continueBtn.disabled = true;
      statusLine.textContent = 'Sending confirmation...';
      try {
        let res = await fetchWithTimeout('/continue?ts=' + Date.now(), { method: 'POST', cache: 'no-store' }, 4000);
        if (!res.ok) {
          res = await fetchWithTimeout('/continue?ts=' + Date.now(), { method: 'GET', cache: 'no-store' }, 4000);
        }
        if (!res.ok) {
          throw new Error('bad status');
        }
        statusLine.textContent = 'Confirmation sent. Returning to startup...';
      } catch (err) {
        statusLine.textContent = 'Failed to send confirmation. Retry in a moment.';
        continueBtn.disabled = false;
      }
    }

    continueBtn.addEventListener('click', continueStartup);
    refreshState();
    window.setInterval(refreshState, 5000);
  </script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    log_path: str = "/server.log"
    mode: str = "startup"
    continue_file: str = "/tmp/continue_cpu"
    cpu_pin_cores: str = ""

    def _send_text(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path, cache_control: str = "public, max-age=3600") -> None:
        try:
            payload = file_path.read_bytes()
        except OSError:
            self._send_text(b"not found", status=404)
            return
        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"
        self.send_response(200)
        if content_type.startswith("text/"):
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        else:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(payload)

    def _confirm_continue(self) -> None:
        try:
            target = Path(self.continue_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("continue\n", encoding="utf-8")
            self._send_json({"ok": True})
        except OSError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def _handle_cpu_confirm_get(self, path: str) -> None:
        if path.startswith("/healthz"):
            self._send_text(b"ok")
            return
        if path.startswith("/ready") or path.startswith("/status"):
            self._send_text(b"cpu-confirm")
            return
        if path.startswith("/state"):
            payload = build_cpu_confirm_payload(self.continue_file, self.cpu_pin_cores)
            self._send_json(payload)
            return
        self._send_html(CPU_CONFIRM_HTML)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path or "/")

        if path.startswith("/continue"):
            self._confirm_continue()
            return

        asset_file = _resolve_asset_file(path)
        if asset_file:
            self._send_file(asset_file)
            return
        if path.startswith("/assets/"):
            self._send_text(b"not found", status=404)
            return

        if self.mode == "cpu-confirm":
            self._handle_cpu_confirm_get(path)
            return

        if path.startswith("/healthz"):
            self._send_text(b"ok")
            return
        if path.startswith("/ready"):
            self._send_text(b"placeholder")
            return
        if path.startswith("/status"):
            self._send_text(b"placeholder")
            return
        if path.startswith("/state"):
            payload = build_state_payload(self.log_path, self.continue_file, self.cpu_pin_cores)
            self._send_json(payload)
            return
        self._send_html(HTML_PAGE)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path or "/")
        if path.startswith("/continue"):
            self._confirm_continue()
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class ReuseTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["startup", "cpu-confirm"], default="startup")
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--refresh", type=int, default=2)  # kept for compatibility
    parser.add_argument("--files", nargs="*", default=["/server.log"])
    parser.add_argument("--continue-file", default="/tmp/continue_cpu")
    parser.add_argument("--cpu-pin-cores", default="")
    args = parser.parse_args()
    Handler.log_path = args.files[0]
    Handler.mode = args.mode
    Handler.continue_file = args.continue_file
    Handler.cpu_pin_cores = args.cpu_pin_cores
    with ReuseTCPServer(("", args.port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass



