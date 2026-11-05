"""Log every transition of one Batch cold cycle, every 5 s, until the job has
finished and no worker instance is left running.

    python3 batch_watch.py <jobId> <computeEnvironmentName>

Watches: the job's status, the compute environment's status reason and desired
vCPUs, every worker instance (type, AZ, state), and the scaling activities of
any Auto Scaling group whose name carries the CE's name (capacity errors land
there). Prints one timestamped line per change.
"""

import datetime as dt
import json
import subprocess
import sys
import time

JOB, CE = sys.argv[1], sys.argv[2]
seen: set[str] = set()


def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}


def say(key: str, line: str) -> None:
    if key in seen:
        return
    seen.add(key)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"{now} {line}", flush=True)


done_at = None
STARTED = dt.datetime.now(dt.timezone.utc)
saw_instance = False
while True:
    job = (aws("batch", "describe-jobs", "--jobs", JOB).get("jobs") or [{}])[0]
    status = job.get("status", "?")
    say(f"job/{status}/{job.get('statusReason')}", f"job {status} {job.get('statusReason') or ''}")
    ce = (aws("batch", "describe-compute-environments", "--compute-environments", CE).get("computeEnvironments") or [{}])[0]
    say(f"ce/{ce.get('status')}/{ce.get('statusReason')}/{ce.get('computeResources', {}).get('desiredvCpus')}",
        f"ce {ce.get('status')} desiredvCpus={ce.get('computeResources', {}).get('desiredvCpus')} {ce.get('statusReason') or ''}")
    res = aws("ec2", "describe-instances", "--filters", "Name=tag:Name,Values=spoken-audio-batch-worker")
    live = 0
    for r in res.get("Reservations", []):
        for i in r["Instances"]:
            saw_instance = True
            state = i["State"]["Name"]
            live += state not in ("terminated",)
            say(f"inst/{i['InstanceId']}/{state}",
                f"instance {i['InstanceId']} {i['InstanceType']} {i['Placement']['AvailabilityZone']} {state} "
                f"launch={i['LaunchTime']} {i.get('StateTransitionReason', '')}")
    for g in aws("autoscaling", "describe-auto-scaling-groups").get("AutoScalingGroups", []):
        if CE not in g["AutoScalingGroupName"]:
            continue
        for a in aws("autoscaling", "describe-scaling-activities", "--auto-scaling-group-name",
                     g["AutoScalingGroupName"], "--max-items", "10").get("Activities", []):
            if dt.datetime.fromisoformat(a["StartTime"]) < STARTED:
                continue
            say(f"asg/{a['ActivityId']}/{a['StatusCode']}",
                f"asg {a['StartTime']} {a['StatusCode']} {a.get('StatusMessage', '')[:300]} | {a.get('Description', '')[:120]}")
    if status in ("SUCCEEDED", "FAILED") and done_at is None:
        done_at = time.time()
    if done_at is not None:
        if saw_instance and live == 0:
            say("zero", "back_to_zero")
            break
        if time.time() - done_at > 2700:
            say("gaveup", "gave_up_waiting_for_scale_in")
            break
    time.sleep(5)
