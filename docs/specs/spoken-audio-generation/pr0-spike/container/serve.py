"""Minimal SageMaker bring-your-own-container server for the PR-0 cold-start spike.

Not the production renderer. It exists to time a cold start honestly, so it
loads MOSS-TTSD the way the real container will (weights from /opt/ml/model,
hub offline) and logs a JSON line at every stage:

  container_start   python is running; carries the host boot time (/proc/stat
                    btime) and the mtime window of the files SageMaker wrote
                    into /opt/ml/model, so provisioning, image pull and weight
                    download can be told apart afterwards
  torch_imported    CUDA initialised
  model_loaded      processor, codec and the 8B model on the GPU
  warmup_done       one short generation, so the first job does not pay for it
  serving           /ping now answers 200

/invocations takes {jobId, script, maxNewTokens?} with turns tagged [S1]..[S5],
renders it in voice_clone_and_continuation mode against the house voices in
/opt/ml/model/voices, encodes a -16 LUFS MP3, uploads it to
s3://$OUTPUT_BUCKET/async/audio/<jobId>.mp3 and returns a JSON manifest.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

T_START = time.time()
ROOT = Path(os.environ.get("MODEL_ROOT", "/opt/ml/model"))
MODEL_DIR = ROOT / "moss-ttsd"
CODEC_DIR = ROOT / "codec"
VOICE_DIR = ROOT / "voices"
SAMPLING = dict(temperature=1.1, top_p=0.9, top_k=50, repetition_penalty=1.1)
FRAMES_PER_SECOND = 12.5


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, "t": round(time.time(), 3), **fields}), flush=True)


def host_boot_time() -> int:
    with open("/proc/stat") as fp:
        for line in fp:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


def model_dir_window(root: Path) -> dict:
    mtimes, total = [], 0
    for path in root.rglob("*"):
        if path.is_file():
            st = path.stat()
            mtimes.append(st.st_mtime)
            total += st.st_size
    if not mtimes:
        return {"files": 0}
    return {"files": len(mtimes), "bytes": total, "mtime_min": round(min(mtimes), 3), "mtime_max": round(max(mtimes), 3)}


log("container_start", python_start=round(T_START, 3), host_boot=host_boot_time(), model_dir=model_dir_window(ROOT))

import torch  # noqa: E402

torch.cuda.init()
log("torch_imported", gpu=torch.cuda.get_device_name(0), torch=torch.__version__)

from transformers import AutoModel, AutoProcessor  # noqa: E402

sys.path.insert(0, "/app/moss")
from generation_utils import prepare_sample, run_infer_batch  # noqa: E402

t = time.time()
processor = AutoProcessor.from_pretrained(str(MODEL_DIR), trust_remote_code=True, codec_path=str(CODEC_DIR))
processor.audio_tokenizer = processor.audio_tokenizer.to("cuda").eval()
model = (
    AutoModel.from_pretrained(
        str(MODEL_DIR), trust_remote_code=True, attn_implementation="sdpa", torch_dtype=torch.bfloat16
    )
    .to("cuda")
    .eval()
)
SAMPLE_RATE = int(processor.model_config.sampling_rate)
log("model_loaded", load_s=round(time.time() - t, 1), gpu_gb=round(torch.cuda.memory_allocated() / 1e9, 1))

VOICES = json.loads((VOICE_DIR / "voices.json").read_text())  # {"1": "transcript", ...}
LOCK = threading.Lock()


def render(script: str, max_new_tokens: int, workdir: Path) -> Path:
    speakers = sorted({int(n) for n in re.findall(r"\[S(\d)\]", script)})
    sample = {"text": script}
    for spk in speakers:
        sample[f"prompt_audio_speaker{spk}"] = str(VOICE_DIR / f"voice_s{spk}.wav")
        sample[f"prompt_text_speaker{spk}"] = VOICES[str(spk)]
    mode = "voice_clone_and_continuation"
    sample_id, record, conversation = prepare_sample(
        line_no=1,
        raw_sample=sample,
        mode=mode,
        processor=processor,
        target_sr=SAMPLE_RATE,
        text_normalize_enabled=True,
        sample_rate_normalize_enabled=True,
    )
    with open(workdir / "out.jsonl", "w", encoding="utf-8") as fp:
        run_infer_batch(
            batch_data=[(sample_id, record, conversation)],
            model=model,
            processor=processor,
            mode=mode,
            device="cuda",
            max_new_tokens=max_new_tokens,
            save_dir=workdir,
            out_fp=fp,
            **SAMPLING,
        )
    torch.cuda.synchronize()
    return workdir / f"{sample_id}.wav"


t = time.time()
with tempfile.TemporaryDirectory() as tmp:
    render("[S1] Warming up.", 60, Path(tmp))
log("warmup_done", warmup_s=round(time.time() - t, 1))


def invoke(body: dict) -> dict:
    import boto3
    import soundfile as sf

    job_id = re.sub(r"[^A-Za-z0-9-]", "", str(body.get("jobId", "job")))[:64]
    script = str(body["script"])
    words = len(re.sub(r"\[S\d\]", " ", script).split())
    max_new_tokens = int(body.get("maxNewTokens") or max(400, int(words / 2.2 * FRAMES_PER_SECOND)))
    with LOCK, tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        torch.cuda.reset_peak_memory_stats()
        t0 = time.time()
        wav = render(script, max_new_tokens, work)
        gen_s = time.time() - t0
        mp3 = work / "out.mp3"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", str(mp3)],
            check=True,
        )
        render_s = time.time() - t0
        audio_s = sf.info(str(wav)).duration
        key = f"async/audio/{job_id}.mp3"
        boto3.client("s3").upload_file(str(mp3), os.environ["OUTPUT_BUCKET"], key)
    manifest = {
        "jobId": job_id,
        "audioKey": key,
        "words": words,
        "audioS": round(audio_s, 1),
        "generateS": round(gen_s, 1),
        "renderS": round(render_s, 1),
        "peakGpuGb": round(torch.cuda.max_memory_allocated() / 1e9, 1),
        "startedAt": round(t0, 3),
    }
    log("invocation_done", **manifest)
    return manifest


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._reply(200 if self.path == "/ping" else 404, {"ok": self.path == "/ping"})

    def do_POST(self) -> None:
        if self.path != "/invocations":
            self._reply(404, {"error": "not found"})
            return
        log("invocation_received")
        length = int(self.headers.get("Content-Length", "0"))
        try:
            self._reply(200, invoke(json.loads(self.rfile.read(length))))
        except Exception as exc:  # the spike reports, it does not recover
            log("invocation_failed", error=repr(exc)[:500])
            self._reply(500, {"error": repr(exc)[:500]})


if __name__ == "__main__":
    log("serving", ready_s=round(time.time() - T_START, 1))
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
