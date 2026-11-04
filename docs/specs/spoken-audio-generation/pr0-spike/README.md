# PR-0 renderer spike

These are the throwaway scripts behind Appendix B of
[`spoken-audio-generation.md`](../../spoken-audio-generation.md). They are kept
so the numbers can be reproduced. They are not production code; PR-1 builds
the real renderer.

| File | Runs on | What it does |
|---|---|---|
| `build_box.sh` | CPU build box (DLAMI, CUDA 12.8) | builds the FlashAttention wheel, copies the pinned weights to S3, and builds and pushes the endpoint image |
| `gpu_box.sh` | GPU box (g6e.xlarge) | sets up the environments, mints the house voices, renders and scores |
| `moss_render.py` | GPU box | MOSS-TTSD mint and render, with per-job timing and memory |
| `score.py` | GPU box | Whisper `medium.en` WER and voice hold (WavLM speaker similarity per turn) |
| `jobs.json`, `scripts/` | GPU box | the render jobs and their synthetic scripts |
| `container/` | SageMaker | the cold-start image: `/ping`, `/invocations`, JSON timing logs |
| `sagemaker.sh`, `cold_watch.sh`, `sm_timeline.py` | laptop | create, invoke, scale and tear down the async endpoint, then assemble its cold-start timeline |

- **The pushed image had to be fixed by hand.** `docker build` pushed an OCI
  image index, and SageMaker refused it and a plain OCI manifest. The spike
  rewrote the manifest's media types to Docker v2 schema 2 and re-put it as tag
  `spike-v2`, which is the tag `sagemaker.sh` uses. A real build should use
  `docker buildx build --provenance=false --output type=registry,oci-mediatypes=false`.
- **No audio files are checked in.** The house voices are rendered from the
  seeds in Appendix B.3.
