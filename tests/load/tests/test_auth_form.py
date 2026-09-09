"""Tests for the Cognito Hosted UI form parser.

This is the most fragile part of the suite — it depends on Cognito's markup —
so it gets the most coverage. The failure mode that matters is a *silent* one:
picking the wrong form or dropping a hidden field would produce a login that
fails for reasons that look like the backend's fault.
"""

from __future__ import annotations

import pytest

from agentcore_load.auth import LoginError, _find_login_form, _pick_username_field

# Shape of the classic Hosted UI page: a search/nav form first, then the real
# sign-in form. Picking the first form on the page would pick the wrong one.
HOSTED_UI_HTML = """
<html><body>
  <form name="unrelated" action="/noop" method="get">
    <input type="text" name="q" value="">
  </form>
  <form name="cognitoSignInForm" action="/login?client_id=abc&amp;redirect_uri=https%3A%2F%2Fx"
        method="post">
    <input type="hidden" name="_csrf" value="csrf-abc123">
    <input type="text" name="username" placeholder="Username">
    <input type="password" name="password" placeholder="Password">
    <input type="submit" name="signInSubmitButton" value="Sign in">
  </form>
</body></html>
"""


def test_picks_the_form_containing_a_password_field() -> None:
    form = _find_login_form(HOSTED_UI_HTML)
    assert form.method == "post"
    assert form.action is not None and form.action.startswith("/login?client_id=abc")
    assert form.password_field == "password"


def test_hidden_csrf_field_is_carried_forward() -> None:
    # Cognito rejects the POST without its own _csrf value.
    form = _find_login_form(HOSTED_UI_HTML)
    assert form.fields["_csrf"] == "csrf-abc123"


def test_html_entities_in_action_are_decoded() -> None:
    # The action contains &amp; — if that is not decoded the query string is
    # malformed and Cognito answers 400.
    form = _find_login_form(HOSTED_UI_HTML)
    assert "&amp;" not in (form.action or "")
    assert "&redirect_uri=" in (form.action or "")


def test_password_value_is_never_captured() -> None:
    html = """
    <form action="/login" method="post">
      <input type="password" name="password" value="should-not-be-read">
    </form>
    """
    assert _find_login_form(html).fields["password"] == ""


def test_missing_password_form_raises_with_diagnostics() -> None:
    html = '<form action="/x" method="post"><input type="text" name="q"></form>'
    with pytest.raises(LoginError) as excinfo:
        _find_login_form(html)
    # The message has to name what it saw, or a markup change is unfixable
    # from a log line alone.
    assert "managed login" in str(excinfo.value)
    assert "Parsed 1 form" in str(excinfo.value)


def test_username_field_resolved_by_name() -> None:
    assert _pick_username_field(_find_login_form(HOSTED_UI_HTML)) == "username"


def test_username_field_falls_back_to_sole_text_input() -> None:
    html = """
    <form action="/login" method="post">
      <input type="hidden" name="_csrf" value="x">
      <input type="text" name="weirdlyNamedField">
      <input type="password" name="password">
    </form>
    """
    assert _pick_username_field(_find_login_form(html)) == "weirdlyNamedField"


def test_ambiguous_username_field_raises() -> None:
    html = """
    <form action="/login" method="post">
      <input type="text" name="first">
      <input type="text" name="second">
      <input type="password" name="password">
    </form>
    """
    with pytest.raises(LoginError, match="username field"):
        _pick_username_field(_find_login_form(html))
