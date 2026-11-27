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


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_FAILURE_SECRET = re.compile(
    r"""(?ix)
    (\bbearer\s+|(?<![\w-])["']?(?:[\w-]*(?:token|api[_-]?key|secret|password|authorization))
       ["']?\s*[:=]\s*(?:(?:bearer|basic)\s+)?)(?:"[^"]*"|'[^']*'|[^\s,;]+)
    |\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+
    |\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+
    |\b(?:https?|wss?)://[^\s<>]+
    |(?<![\w/])/(?:home|root|tmp|var|etc|run|opt|srv|usr)/[^\s,;)]+
    """
)
_FAILURE_DIAGNOSTIC = re.compile(
    r"(?i)^(?:[{\[]|traceback\b|stack trace\b|panic(?:ked)?\b|"
    r"(?:raw_)?(?:stdout|stderr)(?:_tail)?:|(?:exit_code|error_class)=|"
    r"\b(?:file\s+[\x22\x27]|at\s+/))"
)


def user_failure_message(value: object) -> str:
    """Return a bounded public summary from an explicit error field."""
    if not isinstance(value, str):
        return ""
    # Reject a truncated leading line so a partial credential cannot escape.
    sample = _ANSI_ESCAPE.sub("", value[:4097]).lstrip()
    lines = sample.splitlines(keepends=True)
    if len(value) > 4097 and len(lines) <= 1 and not sample.endswith(("\n", "\r")):
        return ""
    line = lines[0].strip() if lines else ""
    # Some providers append a serialized HTTP response to their summary.
    line = re.split(r"(?i)\s+(?:response )?body\s*[:=]|\s+[{\[]\s*\x22", line, maxsplit=1)[0].strip()
    if not line or len(line) > 1000 or _FAILURE_DIAGNOSTIC.match(line):
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in line):
        return ""
    return _FAILURE_SECRET.sub(lambda match: (match[1] or "") + "[redacted]", line)


def _is_error_event(obj: Any) -> bool:
    """True for a stream event of a RECOGNISED type that IS the failure, not
    the agent's narration: an ``is_error`` terminal result, a REJECTED
    rate_limit_event (an ``allowed``/``allowed_warning`` one is not a refusal),
    an ``error`` event, or an ``assistant`` message the CLI synthesised from an
    API error (top-level ``error``). A bare dict with an ``error`` key is not
    evidence — an agent can write one into its reply."""
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
    if kind == "assistant":
        return bool(obj.get("error"))
    return False


#: Attribute a gateway result builder sets when it filled ``raw_stdout`` with a
#: copy of ``output`` because the worker shipped no transcript (legacy worker,
#: managed-turn, reattach). Decided where the mirror is MADE: equality of the
#: two fields is not evidence — print_resume legitimately ships
#: ``output == raw_stdout`` when its stream holds no extractable text.
REPLY_MIRROR_ATTR = "raw_stdout_is_reply_mirror"


def mark_reply_mirror(result: Any, mirrored: bool = True) -> Any:
    """Record on ``result`` whether its ``raw_stdout`` is a copy of the reply."""
    setattr(result, REPLY_MIRROR_ATTR, bool(mirrored))
    return result


def stdout_is_reply_mirror(result: Any) -> bool:
    """True when the builder marked ``raw_stdout`` as only a copy of the
    agent's reply (``mark_reply_mirror``). Its lines are then the agent's
    words, never stream events, so no reader may take structure from them."""
    return bool(getattr(result, REPLY_MIRROR_ATTR, False))


def error_detail_without_stdout(detail: Any) -> str:
    """The worker's ``error_detail`` minus its ``stdout_tail:`` section.

    The worker builds the detail as ``exit_code=`` / ``error_class=`` /
    ``stderr_tail:`` / ``stdout_tail:`` blocks; the stdout tail of a
    print_resume turn is the CLI stream including the agent's own text. When
    the gateway falls back to the detail as ``raw_stderr`` (an error-bearing
    field) only the error parts may go there."""
    text = str(detail or "")
    if text.startswith("stdout_tail:"):
        return ""
    cut = text.find("\n\nstdout_tail:")
    return text if cut < 0 else text[:cut]


def error_bearing_stdout_lines(result: Any) -> List[str]:
    """The raw JSON lines of ``result.raw_stdout`` that carry the failure itself
    (see ``_is_error_event``). Returned verbatim so structural markers
    (``"error":"rate_limit"``, ``overageStatus``) still match. Non-JSON lines
    are not evidence, and nothing is read when ``raw_stdout`` mirrors the reply
    (``stdout_is_reply_mirror``)."""
    lines: List[str] = []
    if stdout_is_reply_mirror(result):
        return lines
    for line in str(getattr(result, "raw_stdout", "") or "").splitlines():
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
