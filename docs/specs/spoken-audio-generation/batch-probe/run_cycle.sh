#!/usr/bin/env bash
# One cold cycle: submit, watch until the CE is back to zero, collect.
# Usage: run_cycle.sh <n> <queue/ce name> <s3|ebs>
set -euo pipefail
cd "$(dirname "$0")"
N=$1 CE=$2 W=$3
now() { python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")'; }
echo "submit_called=$(now)"
JOB=$(aws batch submit-job --job-name "cold$N" --job-queue "$CE" --job-definition spoken-audio-batch \
  --container-overrides "environment=[{name=WEIGHTS,value=$W},{name=JOB,value=batch$N}]" --query jobId --output text)
echo "job=$JOB"
python3 batch_watch.py "$JOB" "$CE"
python3 collect.py "$JOB" "$CE" > "logs/cycle$N.json"
echo "collected logs/cycle$N.json"
