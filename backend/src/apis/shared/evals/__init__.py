"""Evaluation harness — the quality half of the release pass.

Spec: ``docs/specs/eval-harness.md``. A golden set of synthetic cases
(``backend/evals/cases/``) is run against a deployed agent through
``apis.shared.harness.run_agent_headless``, every sample is scored by a pure
function of the case's expectations and the turn's ``RunResult``, and each
family is compared, paired, with a committed per-model baseline.

Nothing here runs on the turn path. The modules live in ``apis.shared`` only
because the later phases (an admin "run now", per-agent test sets) call the
same scoring and mapping from app-api and a worker, and neither may import a
script.

* ``cases``          load and validate case files
* ``score``          deterministic per-sample scoring and paired statistics
* ``execute``        run one sample of one case and produce its result row
* ``report``         baselines, per-family comparison, the gate
* ``session_mapper`` a turn's ``RunResult`` dicts as a strands-evals ``Session``
* ``scrub``          strip what would unblind a judge before it reads a session
* ``judges``         the judge seam (the strands-evals adapter is phase 1 PR-2)
* ``fixtures``       register a synthetic file as a READY upload for a turn
* ``calls``          a sample's model calls from its session's ``C#`` rows, for ``score --explain``
"""
