#!/usr/bin/env python3
"""
Synchronize models, paths, and download hooks between ComfyUI and Fizgig.

Ensures that:
1. All Fizgig files stay cleanly inside /workspace/Fizgig (no loose folders in /workspace root).
2. Any model downloaded through Fizgig (via fetch_models, _fetch_on_use, sammask, helpers, or fetch_turbo_lora)
   is stored directly in ComfyUI's standard model subdirectories (/workspace/ComfyUI/models/<subfolder>/<filename>)
   and symlinked into /workspace/Fizgig/user_data/models/<filename>.
3. Existing models in /workspace/Fizgig/user_data/models (or legacy /workspace/models) are migrated to
   /workspace/ComfyUI/models/<subfolder>/ and symlinked back.
4. Models in /workspace/ComfyUI/models/<subfolder>/ are symlinked into /workspace/Fizgig/user_data/models/
   and wired into Fizgig's prefs.json when present on disk.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Map of exact model filenames to ComfyUI subdirectories
KNOWN_MODEL_MAP: dict[str, str] = {
    # VAEs
    "qwen_image_vae.safetensors": "vae",
    "minimax_h3_video_vae_fp16.safetensors": "vae",
    "minimax_h3_audio_vae_fp32.safetensors": "vae",
    "ae.safetensors": "vae",
    "ace_1.5_vae.safetensors": "vae",
    "flux2-vae.safetensors": "vae",
    "flux2-vae_small.safetensors": "vae",
    "qwen_image_2.1_vae_diffusers.safetensors": "vae",
    "diffusion_pytorch_model.safetensors": "vae",
    "sdxl_vae.safetensors": "vae",

    # Diffusion models / DiTs
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors": "diffusion_models",
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors": "diffusion_models",
    "flux-2-klein-base-9b-fp8.safetensors": "diffusion_models",
    "flux-2-klein-base-9b.safetensors": "diffusion_models",
    "flux-2-klein-9b-fp8.safetensors": "diffusion_models",
    "krea2_raw_bf16.safetensors": "diffusion_models",
    "krea2_turbo_fp8_scaled.safetensors": "diffusion_models",
    "qwen_image_2.1_bf16.safetensors": "diffusion_models",
    "qwen_image_2.1_turbo_int8_convrot.safetensors": "diffusion_models",
    "z_image_turbo_bf16.safetensors": "diffusion_models",
    "z_image_bf16.safetensors": "diffusion_models",
    "moss_image_base_bf16.safetensors": "diffusion_models",
    "moss_image_sft_int8_convrot.safetensors": "diffusion_models",
    "moss_image_sft_bf16.safetensors": "diffusion_models",
    "kromoss_bf16.safetensors": "diffusion_models",
    "kromoss_int8_convrot.safetensors": "diffusion_models",
    "flux3_image_klein_bf16.safetensors": "diffusion_models",
    "flux3_image_klein_sd_bf16.safetensors": "diffusion_models",
    "acestep_v1.5_xl_turbo_bf16.safetensors": "diffusion_models",
    "acestep_v1.5_xl_sft_bf16.safetensors": "diffusion_models",
    "anima-base-v1.0.safetensors": "diffusion_models",

    # Text encoders
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": "text_encoders",
    "qwen3vl_32b_minimax_h3_bf16.safetensors": "text_encoders",
    "qwen3vl_4b_fp8_scaled.safetensors": "text_encoders",
    "qwen3vl_4b_bf16.safetensors": "text_encoders",
    "Huihui-Qwen3-VL-4B-Instruct-abliterated.safetensors": "text_encoders",
    "Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors": "text_encoders",
    "qwen_3_8b.safetensors": "text_encoders",
    "qwen3vl_8b_bf16.safetensors": "text_encoders",
    "qwen3vl_8b_fp8_scaled.safetensors": "text_encoders",
    "qwen_3_4b.safetensors": "text_encoders",
    "qwen_0.6b_ace15.safetensors": "text_encoders",
    "qwen_3_06b_base.safetensors": "text_encoders",
    "qwen3.5_9b_bf16.safetensors": "text_encoders",
    "qwen3.5_9b_int8_convrot.safetensors": "text_encoders",

    # LoRAs and adapters
    "minimax_h3_turbo_v4_step600.safetensors": "loras",
    "minimax_h3_image_training_adapter.safetensors": "loras",
    "minimax_h3_training_adapter_v1.safetensors": "loras",
    "minimax_h3_ref2va_training_adapter_v1.safetensors": "loras",
    "krea2_turbo_lora_rank_64_bf16.safetensors": "loras",
    "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors": "loras",
    "fizgig_qwen_image_2.1_training_adapter_v2.safetensors": "loras",
    "fizgig_qwen_image_2.1_training_adapter.safetensors": "loras",
    "fizgig_z_image_turbo_training_adapter.safetensors": "loras",
    "8-steps-dmd-lora.safetensors": "loras",
    "moss_image_sft_look_lora.safetensors": "loras",
    "acestep15xl_turbo_model_eq_r128.safetensors": "loras",
    "anima-turbo-lora-v0.2.safetensors": "loras",

    # Checkpoints & SAM
    "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors": "checkpoints",
    "Illustrious-XL-v0.1.safetensors": "checkpoints",
    "sam3.1_multiplex_fp16.safetensors": "checkpoints",

    # VAE Approx & Upscalers
    "taew2_1.safetensors": "vae_approx",
    "taeh3.safetensors": "vae_approx",
    "minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors": "latent_upscale_models",
}

# Map of Fizgig prefs.json keys to ComfyUI subfolder and candidate filenames (primary + alternatives)
KEY_TO_COMFY_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "minimax_dit": ("diffusion_models", ("minimax_h3_fl2va_pruned_int8_convrot.safetensors",)),
    "minimax_ref_dit": ("diffusion_models", ("minimax_h3_ref2va_pruned_int8_convrot.safetensors",)),
    "minimax_text_encoder": ("text_encoders", ("qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "qwen3vl_32b_minimax_h3_bf16.safetensors")),
    "minimax_vae": ("vae", ("minimax_h3_video_vae_fp16.safetensors",)),
    "minimax_audio_vae": ("vae", ("minimax_h3_audio_vae_fp32.safetensors",)),
    "minimax_turbo_lora": ("loras", ("minimax_h3_turbo_v4_step600.safetensors",)),
    "minimax_circlestone_adapter": ("loras", ("minimax_h3_image_training_adapter.safetensors",)),
    "minimax_training_adapter": ("loras", ("minimax_h3_training_adapter_v1.safetensors",)),
    "minimax_ref_training_adapter": ("loras", ("minimax_h3_ref2va_training_adapter_v1.safetensors",)),
    "krea2_raw_dit": ("diffusion_models", ("krea2_raw_bf16.safetensors",)),
    "krea2_turbo_dit": ("diffusion_models", ("krea2_turbo_fp8_scaled.safetensors",)),
    "krea2_text_encoder": ("text_encoders", (
        "qwen3vl_4b_fp8_scaled.safetensors",
        "qwen3vl_4b_bf16.safetensors",
        "Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors",
        "Huihui-Qwen3-VL-4B-Instruct-abliterated.safetensors",
    )),
    "krea2_vae": ("vae", ("qwen_image_vae.safetensors",)),
    "krea2_turbo_lora": ("loras", ("krea2_turbo_lora_rank_64_bf16.safetensors",)),
    "base_dit": ("diffusion_models", ("flux-2-klein-base-9b-fp8.safetensors", "flux-2-klein-base-9b.safetensors")),
    "distilled_dit": ("diffusion_models", ("flux-2-klein-9b-fp8.safetensors",)),
    "text_encoder": ("text_encoders", ("qwen_3_8b.safetensors",)),
    "vae": ("vae", ("ae.safetensors",)),
    "qwen21_dit": ("diffusion_models", ("qwen_image_2.1_bf16.safetensors",)),
    "qwen21_turbo_dit": ("diffusion_models", ("qwen_image_2.1_turbo_int8_convrot.safetensors",)),
    "qwen21_text_encoder": ("text_encoders", ("qwen3vl_8b_bf16.safetensors",)),
    "qwen21_vae": ("vae", ("qwen_image_2.1_vae_diffusers.safetensors",)),
    "qwen21_training_adapter": ("loras", ("fizgig_qwen_image_2.1_training_adapter_v2.safetensors", "fizgig_qwen_image_2.1_training_adapter.safetensors")),
    "qwen21_turbo_lora": ("loras", ("qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors",)),
    "zimage_dit": ("diffusion_models", ("z_image_turbo_bf16.safetensors",)),
    "zimage_text_encoder": ("text_encoders", ("qwen_3_4b.safetensors",)),
    "zimage_vae": ("vae", ("ae.safetensors",)),
    "zimage_training_adapter": ("loras", ("fizgig_z_image_turbo_training_adapter.safetensors",)),
    "zimage_base_dit": ("diffusion_models", ("z_image_bf16.safetensors",)),
    "zimage_base_turbo_dit": ("diffusion_models", ("z_image_turbo_bf16.safetensors",)),
    "zimage_base_text_encoder": ("text_encoders", ("qwen_3_4b.safetensors",)),
    "zimage_base_vae": ("vae", ("ae.safetensors",)),
    "sdxl_checkpoint": ("checkpoints", ("Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors", "Illustrious-XL-v0.1.safetensors")),
    "sdxl_vae": ("vae", ("sdxl_vae.safetensors",)),
    "moss_image_dit": ("diffusion_models", ("moss_image_base_bf16.safetensors",)),
    "moss_image_sft_dit": ("diffusion_models", ("moss_image_sft_int8_convrot.safetensors", "moss_image_sft_bf16.safetensors")),
    "moss_image_text_encoder": ("text_encoders", ("qwen3.5_9b_bf16.safetensors", "qwen3.5_9b_int8_convrot.safetensors")),
    "moss_image_vae": ("vae", ("flux2-vae_small.safetensors",)),
    "moss_image_turbo_lora": ("loras", ("8-steps-dmd-lora.safetensors",)),
    "moss_image_sft_look_lora": ("loras", ("moss_image_sft_look_lora.safetensors",)),
    "flux3_dit": ("diffusion_models", ("flux3_image_klein_bf16.safetensors",)),
    "flux3_distilled_dit": ("diffusion_models", ("flux3_image_klein_sd_bf16.safetensors",)),
    "flux3_text_encoder": ("text_encoders", ("qwen3vl_4b_bf16.safetensors", "qwen3vl_4b_fp8_scaled.safetensors")),
    "flux3_vae": ("vae", ("flux2-vae.safetensors",)),
    "acestep15xl_dit": ("diffusion_models", ("acestep_v1.5_xl_turbo_bf16.safetensors",)),
    "acestep15xl_sft_dit": ("diffusion_models", ("acestep_v1.5_xl_sft_bf16.safetensors",)),
    "acestep15xl_text_encoder": ("text_encoders", ("qwen_0.6b_ace15.safetensors",)),
    "acestep15xl_vae": ("vae", ("ace_1.5_vae.safetensors",)),
    "acestep15xl_model_eq": ("loras", ("acestep15xl_turbo_model_eq_r128.safetensors",)),
    "anima_dit": ("diffusion_models", ("anima-base-v1.0.safetensors",)),
    "anima_text_encoder": ("text_encoders", ("qwen_3_06b_base.safetensors",)),
    "anima_vae": ("vae", ("qwen_image_vae.safetensors",)),
    "anima_turbo_lora": ("loras", ("anima-turbo-lora-v0.2.safetensors",)),
    "captioner_secondary": ("text_encoders", ("qwen3vl_8b_fp8_scaled.safetensors", "qwen3vl_8b_bf16.safetensors")),
}

ROLE_TO_SUBFOLDER: dict[str, str] = {
    "dit": "diffusion_models",
    "preview_dit": "diffusion_models",
    "ref_dit": "diffusion_models",
    "vae": "vae",
    "audio_vae": "vae",
    "text_encoder": "text_encoders",
    "speed_lora": "loras",
    "look_lora": "loras",
    "training_adapter": "loras",
    "model_eq": "loras",
    "checkpoint": "checkpoints",
}

VALID_MODEL_EXTENSIONS = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf")
_REGISTRY_LOADED = False


def _load_dynamic_fizgig_registry(fizgig_dir: Path | None = None) -> None:
    """Dynamically populate KNOWN_MODEL_MAP and KEY_TO_COMFY_MAP from Fizgig's live family registry."""
    global _REGISTRY_LOADED
    if _REGISTRY_LOADED:
        return
    _REGISTRY_LOADED = True
    if fizgig_dir is None:
        fizgig_dir = Path(os.environ.get("FIZGIG_DIR", "/workspace/Fizgig"))
    src_dir = fizgig_dir / "src"
    if not src_dir.is_dir():
        return
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from fizgig.families.registry import FAMILIES
        for fam in FAMILIES.values():
            for mf in getattr(fam, "model_files", ()):
                role = getattr(mf, "role", "") or ""
                path = getattr(mf, "path", "") or ""
                local_name = getattr(mf, "local_name", "") or ""
                pref_key = getattr(mf, "pref_key", "") or ""
                fn = local_name or os.path.basename(path)
                subfolder = ROLE_TO_SUBFOLDER.get(role) or classify_model_file(fn, pref_key, path, _skip_dynamic=True)
                if fn:
                    KNOWN_MODEL_MAP.setdefault(fn, subfolder)
                alt_path = getattr(mf, "alt_path", "") or ""
                alt_fn = os.path.basename(alt_path) if alt_path else ""
                if alt_fn:
                    KNOWN_MODEL_MAP.setdefault(alt_fn, subfolder)
                if pref_key and fn:
                    existing = list(KEY_TO_COMFY_MAP.get(pref_key, (subfolder, ()))[1])
                    for cand in (fn, alt_fn):
                        if cand and cand not in existing:
                            existing.append(cand)
                    KEY_TO_COMFY_MAP[pref_key] = (subfolder, tuple(existing))
            for tb in getattr(fam, "text_encoder_builds", ()):
                tb_fn = os.path.basename(getattr(tb, "path", "") or "")
                if tb_fn:
                    KNOWN_MODEL_MAP.setdefault(tb_fn, "text_encoders")
    except Exception:
        pass


