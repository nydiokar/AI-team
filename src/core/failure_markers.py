"""The ONE vocabulary for reading a failed turn's error text (A97).

Two consumers decide expensive things from it — the orchestrator's
``_classify_error`` (retries of paid turns, quota pauses of whole Cases) and the
user-facing failure label (``_short_failure_reason`` /
``result_text.short_failure_reason``) — plus the Claude driver's own
``classify_error_text``. They used to carry three drifted copies of the quota
markers; they now all import them from here. Pure: no I/O, no project imports.

It also owns WHICH text counts as evidence. A failed result carries the agent's
own reply (``output``, assistant events in ``raw_stdout``, ``assistant_text`` in
``parsed_output``) next to the error itself, and an agent that merely *wrote
about* "timeout", "503" or "usage limit" must never change the class. Only
error-bearing fields are read; the reply is a fallback for results that carry
their error nowhere else.
"""
import json
import re
from typing import Any, List

# Wording that means the ACCOUNT's subscription window is spent (reopens hours
# later): a quota PAUSE, never a quick retry. ``overagestatus`` is the key of a
# rate_limit_event's overage field, i.e. the provider's own refusal payload.
USAGE_LIMIT_MARKERS = (
    "usage limit",
    "session limit",
    "hit your limit",
    "hit your session limit",
    "you've hit your limit",
    "overagestatus",
)

# Generic burst/throttle wording. NOT evidence that the window is spent —
# "Rate limit exceeded. Please retry later." is the classic transient a couple
# of quick retries fix, so it keeps its own class.
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "\"error\":\"rate_limit\"",
)

# The failure-label set: either class above (plus the raw event type) is
# rendered as "Claude usage limit reached".
QUOTA_FAILURE_LABEL_MARKERS = ("rate_limit_event",) + USAGE_LIMIT_MARKERS + RATE_LIMIT_MARKERS

# A bare 503/504 is an HTTP status only when it stands alone — not inside an
# identifier, number or path ("abc5031", "src/503/", "v1.504").
UPSTREAM_STATUS_RE = re.compile(r"(?<![\w/.])50[34](?![\w/])")


def _is_error_event(obj: Any) -> bool:
    """True for a stream event that IS the failure, not the agent's narration:
    an ``is_error`` terminal result, a REJECTED rate_limit_event (an
    ``allowed``/``allowed_warning`` one is not a refusal), an ``error`` event,
    or a message the CLI synthesised from an API error (top-level ``error``)."""
    if not isinstance(obj, dict):
        return False
    kind = obj.get("type")
    if kind == "result":
        return obj.get("is_error") is True
    if kind == "rate_limit_event":
        info = obj.get("rate_limit_info")
        return isinstance(info, dict) and info.get("status") == "rejected"
    if kind == "error":
        return True
    return bool(obj.get("error"))


def error_bearing_stdout_lines(raw_stdout: Any) -> List[str]:
    """The raw JSON lines of ``raw_stdout`` that carry the failure itself (see
    ``_is_error_event``). Returned verbatim so structural markers
    (``"error":"rate_limit"``, ``overageStatus``) still match. Non-JSON lines
    are not evidence: on the mesh path ``raw_stdout`` mirrors the reply."""
    lines: List[str] = []
    for line in str(raw_stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if _is_error_event(obj):
            lines.append(line)
    return lines


def error_bearing_values(result: Any) -> List[Any]:
    """The values of a failed result that carry the error: ``errors``,
    ``raw_stderr``, and the error fields of a terminal error ``parsed_output``
    (never its ``assistant_text``). Callers render each with their own text
    extractor and add ``error_bearing_stdout_lines`` verbatim."""
    values: List[Any] = list(getattr(result, "errors", None) or [])
    values.append(getattr(result, "raw_stderr", "") or "")
    parsed = getattr(result, "parsed_output", None)
    if _is_error_event(parsed):
        for key in ("errors", "error", "result"):
            value = parsed.get(key)
            if isinstance(value, list):
                values.extend(value)
            elif value:
                values.append(value)
    return values
