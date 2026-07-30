FROM runpod/comfyui:cuda13.0

ARG CUDA="130"
ARG PYTHON_VERSION=3.12
ARG PYTORCH=2.13.0

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/London \
    PYTHONUNBUFFERED=1 \
    SHELL=/bin/bash

# Install system dependencies
RUN apt update && apt install -y --no-install-recommends \
    build-essential \
    software-properties-common \
    bash \
    dos2unix \
    git \
    git-lfs \
    ncdu \
    nginx \
    net-tools \
    dnsutils \
    inetutils-ping \
    openssh-server \
    libglib2.0-0 \
    libsm6 \
    libgl1 \
    libxrender1 \
    libxext6 \
    ffmpeg \
    wget \
    curl \
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
    pkg-config \
    plocate \
    parallel \
    pv \
    sysstat \
    pigz \
    lz4 \
    zstd \
    cpio \
    jq \
    mc \
    libcairo2-dev \
    libgoogle-perftools4 \
    libtcmalloc-minimal4 \
    apt-transport-https \
    ca-certificates \
    libegl1 \
    portaudio19-dev \
    python3-opencv \
    nvtop \
    cmake \
 && rm -rf /var/lib/apt/lists/*

 # Install Node.js v23.x from nodesource
RUN curl -fsSL https://deb.nodesource.com/setup_23.x | bash - \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Python 3.12 (deadsnakes) + pip ---
RUN add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update && apt-get install -y --no-install-recommends \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python${PYTHON_VERSION}-venv python3-tk \
 && rm -f /usr/bin/python /usr/bin/python3 \
 && ln -s /usr/bin/python${PYTHON_VERSION} /usr/bin/python \
 && ln -s /usr/bin/python${PYTHON_VERSION} /usr/bin/python3 \
 && curl -sS https://bootstrap.pypa.io/get-pip.py | python \
 && python -m pip install --upgrade --no-cache-dir pip

# Install uv (faster installer)
RUN pip install --no-cache-dir uv

# Install JupyterLab and related tools
RUN pip install --no-cache-dir \
    jupyterlab \
    jupyterlab_widgets \
    ipykernel \
    ipywidgets && \
    python3 -m ipykernel install --name "python3" --display-name "Python 3"

# Install FileBrowser
RUN curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

# torch
RUN pip install --no-cache-dir \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130

# --- AI Toolkit (code + isolated venv in image) ---
ENV AITK_DIR=/opt/ai-toolkit
RUN mkdir -p $AITK_DIR
WORKDIR $AITK_DIR
RUN git clone --depth 1 https://github.com/ostris/ai-toolkit.git $AITK_DIR/repo
RUN python -m venv $AITK_DIR/venv && \
    $AITK_DIR/venv/bin/python -m pip install --upgrade pip wheel setuptools
RUN set -e; \
    sys_torch="$(python -c 'import torch; print(torch.__version__)' 2>/dev/null || true)"; \
    sys_torchvision="$(python -c 'import torchvision; print(torchvision.__version__)' 2>/dev/null || true)"; \
    sys_torchaudio="$(python -c 'import torchaudio; print(torchaudio.__version__)' 2>/dev/null || true)"; \
    if [ -n "$sys_torch" ] && [ -n "$sys_torchvision" ] && [ -n "$sys_torchaudio" ]; then \
      $AITK_DIR/venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cu130 \
        "torch==$sys_torch" "torchvision==$sys_torchvision" "torchaudio==$sys_torchaudio"; \
    else \
      $AITK_DIR/venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchvision torchaudio; \
    fi; \
    $AITK_DIR/venv/bin/python -m pip install -r $AITK_DIR/repo/requirements.txt; \
    touch $AITK_DIR/.deps_installed
RUN if [ -f $AITK_DIR/repo/ui/package.json ]; then \
      cd $AITK_DIR/repo/ui && npm install --include=dev && npm rebuild sqlite3 --build-from-source && npm run update_db && npm run build && \
      touch $AITK_DIR/.ui_built; \
    fi
    
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
ENV CUSTOM_NODES="jnxmx/ComfyUI_HuggingFace_Downloader kijai/ComfyUI-KJNodes Fannovel16/comfyui_controlnet_aux crystian/ComfyUI-Crystools Kosinkadink/ComfyUI-VideoHelperSuite willmiao/ComfyUI-Lora-Manager city96/ComfyUI-GGUF Fannovel16/ComfyUI-Frame-Interpolation lquesada/ComfyUI-Inpaint-CropAndStitch ssitu/ComfyUI_UltimateSDUpscale Comfy-Org/ComfyUI-Manager Comfy-Org/Nvidia_RTX_Nodes_ComfyUI jnxmx/ComfyUI-RunPod-Control"
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
    python - <<'PY' > /etc/pip/constraints.txt
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
    (cd /Comfy && python "$manager_cli" show snapshot-list --mode remote) || \
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
WORKDIR /
COPY README.md /usr/share/nginx/html/README.md
COPY 502.html /usr/share/nginx/html/502.html
COPY assets/ /usr/share/nginx/html/assets/
COPY nginx.conf /etc/nginx/nginx.conf
COPY config.ini /Comfy/user/__manager/config.ini
COPY comfy.settings.json /Comfy/user/default/comfy.settings.json
COPY scripts/ /scripts/
COPY wheels/ /wheels/
COPY start.sh /start.sh
RUN dos2unix /start.sh && chmod +x /start.sh \
 && find /scripts -type f -name "*.sh" -exec dos2unix {} \; -exec chmod +x {} \;


## AI Toolkit will be bootstrapped at runtime by start.sh
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
    AI_TOOLKIT_AUTH="password" \
    NIGHTLY_COMFYUI=0 \
    NODE_ENV=production

CMD ["/start.sh"]
