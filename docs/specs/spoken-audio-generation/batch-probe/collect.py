"""Assemble one Batch cold cycle's timeline from AWS's own records.

    python3 collect.py <jobId> <computeEnvironmentName> > cycleN.json

Sources: DescribeJobs (created / started / stopped), the ECS task behind the
job (image pull start and stop), the worker instance (type, AZ, launch time)
and the job's JSON log lines (host boot, weights, model load, render).
"""

import datetime as dt
import json
import subprocess
import sys

JOB, CE = sys.argv[1], sys.argv[2]


def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}


def iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(secs, dt.timezone.utc).isoformat(timespec="milliseconds")
    return str(value)


job = aws("batch", "describe-jobs", "--jobs", JOB)["jobs"][0]
attempt = (job.get("attempts") or [{}])[-1]
container = attempt.get("container") or job.get("container") or {}
cluster = aws("batch", "describe-compute-environments", "--compute-environments", CE)["computeEnvironments"][0]["ecsClusterArn"]
out = {
    "job": {"status": job["status"], "statusReason": job.get("statusReason"), "createdAt": iso(job.get("createdAt")),
            "startedAt": iso(job.get("startedAt")), "stoppedAt": iso(job.get("stoppedAt")),
            "exitCode": container.get("exitCode"), "reason": container.get("reason")},
}
if container.get("taskArn"):
    task = (aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", container["taskArn"]).get("tasks") or [{}])[0]
    out["ecs_task"] = {k: iso(task.get(k)) for k in ("createdAt", "pullStartedAt", "pullStoppedAt", "startedAt", "stoppedAt")}
if container.get("containerInstanceArn"):
    ci = (aws("ecs", "describe-container-instances", "--cluster", cluster, "--container-instances",
              container["containerInstanceArn"]).get("containerInstances") or [{}])[0]
    iid = ci.get("ec2InstanceId")
    inst = aws("ec2", "describe-instances", "--instance-ids", iid)["Reservations"][0]["Instances"][0] if iid else {}
    out["instance"] = {"id": iid, "type": inst.get("InstanceType"), "az": inst.get("Placement", {}).get("AvailabilityZone"),
                       "launchTime": inst.get("LaunchTime"), "state": inst.get("State", {}).get("Name"),
                       "agentRegisteredAt": iso(ci.get("registeredAt"))}
events = []
if container.get("logStreamName"):
    for e in aws("logs", "get-log-events", "--log-group-name", "/aws/batch/job", "--log-stream-name",
                 container["logStreamName"], "--start-from-head").get("events", []):
        msg = e["message"].strip()
        if msg.startswith("{"):
            try:
                body = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if "event" in body:
                body["at"] = iso(body.pop("t"))
                for k in ("python_start", "host_boot", "startedAt"):
                    if k in body:
                        body[k] = iso(body[k])
                events.append(body)
        elif "Error" in msg or "Traceback" in msg or "Killed" in msg:
            events.append({"event": "stderr", "line": msg[:400]})
out["events"] = events
print(json.dumps(out, indent=2, default=str))
