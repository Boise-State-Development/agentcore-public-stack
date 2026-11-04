"""Score PR-0 renders: intelligibility, and whether the voices hold.

Run on the GPU box from the scoring venv (openai-whisper + transformers):
    python score.py voices <voices_dir>                 pick S3-S5 from the minted candidates
    python score.py renders <render_dir> <voices_dir>   score every render in metrics.jsonl

WER is Whisper medium.en against the script, normalised the way the bake-off
did it (Appendix A), so the numbers compare with its 0.050.

Voice hold: Whisper's word timestamps are aligned to the script, which gives
each script turn a time span. Every turn of at least MIN_TURN_S seconds is
embedded with a speaker-verification model (WavLM base-plus-sv) and compared
with each house voice in that script. A turn is attributed to the voice it is
most similar to. Drift shows up as falling similarity to the right voice, or
wrong attributions, in the last third of a render compared with the first.
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import torch
import whisper
from transformers import AutoFeatureExtractor, WavLMForXVector

MIN_TURN_S = 2.0
SV_MODEL = "microsoft/wavlm-base-plus-sv"


def words_with_speaker(script: str) -> tuple[list[str], list[int], list[int]]:
    words, speakers, turns = [], [], []
    for turn_no, m in enumerate(re.finditer(r"\[S(\d)\]([^\[]*)", script)):
        for w in normalise(m.group(2)):
            words.append(w)
            speakers.append(int(m.group(1)))
            turns.append(turn_no)
    return words, speakers, turns


def normalise(text: str) -> list[str]:
    text = text.lower().replace("-", " ").replace("’", "'")
    text = re.sub(r"\bok\b", "okay", text)
    # the bake-off script's three numbers, normalised exactly as Appendix A did
    text = text.replace("13.8", "thirteen point eight").replace("1823", "eighteen twenty three")
    text = text.replace("1848", "eighteen forty eight")
    return re.findall(r"[a-z']+", text)


def align(ref: list[str], hyp: list[str]) -> tuple[dict, list[tuple[int, int]], list[int]]:
    """Levenshtein alignment. Returns counts, (ref_i, hyp_j) pairs for matches and
    substitutions, and per-reference-word error counts (an insertion is charged
    to the next reference word)."""
    n, m = len(ref), len(hyp)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0] = np.arange(n + 1)
    d[0, :] = np.arange(m + 1)
    hyp_arr = np.array(hyp, dtype=object)
    for i in range(1, n + 1):
        cost = (hyp_arr != ref[i - 1]).astype(np.int32)
        row = np.minimum(d[i - 1, 1:] + 1, d[i - 1, :-1] + cost)
        # insertions need a left-to-right pass
        prev = d[i, 0] = i
        for j in range(1, m + 1):
            prev = min(row[j - 1], prev + 1)
            d[i, j] = prev
    i, j = n, m
    subs = dels = ins = 0
    pairs, per_ref = [], [0] * (n + 1)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i, j] == d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]):
            if ref[i - 1] != hyp[j - 1]:
                subs += 1
                per_ref[i - 1] += 1
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and d[i, j] == d[i - 1, j] + 1:
            dels += 1
            per_ref[i - 1] += 1
            i -= 1
        else:
            ins += 1
            per_ref[min(i, n - 1)] += 1
            j -= 1
    pairs.reverse()
    counts = {"wer": round((subs + dels + ins) / n, 3), "sub": subs, "del": dels, "ins": ins}
    return counts, pairs, per_ref[:n]


class Embedder:
    def __init__(self) -> None:
        self.fe = AutoFeatureExtractor.from_pretrained(SV_MODEL)
        self.model = WavLMForXVector.from_pretrained(SV_MODEL).cuda().eval()

    @torch.no_grad()
    def __call__(self, audio16k: np.ndarray) -> np.ndarray:
        inputs = self.fe(audio16k, sampling_rate=16000, return_tensors="pt").to("cuda")
        emb = self.model(**inputs).embeddings[0]
        return torch.nn.functional.normalize(emb, dim=-1).cpu().numpy()


def transcribe(asr, wav: Path) -> dict:
    return asr.transcribe(str(wav), language="en", temperature=0.0, word_timestamps=True)


def pick_voices(voices_dir: Path) -> None:
    """Choose S3-S5 among the minted candidates: drop any candidate Whisper
    cannot read back word for word, then greedily take, for S3, S4 and S5 in
    turn, the candidate least similar to the voices already chosen."""
    cand_dir = voices_dir / "candidates"
    rows = [json.loads(line) for line in (cand_dir / "metrics.jsonl").read_text().splitlines()]
    rows = [r for r in rows if "speaker" in r]
    asr = whisper.load_model("medium.en", device="cuda")
    emb = Embedder()
    for r in rows:
        wav = cand_dir / f"{r['name']}.wav"
        text = asr.transcribe(str(wav), language="en", temperature=0.0)["text"]
        r["transcript"] = text.strip()
        r["clip_wer"] = align(normalise(r["line"]), normalise(text))[0]["wer"]
        r["emb"] = emb(whisper.load_audio(str(wav)))
    chosen = {r["speaker"]: r for r in rows if r["speaker"] in (1, 2)}
    for spk in (3, 4, 5):
        pool = [r for r in rows if r["speaker"] == spk]
        best_wer = min(r["clip_wer"] for r in pool)
        pool = [r for r in pool if r["clip_wer"] == best_wer]
        for r in pool:
            r["max_sim_to_chosen"] = round(max(float(r["emb"] @ c["emb"]) for c in chosen.values()), 3)
        chosen[spk] = min(pool, key=lambda r: (r["max_sim_to_chosen"], r["seed"]))
    sims = {
        f"S{a}-S{b}": round(float(chosen[a]["emb"] @ chosen[b]["emb"]), 3)
        for a in range(1, 6) for b in range(a + 1, 6)
    }
    voices = {}
    for spk, r in sorted(chosen.items()):
        src = cand_dir / f"{r['name']}.wav"
        (voices_dir / f"voice_s{spk}.wav").write_bytes(src.read_bytes())
        voices[str(spk)] = r["line"]
    (voices_dir / "voices.json").write_text(json.dumps(voices, indent=2))
    report = {
        "chosen": {spk: {k: r[k] for k in ("name", "seed", "audio_s", "clip_wer", "transcript")} for spk, r in chosen.items()},
        "pairwise_similarity": sims,
        "candidates": [{k: r.get(k) for k in ("name", "speaker", "seed", "audio_s", "clip_wer", "max_sim_to_chosen", "transcript")} for r in rows],
    }
    (voices_dir / "selection.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: report[k] for k in ("chosen", "pairwise_similarity")}, indent=2, default=str))


def score_renders(render_dir: Path, voices_dir: Path) -> None:
    rows = [json.loads(line) for line in (render_dir / "metrics.jsonl").read_text().splitlines()]
    rows = [r for r in rows if "words" in r]
    asr = whisper.load_model("medium.en", device="cuda")
    emb = Embedder()
    refs = {int(p.stem[-1]): emb(whisper.load_audio(str(p))) for p in voices_dir.glob("voice_s*.wav")}
    results = {}
    for r in rows:
        wav = render_dir / f"{r['name']}.wav"
        script = Path(r["script"]).read_text()
        ref, spk_of, turn_of = words_with_speaker(script)
        out = transcribe(asr, wav)
        hyp, times = [], []
        for seg in out["segments"]:
            for w in seg.get("words", []):
                for piece in normalise(w["word"]):
                    hyp.append(piece)
                    times.append((w["start"], w["end"]))
        counts, pairs, per_ref = align(ref, hyp)
        n = len(ref)
        thirds = [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]
        wer_thirds = [round(sum(per_ref[a:b]) / (b - a), 3) for a, b in thirds]

        spans: dict[int, list[float]] = {}
        for i, j in pairs:
            span = spans.setdefault(turn_of[i], [1e9, -1.0])
            span[0] = min(span[0], times[j][0])
            span[1] = max(span[1], times[j][1])
        audio = whisper.load_audio(str(wav))
        present = sorted(set(spk_of))
        turn_speaker = {t: s for t, s in zip(turn_of, spk_of)}
        turn_rows = []
        for t, (a, b) in sorted(spans.items()):
            if b - a < MIN_TURN_S:
                continue
            e = emb(audio[int(a * 16000): int(b * 16000)])
            sims = {s: float(e @ refs[s]) for s in present}
            own = turn_speaker[t]
            best = max(sims, key=sims.get)
            others = [v for s, v in sims.items() if s != own]
            turn_rows.append({
                "turn": t, "speaker": own, "start": round(a, 1), "end": round(b, 1),
                "sim_own": round(sims[own], 3),
                "margin": round(sims[own] - max(others), 3) if others else None,
                "attributed": best,
            })
        duration = len(audio) / 16000
        by_third = []
        for k in range(3):
            lo, hi = k * duration / 3, (k + 1) * duration / 3
            part = [x for x in turn_rows if lo <= x["start"] < hi]
            margins = [x["margin"] for x in part if x["margin"] is not None]
            by_third.append({
                "turns": len(part),
                "mean_sim_own": round(float(np.mean([x["sim_own"] for x in part])), 3) if part else None,
                "mean_margin": round(float(np.mean(margins)), 3) if margins else None,
                "misattributed": sum(x["attributed"] != x["speaker"] for x in part),
            })
        results[r["name"]] = {
            **{k: r[k] for k in ("attn", "words", "audio_s", "render_s", "rtf", "peak_alloc_gb", "hit_token_cap")},
            "reference_words": n,
            "hyp_words": len(hyp),
            **counts,
            "wer_by_third": wer_thirds,
            "speaker_turns_scored": len(turn_rows),
            "misattributed_turns": sum(x["attributed"] != x["speaker"] for x in turn_rows),
            "voice_by_third": by_third,
            "turns": turn_rows,
            "transcript": out["text"].strip(),
        }
        summary = {k: v for k, v in results[r["name"]].items() if k not in ("turns", "transcript")}
        print(r["name"], json.dumps(summary), flush=True)
        (render_dir / "scores.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "voices":
        pick_voices(Path(sys.argv[2]))
    else:
        score_renders(Path(sys.argv[2]), Path(sys.argv[3]))
