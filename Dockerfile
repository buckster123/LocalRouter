# syntax=docker/dockerfile:1
# Qwen3.6 on rented Vast.ai GPU (5090/4090) via llama.cpp + Unsloth UD GGUFs.
# Built for SM89 (4090) and SM120 (5090). CUDA 12.8 covers both.

FROM nvidia/cuda:12.8.0-devel-ubuntu24.04 AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG LLAMA_CPP_REF=master
# CUDA archs: 89 = Ada (4090), 120 = Blackwell (5090). Build both, fat binary.
ARG CUDA_ARCHS="89-real;120-real"

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential cmake ninja-build curl ca-certificates \
        libcurl4-openssl-dev pciutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggml-org/llama.cpp.git

WORKDIR /opt/llama.cpp
RUN cmake -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON \
        -DGGML_NATIVE=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS}" \
        -DLLAMA_CURL=ON \
        -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build --config Release -j$(nproc) --target llama-server llama-cli llama-mtmd-cli llama-gguf-split llama-bench \
    && strip build/bin/llama-* || true

# ---------------- runtime ----------------
FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv ca-certificates curl jq tini \
        libcurl4 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# hf CLI in its own venv (keeps system Python clean, PEP 668 friendly)
RUN python3 -m venv /opt/hf-venv \
 && /opt/hf-venv/bin/pip install --no-cache-dir -U pip "huggingface_hub[cli]>=0.36" \
 && ln -s /opt/hf-venv/bin/hf /usr/local/bin/hf

COPY --from=builder /opt/llama.cpp/build/bin/llama-server   /usr/local/bin/
COPY --from=builder /opt/llama.cpp/build/bin/llama-cli      /usr/local/bin/
COPY --from=builder /opt/llama.cpp/build/bin/llama-mtmd-cli /usr/local/bin/
COPY --from=builder /opt/llama.cpp/build/bin/llama-bench    /usr/local/bin/
COPY --from=builder /opt/llama.cpp/build/bin/llama-gguf-split /usr/local/bin/

WORKDIR /app
COPY launch.sh /app/launch.sh
RUN chmod +x /app/launch.sh

ENV MODELS_DIR=/workspace/models \
    PORT=8000 \
    HOST=0.0.0.0
EXPOSE 8000
VOLUME ["/workspace"]

# tini handles PID 1 + signal forwarding (clean SIGTERM on vast destroy)
ENTRYPOINT ["/usr/bin/tini","-g","--","/app/launch.sh"]
