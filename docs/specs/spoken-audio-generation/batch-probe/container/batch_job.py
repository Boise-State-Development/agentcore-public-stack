"""One-shot AWS Batch entry point for the cold-start probe (spec Appendix C).

Puts the weights where serve.py expects them, then reuses serve.py's load,
warm-up and render path for a single job and exits. Every stage logs a JSON
line, as serve.py does, so the timeline can be rebuilt from the job's log.

WEIGHTS=s3   copy s3://$BUCKET/model/ to /scratch/model (the host mounts the
             instance's local NVMe at /scratch when it has one)
WEIGHTS=ebs  read /weights/model, an EBS volume restored from a snapshot that
             the host mounted read-only at /weights
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

T0 = time.time()


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, "t": round(time.time(), 3), **fields}), flush=True)


def host_boot_time() -> int:
    with open("/proc/stat") as fp:
        for line in fp:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


def fs_gb(path: str) -> float:
    st = os.statvfs(path)
    return round(st.f_blocks * st.f_frsize / 1e9, 1)


def fetch_from_s3(bucket: str, dest: Path) -> dict:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config

    s3 = boto3.client("s3", config=Config(max_pool_connections=64))
    cfg = TransferConfig(multipart_threshold=64 << 20, multipart_chunksize=64 << 20, max_concurrency=16)
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="model/"):
        objs += [o for o in page.get("Contents", []) if not o["Key"].endswith("/")]

    def one(obj: dict) -> None:
        target = dest / obj["Key"][len("model/"):]
        target.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, obj["Key"], str(target), Config=cfg)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, objs))
    return {"files": len(objs), "bytes": sum(o["Size"] for o in objs)}


def main() -> None:
    source, bucket = os.environ["WEIGHTS"], os.environ["BUCKET"]
    log("job_start", python_start=round(T0, 3), host_boot=host_boot_time(), weights=source)
    t = time.time()
    if source == "s3":
        root = Path("/scratch/model")
        info = fetch_from_s3(bucket, root)
        info["fs_gb"] = fs_gb("/scratch")
    else:
        root = Path("/weights/model")
        info = {"files": sum(1 for p in root.rglob("*") if p.is_file()), "fs_gb": fs_gb("/weights")}
    log("weights_ready", source=source, seconds=round(time.time() - t, 1), **info)

    import boto3

    body = json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=f"jobs/{os.environ['JOB']}.json")["Body"].read())
    os.environ["MODEL_ROOT"] = str(root)
    os.environ["OUTPUT_BUCKET"] = bucket
    sys.path.insert(0, "/app")
    import serve  # loads the model and warms up at import, logging each stage

    serve.invoke(body)
    log("job_done", total_s=round(time.time() - T0, 1))


if __name__ == "__main__":
    main()
