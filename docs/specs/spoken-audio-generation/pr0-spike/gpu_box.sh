#!/usr/bin/env bash
# Runs on the throwaway GPU box (g6e.xlarge, L40S). BUCKET comes from env.sh.
#   gpu_box.sh setup         MOSS-TTSD env (its own pins), scoring env, weights from S3
#   gpu_box.sh mint          mint house-voice candidates, then pick S3-S5
#   gpu_box.sh render <attn> <jobs.json> <out>   sdpa or flash_attention_2
#   gpu_box.sh score <out>   Whisper WER + voice hold for every render in <out>
#   gpu_box.sh fa            install the FlashAttention wheel built on the CPU box
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
MOSS_COMMIT=46973e425d
MOSS="$HOME/MOSS-TTSD"
P="$HOME/pr0"
MODEL="$HOME/model"

smi() { nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv -l 2 > "$1" & echo $!; }

case "${1:-}" in
  setup)
    sudo apt-get update -y -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    [ -d "$MOSS" ] || { git clone -q https://github.com/OpenMOSS/MOSS-TTSD "$MOSS" && git -C "$MOSS" checkout -q "$MOSS_COMMIT"; }
    cd "$MOSS" && { [ -d .venv ] || uv venv -q -p 3.12 .venv; }
    uv pip install -q -p .venv --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cu128 -r requirements.txt
    mkdir -p "$HOME/score" && cd "$HOME/score" && { [ -d .venv ] || uv venv -q -p 3.12 .venv; }
    uv pip install -q -p .venv --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cu128 \
      torch==2.9.1+cu128 torchaudio==2.9.1+cu128 openai-whisper==20250625 transformers==4.57.1 numpy
    start=$(date +%s)
    aws s3 sync "s3://$BUCKET/model/" "$MODEL/" --only-show-errors
    echo "s3_download_s=$(( $(date +%s) - start ))"
    du -sh "$MODEL"/*
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
    ;;
  mint)
    mkdir -p "$P/voices/candidates"
    PID=$(smi "$P/voices/candidates/nvidia_smi.csv")
    (cd "$MOSS" && PYTHONPATH="$MOSS" .venv/bin/python "$P/moss_render.py" mint "$P/voices/candidates" "$MODEL" sdpa)
    kill "$PID"
    "$HOME/score/.venv/bin/python" "$P/score.py" voices "$P/voices"
    ;;
  render)
    OUT="$P/$4"; mkdir -p "$OUT"
    PID=$(smi "$OUT/nvidia_smi.csv")
    (cd "$MOSS" && PYTHONPATH="$MOSS" .venv/bin/python "$P/moss_render.py" render "$OUT" "$MODEL" "$2" "$3")
    kill "$PID"
    ;;
  score)
    "$HOME/score/.venv/bin/python" "$P/score.py" renders "$P/$2" "$P/voices"
    ;;
  fa)
    aws s3 cp "s3://$BUCKET/wheels/" "$HOME/wheels/" --recursive
    uv pip install -p "$MOSS/.venv" --no-deps "$HOME"/wheels/flash_attn-*.whl
    "$MOSS/.venv/bin/python" -c "import flash_attn, torch; print(flash_attn.__version__, torch.__version__)"
    ;;
  *)
    echo "usage: gpu_box.sh setup|mint|render|score|fa" >&2
    exit 2
    ;;
esac
