"""Regression tests for the sandbox in the vendored ``strands_tools.calculator``.

``calculator`` is registered in ``create_default_registry()`` and seeded with
``enabledByDefault=True``, so it is reachable by every user on every agent and
it evaluates model-supplied expressions. Its AST allowlist is therefore a real
security boundary, not an input-validation nicety.

The boundary permits a string literal only as a positional argument to a small
set of constructors that parse it as a plain name or numeric literal
(``Symbol``, ``symbols``, ``Rational``, ``Integer``, ``Float``); everywhere else
a string is rejected, which blocks the ``sympify``-backed re-parse escape.

Up to ``strands-agents-tools`` 0.8.6 that check ignored the call's keyword
arguments, so ``symbols('...', cls=N)`` rerouted ``symbols`` to apply ``N`` —
and therefore ``sympify`` — to the string, re-parsing it outside the restricted
namespace. 0.8.8 trusts a string literal only when every keyword on the call is
a boolean assumption flag, and treats ``**kwargs`` unpacking as untrusted
because it can smuggle in ``cls``.

These tests pin that behaviour to the installed wheel, so a downgrade or a
resolver drift back below 0.8.8 fails the suite instead of silently reopening
the escape.
"""

from __future__ import annotations

import pytest

from strands_tools.calculator import _validate_expression_ast, parse_expression

# ---------------------------------------------------------------------------
# The escape: a keyword that reroutes how the string argument is parsed.
# ---------------------------------------------------------------------------

REROUTED_STRING_ARGS = [
    # The disclosed form: cls=N makes symbols apply N (sympify) to the string.
    "symbols('x', cls=N)",
    # The same reroute carrying a payload that must never reach sympify.
    "symbols('__import__(\"os\").system(\"id\")', cls=N)",
    # cls on the other string constructors is rejected for the same reason.
    "Symbol('1+1', cls=N)",
    # **kwargs unpacking can smuggle in cls, so it is untrusted too.
    "symbols('x', **kw)",
    # A non-boolean keyword is not an assumption flag.
    "symbols('x', cls=Float)",
]


@pytest.mark.parametrize("expression", REROUTED_STRING_ARGS)
def test_string_arg_with_rerouting_keyword_rejected(expression: str) -> None:
    """A string literal is not trusted when the call carries a rerouting keyword."""
    with pytest.raises(ValueError, match="string literals are not supported"):
        parse_expression(expression)


def test_string_arg_outside_safe_constructors_rejected() -> None:
    """The pre-existing half of the boundary: sympify-backed constructors stay closed."""
    with pytest.raises(ValueError, match="string literals are not supported"):
        parse_expression("N('1+1')")


# ---------------------------------------------------------------------------
# The fix must not narrow legitimate use — calculator is on for every user.
# ---------------------------------------------------------------------------

LEGITIMATE_EXPRESSIONS = [
    "2 + 2 * 10",
    "x**2 + 2*x + 1",
    "sin(pi/2) + log(E)",
    "Symbol('x')",
    "symbols('x y')",
    "Rational('1/3')",
    "Integer('42')",
    "Float('3.14')",
]


@pytest.mark.parametrize("expression", LEGITIMATE_EXPRESSIONS)
def test_ordinary_expressions_still_parse(expression: str) -> None:
    """Ordinary arithmetic and symbolic input are unaffected by the tightened check."""
    assert parse_expression(expression) is not None


ASSUMPTION_KEYWORD_CALLS = [
    "Symbol('x', positive=True)",
    "Symbol('x', real=True, positive=True)",
    "symbols('x y', positive=True)",
]


@pytest.mark.parametrize("expression", ASSUMPTION_KEYWORD_CALLS)
def test_assumption_keywords_remain_trusted(expression: str) -> None:
    """Boolean assumption flags do not reroute parsing, so the string stays trusted.

    Asserted against the validator rather than ``parse_expression`` because these
    calls fail further downstream for an unrelated, pre-existing reason: sympy's
    ``implicit_multiplication_application`` transform rewrites ``positive=True``
    into a multiplication before ``parse_expr`` sees it. That behaviour is
    identical on 0.8.6 and 0.8.8 — the security boundary is the layer under test.
    """
    _validate_expression_ast(expression)
