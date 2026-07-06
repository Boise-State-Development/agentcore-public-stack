# Bootstrap container image for the scheduled-runs Lambdas (dispatcher +
# worker).
#
# Same role as bootstrap-assets/kb-sync — the placeholder image
# PlatformStack ships on first deploy; the real image
# (backend/Dockerfile.scheduled-runs) is deployed out-of-band by the
# backend workflow via `aws lambda update-function-code --image-uri ...`.
# Keep these files byte-stable so CDK's asset digest never changes.
#
# Both scheduled-runs functions use this one build context with different
# ImageConfig.Command overrides, so the stub modules must live at the
# SAME plain module paths as the real image's handlers:
#   dispatcher.lambda_handler
#   worker.lambda_handler
#
# DO NOT add functionality here.
#
# Pinning rationale: same digest as the real scheduled-runs image (and
# the other Lambda images) so the supply-chain dockerfile-pinning test
# passes.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

COPY dispatcher.py ${LAMBDA_TASK_ROOT}/dispatcher.py
COPY worker.py ${LAMBDA_TASK_ROOT}/worker.py

CMD ["dispatcher.lambda_handler"]
