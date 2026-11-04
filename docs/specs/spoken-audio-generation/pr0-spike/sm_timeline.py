"""Assemble the cold-start timeline for the PR-0 SageMaker endpoint.

    python3 sm_timeline.py <bucket> > timeline.json

Sources, all read back after the fact rather than estimated:
  - the container's JSON log lines (CloudWatch, one stream per instance):
    host boot (from /proc/stat), the mtime window of the files SageMaker wrote
    to /opt/ml/model, python start, CUDA up, model loaded, warm-up, serving,
    and each invocation
  - Application Auto Scaling activities and the HasBacklogWithoutCapacity
    alarm's state history
  - the async output objects' LastModified times in S3
"""

import datetime as dt
import json
import subprocess
import sys

EP = "spoken-audio-spike"


def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--output", "json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="milliseconds")


def container_events() -> dict:
    group = f"/aws/sagemaker/Endpoints/{EP}"
    streams: dict[str, list] = {}
    token = None
    while True:
        args = ["logs", "filter-log-events", "--log-group-name", group]
        if token:
            args += ["--next-token", token]
        page = aws(*args)
        for e in page.get("events", []):
            msg = e["message"].strip()
            if not msg.startswith("{"):
                continue
            try:
                body = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if "event" not in body:
                continue
            row = {"event": body.pop("event"), "at": iso(body.pop("t"))}
            for key in ("python_start", "host_boot"):
                if key in body:
                    body[key] = iso(body[key])
            md = body.get("model_dir")
            if isinstance(md, dict) and "mtime_min" in md:
                md["mtime_min"] = iso(md["mtime_min"])
                md["mtime_max"] = iso(md["mtime_max"])
            streams.setdefault(e["logStreamName"], []).append({**row, **body})
        token = page.get("nextToken")
        if not token:
            break
    return streams


def main() -> None:
    bucket = sys.argv[1]
    rid = f"endpoint/{EP}/variant/AllTraffic"
    activities = aws("application-autoscaling", "describe-scaling-activities", "--service-namespace", "sagemaker",
                     "--resource-id", rid).get("ScalingActivities", [])
    alarm = aws("cloudwatch", "describe-alarm-history", "--alarm-name", f"{EP}-has-backlog-without-capacity",
                "--history-item-type", "StateUpdate").get("AlarmHistoryItems", [])
    outputs = aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "async/").get("Contents", [])
    print(json.dumps({
        "containers": container_events(),
        "scaling_activities": [
            {k: a.get(k) for k in ("StartTime", "EndTime", "StatusCode", "Description", "Cause")} for a in activities
        ],
        "alarm_history": [{k: h.get(k) for k in ("Timestamp", "HistorySummary")} for h in alarm],
        "s3": [{"key": o["Key"], "last_modified": o["LastModified"], "size": o["Size"]} for o in outputs],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
