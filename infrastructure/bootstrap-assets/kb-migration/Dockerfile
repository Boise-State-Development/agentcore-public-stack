# Bootstrap container image for the Managed_KB migration Lambdas
# (dispatcher + worker + reconciler + ingestion consumer).
#
# Same role as bootstrap-assets/kb-sync and bootstrap-assets/rag-ingestion —
# the placeholder image PlatformStack ships on first deploy; the real
# image (backend/Dockerfile.kb-migration) is deployed out-of-band by the
# backend workflow via `aws lambda update-function-code --image-uri ...`
# once the handler modules land (spec task groups 9, 10 and 13).
#
# WHY THIS MUST STAY BYTE-STABLE
# CloudFormation tracks a Lambda's `Code` property from its own model,
# not by querying live AWS. CDK derives the image asset's digest from
# the bytes of this build context, so as long as these files do not
# change, every subsequent `cdk deploy` computes the SAME digest, sees
# no change to `Code`, and leaves the out-of-band-deployed REAL image
# alone. Introduce anything that varies between synths — a timestamp, a
# generated file, an unpinned base tag — and the next platform deploy
# silently reverts all four functions to this no-op stub.
#
# All FOUR functions use this ONE build context with different
# ImageConfig.Command overrides, so the stub modules must live at the
# SAME dotted paths as the real image's handlers:
#   apis.app_api.kb_migration.dispatcher.lambda_handler
#   apis.app_api.kb_migration.worker.lambda_handler
#   apis.app_api.kb_migration.reconciler.lambda_handler
#   apis.app_api.kb_migration.ingestion_consumer.lambda_handler
#
# DO NOT add functionality here.
#
# Pinning rationale: same digest as the other Lambda bootstrap images
# (kb-sync, scheduled-runs, rag-ingestion) so the supply-chain
# base-image pinning posture is identical across every image, and so
# the digest cannot drift when upstream retags `python:3.12`.

FROM public.ecr.aws/lambda/python:3.12@sha256:745b0eb8a9787e9c4bfd4fc4cae942399a2225831c96394ae70c0c2a7c7c6168

COPY dispatcher.py ${LAMBDA_TASK_ROOT}/apis/app_api/kb_migration/dispatcher.py
COPY worker.py ${LAMBDA_TASK_ROOT}/apis/app_api/kb_migration/worker.py
COPY reconciler.py ${LAMBDA_TASK_ROOT}/apis/app_api/kb_migration/reconciler.py
COPY ingestion_consumer.py ${LAMBDA_TASK_ROOT}/apis/app_api/kb_migration/ingestion_consumer.py

CMD ["apis.app_api.kb_migration.dispatcher.lambda_handler"]
