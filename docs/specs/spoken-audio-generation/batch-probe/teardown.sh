#!/bin/bash
set -u
# Run with batch.env sourced (set -a; source batch.env; set +a), as written by setup_net_iam.sh.
Q="spoken-audio-batch-s3 spoken-audio-batch-ebs"
# Cancel anything still queued first: aws batch terminate-job --job-id <id> --reason teardown
for q in $Q; do aws batch update-job-queue --job-queue $q --state DISABLED >/dev/null && echo "disabled queue $q"; done
for i in $(seq 60); do s=$(aws batch describe-job-queues --job-queues $Q --query 'jobQueues[].status' --output text); [[ "$s" =~ UPDATING|CREATING ]] || break; sleep 5; done
for q in $Q; do aws batch delete-job-queue --job-queue $q && echo "deleting queue $q"; done
for i in $(seq 90); do n=$(aws batch describe-job-queues --job-queues $Q --query 'length(jobQueues)' --output text); [ "$n" = "0" ] && break; sleep 5; done; echo "queues left: $n"
for c in $Q; do aws batch update-compute-environment --compute-environment $c --state DISABLED >/dev/null && echo "disabled ce $c"; done
for i in $(seq 60); do s=$(aws batch describe-compute-environments --compute-environments $Q --query 'computeEnvironments[].status' --output text); [[ "$s" =~ UPDATING|CREATING ]] || break; sleep 5; done
for c in $Q; do aws batch delete-compute-environment --compute-environment $c && echo "deleting ce $c"; done
for i in $(seq 120); do n=$(aws batch describe-compute-environments --compute-environments $Q --query 'length(computeEnvironments)' --output text); [ "$n" = "0" ] && break; sleep 5; done; echo "ces left: $n"
for r in $(aws batch describe-job-definitions --job-definition-name spoken-audio-batch --status ACTIVE --query 'jobDefinitions[].jobDefinitionArn' --output text); do aws batch deregister-job-definition --job-definition $r && echo "deregistered $r"; done
aws ec2 delete-launch-template --launch-template-id $LT --query 'LaunchTemplate.LaunchTemplateId' --output text
aws ecr delete-repository --repository-name spoken-audio-batch --force --query 'repository.repositoryName' --output text
aws s3 rm s3://$BUCKET --recursive --quiet; aws s3api list-object-versions --bucket $BUCKET --query 'length(Versions||`[]`)' --output text; aws s3 rb s3://$BUCKET && echo "bucket gone"
aws ec2 delete-snapshot --snapshot-id $SNAP && echo "snapshot deleted"
for r in spoken-audio-batch-build spoken-audio-batch-instance; do
  aws iam remove-role-from-instance-profile --instance-profile-name $r --role-name $r
  aws iam delete-instance-profile --instance-profile-name $r
done
aws iam delete-role-policy --role-name spoken-audio-batch-build --policy-name spike
aws iam detach-role-policy --role-name spoken-audio-batch-instance --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role
aws iam delete-role-policy --role-name spoken-audio-batch-job --policy-name spike
for r in spoken-audio-batch-build spoken-audio-batch-instance spoken-audio-batch-job; do aws iam delete-role --role-name $r; done
aws ec2 delete-security-group --group-id $SG
for s in ${SUBNETS//,/ }; do aws ec2 delete-subnet --subnet-id $s; done
aws ec2 delete-route-table --route-table-id $RT
aws ec2 detach-internet-gateway --internet-gateway-id $IGW --vpc-id $VPC
aws ec2 delete-internet-gateway --internet-gateway-id $IGW
aws ec2 delete-vpc --vpc-id $VPC
# Only if this probe created them: the job log group and the Batch service-linked role.
aws logs delete-log-group --log-group-name /aws/batch/job
aws iam delete-service-linked-role --role-name AWSServiceRoleForBatch
