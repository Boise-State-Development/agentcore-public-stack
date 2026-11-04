# AWS Batch cold-start probe

These are the throwaway scripts behind Appendix C of
[`spoken-audio-generation.md`](../../spoken-audio-generation.md). They are kept
so the numbers can be reproduced. They are not production code; PR-1 builds
the real renderer.

| File | Runs on | What it does |
|---|---|---|
| `setup_net_iam.sh` | laptop | a throwaway VPC with public subnets in us-west-2a–2d, an egress-only security group, the bucket, the ECR repository and the three roles; writes every id to `batch.env` |
| `build_userdata.sh` | CPU build box (c7i.2xlarge, AL2023) | copies the pinned weights to S3, builds and pushes the image, and makes the weights EBS snapshot |
| `lt_userdata.txt`, `lt_v1.json`, `lt_v2.json` | launch template | a 2-hour self-terminate, the instance NVMe at `/scratch`, and (v2) the weights snapshot at `/weights` |
| `ce.py`, `jobdef.json` | laptop | the compute environment and job definition |
| `container/` | Batch job | the PR-0 image plus `batch_job.py`: copy the weights, load, render one job, upload the MP3, exit. `requirements.txt` and `serve.py` are the PR-0 copies in [`../pr0-spike/container/`](../pr0-spike/container/) |
| `run_cycle.sh`, `batch_watch.py`, `collect.py` | laptop | one cold cycle: submit, poll every 5 s until the compute environment is back to zero, then join the job, ECS task, instance and container log timestamps |
| `teardown.sh` | laptop | removes everything `setup_net_iam.sh` and the build created |

- **`lt_v2.json` is wrong as committed, on purpose.** It is the template the
  EBS cycle ran with: a 30 GB gp3 volume from the snapshot with 16,000 IOPS and
  no `VolumeSize`. That is 533 IOPS per GB, over gp3's 500, and 29 of that
  cycle's launch attempts failed on it (Appendix C.3). A corrected template
  sets `VolumeSize` to at least 32.
- **The image was built with `--provenance=false`**, which pushed a Docker v2
  schema 2 manifest. ECS pulled it without changes.
- **No audio files are checked in.** The house voices are rendered from the
  seeds in Appendix B.3.
