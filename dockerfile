FROM runpod/comfyui:2.5.0-comfyuiv0.45.0-cuda13.0

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/London \
    PYTHONUNBUFFERED=1 \
    SHELL=/bin/bash \
    NVIDIA_DRIVER_CAPABILITIES=all

# Drop unused RunPod baked ComfyUI copy and auth proxy (~500 MB) — we manage /Comfy & nginx directly
RUN rm -rf /opt/comfyui-baked /opt/comfyui-auth-proxy

# Initialize /etc/pip/constraints.txt BEFORE any pip install so nothing can alter the CUDA/Torch/SageAttention foundation
RUN mkdir -p /etc/pip && \
    python3 - <<'PY' > /etc/pip/constraints.txt
from importlib import metadata
from pathlib import Path

lines = []
seen = set()
runtime_constraints = Path("/opt/comfyui-runtime-constraints.txt")
if runtime_constraints.is_file():
    for raw in runtime_constraints.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and "==" in line:
            pkg = line.split("==", 1)[0].strip().lower()
            lines.append(line)
            seen.add(pkg)

for pkg in ("torch", "torchvision", "torchaudio", "bitsandbytes", "onnxruntime-gpu", "torchcodec", "sageattention"):
    if pkg in seen:
        continue
    try:
        ver = metadata.version(pkg)
    except metadata.PackageNotFoundError:
        continue
    lines.append(f"{pkg}=={ver}")
    seen.add(pkg)

print("\n".join(lines))
PY

ENV PIP_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu130" \
    UV_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu130" \
    PIP_CONSTRAINT="/etc/pip/constraints.txt" \
    PIP_BUILD_CONSTRAINT="/etc/pip/constraints.txt" \
    UV_CONSTRAINT="/etc/pip/constraints.txt" \
    UV_BUILD_CONSTRAINT="/etc/pip/constraints.txt"

