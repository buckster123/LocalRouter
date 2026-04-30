# syntax=docker/dockerfile:1
# vastai-gguf-launcher — builder image
#
# Ships the CUDA dev toolchain, cmake, and HF tools but NO pre-compiled
# llama-server. launch.sh detects the GPU's SM arch at runtime and compiles
# llama.cpp for the exact target. Adds ~8-12 min to cold start but works on
# any CUDA-capable GPU: H100 (SM90), A100 (SM80), B200 (SM100), etc.
#
# Build:
#   docker build -f Dockerfile.builder -t ghcr.io/<you>/vastai-gguf:builder .
#
# For B200 (SM100) you may need CUDA 12.9+. Swap the base image ARG if so.

ARG CUDA_VERSION=12.8.0
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG LLAMA_CPP_REF=master

# Build tools + runtime deps in one layer (no compile here — launch.sh does it)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential cmake ninja-build \
        curl ca-certificates jq tini \
        python3 python3-pip python3-venv \
        libcurl4-openssl-dev libcurl4 libgomp1 pciutils \
    && rm -rf /var/lib/apt/lists/*

# Cache the llama.cpp source so the compile step only needs to run cmake
# (no re-clone on every boot when the image is cached on the host)
WORKDIR /opt
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} \
        https://github.com/ggml-org/llama.cpp.git

# hf CLI in its own venv
RUN python3 -m venv /opt/hf-venv \
 && /opt/hf-venv/bin/pip install --no-cache-dir -U pip "huggingface_hub[cli]>=0.36" \
 && ln -s /opt/hf-venv/bin/hf /usr/local/bin/hf

WORKDIR /app
COPY launch.sh /app/launch.sh
RUN chmod +x /app/launch.sh

ENV MODELS_DIR=/workspace/models \
    PORT=8000 \
    HOST=0.0.0.0
EXPOSE 8000
VOLUME ["/workspace"]

# tini handles PID 1 + signal forwarding
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/launch.sh"]
