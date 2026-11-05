#!/usr/bin/env bash
# Drives the PR-0 SageMaker async cold-start measurement from a laptop.
# Needs ACCT, BUCKET (state.env) and AWS_PROFILE/AWS_REGION.
#   sagemaker.sh create          model + endpoint config + endpoint (InitialInstanceCount 1, the API minimum)
#   sagemaker.sh wait            poll DescribeEndpoint every 10 s until it settles, logging transitions
#   sagemaker.sh invoke <name>   queue s3://$BUCKET/async/in/<name>.json, print the inference id
#   sagemaker.sh zero            drop the variant to 0 instances
#   sagemaker.sh autoscale       register 0..1 and the HasBacklogWithoutCapacity step policy + alarm
#                                (values verbatim from the SageMaker async autoscaling guide)
#   sagemaker.sh teardown        delete everything this script created
set -euo pipefail
EP=spoken-audio-spike
REPO="$ACCT.dkr.ecr.us-west-2.amazonaws.com/spoken-audio-spike"
ROLE="arn:aws:iam::$ACCT:role/spoken-audio-spike-sagemaker"
RID="endpoint/$EP/variant/AllTraffic"
ALARM="$EP-has-backlog-without-capacity"
now() { python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")'; }

case "${1:-}" in
  create)
    aws sagemaker create-model --model-name "$EP" --execution-role-arn "$ROLE" --primary-container "{
      \"Image\": \"$REPO:spike-v2\",
      \"ModelDataSource\": {\"S3DataSource\": {\"S3Uri\": \"s3://$BUCKET/model/\", \"S3DataType\": \"S3Prefix\", \"CompressionType\": \"None\"}},
      \"Environment\": {\"OUTPUT_BUCKET\": \"$BUCKET\"}}" --query ModelArn --output text
    aws sagemaker create-endpoint-config --endpoint-config-name "$EP" --production-variants "[{
      \"VariantName\": \"AllTraffic\", \"ModelName\": \"$EP\", \"InitialInstanceCount\": 1,
      \"InstanceType\": \"ml.g6e.xlarge\", \"InferenceAmiVersion\": \"al2023-ami-sagemaker-inference-gpu-4-1\",
      \"ModelDataDownloadTimeoutInSeconds\": 1800, \"ContainerStartupHealthCheckTimeoutInSeconds\": 1800}]" \
      --async-inference-config "{
      \"OutputConfig\": {\"S3OutputPath\": \"s3://$BUCKET/async/out/\", \"S3FailurePath\": \"s3://$BUCKET/async/fail/\"},
      \"ClientConfig\": {\"MaxConcurrentInvocationsPerInstance\": 1}}" --query EndpointConfigArn --output text
    echo "create_endpoint_called=$(now)"
    aws sagemaker create-endpoint --endpoint-name "$EP" --endpoint-config-name "$EP" --query EndpointArn --output text
    ;;
  wait)
    last=""
    while :; do
      s=$(aws sagemaker describe-endpoint --endpoint-name "$EP" \
        --query '[EndpointStatus, ProductionVariants[0].CurrentInstanceCount, ProductionVariants[0].DesiredInstanceCount, FailureReason]' --output text)
      if [ "$s" != "$last" ]; then echo "$(now) $s"; last="$s"; fi
      case "$s" in InService*|Failed*) [ "${2:-}" = "--forever" ] || break ;; esac
      sleep 10
    done
    ;;
  invoke)
    aws sagemaker-runtime invoke-endpoint-async --endpoint-name "$EP" \
      --input-location "s3://$BUCKET/async/in/$2.json" --content-type application/json \
      --invocation-timeout-seconds 3600 --query '[InferenceId, OutputLocation]' --output text
    echo "invoked_at=$(now)"
    ;;
  zero)
    echo "zero_called=$(now)"
    aws sagemaker update-endpoint-weights-and-capacities --endpoint-name "$EP" \
      --desired-weights-and-capacities VariantName=AllTraffic,DesiredInstanceCount=0 --query EndpointArn --output text
    ;;
  autoscale)
    aws application-autoscaling register-scalable-target --service-namespace sagemaker --resource-id "$RID" \
      --scalable-dimension sagemaker:variant:DesiredInstanceCount --min-capacity 0 --max-capacity 1
    ARN=$(aws application-autoscaling put-scaling-policy --policy-name HasBacklogWithoutCapacity-ScalingPolicy \
      --service-namespace sagemaker --resource-id "$RID" --scalable-dimension sagemaker:variant:DesiredInstanceCount \
      --policy-type StepScaling --step-scaling-policy-configuration '{
        "AdjustmentType": "ChangeInCapacity", "MetricAggregationType": "Average", "Cooldown": 300,
        "StepAdjustments": [{"MetricIntervalLowerBound": 0, "ScalingAdjustment": 1}]}' --query PolicyARN --output text)
    aws cloudwatch put-metric-alarm --alarm-name "$ALARM" --metric-name HasBacklogWithoutCapacity \
      --namespace AWS/SageMaker --statistic Average --evaluation-periods 2 --datapoints-to-alarm 2 \
      --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data missing \
      --dimensions Name=EndpointName,Value="$EP" --period 60 --alarm-actions "$ARN"
    echo "autoscale registered"
    ;;
  teardown)
    aws cloudwatch delete-alarms --alarm-names "$ALARM" || true
    aws application-autoscaling deregister-scalable-target --service-namespace sagemaker --resource-id "$RID" \
      --scalable-dimension sagemaker:variant:DesiredInstanceCount || true
    aws sagemaker delete-endpoint --endpoint-name "$EP" || true
    aws sagemaker delete-endpoint-config --endpoint-config-name "$EP" || true
    aws sagemaker delete-model --model-name "$EP" || true
    ;;
  *)
    echo "usage: sagemaker.sh create|wait|invoke|zero|autoscale|teardown" >&2
    exit 2
    ;;
esac