def classify_model_file(filename: str, key: str = "", repo_path: str = "", _skip_dynamic: bool = False) -> str:
    """Determine the appropriate ComfyUI model subfolder for a given file."""
    if not _skip_dynamic:
        _load_dynamic_fizgig_registry()
    if filename in KNOWN_MODEL_MAP:
        return KNOWN_MODEL_MAP[filename]
    if key in KEY_TO_COMFY_MAP:
        return KEY_TO_COMFY_MAP[key][0]

    fn_lower = (filename or "").lower()
    key_lower = (key or "").lower()
    repo_lower = (repo_path or "").lower()
    combined = f"{fn_lower} {key_lower} {repo_lower}"

    # Inspect repo path prefix first when present (e.g. "diffusion_models/...", "split_files/vae/...")
    for prefix, sub in (
        ("diffusion_models/", "diffusion_models"),
        ("split_files/diffusion_models/", "diffusion_models"),
        ("text_encoders/", "text_encoders"),
        ("split_files/text_encoders/", "text_encoders"),
        ("vae/", "vae"),
        ("split_files/vae/", "vae"),
        ("loras/", "loras"),
        ("split_files/loras/", "loras"),
        ("checkpoints/", "checkpoints"),
    ):
        if repo_lower.startswith(prefix):
            return sub

    if "tae" in fn_lower or "vae_approx" in combined:
        return "vae_approx"
    if "latent_upscale" in combined or "upscaler" in combined:
        return "latent_upscale_models"
    if "controlnet" in combined or "t2i_adapter" in combined:
        return "controlnet"
    if "upscale" in combined:
        return "upscale_models"
    if "vae" in combined or "ae.safetensors" in fn_lower:
        return "vae"
    if any(x in combined for x in ["lora", "adapter", "model_eq", "dmd"]):
        return "loras"
    if any(x in combined for x in [
        "text_encoder", "qwen3vl", "qwen_3", "qwen3.", "qwen3_", "qwen_0.6b",
        "qwen_3_06b", "qwen_3_4b", "qwen_3_8b", "huihui", "clip", "t5", "captioner"
    ]):
        return "text_encoders"
    if any(x in combined for x in ["checkpoint", "juggernaut", "illustrious", "sam3"]):
        return "checkpoints"
    if any(x in combined for x in [
        "_dit", "base_dit", "distilled_dit", "fl2va", "ref2va", "flux",
        "klein", "z_image", "moss_image", "kromoss", "acestep", "anima", "unet", "diffusion"
    ]):
        return "diffusion_models"

    return "diffusion_models"


