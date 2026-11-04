"""MOSS-TTSD v1.0 renders for the PR-0 spike (spoken-audio-generation.md §12).

Run from inside a MOSS-TTSD checkout (it imports the repo's generation_utils):
    PYTHONPATH=$MOSS .venv/bin/python moss_render.py mint  <out_dir> <model_root> <attn>
    PYTHONPATH=$MOSS .venv/bin/python moss_render.py render <out_dir> <model_root> <attn> <jobs.json>

<model_root> holds moss-ttsd/ and codec/ (the same files the SageMaker container
reads). <attn> is sdpa or flash_attention_2; there is no fallback, so a run is
never silently on the other kernel.

mint    renders every house-voice line under every candidate seed in
        `generation` mode, with no reference audio. score.py picks the voices.
render  renders each job in voice_clone_and_continuation mode against the house
        voices in <out_dir>/../voices (voice_sN.wav + voices.json) and appends
        one metrics line per job to <out_dir>/metrics.jsonl.
"""

import json
import re
import sys
import time
import zlib
from pathlib import Path

import soundfile as sf
import torch
from transformers import AutoModel, AutoProcessor

from generation_utils import prepare_sample, run_infer_batch

SAMPLING = dict(temperature=1.1, top_p=0.9, top_k=50, repetition_penalty=1.1)
FRAMES_PER_SECOND = 12.5
# The bake-off's two lines (seeds 7 and 11) plus three new ones.
VOICE_LINES = {
    1: "Hi there, and welcome in. Grab a coffee, get comfortable, and let's dig into today's question together.",
    2: "Thanks for having me. I've been looking forward to this one all week, because the answer is stranger than you'd expect.",
    3: "Good morning. I spent the whole weekend reading about this, and honestly, I have more questions now than when I started.",
    4: "Let me put this as simply as I can. The numbers tell one story, but the people behind them tell another.",
    5: "Oh, I love that. It reminds me of something my grandmother used to say whenever the weather turned cold.",
}
BAKEOFF_SEEDS = {1: 7, 2: 11}
CANDIDATE_SEEDS = [13, 17, 19, 23, 29, 31]


def load(root: Path, attn: str):
    processor = AutoProcessor.from_pretrained(
        str(root / "moss-ttsd"), trust_remote_code=True, codec_path=str(root / "codec")
    )
    processor.audio_tokenizer = processor.audio_tokenizer.to("cuda").eval()
    model = AutoModel.from_pretrained(
        str(root / "moss-ttsd"), trust_remote_code=True, attn_implementation=attn, torch_dtype=torch.bfloat16
    ).to("cuda")
    # transformers 5.0 applies attn_implementation to the outer MossTTSDelay
    # config only; the Qwen3 backbone (language_config) stays on sdpa. Set it
    # on the backbone too, and refuse to run if any attention layer disagrees.
    model.language_model.config._attn_implementation = attn
    seen = {m.config._attn_implementation for n, m in model.named_modules() if n.endswith("self_attn")}
    assert seen == {attn}, f"attention layers report {seen}, wanted {attn}"
    return model.eval(), processor


def render(model, processor, out: Path, name: str, mode: str, sample: dict, seed: int, max_new_tokens: int) -> dict:
    sr = int(processor.model_config.sampling_rate)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    sample_id, record, conversation = prepare_sample(
        line_no=zlib.crc32(name.encode()) % 999999,
        raw_sample=sample,
        mode=mode,
        processor=processor,
        target_sr=sr,
        text_normalize_enabled=True,
        sample_rate_normalize_enabled=True,
    )
    start = time.perf_counter()
    with open(out / "raw.jsonl", "a", encoding="utf-8") as fp:
        run_infer_batch(
            batch_data=[(sample_id, record, conversation)],
            model=model,
            processor=processor,
            mode=mode,
            device="cuda",
            max_new_tokens=max_new_tokens,
            save_dir=raw,
            out_fp=fp,
            **SAMPLING,
        )
    torch.cuda.synchronize()
    render_s = time.perf_counter() - start
    dst = out / f"{name}.wav"
    (raw / f"{sample_id}.wav").rename(dst)
    audio_s = sf.info(str(dst)).duration
    return {
        "name": name,
        "mode": mode,
        "seed": seed,
        "max_new_tokens": max_new_tokens,
        "render_s": round(render_s, 1),
        "audio_s": round(audio_s, 1),
        "rtf": round(render_s / audio_s, 3) if audio_s else None,
        "hit_token_cap": audio_s >= max_new_tokens / FRAMES_PER_SECOND - 1,
        "peak_alloc_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2),
        "peak_reserved_gb": round(torch.cuda.max_memory_reserved() / 1e9, 2),
    }


def emit(out: Path, row: dict) -> None:
    print(json.dumps(row), flush=True)
    with open(out / "metrics.jsonl", "a") as fp:
        fp.write(json.dumps(row) + "\n")


def main() -> None:
    cmd, out, root, attn = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]), sys.argv[4]
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    model, processor = load(root, attn)
    emit(out, {"event": "load", "attn": attn, "load_s": round(time.perf_counter() - t0, 1),
               "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__})

    if cmd == "mint":
        for spk, line in VOICE_LINES.items():
            seeds = [BAKEOFF_SEEDS[spk]] if spk in BAKEOFF_SEEDS else CANDIDATE_SEEDS
            for seed in seeds:
                row = render(model, processor, out, f"s{spk}_seed{seed}", "generation",
                             {"text": f"[S{spk}] {line}"}, seed, 400)
                emit(out, {**row, "speaker": spk, "line": line})
        return

    voices_dir = out.parent / "voices"
    voices = json.loads((voices_dir / "voices.json").read_text())
    for job in json.loads(Path(sys.argv[5]).read_text()):
        script = Path(job["script"]).read_text().strip()
        speakers = sorted({int(n) for n in re.findall(r"\[S(\d)\]", script)})
        words = len(re.sub(r"\[S\d\]", " ", script).split())
        sample = {"text": script}
        for spk in speakers:
            sample[f"prompt_audio_speaker{spk}"] = str(voices_dir / f"voice_s{spk}.wav")
            sample[f"prompt_text_speaker{spk}"] = voices[str(spk)]
        max_new_tokens = job.get("max_new_tokens") or int(words / 2.2 * FRAMES_PER_SECOND)
        row = render(model, processor, out, job["name"], "voice_clone_and_continuation", sample,
                     job.get("seed", 42), max_new_tokens)
        emit(out, {**row, "attn": attn, "words": words, "speakers": speakers, "script": job["script"]})


if __name__ == "__main__":
    main()
