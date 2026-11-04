#!/bin/bash
# Build box for the Batch probe: weights to S3, image to ECR, weights snapshot.
exec > >(tee /var/log/spike-build.log) 2>&1
set -euxo pipefail
shutdown -h +180
BUCKET=spoken-audio-batch-<account>-usw2
REPO=<account>.dkr.ecr.us-west-2.amazonaws.com/spoken-audio-batch
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2
fail() { echo FAILED > /tmp/status; aws s3 cp /var/log/spike-build.log s3://$BUCKET/build/build.log; aws s3 cp /tmp/status s3://$BUCKET/build/status; }
trap fail ERR
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
dnf install -y -q docker git xfsprogs
systemctl start docker
docker version --format '{{.Server.Version}}'; docker buildx version || echo "no buildx"
mkdir -p /work && cd /work
aws s3 cp s3://$BUCKET/src/ container/ --recursive --only-show-errors
curl -sSfL https://raw.githubusercontent.com/OpenMOSS/MOSS-TTSD/46973e425d/generation_utils.py -o container/generation_utils.py
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
s=$(date +%s); aws s3 sync s3://$BUCKET/model/ model/ --only-show-errors; echo "s3_download_s=$(( $(date +%s) - s ))"
du -sb model/*
s=$(date +%s); docker build --provenance=false -q -t $REPO:batch container; echo "docker_build_s=$(( $(date +%s) - s ))"
docker image inspect $REPO:batch --format '{{.Size}}'
aws ecr get-login-password | docker login -u AWS --password-stdin "${REPO%%/*}"
s=$(date +%s); docker push -q $REPO:batch; echo "docker_push_s=$(( $(date +%s) - s ))"
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone)
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
VOL=$(aws ec2 create-volume --availability-zone $AZ --size 30 --volume-type gp3 --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=spoken-audio-batch}]' --query VolumeId --output text)
aws ec2 wait volume-available --volume-ids $VOL
aws ec2 attach-volume --volume-id $VOL --instance-id $IID --device /dev/sdf
aws ec2 wait volume-in-use --volume-ids $VOL
sleep 10
DEV=$(lsblk -dpno NAME,SERIAL | awk -v s="${VOL/-/}" '$2==s{print $1}')
mkfs.xfs -q -L weights $DEV && mkdir -p /mnt/w && mount $DEV /mnt/w
cp -r model /mnt/w/model && sync && umount /mnt/w
aws ec2 detach-volume --volume-id $VOL
aws ec2 wait volume-available --volume-ids $VOL
echo "snapshot_start=$(ts)"
SNAP=$(aws ec2 create-snapshot --volume-id $VOL --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Name,Value=spoken-audio-batch}]' --query SnapshotId --output text)
echo "snapshot=$SNAP"
for i in $(seq 1 120); do
  st=$(aws ec2 describe-snapshots --snapshot-ids $SNAP --query 'Snapshots[0].State' --output text)
  [ "$st" = completed ] && break; sleep 15
done
echo "snapshot_done=$(ts) state=$st"
aws ec2 delete-volume --volume-id $VOL
echo OK > /tmp/status
aws s3 cp /var/log/spike-build.log s3://$BUCKET/build/build.log
aws s3 cp /tmp/status s3://$BUCKET/build/status
shutdown -h now