def get_comfy_models_root() -> str:
    """Return the active ComfyUI models directory (/workspace/ComfyUI/models or /Comfy/models)."""
    if os.path.isdir("/workspace/ComfyUI"):
        return "/workspace/ComfyUI/models"
    if os.path.isdir("/Comfy"):
        return "/Comfy/models"
    return "/workspace/ComfyUI/models"


def get_comfy_target(key: str = "", repo_path: str = "", filename: str = "", comfy_models_dir: str | None = None) -> str:
    """Return the absolute path where this model should live inside ComfyUI's model directory."""
    models_root = comfy_models_dir or get_comfy_models_root()
    fn = filename or os.path.basename(repo_path)
    subfolder = classify_model_file(fn, key, repo_path)
    return os.path.join(models_root, subfolder, fn)


def ensure_symlink(target: Path, link: Path) -> bool:
    """Create a symlink pointing to target if it does not already exist."""
    try:
        if link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    return True
            except OSError:
                pass
            link.unlink()
        elif link.exists():
            return False
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        return True
    except OSError:
        return False


def route_to_comfy(
    got_path: str,
    dest_in_models_dir: str = "",
    pref_key: str = "",
    repo_path: str = "",
    filename: str = "",
    log=None,
) -> str:
    """Move a downloaded model file into ComfyUI's models/<subfolder>/ and symlink it in Fizgig's models dir."""
    fn = filename or os.path.basename(dest_in_models_dir or got_path)
    comfy_dest = get_comfy_target(pref_key, repo_path, fn)
    os.makedirs(os.path.dirname(comfy_dest), exist_ok=True)

    abs_got = os.path.abspath(got_path)
    abs_comfy = os.path.abspath(comfy_dest)

    if abs_got != abs_comfy and os.path.exists(got_path):
        if os.path.islink(comfy_dest) or os.path.exists(comfy_dest):
            try:
                os.remove(comfy_dest)
            except OSError:
                pass
        shutil.move(got_path, comfy_dest)
        if log:
            try:
                log(f"  [comfy] stored {fn} -> {comfy_dest}")
            except Exception:
                pass

    if dest_in_models_dir:
        abs_models_dest = os.path.abspath(dest_in_models_dir)
        if abs_models_dest != abs_comfy:
            ensure_symlink(Path(comfy_dest), Path(dest_in_models_dir))

    return comfy_dest


