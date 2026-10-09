# Bootstrap container image for the memory-maintenance worker Lambda.
#
# Same role as bootstrap-assets/scheduled-runs: the placeholder image
# PlatformStack ships on first deploy. The real image
# (backend/Dockerfile.memory-maintenance) is deployed out-of-band by the
# backend workflow via `aws lambda update-function-code --image-uri ...`.
# Keep these files byte-stable so CDK's asset digest never changes; a change
# here would revert the function to this stub on the next platform deploy.
#
# The stub module lives at the real handler's plain module path:
#   worker.lambda_handler
#
# DO NOT add functionality here.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

COPY worker.py ${LAMBDA_TASK_ROOT}/worker.py

CMD ["worker.lambda_handler"]
