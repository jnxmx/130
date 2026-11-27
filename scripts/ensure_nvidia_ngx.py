#!/usr/bin/env python3
"""Ensure NVIDIA NGX runtime libraries (libnvidia-ngx.so.1) are available.

Used by start.sh to keep ComfyUI_Nvidia_VFX_Nodes (RTX Video Super Resolution /
HDR / Frame Generation) working on cloud GPU hosts whose container runtime does
not inject NGX/graphics driver libraries into the container.

Always exits 0 on runtime/network errors so pod startup is never blocked.
"""

from __future__ import annotations

import ctypes
import fnmatch
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

DRIVER_VERSION_RE = re.compile(r"^(\d+\.\d+(?:\.\d+)?)$")
PROC_DRIVER_VERSION_RE = re.compile(r"Kernel Module\s+(\d+\.\d+(?:\.\d+)?)")
LDCONFIG_LINE_RE = re.compile(r"^\s*([^\s]+)\s+\([^)]+\)\s+=>\s+(.+)$")

HARD_DENY_PATTERNS = (
    "libcuda*",
    "libnvidia-ml*",
    "libnvidia-ptxjitcompiler*",
    "libnvidia-nvvm*",
    "libnvidia-opencl*",
    "libnvidia-cfg*",
    "libnvidia-pkcs11*",
    "libnvcuvid*",
    "libnvidia-encode*",
    "libnvidia-opticalflow*",
    "libnvidia-allocator*",
)

NGX_ALLOW_PATTERNS = (
    "libnvidia-ngx.so*",
    "nvngx*",
    "_nvngx*",
)

COMPANION_ALLOW_PATTERNS = (
    "libnvidia-rtcore.so*",
    "libnvidia-gpucomp.so*",
    "libnvidia-tls.so*",
    "libnvidia-glcore.so*",
    "libnvidia-eglcore.so*",
    "libnvidia-glsi.so*",
    "libnvidia-glvkspirv.so*",
    "libEGL_nvidia.so*",
    "libGLESv2_nvidia.so*",
    "libGLX_nvidia.so*",
)

ALL_ALLOW_PATTERNS = NGX_ALLOW_PATTERNS + COMPANION_ALLOW_PATTERNS

DEFAULT_OPT_ROOT = Path("/opt/nvidia-ngx")
DEFAULT_CACHE_DIR = Path("/workspace/.cache/nvidia-ngx")
DEFAULT_LEGACY_NVDRV = Path("/workspace/nvdrv")
DEFAULT_LD_CONF = Path("/etc/ld.so.conf.d/99-nvidia-ngx-fallback.conf")
DEFAULT_EGL_VENDOR_DIR = Path("/usr/share/glvnd/egl_vendor.d")
DEFAULT_VULKAN_ICD_DIR = Path("/etc/vulkan/icd.d")


def is_truthy(val: Optional[str], default: bool = True) -> bool:
    if val is None:
        return default
    cleaned = str(val).strip().lower()
    if not cleaned:
        return default
    return cleaned not in {"0", "false", "no", "off"}


def log(msg: str) -> None:
    print(f"[ngx] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[ngx][warn] {msg}", flush=True)


