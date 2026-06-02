# Bootstrap container image for the RAG ingestion Lambda.
#
# This is the placeholder image that PlatformStack ships into the
# Lambda function on its very first deploy. PlatformStack owns the
# Lambda's *configuration* (runtime, IAM, env vars, timeout, memory,
# event source mapping); the *real* container image is deployed
# independently by the backend workflow's
# `scripts/build/deploy-image-lambda-one.sh` step, which calls
# `aws lambda update-function-code --image-uri ...` directly.
#
# The contents of this directory are fed to
# `lambda.DockerImageCode.fromImageAsset()` in the CDK construct.
# CDK content-hashes the build context to produce a stable asset
# digest — by keeping these files byte-stable and never editing
# them, every Platform synth produces the same digest, which CFN
# sees as no change to the `ImageUri` and therefore leaves the
# Lambda's actual image (the real container, deployed out-of-band)
# untouched.
#
# DO NOT add functionality here. If you find yourself wanting to,
# put it in backend/Dockerfile.rag-ingestion + the related source
# trees instead. This image just needs to exist and start
# successfully — Lambda invokes it during the brief first-deploy
# window before the workflow runs.
#
# Pinning rationale:
#   public.ecr.aws/lambda/python:3.12 (same minor as the real
#   ingestion image — see backend/Dockerfile.rag-ingestion).
#   Digest-pinned, identical to the real image's pin, so the
#   supply-chain dockerfile-pinning test passes.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

# Lambda runtime expects the handler at LAMBDA_TASK_ROOT.
COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