def _write_if_valid_python(path: Path, content: str) -> bool:
    """Parse content with ast.parse before writing to guarantee syntax/indentation validity."""
    try:
        ast.parse(content, filename=str(path))
    except SyntaxError as exc:
        print(f"[model_sync][error] Refusing to write invalid Python to {path}: {exc}")
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _reset_file_if_broken_or_patched(fizgig_dir: Path, rel_path: str, marker: str = "fizgig.comfy_sync") -> str:
    """Return clean file text, resetting via git checkout if a previous patch left syntax errors or old hooks."""
    fpath = fizgig_dir / rel_path
    if not fpath.is_file():
        return ""
    text = fpath.read_text(encoding="utf-8")
    needs_reset = False
    try:
        ast.parse(text, filename=str(fpath))
    except SyntaxError:
        needs_reset = True

    if marker in text and "# COMFY_SYNC_V2" not in text:
        needs_reset = True

    if needs_reset and (fizgig_dir / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(fizgig_dir), "checkout", "--", rel_path],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        text = fpath.read_text(encoding="utf-8")
    return text


def patch_fizgig_sources(fizgig_dir: Path) -> None:
    """Install comfy_sync into Fizgig and hook all Fizgig model download entry points."""
    if not fizgig_dir.is_dir() or not (fizgig_dir / "src" / "fizgig").is_dir():
        return

    # 1. Install comfy_sync.py inside fizgig package
    self_path = Path(__file__).resolve()
    comfy_sync_dest = fizgig_dir / "src" / "fizgig" / "comfy_sync.py"
    if self_path.is_file():
        comfy_sync_dest.write_text(self_path.read_text(encoding="utf-8"), encoding="utf-8")

    # 2. Patch src/fizgig/web/server.py (auth bypass + _fetch_on_use ComfyUI routing)
    server_rel = "src/fizgig/web/server.py"
    server_file = fizgig_dir / server_rel
    text = _reset_file_if_broken_or_patched(fizgig_dir, server_rel)
    if text:
        text = re.sub(
            r"def _logged_in\(self\):.*?(?=\n    def )",
            "def _logged_in(self):\n        return True\n",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(
            r"def _host_ok\(self\):.*?(?=\n    def )",
            "def _host_ok(self):\n        return True\n",
            text,
            flags=re.DOTALL,
        )
        text = text.replace("Handler.password = password or None", "Handler.password = None")
        text = text.replace("Handler.local_only = not password", "Handler.local_only = False")
        if "a.password = None" not in text:
            text = text.replace("a = ap.parse_args(argv)", "a = ap.parse_args(argv)\n    a.password = None")

        if "# COMFY_SYNC_V2" not in text:
            target_srv = (
                "            if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
                "                os.replace(got, dest)             # a file in a repo subfolder: to the models folder itself\n"
                "            update_prefs({f.pref_key: dest})"
            )
            replacement_srv = (
                "            if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
                "                os.replace(got, dest)             # a file in a repo subfolder: to the models folder itself\n"
                "            try:  # COMFY_SYNC_V2\n"
                "                from fizgig.comfy_sync import route_to_comfy\n"
                "                dest = route_to_comfy(dest, dest, f.pref_key, getattr(f, 'path', ''), os.path.basename(dest))\n"
                "            except Exception:\n"
                "                pass\n"
                "            update_prefs({f.pref_key: dest})"
            )
            if target_srv in text:
                text = text.replace(target_srv, replacement_srv, 1)

        _write_if_valid_python(server_file, text)

    # 3. Patch src/fizgig/scripts/fetch_models.py (bulk family downloads -> ComfyUI models/<subfolder>/)
    fm_rel = "src/fizgig/scripts/fetch_models.py"
    fm_file = fizgig_dir / fm_rel
    fm_text = _reset_file_if_broken_or_patched(fizgig_dir, fm_rel)
    if fm_text and "# COMFY_SYNC_V2" not in fm_text:
        target_check = (
            "    if os.path.isfile(dest) and _valid_safetensors(dest, min_bytes):\n"
            '        log(f"  [have] {w.filename} — on disk, linking into Preferences")\n'
            "        prefs[w.pref_key] = dest\n"
            "        return True"
        )
        replacement_check = (
            "    try:  # COMFY_SYNC_V2\n"
            "        from fizgig.comfy_sync import get_comfy_target, ensure_symlink, route_to_comfy\n"
            "        from pathlib import Path as _P\n"
            "        comfy_dest = get_comfy_target(w.pref_key, getattr(w, 'path_in_repo', ''), w.filename)\n"
            "        if comfy_dest and os.path.isfile(comfy_dest) and _valid_safetensors(comfy_dest, min_bytes):\n"
            "            ensure_symlink(_P(comfy_dest), _P(dest))\n"
            '            log(f"  [have] {w.filename} — found in ComfyUI models, linking into Preferences")\n'
            "            prefs[w.pref_key] = comfy_dest\n"
            "            return True\n"
            "    except Exception:\n"
            "        pass\n\n"
            "    if os.path.isfile(dest) and _valid_safetensors(dest, min_bytes):\n"
            "        try:\n"
            "            from fizgig.comfy_sync import route_to_comfy\n"
            "            dest = route_to_comfy(dest, dest, w.pref_key, getattr(w, 'path_in_repo', ''), w.filename, log=log)\n"
            "        except Exception:\n"
            "            pass\n"
            '        log(f"  [have] {w.filename} — on disk, linking into Preferences")\n'
            "        prefs[w.pref_key] = dest\n"
            "        return True"
        )
        target_save = (
            "    prefs[w.pref_key] = dest\n"
            '    log(f"  [done] {w.filename}")'
        )
        replacement_save = (
            "    try:\n"
            "        from fizgig.comfy_sync import route_to_comfy\n"
            "        dest = route_to_comfy(dest, dest, w.pref_key, getattr(w, 'path_in_repo', ''), w.filename, log=log)\n"
            "    except Exception as _e:\n"
            '        log(f"  [warn] ComfyUI model routing skipped for {w.filename}: {_e}")\n'
            "    prefs[w.pref_key] = dest\n"
            '    log(f"  [done] {w.filename}")'
        )
        if target_check in fm_text and target_save in fm_text:
            fm_text = fm_text.replace(target_check, replacement_check, 1)
            fm_text = fm_text.replace(target_save, replacement_save, 1)
            _write_if_valid_python(fm_file, fm_text)

    # 4. Patch src/fizgig/dataset/sammask.py (SAM 3.1 -> ComfyUI models/checkpoints/)
    sam_rel = "src/fizgig/dataset/sammask.py"
    sam_file = fizgig_dir / sam_rel
    sam_text = _reset_file_if_broken_or_patched(fizgig_dir, sam_rel)
    if sam_text and "# COMFY_SYNC_V2" not in sam_text:
        target_find = (
            "def find_model(models_dir):\n"
            '    """The downloaded SAM 3.1 file in the models folder, or "" before the download."""\n'
            "    p = os.path.join(models_dir, NAME)\n"
            '    return p if os.path.isfile(p) else ""'
        )
        replacement_find = (
            "def find_model(models_dir):  # COMFY_SYNC_V2\n"
            '    """The downloaded SAM 3.1 file in ComfyUI models or the models folder, or "" before the download."""\n'
            "    try:\n"
            "        from fizgig.comfy_sync import get_comfy_target, ensure_symlink\n"
            "        from pathlib import Path as _P\n"
            "        cp = get_comfy_target('', FILE, NAME)\n"
            "        if cp and os.path.isfile(cp):\n"
            "            ensure_symlink(_P(cp), _P(os.path.join(models_dir, NAME)))\n"
            "            return cp\n"
            "    except Exception:\n"
            "        pass\n"
            "    p = os.path.join(models_dir, NAME)\n"
            '    return p if os.path.isfile(p) else ""'
        )
        target_dl = (
            "            if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
            "                os.replace(got, dest)              # from the repo's checkpoints/ subfolder to the models folder"
        )
        replacement_dl = (
            "            if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
            "                os.replace(got, dest)              # from the repo's checkpoints/ subfolder to the models folder\n"
            "            try:\n"
            "                from fizgig.comfy_sync import route_to_comfy\n"
            "                route_to_comfy(dest, dest, '', FILE, NAME)\n"
            "            except Exception:\n"
            "                pass"
        )
        if target_find in sam_text and target_dl in sam_text:
            sam_text = sam_text.replace(target_find, replacement_find, 1)
            sam_text = sam_text.replace(target_dl, replacement_dl, 1)
            _write_if_valid_python(sam_file, sam_text)

    # 5. Patch src/fizgig/web/helpers.py (remote TE & shared build downloads -> ComfyUI models/<subfolder>/)
    hlp_rel = "src/fizgig/web/helpers.py"
    hlp_file = fizgig_dir / hlp_rel
    hlp_text = _reset_file_if_broken_or_patched(fizgig_dir, hlp_rel)
    if hlp_text and "# COMFY_SYNC_V2" not in hlp_text:
        target_te = (
            "    dest = os.path.join(user_data.MODELS_DIR, os.path.basename(path))\n"
            "    if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
            "        os.replace(got, dest)\n"
            "    s.update_prefs({f.pref_key: dest})"
        )
        replacement_te = (
            "    dest = os.path.join(user_data.MODELS_DIR, os.path.basename(path))\n"
            "    if os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(dest)):\n"
            "        os.replace(got, dest)\n"
            "    try:  # COMFY_SYNC_V2\n"
            "        from fizgig.comfy_sync import route_to_comfy\n"
            "        dest = route_to_comfy(dest, dest, f.pref_key, path, os.path.basename(dest))\n"
            "    except Exception:\n"
            "        pass\n"
            "    s.update_prefs({f.pref_key: dest})"
        )
        if target_te in hlp_text:
            hlp_text = hlp_text.replace(target_te, replacement_te, 1)
            _write_if_valid_python(hlp_file, hlp_text)

    # 6. Patch src/fizgig/scripts/fetch_turbo_lora.py (updater/fallback LoRAs -> ComfyUI models/loras/)
    ftl_rel = "src/fizgig/scripts/fetch_turbo_lora.py"
    ftl_file = fizgig_dir / ftl_rel
    ftl_text = _reset_file_if_broken_or_patched(fizgig_dir, ftl_rel)
    if ftl_text and "# COMFY_SYNC_V2" not in ftl_text:
        target_ftl = (
            "        _save_pref(prefs_file, pref_key, dest)\n"
            '        log(f"Preferences updated: {pref_key} -> {dest}")\n'
            "        return dest"
        )
        replacement_ftl = (
            "        try:  # COMFY_SYNC_V2\n"
            "            from fizgig.comfy_sync import route_to_comfy\n"
            "            dest = route_to_comfy(dest, dest, pref_key, 'loras/' + filename, filename, log=log)\n"
            "        except Exception:\n"
            "            pass\n"
            "        _save_pref(prefs_file, pref_key, dest)\n"
            '        log(f"Preferences updated: {pref_key} -> {dest}")\n'
            "        return dest"
        )
        if target_ftl in ftl_text:
            ftl_text = ftl_text.replace(target_ftl, replacement_ftl, 1)
            _write_if_valid_python(ftl_file, ftl_text)


def _migrate_directory_contents(src_dir: Path, dst_dir: Path) -> None:
    """Move all entries from src_dir into dst_dir and remove src_dir if empty."""
    if not src_dir.exists() or src_dir.is_symlink():
        if src_dir.is_symlink():
            try:
                src_dir.unlink()
            except OSError:
                pass
        return
    if not src_dir.is_dir():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in list(src_dir.iterdir()):
        target = dst_dir / item.name
        if not target.exists():
            try:
                shutil.move(str(item), str(target))
            except OSError:
                pass
        elif item.is_dir() and target.is_dir() and not item.is_symlink():
            _migrate_directory_contents(item, target)
    try:
        src_dir.rmdir()
    except OSError:
        pass


def sync_all(workspace_dir: str = "/workspace") -> dict[str, int]:
    """Execute complete model synchronization and workspace root cleanup."""
    ws = Path(workspace_dir)
    comfy_root = ws / "ComfyUI"
    if not comfy_root.is_dir() and Path("/Comfy").is_dir():
        comfy_root = Path("/Comfy")

    comfy_models = comfy_root / "models"
    fizgig_dir = Path(os.environ.get("FIZGIG_DIR", str(ws / "Fizgig")))
    _load_dynamic_fizgig_registry(fizgig_dir)

    user_data_dir = fizgig_dir / "user_data"
    fizgig_models = user_data_dir / "models"
    fizgig_datasets = fizgig_dir / "datasets"
    fizgig_profiles = user_data_dir / "profiles"
    fizgig_cache = user_data_dir / "cache"
    prefs_file = user_data_dir / "prefs.json"

    # 1. Ensure standard ComfyUI model subdirectories exist
    standard_subdirs = [
        "checkpoints", "diffusion_models", "text_encoders", "vae", "loras",
        "vae_approx", "latent_upscale_models", "controlnet", "upscale_models"
    ]
    for sub in standard_subdirs:
        (comfy_models / sub).mkdir(parents=True, exist_ok=True)

    # 2. If Fizgig's user_data/models was a symlink to /workspace/models, replace it with a real directory inside Fizgig
    if fizgig_models.is_symlink():
        try:
            fizgig_models.unlink()
        except OSError:
            pass
    fizgig_models.mkdir(parents=True, exist_ok=True)
    fizgig_datasets.mkdir(parents=True, exist_ok=True)
    fizgig_profiles.mkdir(parents=True, exist_ok=True)
    fizgig_cache.mkdir(parents=True, exist_ok=True)

    # Ensure /workspace/Fizgig/output_loras symlinks to ComfyUI's loras directory
    fizgig_output_loras = fizgig_dir / "output_loras"
    if fizgig_output_loras.is_dir() and not fizgig_output_loras.is_symlink():
        _migrate_directory_contents(fizgig_output_loras, comfy_models / "loras")
    ensure_symlink(comfy_models / "loras", fizgig_output_loras)

    migrated = 0
    linked = 0

    # 3. Clean up legacy /workspace root folders (/workspace/models, /workspace/datasets, /workspace/profiles, /workspace/cache, /workspace/output_loras)
    legacy_models = ws / "models"
    if legacy_models.is_symlink():
        try:
            legacy_models.unlink()
        except OSError:
            pass
    elif legacy_models.is_dir():
        for item in list(legacy_models.iterdir()):
            if item.is_symlink():
                try:
                    item.unlink()
                except OSError:
                    pass
            elif item.is_file():
                if item.suffix.lower() in VALID_MODEL_EXTENSIONS:
                    subfolder = classify_model_file(item.name)
                    target = comfy_models / subfolder / item.name
                    if not target.exists():
                        try:
                            shutil.move(str(item), str(target))
                            migrated += 1
                            print(f"[model_sync] Migrated {item.name} -> ComfyUI models/{subfolder}/")
                        except OSError:
                            continue
                    else:
                        try:
                            item.unlink()
                        except OSError:
                            pass
                else:
                    # Non-model helper file (e.g. selfie_multiclass_256x256.tflite)
                    target_helper = fizgig_models / item.name
                    if not target_helper.exists():
                        try:
                            shutil.move(str(item), str(target_helper))
                        except OSError:
                            pass
                    else:
                        try:
                            item.unlink()
                        except OSError:
                            pass
            elif item.is_dir():
                _migrate_directory_contents(item, fizgig_models / item.name)
        try:
            legacy_models.rmdir()
        except OSError:
            pass

    _migrate_directory_contents(ws / "datasets", fizgig_datasets)
    _migrate_directory_contents(ws / "profiles", fizgig_profiles)
    _migrate_directory_contents(ws / "cache", fizgig_cache)
    _migrate_directory_contents(ws / "output_loras", comfy_models / "loras")

    # 4. Migrate any real model files in /workspace/Fizgig/user_data/models/ into /workspace/ComfyUI/models/<subfolder>/
    if fizgig_models.is_dir():
        for f in list(fizgig_models.iterdir()):
            if f.is_file() and not f.is_symlink() and f.suffix.lower() in VALID_MODEL_EXTENSIONS:
                subfolder = classify_model_file(f.name)
                target = comfy_models / subfolder / f.name
                if not target.exists():
                    try:
                        shutil.move(str(f), str(target))
                        migrated += 1
                        print(f"[model_sync] Migrated {f.name} -> ComfyUI models/{subfolder}/")
                    except OSError as e:
                        print(f"[model_sync][warn] Failed migrating {f.name}: {e}")
                        continue
                else:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                ensure_symlink(target, f)

    # 5. Create symlinks in /workspace/Fizgig/user_data/models/ for every model found in ComfyUI
    for sub in comfy_models.iterdir():
        if sub.is_dir():
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in VALID_MODEL_EXTENSIONS:
                    if ensure_symlink(f, fizgig_models / f.name):
                        linked += 1

    # 6. Update Fizgig prefs.json with clean Fizgig-internal paths and real on-disk ComfyUI model paths
    prefs: dict[str, object] = {}
    if prefs_file.is_file():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}

    prefs["lora_output_dir"] = str(comfy_models / "loras")
    if not prefs.get("input_dataset_dir") or str(prefs.get("input_dataset_dir")) == "/workspace/datasets":
        prefs["input_dataset_dir"] = str(fizgig_datasets)
    if not prefs.get("profiles_dir") or str(prefs.get("profiles_dir")) == "/workspace/profiles":
        prefs["profiles_dir"] = "user_data/profiles"
    if not prefs.get("cache_dir") or str(prefs.get("cache_dir")) == "/workspace/cache":
        prefs["cache_dir"] = "user_data/cache"
    prefs["web_password"] = ""

    search_roots = [
        comfy_models,
        fizgig_models,
        Path("/Comfy/models"),
    ]

    for key, (subfolder, filenames) in KEY_TO_COMFY_MAP.items():
        current_val = str(prefs.get(key) or "").strip()
        # If current_val points to a symlink inside Fizgig models or legacy /workspace/models, resolve to canonical ComfyUI path
        if current_val and os.path.isfile(current_val):
            try:
                resolved = str(Path(current_val).resolve())
                if os.path.isfile(resolved):
                    prefs[key] = resolved
                    continue
            except OSError:
                pass

        found_path: str | None = None
        for filename in filenames:
            for root in search_roots:
                candidate = root / subfolder / filename
                if candidate.is_file():
                    found_path = str(candidate.resolve() if candidate.is_symlink() else candidate)
                    break
                candidate_flat = root / filename
                if candidate_flat.is_file():
                    found_path = str(candidate_flat.resolve() if candidate_flat.is_symlink() else candidate_flat)
                    break
            if found_path:
                break

        if found_path:
            prefs[key] = found_path
        elif current_val and not os.path.isfile(current_val):
            # Clear stale/unfetched pre-filled paths so Fizgig UI accurately shows what is installed
            prefs.pop(key, None)

    try:
        prefs_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_prefs = prefs_file.with_suffix(".json.tmp")
        tmp_prefs.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
        os.replace(tmp_prefs, prefs_file)
    except OSError as e:
        print(f"[model_sync][warn] Failed writing prefs.json: {e}")

    # 7. Patch Fizgig sources so any runtime downloads go straight to ComfyUI models
    patch_fizgig_sources(fizgig_dir)

    # 8. Ensure extra_model_paths.yaml in ComfyUI points to /workspace/Fizgig/user_data/models as fallback
    extra_yaml_content = """# ComfyUI extra model paths configuration for Fizgig
fizgig_models:
    base_path: /workspace/Fizgig/user_data/models
    checkpoints: .
    diffusion_models: .
    text_encoders: .
    vae: .
    loras: .
"""
    for ypath in [comfy_root / "extra_model_paths.yaml", Path("/Comfy/extra_model_paths.yaml")]:
        try:
            if ypath.parent.is_dir():
                ypath.write_text(extra_yaml_content, encoding="utf-8")
        except OSError:
            pass

    print(f"[model_sync] Complete: migrated {migrated} models, linked {linked} models.")
    return {"migrated": migrated, "linked": linked}


if __name__ == "__main__":
    target_ws = sys.argv[1] if len(sys.argv) > 1 else "/workspace"
    sync_all(target_ws)
