#!/usr/bin/env python3
"""
Synchronize models and configuration between ComfyUI and Fizgig.

Ensures that:
1. Any model downloaded through Fizgig is stored in or symlinked to ComfyUI's standard
   model subdirectories (/workspace/ComfyUI/models/<subfolder>/<filename>).
2. Existing models in /workspace/models are migrated to ComfyUI models and symlinked back.
3. Models in ComfyUI models are symlinked into Fizgig's models directory so Fizgig
   can use them without re-downloading.
4. Fizgig's prefs.json points to the canonical ComfyUI model paths.
5. ComfyUI's extra_model_paths.yaml is generated so ComfyUI discovers models in both locations.
"""

from __future__ import annotations

import json
import os
import shutil
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
    "sdxl_vae.safetensors": "vae",

    # Diffusion models / DiTs
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors": "diffusion_models",
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors": "diffusion_models",
    "flux-2-klein-base-9b-fp8.safetensors": "diffusion_models",
    "flux-2-klein-9b-fp8.safetensors": "diffusion_models",
    "krea2_raw_bf16.safetensors": "diffusion_models",
    "krea2_turbo_fp8_scaled.safetensors": "diffusion_models",
    "qwen_image_2.1_bf16.safetensors": "diffusion_models",
    "qwen_image_2.1_turbo_int8_convrot.safetensors": "diffusion_models",
    "z_image_turbo_bf16.safetensors": "diffusion_models",
    "moss_image_base_bf16.safetensors": "diffusion_models",
    "moss_image_sft_int8_convrot.safetensors": "diffusion_models",
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

    # Checkpoints
    "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors": "checkpoints",

    # VAE Approx & Upscalers
    "taew2_1.safetensors": "vae_approx",
    "taeh3.safetensors": "vae_approx",
    "minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors": "latent_upscale_models",
}