# Install system dependencies (extra EGL/OpenGL/Vulkan libraries & utilities missing in base image)
RUN apt update && apt install -y --no-install-recommends \
    libglu1-mesa \
    libopengl0 \
    libegl1 \
    libgles2 \
    libegl-mesa0 \
    libgl1-mesa-dri \
    libgbm1 \
    libvulkan1 \
    mesa-vulkan-drivers \
    dos2unix \
    file \
    ncdu \
    nginx \
    psmisc \
    rsync \
    vim \
    nano \
    zip \
    unzip \
    p7zip-full \
    htop \
    screen \
    tmux \
    bc \
    aria2 \
    cron \
    parallel \
    pv \
    sysstat \
    pigz \
    lz4 \
    zstd \
    cpio \
    jq \
    mc \
    libgoogle-perftools4 \
    libtcmalloc-minimal4 \
    nvtop \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    fonts-dejavu-core \
    fonts-liberation \
    fontconfig \
 && rm -rf /var/lib/apt/lists/*

# Install uv (removed in base image) & ensure JupyterLab widgets/ipykernel
RUN pip install --no-cache-dir \
    uv \
    jupyterlab \
    jupyterlab_widgets \
    ipykernel \
    ipywidgets && \
    python3 -m ipykernel install --name "python3" --display-name "Python 3"

# Preserve base image's patched FileBrowser if present; otherwise install it
RUN if ! command -v filebrowser >/dev/null 2>&1; then \
        curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash; \
    fi

# Ensure python symlink exists for python3
RUN ln -sf $(which python3) /usr/local/bin/python || true

# Set up ComfyUI
RUN mkdir -p /Comfy
WORKDIR /Comfy
RUN git clone https://github.com/Comfy-Org/ComfyUI.git . && \
    git fetch --tags && \
    latest_tag=$(git tag -l 'v[0-9]*' | sort -V | tail -n 1) && \
    if [ -n "$latest_tag" ]; then echo "Build-time checking out stable ComfyUI tag: $latest_tag" && git checkout -f "$latest_tag"; fi

RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir \
        opencv-python \
        imageio \
        imageio-ffmpeg \
        ffmpeg-python \
        av \
        runpod \
        hf-transfer \
        huggingface_hub \
        diffusers \
        accelerate \
        insightface \
        face-alignment \
        onnxruntime-gpu \
        comfy-cli \
        packaging \
        pyyaml \
        torchcodec \
        ninja && \
    pip install --no-cache-dir --upgrade comfyui-manager && \
    pip install --no-cache-dir --upgrade --force-reinstall --no-deps comfyui-manager && \
    MANAGER_UTIL="$(python3 -c 'import importlib.util, os; spec = importlib.util.find_spec("comfyui_manager"); print(os.path.join(os.path.dirname(spec.origin), "common", "manager_util.py") if spec and spec.origin else "")')" && \
    if [ -n "$MANAGER_UTIL" ] && [ -f "$MANAGER_UTIL" ]; then \
        sed -i 's/timeout=5)/timeout=30)/g' "$MANAGER_UTIL" && \
        python3 -m compileall -q "$MANAGER_UTIL"; \
    fi

# Clone and install custom nodes
ENV CUSTOM_NODES="jnxmx/ComfyUI_HuggingFace_Downloader kijai/ComfyUI-KJNodes Fannovel16/comfyui_controlnet_aux Kosinkadink/ComfyUI-VideoHelperSuite willmiao/ComfyUI-Lora-Manager city96/ComfyUI-GGUF Fannovel16/ComfyUI-Frame-Interpolation lquesada/ComfyUI-Inpaint-CropAndStitch ssitu/ComfyUI_UltimateSDUpscale Comfy-Org/ComfyUI-Manager Comfy-Org/ComfyUI_Nvidia_VFX_Nodes jnxmx/ComfyUI-RunPod-Control xmarre/ComfyUI-Spectrum-MiniMax-H3"
WORKDIR /Comfy/custom_nodes
RUN for repo in $CUSTOM_NODES; do \
    repo_url="https://github.com/$repo.git"; \
    repo_name=$(basename -s .git "$repo"); \
    if [ -d "$repo_name/.git" ]; then \
        git -C "$repo_name" pull --rebase; \
    else \
        git clone --depth=1 "$repo_url" "$repo_name"; \
    fi; \
done && \
for dir in /Comfy/custom_nodes/*; do \
    if [ -f "$dir/requirements.txt" ]; then \
    pip install --no-cache-dir -r "$dir/requirements.txt"; \
    fi; \
done

# Lock ONLY the binary CUDA/Torch/SageAttention foundation in /etc/pip/constraints.txt
# (leave ComfyUI frontend/templates unconstrained so ComfyUI updates stay clean) and
# verify at build time that nothing drifted from the base runtime stack.
RUN mkdir -p /etc/pip && \
    python3 - <<'PY' > /etc/pip/constraints.txt
from importlib import metadata
from packaging.utils import canonicalize_name

locked = [
    "torch",
    "torchvision",
    "torchaudio",
    "bitsandbytes",
    "onnxruntime-gpu",
    "torchcodec",
    "sageattention",
]
seen = set()

for name in locked:
    try:
        version = metadata.version(name)
    except metadata.PackageNotFoundError:
        continue
    print(f"{name}=={version}")
    seen.add(canonicalize_name(name))
PY
RUN python3 - <<'PY'
from importlib import metadata
from pathlib import Path

runtime_file = Path("/opt/comfyui-runtime-constraints.txt")
if runtime_file.is_file():
    for raw in runtime_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and "==" in line:
            pkg, exp = [x.strip() for x in line.split("==", 1)]
            got = metadata.version(pkg)
            assert got == exp, f"{pkg} drifted during build: expected {exp}, got {got}"

sage_ver = metadata.version("sageattention")
sage_reqs = metadata.requires("sageattention") or []
torch_pin = f"torch=={metadata.version('torch')}"
assert torch_pin in sage_reqs, f"sageattention {sage_ver} requires {sage_reqs}, expected {torch_pin}"
print(f"[build] verified CUDA foundation: {torch_pin}, sageattention=={sage_ver}")
PY

RUN set -e; \
  manager_cli=""; \
  if [ -f /Comfy/custom_nodes/ComfyUI-Manager/cm-cli.py ]; then \
    manager_cli=/Comfy/custom_nodes/ComfyUI-Manager/cm-cli.py; \
  elif [ -f /Comfy/custom_nodes/comfyui-manager/cm-cli.py ]; then \
    manager_cli=/Comfy/custom_nodes/comfyui-manager/cm-cli.py; \
  fi; \
  if [ -n "$manager_cli" ]; then \
    echo "[manager] prefetching snapshot list via $manager_cli"; \
    (cd /Comfy && python3 "$manager_cli" show snapshot-list --mode remote) || \
      echo "[manager][warn] snapshot-list prefetch failed; continuing build."; \
  else \
    echo "ComfyUI-Manager cm-cli not available at build time; skipping snapshot-list prefetch."; \
  fi

# Copy configuration and scripts
COPY 502.html /usr/share/nginx/html/502.html
COPY assets/ /usr/share/nginx/html/assets/
COPY nginx.conf /etc/nginx/nginx.conf
COPY config.ini /Comfy/user/__manager/config.ini
COPY config.ini /config.ini
COPY comfy.settings.json /Comfy/user/default/comfy.settings.json
COPY scripts/ /scripts/
COPY custom_nodes/ComfyUI-Web-Terminal /Comfy/custom_nodes/ComfyUI-Web-Terminal
COPY start.sh /start.sh
RUN dos2unix /start.sh && chmod +x /start.sh \
 && find /scripts -type f -name "*.sh" -exec dos2unix {} \; -exec chmod +x {} \;

WORKDIR /workspace

# Set environment variables
ENV HF_HOME="/workspace/.cache/huggingface" \
    HF_DATASETS_CACHE="/workspace/.cache/huggingface/datasets/" \
    DEFAULT_HF_METRICS_CACHE="/workspace/.cache/huggingface/metrics/" \
    DEFAULT_HF_MODULES_CACHE="/workspace/.cache/huggingface/modules/" \
    HUGGINGFACE_HUB_CACHE="/workspace/.cache/huggingface/hub/" \
    HUGGINGFACE_ASSETS_CACHE="/workspace/.cache/huggingface/assets/" \
    VIRTUALENV_OVERRIDE_APP_DATA="/workspace/.cache/virtualenv/" \
    PIP_CACHE_DIR="/root/.cache/pip/" \
    UV_CACHE_DIR="/root/.cache/uv/" \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    TRANSFORMERS_CACHE="/workspace/.cache/huggingface/transformers" \
    INSIGHTFACE_HOME="/workspace/.cache/insightface" \
    FIZGIG_HOME="/workspace/Fizgig" \
    FIZGIG_POD=1 \
    NIGHTLY_COMFYUI=0 \
    ENSURE_NVIDIA_NGX=1

ENTRYPOINT ["/start.sh"]
