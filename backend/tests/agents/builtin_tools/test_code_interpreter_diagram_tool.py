"""Tests for the diagram-generation tool's policy gate.

These tests verify that the tool short-circuits with a generic error when
the supplied ``python_code`` violates the static policy, *without* invoking
the Bedrock Code Interpreter API.
"""

from __future__ import annotations

from unittest.mock import patch


from agents.builtin_tools.code_interpreter_diagram_tool import (
    generate_diagram_and_validate,
)


def _call(tool, **kwargs):
    """Strands @tool wraps the function in a tool spec object; reach the
    underlying callable so we can invoke it directly in unit tests."""
    inner = getattr(tool, "_tool_func", None) or getattr(tool, "func", None) or tool
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner(**kwargs)


def test_subprocess_call_rejected_without_invoking_sandbox() -> None:
    with patch("agents.builtin_tools.code_interpreter_diagram_tool._get_code_interpreter_id") as get_id:
        result = _call(
            generate_diagram_and_validate,
            python_code="import subprocess\nsubprocess.run(['id'])",
            diagram_filename="x.png",
        )
    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    # Sandbox lookup must not happen — rejection occurs before any AWS work.
    get_id.assert_not_called()


def test_dunder_escape_rejected_without_invoking_sandbox() -> None:
    src = "().__class__.__bases__[0].__subclasses__()"
    with patch("agents.builtin_tools.code_interpreter_diagram_tool._get_code_interpreter_id") as get_id:
        result = _call(
            generate_diagram_and_validate,
            python_code=src,
            diagram_filename="x.png",
        )
    assert result["status"] == "error"
    assert "policy" in result["content"][0]["text"].lower()
    get_id.assert_not_called()


def test_eval_call_rejected_without_invoking_sandbox() -> None:
    with patch("agents.builtin_tools.code_interpreter_diagram_tool._get_code_interpreter_id") as get_id:
        result = _call(
            generate_diagram_and_validate,
            python_code="eval('1+1')",
            diagram_filename="x.png",
        )
    assert result["status"] == "error"
    get_id.assert_not_called()


def test_invalid_filename_rejected_before_policy_check() -> None:
    """Filename validation runs first; that path's behavior is unchanged."""
    result = _call(
        generate_diagram_and_validate,
        python_code="import os\nos.system('id')",  # would also fail policy
        diagram_filename="not-a-png.txt",
    )
    assert result["status"] == "error"
    # The filename error message wins because that check runs first.
    assert ".png" in result["content"][0]["text"]


def test_policy_does_not_short_circuit_valid_chart_code() -> None:
    """Valid code passes policy and proceeds to sandbox lookup.

    We don't actually invoke the Code Interpreter (no credentials in the
    test env); we patch the lookup to return None so the tool falls
    through to its existing "no Code Interpreter configured" branch — the
    important assertion is that policy did *not* reject the input.
    """
    src = "import matplotlib.pyplot as plt\n" "import numpy as np\n" "x = np.linspace(0, 1, 10)\n" "plt.plot(x, x)\n" "plt.savefig('chart.png')\n"
    with patch(
        "agents.builtin_tools.code_interpreter_diagram_tool._get_code_interpreter_id",
        return_value=None,
    ):
        result = _call(
            generate_diagram_and_validate,
            python_code=src,
            diagram_filename="chart.png",
        )
    # Policy did not reject; we landed in the "no CI configured" branch.
    assert result["status"] == "error"
    text = result["content"][0]["text"].lower()
    assert "policy" not in text
    assert "code interpreter id not found" in text
