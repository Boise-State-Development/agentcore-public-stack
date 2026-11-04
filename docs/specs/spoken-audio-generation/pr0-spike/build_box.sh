#!/usr/bin/env bash
# Runs on the throwaway CPU build box (c7i.8xlarge, DLAMI Base with CUDA 12.8).
#   build_box.sh fa      build a FlashAttention 2 wheel for torch 2.9.1+cu128 / cp312
#   build_box.sh weights download MOSS-TTSD + codec and copy them, uncompressed, to S3
#   build_box.sh image   build the cold-start image and push it to ECR
# BUCKET and REPO come from the environment.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
FA_VERSION=v2.8.3.post2
MOSS_COMMIT=46973e425d
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh

case "${1:-}" in
  fa)
    mkdir -p ~/fa && cd ~/fa
    [ -d .venv ] || uv venv -q -p 3.12 .venv
    uv pip install -q -p .venv --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cu128 \
      torch==2.9.1+cu128 psutil packaging ninja setuptools wheel einops pip
    [ -d src ] || git clone -q --depth 1 --branch "$FA_VERSION" https://github.com/Dao-AILab/flash-attention src
    cd src && git submodule update -q --init --depth 1 csrc/cutlass
    grep -n "FLASH_ATTN_CUDA_ARCHS\|def cuda_archs" setup.py | head
    # sm_80 code runs on the L40S (sm_89): same major version.
    start=$(date +%s)
    CUDA_HOME=/usr/local/cuda-12.8 PATH=$HOME/fa/.venv/bin:/usr/local/cuda-12.8/bin:$PATH \
      FLASH_ATTN_CUDA_ARCHS=80 FLASH_ATTENTION_FORCE_BUILD=TRUE MAX_JOBS=10 NVCC_THREADS=1 \
      ../.venv/bin/python -m pip wheel . --no-build-isolation --no-deps -w ../dist > ../build.log 2>&1 || { tail -n 40 ../build.log; exit 1; }
    echo "fa_build_s=$(( $(date +%s) - start ))"
    ls -la ../dist
    aws s3 cp ../dist/ "s3://$BUCKET/wheels/" --recursive
    ;;
  weights)
    mkdir -p ~/w && cd ~/w
    { [ -d .venv ] || uv venv -q -p 3.12 .venv; } && uv pip install -q -p .venv huggingface_hub==1.1.2 hf_transfer
    HF_HUB_ENABLE_HF_TRANSFER=1 .venv/bin/python - <<'PY'
import json
from huggingface_hub import HfApi, snapshot_download
out = {}
for repo, local in [("OpenMOSS-Team/MOSS-TTSD-v1.0", "model/moss-ttsd"), ("OpenMOSS-Team/MOSS-Audio-Tokenizer", "model/codec")]:
    sha = HfApi().model_info(repo).sha
    snapshot_download(repo, revision=sha, local_dir=local)
    out[repo] = sha
json.dump(out, open("revisions.json", "w"), indent=2)
print(out)
PY
    rm -rf model/*/.cache
    du -sh model/*
    start=$(date +%s)
    aws s3 sync model "s3://$BUCKET/model/" --only-show-errors
    echo "s3_upload_s=$(( $(date +%s) - start ))"
    aws s3 cp revisions.json "s3://$BUCKET/revisions.json"
    ;;
  image)
    cd ~/container
    curl -sSfL "https://raw.githubusercontent.com/OpenMOSS/MOSS-TTSD/$MOSS_COMMIT/generation_utils.py" -o generation_utils.py
    start=$(date +%s)
    sudo docker build -q -t "$REPO:spike" .
    echo "docker_build_s=$(( $(date +%s) - start ))"
    sudo docker image inspect "$REPO:spike" --format '{{.Size}}'
    aws ecr get-login-password | sudo docker login -u AWS --password-stdin "${REPO%%/*}"
    start=$(date +%s)
    sudo docker push -q "$REPO:spike"
    echo "docker_push_s=$(( $(date +%s) - start ))"
    ;;
  *)
    echo "usage: build_box.sh fa|weights|image" >&2
    exit 2
    ;;
esac
