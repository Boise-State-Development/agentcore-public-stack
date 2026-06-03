# Bootstrap handler for the artifact-render Lambda.
#
# This is the placeholder code that PlatformStack ships into the
# Lambda function on its very first deploy. PlatformStack owns the
# Lambda's *configuration* (runtime, arch, timeout, memory, env vars,
# IAM role, function URL, CloudFront distribution, Route53 alias);
# the *real* handler code is deployed independently by the backend
# workflow's `scripts/build/deploy-artifact-render-code.sh` step,
# which calls `aws lambda update-function-code` directly.
#
# The contents of this directory are fed to `lambda.Code.fromAsset()`
# in the CDK construct. CDK content-hashes the directory to produce a
# stable S3 asset key — by keeping this file byte-stable and never
# editing it, every Platform synth produces the same `Code.S3Key`,
# which CFN sees as no change to the `Code` property and therefore
# leaves the Lambda's actual code (the real handler, deployed
# out-of-band) untouched.
#
# This file is intentionally minimal: stdlib only, no business logic,
# returns an honest 503 so users hitting the artifact origin during
# the brief first-deploy window before the workflow runs see a
# graceful error rather than a hard failure or a leaked stack trace.
#
# DO NOT add functionality here. If you find yourself wanting to,
# put it in `backend/src/lambdas/artifact_render/handler.py` instead.

from __future__ import annotations

from typing import Any


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    body = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<title>Artifact service updating</title>'
        '<style>body{font:14px/1.5 system-ui,sans-serif;color:#333;'
        'max-width:480px;margin:80px auto;padding:24px;text-align:center}</style>'
        '</head><body>'
        '<h1 style="font-size:18px">Artifact service is updating</h1>'
        '<p>The artifact rendering service is being deployed. Please retry in a moment.</p>'
        '</body></html>'
    )
    return {
        'statusCode': 503,
        'headers': {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-store',
            'Retry-After': '30',
        },
        'body': body,
    }