def detect_driver_version() -> Optional[str]:
    """Return the exact host NVIDIA driver version, e.g. '615.71.09'."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        for line in out.splitlines():
            candidate = line.strip()
            match = DRIVER_VERSION_RE.match(candidate)
            if match:
                return match.group(1)
    except Exception:
        pass

    proc_ver = Path("/proc/driver/nvidia/version")
    try:
        if proc_ver.is_file():
            text = proc_ver.read_text(encoding="utf-8", errors="ignore")
            match = PROC_DRIVER_VERSION_RE.search(text)
            if match:
                return match.group(1)
    except OSError:
        pass

    return None


def probe_ngx_load_in_subprocess(timeout: int = 10) -> tuple[bool, str]:
    """Probe libnvidia-ngx.so.1 in a fresh child process so ld.so.cache is fresh."""
    code = (
        "import ctypes\n"
        "ctypes.CDLL('libnvidia-ngx.so.1', mode=ctypes.RTLD_NOW)\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0 and "OK" in (proc.stdout or ""):
            return True, "OK"
        err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip().splitlines()
        return False, err[-1] if err else f"exit={proc.returncode}"
    except Exception as exc:
        return False, str(exc)


def parse_ldconfig_sonames(output: str) -> set[str]:
    """Parse `ldconfig -p` output and return the set of already-available SONAMEs."""
    sonames: set[str] = set()
    for line in output.splitlines():
        match = LDCONFIG_LINE_RE.match(line)
        if match:
            sonames.add(match.group(1).strip())
    return sonames


def get_host_ldconfig_sonames() -> set[str]:
    try:
        out = subprocess.check_output(
            ["ldconfig", "-p"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        return parse_ldconfig_sonames(out)
    except Exception:
        return set()


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _host_has_library_family(filename: str, host_sonames: set[str]) -> bool:
    """Return True if host ldconfig already provides this .so library family."""
    if ".so" not in filename:
        return filename in host_sonames
    head = filename.split(".so", 1)[0]
    prefix = f"{head}.so"
    prefix_dot = f"{prefix}."
    return any(s == prefix or s.startswith(prefix_dot) for s in host_sonames)


def is_library_allowed(
    filename: str,
    host_sonames: set[str],
    driver_version: Optional[str] = None,
) -> bool:
    """Decide whether a single 64-bit extracted driver file should be staged."""
    if not filename or filename.startswith("."):
        return False
    if _matches_any(filename, HARD_DENY_PATTERNS):
        return False
    if not _matches_any(filename, ALL_ALLOW_PATTERNS):
        return False

    # Never shadow libraries already injected by the host container runtime
    # (except libnvidia-ngx itself if a broken symlink was present).
    if not _matches_any(filename, NGX_ALLOW_PATTERNS):
        if _host_has_library_family(filename, host_sonames):
            return False

    # If a driver version is specified, avoid staging mismatched versioned .so files
    # when multiple versions exist, while still allowing non-versioned files.
    if driver_version and ".so." in filename:
        suffix = filename.split(".so.", 1)[1]
        if suffix and not suffix.startswith(driver_version) and not suffix.isdigit():
            # Keep if it looks like an unversioned ABI suffix, skip mismatched full versions.
            if re.match(r"^\d+\.\d+", suffix):
                return False

    return True


def build_deb_url(driver_version: str) -> str:
    return (
        "https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/"
        f"libnvidia-ngx1_{driver_version}-1_amd64.deb"
    )


def build_run_mirror_urls(driver_version: str) -> list[str]:
    filename = f"NVIDIA-Linux-x86_64-{driver_version}.run"
    return [
        f"https://us.download.nvidia.com/XFree86/Linux-x86_64/{driver_version}/{filename}",
        f"https://download.nvidia.com/XFree86/Linux-x86_64/{driver_version}/{filename}",
        f"https://us.download.nvidia.com/tesla/{driver_version}/{filename}",
        f"https://international.download.nvidia.com/tesla/{driver_version}/{filename}",
        f"https://international.download.nvidia.com/XFree86/Linux-x86_64/{driver_version}/{filename}",
    ]


def url_exists(url: str, timeout: int = 8) -> bool:
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"} if method == "GET" else {"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= getattr(resp, "status", 200) < 400:
                    return True
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405):
                continue
            return False
        except Exception:
            return False
    return False


def download_file(url: str, dest: Path, timeout: int = 300) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    tmp_dest.unlink(missing_ok=True)

    if shutil.which("aria2c"):
        cmd = [
            "aria2c",
            "--console-log-level=warn",
            "--summary-interval=0",
            "-x", "16",
            "-s", "16",
            "-k", "4M",
            "--max-tries=2",
            "--retry-wait=1",
            "--connect-timeout=10",
            "--timeout=30",
            "-d", str(tmp_dest.parent),
            "-o", tmp_dest.name,
            url,
        ]
        try:
            rc = subprocess.run(cmd, timeout=timeout, check=False).returncode
            if rc == 0 and tmp_dest.is_file() and tmp_dest.stat().st_size > 0:
                tmp_dest.replace(dest)
                return True
        except Exception:
            pass
        tmp_dest.unlink(missing_ok=True)

    if shutil.which("curl"):
        cmd = [
            "curl",
            "-fL",
            "--silent",
            "--show-error",
            "--retry", "2",
            "--connect-timeout", "10",
            "--max-time", str(timeout),
            "-o", str(tmp_dest),
            url,
        ]
        try:
            rc = subprocess.run(cmd, timeout=timeout + 15, check=False).returncode
            if rc == 0 and tmp_dest.is_file() and tmp_dest.stat().st_size > 0:
                tmp_dest.replace(dest)
                return True
        except Exception:
            pass
        tmp_dest.unlink(missing_ok=True)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, tmp_dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        if tmp_dest.is_file() and tmp_dest.stat().st_size > 0:
            tmp_dest.replace(dest)
            return True
    except Exception:
        pass
    tmp_dest.unlink(missing_ok=True)
    return False


def ensure_soname_symlinks(lib_dir: Path, driver_version: Optional[str] = None) -> None:
    """Create standard SONAME symlinks (and run ldconfig -n if available)."""
    if not lib_dir.is_dir():
        return

    if shutil.which("ldconfig"):
        subprocess.run(["ldconfig", "-n", str(lib_dir)], check=False)

    # Ensure explicit libnvidia-ngx.so.1 and libnvidia-ngx.so symlinks exist.
    ngx_candidates = sorted(
        p for p in lib_dir.iterdir()
        if p.name.startswith("libnvidia-ngx.so.") and p.name != "libnvidia-ngx.so.1" and p.is_file()
    )
    if ngx_candidates:
        target = None
        if driver_version:
            exact = lib_dir / f"libnvidia-ngx.so.{driver_version}"
            if exact.exists():
                target = exact
        if target is None:
            target = ngx_candidates[-1]

        so1 = lib_dir / "libnvidia-ngx.so.1"
        if not so1.exists() and not so1.is_symlink():
            so1.symlink_to(target.name)
        so0 = lib_dir / "libnvidia-ngx.so"
        if not so0.exists() and not so0.is_symlink():
            so0.symlink_to("libnvidia-ngx.so.1")


def stage_extracted_files(
    source_dir: Path,
    dest_lib_dir: Path,
    host_sonames: set[str],
    driver_version: Optional[str] = None,
    recursive_x86_64: bool = False,
) -> list[str]:
    """Copy filtered 64-bit libraries from source_dir into dest_lib_dir."""
    dest_lib_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    if recursive_x86_64:
        candidates: list[Path] = []
        for root, dirnames, filenames in os.walk(source_dir):
            # Never touch 32-bit trees
            dirnames[:] = [
                d for d in dirnames
                if d not in {"32", "i386-linux-gnu", "lib32"}
            ]
            for fname in filenames:
                candidates.append(Path(root) / fname)
    else:
        # .run --extract-only places 64-bit userspace libs directly at top-level
        candidates = [p for p in source_dir.iterdir() if p.is_file() or p.is_symlink()]

    for src in sorted(candidates, key=lambda p: p.name):
        if not is_library_allowed(src.name, host_sonames, driver_version=driver_version):
            continue
        if not src.exists():
            continue
        dst = dest_lib_dir / src.name
        if src.is_symlink():
            link_target = os.readlink(src)
            if os.path.isabs(link_target):
                # Materialize absolute symlinks as files
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                shutil.copy2(src.resolve(), dst)
            else:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(link_target)
        else:
            shutil.copy2(src, dst)
            if ".so" in dst.name:
                os.chmod(dst, 0o755)
        copied.append(src.name)

    ensure_soname_symlinks(dest_lib_dir, driver_version=driver_version)
    return copied


def register_ld_conf(
    lib_dir: Path,
    ld_conf_path: Path = DEFAULT_LD_CONF,
) -> None:
    ld_conf_path.parent.mkdir(parents=True, exist_ok=True)
    ld_conf_path.write_text(f"{lib_dir}\n", encoding="utf-8")
    subprocess.run(["ldconfig"], check=False)


def has_accessible_dri_render_node() -> bool:
    """Return True only if at least one /dev/dri/renderD* node opens for RDWR."""
    for dev in sorted(glob.glob("/dev/dri/renderD*")):
        try:
            fd = os.open(dev, os.O_RDWR)
            os.close(fd)
            return True
        except OSError:
            continue
    return False


def maybe_register_graphics_icds(
    lib_dir: Path,
    egl_vendor_dir: Path = DEFAULT_EGL_VENDOR_DIR,
    vulkan_icd_dir: Path = DEFAULT_VULKAN_ICD_DIR,
) -> None:
    """Register NVIDIA EGL/Vulkan ICD JSONs only when /dev/dri/renderD* is usable."""
    if not has_accessible_dri_render_node():
        log("DRI render nodes are inaccessible; skipping NVIDIA EGL/Vulkan ICD registration to preserve CPU Lavapipe fallback")
        return

    egl_lib = lib_dir / "libEGL_nvidia.so.0"
    if not egl_lib.exists():
        matches = sorted(lib_dir.glob("libEGL_nvidia.so*"))
        egl_lib = matches[0] if matches else None
    if egl_lib is not None:
        egl_vendor_dir.mkdir(parents=True, exist_ok=True)
        egl_json = egl_vendor_dir / "10_nvidia.json"
        if not egl_json.exists():
            egl_json.write_text(
                json.dumps(
                    {
                        "file_format_version": "1.0.0",
                        "ICD": {"library_path": str(egl_lib)},
                    },
                    indent=4,
                ) + "\n",
                encoding="utf-8",
            )

    glx_or_vk = sorted(lib_dir.glob("libGLX_nvidia.so*"))
    if glx_or_vk:
        vulkan_icd_dir.mkdir(parents=True, exist_ok=True)
        vk_json = vulkan_icd_dir / "nvidia_icd.json"
        if not vk_json.exists():
            vk_json.write_text(
                json.dumps(
                    {
                        "file_format_version": "1.0.0",
                        "ICD": {
                            "library_path": str(glx_or_vk[0]),
                            "api_version": "1.3.0",
                        },
                    },
                    indent=4,
                ) + "\n",
                encoding="utf-8",
            )


def find_cached_archive(cache_dir: Path, driver_version: str) -> Optional[Path]:
    for ext in (".tar.zst", ".tar.gz"):
        candidate = cache_dir / f"ngx-{driver_version}{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def restore_from_cache_archive(
    archive_path: Path,
    dest_lib_dir: Path,
) -> bool:
    if not archive_path.is_file():
        return False
    tmp_dir = Path(tempfile.mkdtemp(prefix="ngx-restore-", dir="/tmp"))
    try:
        if archive_path.name.endswith(".tar.zst"):
            if not shutil.which("zstd"):
                return False
            rc = subprocess.run(
                ["tar", "--zstd", "-xf", str(archive_path), "-C", str(tmp_dir)],
                check=False,
            ).returncode
        else:
            rc = subprocess.run(
                ["tar", "-xzf", str(archive_path), "-C", str(tmp_dir)],
                check=False,
            ).returncode
        if rc != 0:
            return False
        if dest_lib_dir.exists():
            shutil.rmtree(dest_lib_dir, ignore_errors=True)
        dest_lib_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_dir), str(dest_lib_dir))
        return True
    except Exception:
        return False
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def save_cache_archive(
    src_lib_dir: Path,
    cache_dir: Path,
    driver_version: str,
    max_versions: int = 2,
) -> Optional[Path]:
    if not src_lib_dir.is_dir():
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        use_zstd = shutil.which("zstd") is not None
        ext = ".tar.zst" if use_zstd else ".tar.gz"
        final_path = cache_dir / f"ngx-{driver_version}{ext}"
        tmp_path = cache_dir / f".ngx-{driver_version}.{os.getpid()}{ext}.tmp"
        tmp_path.unlink(missing_ok=True)

        cmd = (
            ["tar", "--zstd", "-cf", str(tmp_path), "-C", str(src_lib_dir), "."]
            if use_zstd
            else ["tar", "-czf", str(tmp_path), "-C", str(src_lib_dir), "."]
        )
        rc = subprocess.run(cmd, check=False).returncode
        if rc != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            return None

        os.replace(tmp_path, final_path)
        prune_cache_archives(cache_dir, max_versions=max_versions, keep_path=final_path)
        return final_path
    except Exception as exc:
        warn(f"failed to save NGX cache archive: {exc}")
        return None


def prune_cache_archives(
    cache_dir: Path,
    max_versions: int = 2,
    keep_path: Optional[Path] = None,
) -> None:
    if max_versions < 1 or not cache_dir.is_dir():
        return
    archives = sorted(
        [
            p for p in cache_dir.iterdir()
            if p.is_file() and p.name.startswith("ngx-") and (p.name.endswith(".tar.zst") or p.name.endswith(".tar.gz"))
        ],
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    if keep_path and keep_path in archives:
        archives.remove(keep_path)
        archives.insert(0, keep_path)
    for stale in archives[max_versions:]:
        try:
            stale.unlink(missing_ok=True)
            log(f"pruned old cached NGX archive {stale.name}")
        except OSError:
            pass


def try_migrate_legacy_nvdrv(
    legacy_dir: Path,
    dest_lib_dir: Path,
    host_sonames: set[str],
    driver_version: str,
) -> bool:
    """One-time migration from manual /workspace/nvdrv if driver version matches."""
    if not legacy_dir.is_dir():
        return False
    search_roots = [legacy_dir / "lib", legacy_dir]
    for root in search_roots:
        if not root.is_dir():
            continue
        exact_ngx = root / f"libnvidia-ngx.so.{driver_version}"
        if exact_ngx.is_file():
            log(f"found matching legacy NGX libraries in {root}; migrating to {dest_lib_dir}")
            copied = stage_extracted_files(
                root,
                dest_lib_dir,
                host_sonames=host_sonames,
                driver_version=driver_version,
                recursive_x86_64=False,
            )
            if copied:
                return True
    return False


def bootstrap_from_deb(
    driver_version: str,
    dest_lib_dir: Path,
    host_sonames: set[str],
    work_dir: Path,
) -> bool:
    if not shutil.which("dpkg-deb"):
        return False
    deb_url = build_deb_url(driver_version)
    if not url_exists(deb_url):
        return False
    deb_path = work_dir / f"libnvidia-ngx1_{driver_version}.deb"
    extract_dir = work_dir / "deb_extracted"
    log(f"downloading lightweight NGX package ({deb_url})...")
    if not download_file(deb_url, deb_path, timeout=120):
        return False
    rc = subprocess.run(
        ["dpkg-deb", "-x", str(deb_path), str(extract_dir)],
        check=False,
    ).returncode
    if rc != 0:
        return False
    copied = stage_extracted_files(
        extract_dir,
        dest_lib_dir,
        host_sonames=host_sonames,
        driver_version=driver_version,
        recursive_x86_64=True,
    )
    return any(name.startswith("libnvidia-ngx.so") for name in copied)


def bootstrap_from_run(
    driver_version: str,
    dest_lib_dir: Path,
    host_sonames: set[str],
    work_dir: Path,
) -> bool:
    mirrors = build_run_mirror_urls(driver_version)
    chosen_url = None
    for url in mirrors:
        if url_exists(url):
            chosen_url = url
            break
    if not chosen_url:
        warn(f"no NVIDIA .run mirror found for driver {driver_version}")
        return False

    run_path = work_dir / f"NVIDIA-Linux-x86_64-{driver_version}.run"
    extract_dir = work_dir / "run_extracted"
    log(f"downloading NVIDIA driver package for NGX ({chosen_url})...")
    if not download_file(chosen_url, run_path, timeout=420):
        warn(f"failed to download {chosen_url}")
        return False

    log("extracting userspace NGX libraries in /tmp...")
    rc = subprocess.run(
        ["sh", str(run_path), "--extract-only", "--target", str(extract_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    ).returncode
    # Remove installer immediately to free space before staging
    run_path.unlink(missing_ok=True)
    if rc != 0 or not extract_dir.is_dir():
        warn(f"failed to extract NVIDIA driver installer (rc={rc})")
        return False

    copied = stage_extracted_files(
        extract_dir,
        dest_lib_dir,
        host_sonames=host_sonames,
        driver_version=driver_version,
        recursive_x86_64=False,
    )
    if not any(name.startswith("libnvidia-ngx.so") for name in copied):
        warn("extracted driver package did not contain libnvidia-ngx.so")
        return False
    log(f"staged {len(copied)} filtered driver library file(s) to {dest_lib_dir}")
    return True


def main() -> int:
    if not is_truthy(os.environ.get("ENSURE_NVIDIA_NGX"), default=True):
        log("disabled (ENSURE_NVIDIA_NGX=0)")
        return 0

    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        log(f"skipping on unsupported platform {platform.system()}/{platform.machine()}")
        return 0

    driver_version = detect_driver_version()
    if not driver_version:
        warn("could not detect NVIDIA driver version; skipping NGX check")
        return 0

    ok, detail = probe_ngx_load_in_subprocess()
    if ok:
        log(f"host runtime already provides libnvidia-ngx.so.1 (driver {driver_version})")
        return 0

    print("STAGE: Ensuring NVIDIA NGX runtime", flush=True)
    log(f"libnvidia-ngx.so.1 missing ({detail}); bootstrapping for driver {driver_version}")

    opt_root = Path(os.environ.get("NGX_OPT_DIR", str(DEFAULT_OPT_ROOT)))
    cache_dir = Path(os.environ.get("NGX_CACHE_DIR", str(DEFAULT_CACHE_DIR)))
    legacy_dir = Path(os.environ.get("NGX_LEGACY_DIR", str(DEFAULT_LEGACY_NVDRV)))
    try:
        max_cache_versions = int(os.environ.get("NGX_CACHE_MAX_VERSIONS", "2"))
    except ValueError:
        max_cache_versions = 2

    dest_lib_dir = opt_root / driver_version / "lib"
    host_sonames = get_host_ldconfig_sonames()

    # 1. Fast cache restore (~0.1s, no network)
    cached_archive = find_cached_archive(cache_dir, driver_version)
    if cached_archive and restore_from_cache_archive(cached_archive, dest_lib_dir):
        ensure_soname_symlinks(dest_lib_dir, driver_version=driver_version)
        register_ld_conf(dest_lib_dir)
        maybe_register_graphics_icds(dest_lib_dir)
        ok, detail = probe_ngx_load_in_subprocess()
        if ok:
            log(f"restored NGX runtime from cache {cached_archive.name} (driver {driver_version})")
            return 0
        warn(f"cached NGX archive {cached_archive.name} failed verification ({detail}); re-fetching")
        shutil.rmtree(dest_lib_dir, ignore_errors=True)

    # 2. One-time migration from manual /workspace/nvdrv if version matches
    if try_migrate_legacy_nvdrv(legacy_dir, dest_lib_dir, host_sonames, driver_version):
        register_ld_conf(dest_lib_dir)
        maybe_register_graphics_icds(dest_lib_dir)
        ok, detail = probe_ngx_load_in_subprocess()
        if ok:
            save_cache_archive(dest_lib_dir, cache_dir, driver_version, max_versions=max_cache_versions)
            log(f"migrated and verified NGX runtime for driver {driver_version}")
            return 0
        warn(f"migrated NGX libraries failed verification ({detail}); falling back to download")
        shutil.rmtree(dest_lib_dir, ignore_errors=True)

    # 3. Cascading download (.deb -> .run) in /tmp with guaranteed cleanup
    work_dir = Path(tempfile.mkdtemp(prefix="ngx-bootstrap-", dir="/tmp"))
    try:
        staged = False
        if bootstrap_from_deb(driver_version, dest_lib_dir, host_sonames, work_dir):
            register_ld_conf(dest_lib_dir)
            maybe_register_graphics_icds(dest_lib_dir)
            ok, detail = probe_ngx_load_in_subprocess()
            if ok:
                staged = True
                log(f"verified lightweight .deb NGX runtime for driver {driver_version}")
            else:
                log(f"lightweight .deb NGX probe failed ({detail}); trying full .run userspace package")
                shutil.rmtree(dest_lib_dir, ignore_errors=True)

        if not staged and bootstrap_from_run(driver_version, dest_lib_dir, host_sonames, work_dir):
            register_ld_conf(dest_lib_dir)
            maybe_register_graphics_icds(dest_lib_dir)
            ok, detail = probe_ngx_load_in_subprocess()
            if ok:
                staged = True
                log(f"verified NGX runtime for driver {driver_version}")
            else:
                warn(f"NGX runtime staged for driver {driver_version}, but verification failed: {detail}")

        if staged:
            saved = save_cache_archive(dest_lib_dir, cache_dir, driver_version, max_versions=max_cache_versions)
            if saved:
                log(f"cached NGX runtime to {saved}")
            return 0

        warn(f"could not provision libnvidia-ngx.so.1 for driver {driver_version}; RTX VFX nodes may fail on this host")
        return 0
    except Exception as exc:
        warn(f"unexpected NGX bootstrap error: {exc}")
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
