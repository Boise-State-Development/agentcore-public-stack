"""Log-injection sanitization.

User-controlled values written into log records must have their line
terminators neutralized; otherwise an attacker can embed ``\\r``/``\\n`` in an
input (a filename, connector id, tool name, error message, …) and forge or
inject additional log lines, corrupting audit trails and log-based alerting.

This addresses CodeQL ``py/log-injection``. Wrap any user-influenced value
that flows into a ``logger.*`` call with :func:`scrub_log`.

    logger.warning("list_roots failed for connector %s: %s",
                   scrub_log(provider_id), scrub_log(err))
"""

from typing import Any

# C0/C1 control characters except ordinary tab (handled explicitly below).
_CONTROL_TRANSLATION = {
    i: None
    for i in range(0x20)
    if i not in (0x09,)  # keep \t out of the "delete" set; escaped below
}
_CONTROL_TRANSLATION.update({0x7F: None})


def scrub_log(value: Any) -> str:
    """Return ``str(value)`` with line breaks and control characters neutralized
    so it is safe to embed in a single log record.

    - ``\\r`` and ``\\n`` (and ``\\t``) are replaced with visible escapes so the
      original content is preserved for debugging but cannot start a new line.
    - Other C0/C1 control characters are stripped.
    """
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return text.translate(_CONTROL_TRANSLATION)
