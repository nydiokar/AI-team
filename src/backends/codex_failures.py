"""User-facing reasons derived from the Codex app-server's error fields."""
import re

from src.core.failure_markers import RATE_LIMIT_MARKERS, USAGE_LIMIT_MARKERS, user_failure_message


MISSING_TURN_REASON = "Codex reported a failed turn without a provider reason."
LEGACY_TURN_FAILURE = "codex_turn_failed"

# CodexErrorInfo in the app-server v2 protocol. The object variants can carry
# an upstream status, which takes precedence over a transport symptom.
_NATIVE_ERRORS: dict[str, tuple[str, str]] = {
    "contextWindowExceeded": ("context_overflow", "The context window is full. Use /compact or start a new session."),
    "sessionBudgetExceeded": ("session_budget", "The session budget is exhausted."),
    "usageLimitExceeded": ("usage_limit", "The Codex usage limit was reached."),
    "rateLimitExceeded": ("rate_limit", "Codex requests are being rate limited."),
    "flexUnavailable": ("upstream_error", "The Flex service tier is unavailable."),
    "serverOverloaded": ("upstream_error", "The Codex provider is overloaded."),
    "modelNotFound": ("model_unavailable", "The selected model is unavailable."),
    "policyCapacityUnavailable": ("upstream_error", "The policy-required provider capacity is unavailable."),
    "cyberPolicy": ("permission_block", "The request was blocked by the provider's cyber policy."),
    "misalignmentPolicyViolation": ("permission_block", "The request was blocked by the provider's misalignment policy."),
    "tooManyDenials": ("permission_block", "The turn stopped after too many denied operations."),
    "inferenceHookDenied": ("permission_block", "The workspace inference hook denied the request."),
    "internalServerError": ("upstream_error", "The Codex provider encountered an internal error."),
    "unauthorized": ("auth", "Codex authentication failed. Sign in again on this session's node."),
    "badRequest": ("invalid_request", "The Codex provider rejected the request."),
    "threadRollbackFailed": ("session_error", "Codex could not roll back the session."),
    "sandboxError": ("sandbox_error", "The Codex sandbox failed."),
    "httpConnectionFailed": ("network", "Codex could not connect to the provider."),
    "responseStreamConnectionFailed": ("network", "Codex could not open the provider's response stream."),
    "responseStreamDisconnected": ("network", "The provider disconnected before completing the response."),
    "responseTooManyFailedAttempts": ("network", "The provider request still failed after Codex retried it."),
    "activeTurnNotSteerable": ("turn_conflict", "The active Codex turn cannot accept this request."),
}

_GENERIC_MESSAGES = {"", "failed", "error", "other", LEGACY_TURN_FAILURE}
_HTTP_STATUS = re.compile(r"\b(?:http(?: status)?|status(?: code)?)\s*[:=]?\s*([45]\d\d)\b", re.I)
_HTTP_REASONS = {
    400: ("invalid_request", "The Codex provider rejected the request."),
    401: _NATIVE_ERRORS["unauthorized"],
    403: ("permission_block", "The Codex provider denied the request."),
    429: _NATIVE_ERRORS["rateLimitExceeded"],
}


def codex_failure(error: object) -> tuple[str, str] | None:
    """Normalize attributed evidence, or let the caller use its next source."""
    if not isinstance(error, dict):
        return None
    info = error.get("codexErrorInfo")
    status: object = None
    if isinstance(info, dict) and len(info) == 1:
        info, details = next(iter(info.items()))
        if isinstance(details, dict):
            status = details.get("httpStatusCode")
    native = _NATIVE_ERRORS.get(info) if isinstance(info, str) else None
    message = user_failure_message(error.get("message"))
    words = message.casefold()
    if native and native[0] != "network":
        return native[0], message if words not in _GENERIC_MESSAGES else native[1]
    if type(status) is not int:
        match = _HTTP_STATUS.search(message)
        status = int(match[1]) if match else None
    if any(marker in words for marker in USAGE_LIMIT_MARKERS):
        category, default = _NATIVE_ERRORS["usageLimitExceeded"]
    elif type(status) is int and status in _HTTP_REASONS:
        category, default = _HTTP_REASONS[status]
    elif type(status) is int and 500 <= status < 600:
        category, default = _NATIVE_ERRORS["internalServerError"]
    elif any(marker in words for marker in RATE_LIMIT_MARKERS):
        category, default = _NATIVE_ERRORS["rateLimitExceeded"]
    else:
        category, default = native or ("backend_error", "")
    if words not in _GENERIC_MESSAGES:
        return category, message
    return (category, default) if default else None
