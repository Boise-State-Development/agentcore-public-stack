# Bootstrap handler for the AgentCore Runtime (inference-api).
#
# This is the placeholder code that PlatformStack ships into the
# AgentCore Runtime on its very first deploy. PlatformStack owns
# the Runtime's *configuration* (IAM, env vars, network mode, JWT
# authorizer, observability); the *real* container image is
# deployed independently by the backend workflow's
# `scripts/build/deploy-runtime-image-one.sh` step, which calls
# `aws bedrock-agentcore-control update-agent-runtime` with the
# project's freshly-built ECR image URI.
#
# This bootstrap only needs to:
#   1. Listen on port 8080 (the AgentCore Runtime standard port).
#   2. Respond to GET /ping with 200 so AgentCore's health check
#      passes during initial provisioning.
#   3. Respond to POST /invocations and GET /ws with a graceful
#      503 in case anyone hits the Runtime in the brief window
#      before the workflow ships the real image.
#
# Pure stdlib (no FastAPI / uvicorn) so the container builds in
# milliseconds and has zero supply-chain surface beyond Python
# itself. DO NOT add functionality here — put it in
# backend/src/apis/inference_api/main.py.

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8080'))

logging.basicConfig(
    level=logging.INFO,
    format='[bootstrap] %(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('inference-bootstrap')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        # Route BaseHTTPRequestHandler's per-request log line
        # through the same logger as our explicit logs.
        log.info('%s - %s', self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == '/ping':
            self._send_json(200, {'status': 'ok', 'mode': 'bootstrap'})
            return
        self._send_json(404, {'error': 'not found'})

    def do_POST(self) -> None:  # noqa: N802
        # /invocations is the AgentCore Runtime entrypoint. Return
        # a 503 with a plain JSON body — the agent client will
        # surface the message to the user.
        if self.path == '/invocations':
            self._send_json(
                503,
                {
                    'error': 'service_updating',
                    'message': (
                        'Inference API is being deployed. The bootstrap '
                        'container is currently active; please retry in '
                        'a moment after the deployment completes.'
                    ),
                },
            )
            return
        self._send_json(404, {'error': 'not found'})

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    log.info('inference-api bootstrap starting on 0.0.0.0:%d', PORT)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
