# Bootstrap container image for the conversation-index consumer Lambda.
#
# Same role as bootstrap-assets/kb-migration and bootstrap-assets/kb-sync: the
# placeholder image PlatformStack ships on first deploy. The real image
# (backend/Dockerfile.conversation-index) is deployed out-of-band by the backend
# workflow via `aws lambda update-function-code --image-uri ...`.
#
# WHY THIS MUST STAY BYTE-STABLE
# CDK derives the image asset's digest from the bytes of this build context. As
# long as these files do not change, every `cdk deploy` computes the same digest,
# sees no change to the function's `Code`, and leaves the out-of-band-deployed
# real image alone. Anything that varies between synths reverts the function to
# this stub on the next platform deploy.
#
# The stub module lives at the real handler's dotted path:
#   apis.app_api.conversation_index.consumer.lambda_handler
#
# DO NOT add functionality here.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

COPY consumer.py ${LAMBDA_TASK_ROOT}/apis/app_api/conversation_index/consumer.py

CMD ["apis.app_api.conversation_index.consumer.lambda_handler"]
