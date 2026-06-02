# Bootstrap handler for the App API ECS Fargate task.
#
# This is the placeholder code that PlatformStack ships into the
# App API task definition on its very first deploy. PlatformStack
# owns the *configuration* (task def CPU/memory, IAM, env vars,
# port mappings, target group registration, auto-scaling, log
# group); the *real* container image is deployed independently by
# the backend workflow's `scripts/build/deploy-ecs-service-one.sh`
# step, which calls `aws ecs register-task-definition` with the
# project's freshly-built ECR image URI and then
# `aws ecs update-service --task-definition family:N` to roll the
# service over.
#
# This bootstrap only needs to:
#   1. Listen on port 8000 (the App API standard port).
#   2. Respond to GET /health with 200 so the ALB target group
#      health check passes during initial provisioning.
#   3. Respond to anything else with a graceful 503 in case the
#      ALB routes a real request before the workflow ships the
#      real image.
#
# Pure stdlib (no FastAPI / uvicorn) so the container builds in
# milliseconds and has zero supply-chain surface beyond Python
# itself. DO NOT add functionality here — put it in
# backend/src/apis/app_api/main.py.

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8000'))

logging.basicConfig(
    level=logging.INFO,
    format='[bootstrap] %(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('app-api-bootstrap')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        log.info('%s - %s', self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == '/health':
            self._send_json(200, {'status': 'ok', 'mode': 'bootstrap'})
            return
        self._send_503_unavailable()

    def do_POST(self) -> None:  # noqa: N802
        self._send_503_unavailable()

    def do_PUT(self) -> None:  # noqa: N802
        self._send_503_unavailable()

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_503_unavailable()

    def do_PATCH(self) -> None:  # noqa: N802
        self._send_503_unavailable()

    def _send_503_unavailable(self) -> None:
        self._send_json(
            503,
            {
                'error': 'service_updating',
                'message': (
                    'App API is being deployed. The bootstrap container '
                    'is currently active; please retry in a moment after '
                    'the deployment completes.'
                ),
            },
        )

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    log.info('app-api bootstrap starting on 0.0.0.0:%d', PORT)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