# Map of Fizgig prefs.json keys to ComfyUI subfolder and default filename
KEY_TO_COMFY_MAP: dict[str, tuple[str, str]] = {
    "minimax_dit": ("diffusion_models", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
    "minimax_ref_dit": ("diffusion_models", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
    "minimax_text_encoder": ("text_encoders", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    "minimax_vae": ("vae", "minimax_h3_video_vae_fp16.safetensors"),
    "minimax_audio_vae": ("vae", "minimax_h3_audio_vae_fp32.safetensors"),
    "minimax_turbo_lora": ("loras", "minimax_h3_turbo_v4_step600.safetensors"),
    "minimax_circlestone_adapter": ("loras", "minimax_h3_image_training_adapter.safetensors"),
    "minimax_training_adapter": ("loras", "minimax_h3_training_adapter_v1.safetensors"),
    "minimax_ref_training_adapter": ("loras", "minimax_h3_ref2va_training_adapter_v1.safetensors"),
    "krea2_raw_dit": ("diffusion_models", "krea2_raw_bf16.safetensors"),
    "krea2_turbo_dit": ("diffusion_models", "krea2_turbo_fp8_scaled.safetensors"),
    "krea2_text_encoder": ("text_encoders", "qwen3vl_4b_fp8_scaled.safetensors"),
    "krea2_vae": ("vae", "qwen_image_vae.safetensors"),
    "krea2_turbo_lora": ("loras", "krea2_turbo_lora_rank_64_bf16.safetensors"),
    "base_dit": ("diffusion_models", "flux-2-klein-base-9b-fp8.safetensors"),
    "distilled_dit": ("diffusion_models", "flux-2-klein-9b-fp8.safetensors"),
    "text_encoder": ("text_encoders", "qwen_3_8b.safetensors"),
    "vae": ("vae", "ae.safetensors"),
    "qwen21_dit": ("diffusion_models", "qwen_image_2.1_bf16.safetensors"),
    "qwen21_turbo_dit": ("diffusion_models", "qwen_image_2.1_turbo_int8_convrot.safetensors"),
    "qwen21_text_encoder": ("text_encoders", "qwen3vl_8b_bf16.safetensors"),
    "qwen21_vae": ("vae", "qwen_image_2.1_vae_diffusers.safetensors"),
    "qwen21_training_adapter": ("loras", "fizgig_qwen_image_2.1_training_adapter_v2.safetensors"),
    "qwen21_turbo_lora": ("loras", "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"),
    "zimage_dit": ("diffusion_models", "z_image_turbo_bf16.safetensors"),
    "zimage_text_encoder": ("text_encoders", "qwen_3_4b.safetensors"),
    "zimage_vae": ("vae", "ae.safetensors"),
    "zimage_training_adapter": ("loras", "fizgig_z_image_turbo_training_adapter.safetensors"),
    "sdxl_checkpoint": ("checkpoints", "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"),
    "sdxl_vae": ("vae", "sdxl_vae.safetensors"),
    "moss_image_dit": ("diffusion_models", "moss_image_base_bf16.safetensors"),
    "moss_image_sft_dit": ("diffusion_models", "moss_image_sft_int8_convrot.safetensors"),
    "moss_image_text_encoder": ("text_encoders", "qwen3.5_9b_bf16.safetensors"),
    "moss_image_vae": ("vae", "flux2-vae_small.safetensors"),
    "moss_image_turbo_lora": ("loras", "8-steps-dmd-lora.safetensors"),
    "moss_image_sft_look_lora": ("loras", "moss_image_sft_look_lora.safetensors"),
    "flux3_dit": ("diffusion_models", "flux3_image_klein_bf16.safetensors"),
    "flux3_distilled_dit": ("diffusion_models", "flux3_image_klein_sd_bf16.safetensors"),
    "flux3_text_encoder": ("text_encoders", "qwen3vl_4b_bf16.safetensors"),
    "flux3_vae": ("vae", "flux2-vae.safetensors"),
    "acestep15xl_dit": ("diffusion_models", "acestep_v1.5_xl_turbo_bf16.safetensors"),
    "acestep15xl_sft_dit": ("diffusion_models", "acestep_v1.5_xl_sft_bf16.safetensors"),
    "acestep15xl_text_encoder": ("text_encoders", "qwen_0.6b_ace15.safetensors"),
    "acestep15xl_vae": ("vae", "ace_1.5_vae.safetensors"),
    "acestep15xl_model_eq": ("loras", "acestep15xl_turbo_model_eq_r128.safetensors"),
    "anima_dit": ("diffusion_models", "anima-base-v1.0.safetensors"),
    "anima_text_encoder": ("text_encoders", "qwen_3_06b_base.safetensors"),
    "anima_vae": ("vae", "qwen_image_vae.safetensors"),
    "anima_turbo_lora": ("loras", "anima-turbo-lora-v0.2.safetensors"),
}

VALID_MODEL_EXTENSIONS = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf")


def classify_model_file(filename: str, key: str = "", repo_path: str = "") -> str:
    """Determine the appropriate ComfyUI model subfolder for a given file."""
    if filename in KNOWN_MODEL_MAP:
        return KNOWN_MODEL_MAP[filename]
    if key in KEY_TO_COMFY_MAP:
        return KEY_TO_COMFY_MAP[key][0]

    fn_lower = (filename or "").lower()
    key_lower = (key or "").lower()
    repo_lower = (repo_path or "").lower()
    combined = f"{fn_lower} {key_lower} {repo_lower}"

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
        "qwen_3_06b", "qwen_3_4b", "qwen_3_8b", "huihui", "clip", "t5"
    ]):
        return "text_encoders"
    if any(x in combined for x in [
        "_dit", "base_dit", "distilled_dit", "fl2va", "ref2va", "flux",
        "klein", "z_image", "moss_image", "acestep", "anima", "unet", "diffusion"
    ]):
        return "diffusion_models"
    if "checkpoint" in combined or "juggernaut" in combined:
        return "checkpoints"

    # Inspect repo path prefix if available
    if repo_lower.startswith("diffusion_models/") or repo_lower.startswith("split_files/diffusion_models/"):
        return "diffusion_models"
    if repo_lower.startswith("text_encoders/") or repo_lower.startswith("split_files/text_encoders/"):
        return "text_encoders"
    if repo_lower.startswith("vae/") or repo_lower.startswith("split_files/vae/"):
        return "vae"
    if repo_lower.startswith("loras/"):
        return "loras"
    if repo_lower.startswith("checkpoints/"):
        return "checkpoints"

    return "diffusion_models"


def get_comfy_target(key: str, repo_path: str, filename: str, comfy_models_dir: str = "/workspace/ComfyUI/models") -> str:
    """Return the absolute path where this model should live inside ComfyUI's model directory."""
    fn = filename or os.path.basename(repo_path)
    subfolder = classify_model_file(fn, key, repo_path)
    return os.path.join(comfy_models_dir, subfolder, fn)


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
            return False  # Real file already exists at link path; do not overwrite blindly
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        return True
    except OSError:
        return False


def sync_all(workspace_dir: str = "/workspace") -> dict[str, int]:
    """Execute complete bidirectional model synchronization."""
    ws = Path(workspace_dir)
    comfy_root = ws / "ComfyUI"
    if not comfy_root.is_dir() and Path("/Comfy").is_dir():
        comfy_root = Path("/Comfy")

    comfy_models = comfy_root / "models"
    legacy_models = ws / "models"
    fizgig_dir = Path(os.environ.get("FIZGIG_DIR", str(ws / "Fizgig")))
    fizgig_models = fizgig_dir / "user_data" / "models"
    prefs_file = fizgig_dir / "user_data" / "prefs.json"

    # 1. Ensure standard ComfyUI model subdirectories exist
    standard_subdirs = [
        "checkpoints", "diffusion_models", "text_encoders", "vae", "loras",
        "vae_approx", "latent_upscale_models", "controlnet", "upscale_models"
    ]
    for sub in standard_subdirs:
        (comfy_models / sub).mkdir(parents=True, exist_ok=True)

    legacy_models.mkdir(parents=True, exist_ok=True)
    if not fizgig_models.is_symlink() and not fizgig_models.is_dir():
        fizgig_models.mkdir(parents=True, exist_ok=True)

    migrated = 0
    linked = 0

    # 2. Migrate real files from /workspace/models to /workspace/ComfyUI/models/<subfolder>/
    if legacy_models.is_dir():
        for f in list(legacy_models.iterdir()):
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

    # 3. Migrate real files from Fizgig models dir if it is an independent real directory
    try:
        same_as_legacy = fizgig_models.resolve() == legacy_models.resolve()
    except OSError:
        same_as_legacy = False

    if fizgig_models.is_dir() and not same_as_legacy:
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

    # 4. Create symlinks in legacy_models and fizgig_models for every model found in ComfyUI
    for sub in comfy_models.iterdir():
        if sub.is_dir():
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in VALID_MODEL_EXTENSIONS:
                    if ensure_symlink(f, legacy_models / f.name):
                        linked += 1
                    if not same_as_legacy and fizgig_models.is_dir():
                        if ensure_symlink(f, fizgig_models / f.name):
                            linked += 1

    # 5. Update Fizgig prefs.json
    prefs: dict[str, str] = {}
    if prefs_file.is_file():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}

    prefs["lora_output_dir"] = "/workspace/ComfyUI/models/loras"
    prefs["input_dataset_dir"] = "/workspace/datasets"
    prefs["profiles_dir"] = "/workspace/profiles"
    prefs["cache_dir"] = "/workspace/cache"
    prefs["web_password"] = ""

    search_roots = [
        comfy_models,
        legacy_models,
        fizgig_models,
        Path("/Comfy/models"),
    ]

    for key, (subfolder, filename) in KEY_TO_COMFY_MAP.items():
        found_path: str | None = None
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
        elif not prefs.get(key) or not os.path.isfile(str(prefs.get(key))):
            prefs[key] = f"/workspace/ComfyUI/models/{subfolder}/{filename}"

    try:
        prefs_file.parent.mkdir(parents=True, exist_ok=True)
        prefs_file.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[model_sync][warn] Failed writing prefs.json: {e}")

    # 6. Ensure extra_model_paths.yaml exists in ComfyUI
    extra_yaml_content = """# ComfyUI extra model paths configuration for Fizgig & Workspace
fizgig_workspace:
    base_path: /workspace/models
    checkpoints: checkpoints
    diffusion_models: diffusion_models
    text_encoders: text_encoders
    vae: vae
    loras: loras

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
            if ypath.parent.is_dir() and not ypath.is_file():
                ypath.write_text(extra_yaml_content, encoding="utf-8")
                print(f"[model_sync] Wrote {ypath}")
        except OSError:
            pass

    print(f"[model_sync] Complete: migrated {migrated} models, linked {linked} models.")
    return {"migrated": migrated, "linked": linked}


if __name__ == "__main__":
    target_ws = sys.argv[1] if len(sys.argv) > 1 else "/workspace"
    sync_all(target_ws)
