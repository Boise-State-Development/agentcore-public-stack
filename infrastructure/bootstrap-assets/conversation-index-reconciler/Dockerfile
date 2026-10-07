# Bootstrap container image for the conversation-index daily reconciler Lambda.
#
# A separate build context from bootstrap-assets/conversation-index on purpose:
# CDK derives each image asset's digest from its context's bytes, so adding this
# stub beside the consumer's would change the consumer's digest and revert the
# live consumer to its stub on the next platform deploy. The real image
# (backend/Dockerfile.conversation-index, shared with the consumer) is deployed
# out-of-band by the backend workflow via `aws lambda update-function-code`.
#
# Keep this byte-stable for the same reason. The stub module lives at the real
# handler's dotted path:
#   apis.app_api.conversation_index.reconciler.lambda_handler
#
# DO NOT add functionality here.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

COPY reconciler.py ${LAMBDA_TASK_ROOT}/apis/app_api/conversation_index/reconciler.py

CMD ["apis.app_api.conversation_index.reconciler.lambda_handler"]
