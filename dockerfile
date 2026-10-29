FROM runpod/comfyui:cuda13.0

ARG CUDA="130"
ARG PYTORCH=2.13.0

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/London \
    PYTHONUNBUFFERED=1 \
    SHELL=/bin/bash

# Install system dependencies (extra EGL/OpenGL libraries & utilities missing in base image)
RUN apt update && apt install -y --no-install-recommends \
    libglu1-mesa \
    libopengl0 \
    libegl1 \
    libgles2 \
    libegl-mesa0 \
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

# Install uv (faster installer) & JupyterLab
RUN pip install --no-cache-dir \
    uv \
    jupyterlab \
    jupyterlab_widgets \
    ipykernel \
    ipywidgets && \
    python3 -m ipykernel install --name "python3" --display-name "Python 3"

# Install FileBrowser
RUN curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

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
        ninja 

# Clone and install custom nodes
ENV CUSTOM_NODES="jnxmx/ComfyUI_HuggingFace_Downloader kijai/ComfyUI-KJNodes Fannovel16/comfyui_controlnet_aux crystian/ComfyUI-Crystools Kosinkadink/ComfyUI-VideoHelperSuite willmiao/ComfyUI-Lora-Manager city96/ComfyUI-GGUF Fannovel16/ComfyUI-Frame-Interpolation lquesada/ComfyUI-Inpaint-CropAndStitch ssitu/ComfyUI_UltimateSDUpscale Comfy-Org/ComfyUI-Manager Comfy-Org/ComfyUI_Nvidia_VFX_Nodes jnxmx/ComfyUI-RunPod-Control xmarre/ComfyUI-Spectrum-MiniMax-H3 LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler"
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

# Keep a constraints file with the image's known-good runtime foundation so
# node installs cannot silently replace CUDA/Torch-adjacent wheels later.
RUN mkdir -p /etc/pip && \
    python3 - <<'PY' > /etc/pip/constraints.txt
from importlib import metadata

locked = [
    "torch",
    "torchvision",
    "torchaudio",
    "bitsandbytes",
    "onnxruntime-gpu",
    "torchcodec",
]

for name in locked:
    try:
        version = metadata.version(name)
    except metadata.PackageNotFoundError:
        continue
    print(f"{name}=={version}")
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

# Clone node from Codeberg
#RUN git clone --depth=1 https://codeberg.org/Gourieff/comfyui-reactor-node.git /Comfy/custom_nodes/comfyui-reactor-node && \
#    if [ -f "/Comfy/custom_nodes/comfyui-reactor-node/requirements.txt" ]; then \
#    pip install --no-cache-dir -r /Comfy/custom_nodes/comfyui-reactor-node/requirements.txt; \
#    fi 
    #&& \
    #mv /Comfy/custom_nodes/comfyui-reactor-node /Comfy/custom_nodes/comfyui-reactor-node.disabled

# Copy configuration and scripts
COPY 502.html /usr/share/nginx/html/502.html
COPY assets/ /usr/share/nginx/html/assets/
COPY nginx.conf /etc/nginx/nginx.conf
COPY config.ini /Comfy/user/__manager/config.ini
COPY config.ini /config.ini
COPY extra_model_paths.yaml /Comfy/extra_model_paths.yaml
COPY extra_model_paths.yaml /extra_model_paths.yaml
COPY comfy.settings.json /Comfy/user/default/comfy.settings.json
COPY scripts/ /scripts/
COPY wheels/ /wheels/
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
    PIP_CACHE_DIR="/workspace/.cache/pip/" \
    UV_CACHE_DIR="/workspace/.cache/uv/" \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    TRANSFORMERS_CACHE="/workspace/.cache/huggingface/transformers" \
    INSIGHTFACE_HOME="/workspace/.cache/insightface" \
    FIZGIG_HOME="/workspace/Fizgig" \
    FIZGIG_POD=1 \
    NIGHTLY_COMFYUI=0

CMD ["/start.sh"]
