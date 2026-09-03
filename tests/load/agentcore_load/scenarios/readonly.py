"""Cheap scenario: authenticated reads, no inference.

Value of running this separately: it loads the same ALB, the same Fargate
service, the same session-refresh middleware and the same DynamoDB tables as
the chat path, but spends nothing on Bedrock. If latency degrades here too,
the problem is the BFF layer; if it degrades only on ``/chat/stream``, the
problem is downstream. It is also the right scenario for finding out how many
concurrent signed-in users app-api can hold before anything is asked of a
model.

Every endpoint below is a plain GET on a non-admin router. Paths are the ones
FastAPI actually registers, which is not always the prefix: a router declaring
``@router.get("/")`` serves ``/prefix/``, and a prefix with no root route (as
``/costs`` has none) 404s. Check the router, not the prefix, before adding a
task here.
"""

from __future__ import annotations

from locust import between, task

from ..users import AuthenticatedUser


class BrowsingUser(AuthenticatedUser):
    """Polls the endpoints the SPA reads while a user clicks around."""

    wait_time = between(1, 5)

    @task(5)
    def list_sessions(self) -> None:
        self._get("/sessions")

    @task(3)
    def list_models(self) -> None:
        self._get("/models")

    @task(2)
    def list_tools(self) -> None:
        # Declared as @router.get("/") on a prefix="/tools" router, so the real
        # path is /tools/ and FastAPI 307s /tools -> /tools/. Requesting the
        # canonical path avoids charging this task two round trips per
        # iteration, which would show up as inflated request counts rather
        # than as an error.
        self._get("/tools/")

    @task(2)
    def read_settings(self) -> None:
        self._get("/users/me/settings")

    @task(1)
    def read_session(self) -> None:
        # The SPA's bootstrap call. Also the cheapest way to confirm the
        # session is still alive under load.
        self._get("/auth/session")

    @task(1)
    def read_costs(self) -> None:
        # /costs has no root route — the costs router defines only /summary and
        # /detailed-report. This is the fast path the SPA reads.
        self._get("/costs/summary")

    def _get(self, path: str) -> None:
        with self.client.get(path, catch_response=True, name=f"GET {path}") as response:
            if response.status_code == 200:
                response.success()
                return
            if response.status_code == 403:
                # RBAC varies by deployment and by the role the load-test users
                # were given. Naming it keeps a permissions gap from being read
                # as a performance problem.
                response.failure(
                    f"403 — the load-test user lacks the scope for {path}; "
                    "grant it or drop this task"
                )
                return
            if response.status_code == 404:
                # A wrong path fails every single iteration at near-zero cost,
                # so it contributes no load while inflating the aggregate error
                # rate — which is one of the signals this scenario exists to
                # measure. Say so plainly instead of reporting it as a status.
                response.failure(
                    f"404 — {path} does not exist on this deployment; "
                    "this is a bug in the scenario, not a backend failure"
                )
                return
            response.failure(f"unexpected status {response.status_code}")
