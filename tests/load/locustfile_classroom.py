"""Classroom-burst entry point — the campus-realistic worst case.

    source tests/load/profiles/campus-representative.env
    locust -f locustfile_classroom.py --host https://boisestate.ai/api --headless

``--users`` and ``--spawn-rate`` are IGNORED: ClassroomBurstShape drives the
user count. ``--run-time`` is also unnecessary; the shape ends the run itself.

WARNING: this is the expensive one, and it is designed to be pointed at
production. Every turn is a live Bedrock invocation on the deployment's default
model. At production's measured ~26,700 quota tokens per turn, a 300-user burst
issuing one turn each inside a minute consumes ~8,000,000 tokens/minute against
an applied quota of 6,000,000 — so throttling is an *expected outcome* of the
default settings, not a bug. That is the finding the test exists to produce.
Run scripts/load-test/watch-tpm.sh alongside it.

Differences from the steady-state chat scenario, and why:

* ``wait_time`` is 1-3s rather than 5-15s. A class asked to do a thing does it
  immediately; think time is what a browsing population has.
* Fewer turns per conversation. A burst is a first question and maybe a
  follow-up, not a working session. That comes from config rather than this
  class, so set it explicitly after sourcing the profile:

      export AGENTCORE_LOAD_TURNS_PER_CONVERSATION=2
"""

from locust import between

from agentcore_load.scenarios.chat import ChatConversationUser
from shapes import ClassroomBurstShape


class ClassroomUser(ChatConversationUser):
    """A student following an instruction, not browsing."""

    wait_time = between(1, 3)


__all__ = ["ClassroomUser", "ClassroomBurstShape"]
