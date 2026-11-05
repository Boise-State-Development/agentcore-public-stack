#!/usr/bin/env bash
# Throwaway network, bucket, ECR repo and IAM for the Batch cold-start probe.
# Appends every id it creates to batch.env so teardown.sh can remove them.
set -euo pipefail
cd "$(dirname "$0")"
ACCT=$(aws sts get-caller-identity --query Account --output text)
TAG="ResourceType=%s,Tags=[{Key=Name,Value=spoken-audio-batch},{Key=Purpose,Value=spoken-audio-pr0-batch-probe}]"
st() { echo "$1=$2" >> batch.env; }
: > batch.env
st ACCT "$ACCT"
VPC=$(aws ec2 create-vpc --cidr-block 10.77.0.0/16 --tag-specifications "$(printf "$TAG" vpc)" --query Vpc.VpcId --output text); st VPC $VPC
aws ec2 modify-vpc-attribute --vpc-id $VPC --enable-dns-hostnames
IGW=$(aws ec2 create-internet-gateway --tag-specifications "$(printf "$TAG" internet-gateway)" --query InternetGateway.InternetGatewayId --output text); st IGW $IGW
aws ec2 attach-internet-gateway --internet-gateway-id $IGW --vpc-id $VPC
RT=$(aws ec2 create-route-table --vpc-id $VPC --tag-specifications "$(printf "$TAG" route-table)" --query RouteTable.RouteTableId --output text); st RT $RT
aws ec2 create-route --route-table-id $RT --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW >/dev/null
SUBNETS=""
i=0
for az in a b c d; do
  SN=$(aws ec2 create-subnet --vpc-id $VPC --availability-zone us-west-2$az --cidr-block 10.77.$i.0/24 \
    --tag-specifications "$(printf "$TAG" subnet)" --query Subnet.SubnetId --output text)
  aws ec2 modify-subnet-attribute --subnet-id $SN --map-public-ip-on-launch
  aws ec2 associate-route-table --route-table-id $RT --subnet-id $SN >/dev/null
  SUBNETS="$SUBNETS,$SN"; i=$((i+1))
done
st SUBNETS "${SUBNETS#,}"
SG=$(aws ec2 create-security-group --group-name spoken-audio-batch --description "Batch probe: egress only" --vpc-id $VPC \
  --tag-specifications "$(printf "$TAG" security-group)" --query GroupId --output text); st SG $SG

BUCKET=spoken-audio-batch-$ACCT-usw2; st BUCKET $BUCKET
aws s3api create-bucket --bucket $BUCKET --create-bucket-configuration LocationConstraint=us-west-2 >/dev/null
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
REPO=$(aws ecr create-repository --repository-name spoken-audio-batch --query repository.repositoryUri --output text); st REPO $REPO

ec2_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
task_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# Build box: weights to S3, image to ECR, and a weights volume it snapshots.
aws iam create-role --role-name spoken-audio-batch-build --assume-role-policy-document "$ec2_trust" >/dev/null
aws iam put-role-policy --role-name spoken-audio-batch-build --policy-name spike --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]},
 {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
 {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",\"ecr:CompleteLayerUpload\",\"ecr:PutImage\",\"ecr:BatchGetImage\"],\"Resource\":\"arn:aws:ecr:us-west-2:$ACCT:repository/spoken-audio-batch\"},
 {\"Effect\":\"Allow\",\"Action\":[\"ec2:CreateVolume\",\"ec2:AttachVolume\",\"ec2:DetachVolume\",\"ec2:DeleteVolume\",\"ec2:CreateSnapshot\",\"ec2:CreateTags\",\"ec2:DescribeVolumes\",\"ec2:DescribeSnapshots\"],\"Resource\":\"*\"}]}"
aws iam create-instance-profile --instance-profile-name spoken-audio-batch-build >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name spoken-audio-batch-build --role-name spoken-audio-batch-build

# Batch container instances: the standard ECS instance policy (ECR pull, logs).
aws iam create-role --role-name spoken-audio-batch-instance --assume-role-policy-document "$ec2_trust" >/dev/null
aws iam attach-role-policy --role-name spoken-audio-batch-instance --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role
aws iam create-instance-profile --instance-profile-name spoken-audio-batch-instance >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name spoken-audio-batch-instance --role-name spoken-audio-batch-instance

# The job itself: read weights and inputs, write the MP3.
aws iam create-role --role-name spoken-audio-batch-job --assume-role-policy-document "$task_trust" >/dev/null
aws iam put-role-policy --role-name spoken-audio-batch-job --policy-name spike --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]},
 {\"Effect\":\"Allow\",\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/async/*\"}]}"
echo setup done
