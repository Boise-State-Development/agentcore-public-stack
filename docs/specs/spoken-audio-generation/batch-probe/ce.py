import json, sys
name, lt, ver, subnets, sg, ip = sys.argv[1:7]
print(json.dumps({
  "computeEnvironmentName": name, "type": "MANAGED", "state": "ENABLED",
  "computeResources": {
    "type": "EC2", "allocationStrategy": "BEST_FIT_PROGRESSIVE",
    "minvCpus": 0, "maxvCpus": 8, "desiredvCpus": 0,
    "instanceTypes": ["g6e.xlarge", "g6e.2xlarge", "g7e.2xlarge"],
    "subnets": subnets.split(","), "securityGroupIds": [sg], "instanceRole": ip,
    "launchTemplate": {"launchTemplateId": lt, "version": ver},
    "ec2Configuration": [{"imageType": "ECS_AL2023_NVIDIA"}],
    "tags": {"Purpose": "spoken-audio-pr0-batch-probe"},
  },
}))
