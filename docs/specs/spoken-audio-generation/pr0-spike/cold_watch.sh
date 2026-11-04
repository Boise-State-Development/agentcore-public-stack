#!/usr/bin/env bash
# Log endpoint transitions until the async output for $1 lands; prints its manifest.
set -euo pipefail
now() { python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")'; }
last=""
while ! aws s3 ls "s3://$BUCKET/async/audio/$1.mp3" >/dev/null 2>&1; do
  s=$(aws sagemaker describe-endpoint --endpoint-name spoken-audio-spike \
    --query '[EndpointStatus, ProductionVariants[0].CurrentInstanceCount, ProductionVariants[0].DesiredInstanceCount]' --output text)
  a=$(aws cloudwatch describe-alarms --alarm-names spoken-audio-spike-has-backlog-without-capacity --query 'MetricAlarms[0].StateValue' --output text)
  if [ "$s $a" != "$last" ]; then echo "$(now) $s alarm=$a"; last="$s $a"; fi
  if aws s3 ls "s3://$BUCKET/async/fail/" 2>/dev/null | grep -q .; then echo FAILED; exit 1; fi
  sleep 10
done
echo "audio_landed_seen=$(now)"
aws s3 ls "s3://$BUCKET/async/audio/$1.mp3"
