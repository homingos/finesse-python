"""Errors raised by the SDK.

Every error derives from :class:`FinesseError`. HTTP errors derive from
:class:`APIStatusError` and are split by status code, so callers can catch
exactly what they handle::

    try:
        client.speech.create(voice_id=vid, text="Hi")
    except flam_finesse.PaymentRequiredError as e:
        print("Out of credits, remaining:", e.billing_remaining)
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

__all__ = [
    "FinesseError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PaymentRequiredError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "PayloadTooLargeError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "StreamError",
    "CloneFailedError",
    "CloneTimeoutError",
]


class FinesseError(Exception):
    """Base class for every error the SDK raises."""


class APIConnectionError(FinesseError):
    """The request never got a response: DNS, TLS, connection reset, and so on."""

    def __init__(
        self, message: str = "Connection error.", *, request: Optional[httpx.Request] = None
    ):
        super().__init__(message)
        self.request = request


class APITimeoutError(APIConnectionError):
    """The request timed out."""

    def __init__(self, *, request: Optional[httpx.Request] = None):
        super().__init__("Request timed out.", request=request)


class APIStatusError(FinesseError):
    """The API answered with a 4xx or 5xx status.

    The API returns three error body shapes; all of them are normalised here:

    - ``{"status": 404, "error": true, "message": "Voice not found", "code": "not_found"}``
    - ``{"error": "insufficient_credits", "unit": "character", "limit": 100, "used": 100}``
    - ``{"error": "plan limit reached", "feature": "max_voices", "limit": 3}``

    Attributes:
        status_code: HTTP status.
        message: Human-readable reason.
        code: Machine-readable code. Branch on this, never on ``message``. Envelope codes:
            ``invalid_request``, ``unauthorized``, ``key_expired``, ``token_invalid``,
            ``insufficient_scope``, ``voice_not_allowed``, ``not_found``,
            ``payload_too_large``, ``invalid_input``, ``rate_limited``,
            ``concurrency_limited``, ``upstream_error``, ``upstream_busy``, ...; plan
            denials: ``insufficient_credits``, ``quota_exhausted``, ``feature_disabled``,
            ``plan_limit_reached``; realtime: ``too_many_sessions``. ``None`` when absent.
        body: The decoded JSON body, or the raw text when it is not JSON.
        billing_remaining: ``X-Billing-Remaining`` header as an int, when sent.
        request_id: The service's ``X-Request-Id``, when sent.
    """

    def __init__(self, message: str, *, response: httpx.Response, body: Any, code: Optional[str]):
        super().__init__(message)
        self.message = message
        self.response = response
        self.status_code = response.status_code
        self.body = body
        self.code = code
        self.billing_remaining = _int_header(response, "X-Billing-Remaining")
        rid = response.headers.get("X-Request-Id")
        if rid is None and isinstance(body, dict) and isinstance(body.get("request_id"), str):
            rid = body["request_id"]
        self.request_id: Optional[str] = rid
        """The service's ``X-Request-Id`` for the failed request; quote it to support."""

    @property
    def retry_after(self) -> Optional[float]:
        """Seconds the server asked you to wait (``Retry-After``), when it sent one."""
        value = self.response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @property
    def limit(self) -> Optional[int]:
        """Plan limit from an entitlement or plan-limit denial, when present."""
        return _int_field(self.body, "limit")

    @property
    def used(self) -> Optional[int]:
        """Amount already used, from an entitlement denial, when present."""
        return _int_field(self.body, "used")

    def __str__(self) -> str:
        code = f" ({self.code})" if self.code and self.code != self.message else ""
        return f"{self.status_code}: {self.message}{code}"


class BadRequestError(APIStatusError):
    """400: the request was invalid (missing field, text too long, voice not enrolled, ...)."""


class AuthenticationError(APIStatusError):
    """401: the API key is missing, unknown or revoked; ``key_expired``; or a short-lived
    token is invalid, expired or already used (``token_invalid``)."""


class PaymentRequiredError(APIStatusError):
    """402: out of credits (``insufficient_credits``) or quota (``quota_exhausted``)."""


class PermissionDeniedError(APIStatusError):
    """403: the key lacks the route's scope (``insufficient_scope``), a token is limited to
    other voices (``voice_not_allowed``), the plan lacks the feature (``feature_disabled``), or
    a cap is reached (``plan_limit_reached``, see ``feature``)."""

    @property
    def feature(self) -> Optional[str]:
        if isinstance(self.body, dict):
            value = self.body.get("feature")
            return value if isinstance(value, str) else None
        return None


class NotFoundError(APIStatusError):
    """404: no such resource, or it is not visible to this workspace."""


class ConflictError(APIStatusError):
    """409: e.g. a request with the same ``Idempotency-Key`` is still running."""


class PayloadTooLargeError(APIStatusError):
    """413: the request body is over 21 MiB (``payload_too_large``)."""


class UnprocessableEntityError(APIStatusError):
    """422: the synthesis service could not use the input (``invalid_input``), or an
    ``Idempotency-Key`` was reused for a different request (``idempotency_key_reused``)."""


class RateLimitError(APIStatusError):
    """429: over the plan's requests per second (``rate_limited``) or concurrent synthesis
    (``concurrency_limited``), or (realtime) ``too_many_sessions``. Wait ``retry_after``."""


class InternalServerError(APIStatusError):
    """5xx: a server error, the synthesis service failing (``upstream_error``, 502) or at
    capacity (``upstream_busy``, 503, with ``retry_after``). Usually safe to retry."""


class StreamError(FinesseError):
    """A streaming response failed after it started (an SSE ``error`` event)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{message} ({code})" if message else code)
        self.code = code
        self.message = message


class CloneFailedError(FinesseError):
    """A clone job ended without producing a ready voice."""

    def __init__(self, job: Any):
        err = getattr(job, "error", None) or {}
        reason = err.get("message") if isinstance(err, dict) else None
        super().__init__(f"Clone job {job.id} {job.status}" + (f": {reason}" if reason else ""))
        self.job = job


class CloneTimeoutError(FinesseError):
    """Waiting for a clone job took longer than the timeout. The job may still finish."""

    def __init__(self, job_id: str, timeout: float):
        super().__init__(f"Clone job {job_id} did not finish within {timeout:g}s")
        self.job_id = job_id
        self.timeout = timeout


_MAX_MESSAGE = 300

_STATUS_ERRORS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: PaymentRequiredError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def make_status_error(response: httpx.Response) -> APIStatusError:
    """Build the right :class:`APIStatusError` subclass for an error response."""
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

    message: Optional[str] = None
    code: Optional[str] = None
    if isinstance(body, dict):
        msg = body.get("message")
        err = body.get("error")
        if isinstance(msg, str) and msg:
            message = msg
            # The credit check inside synthesis sends the code as the message.
            if msg.replace("_", "").isalpha() and "_" in msg and msg.islower():
                code = msg
        if isinstance(body.get("code"), str) and body["code"]:
            code = body["code"]
        elif isinstance(err, str) and err:
            code = "plan_limit_reached" if err == "plan limit reached" else err
            message = message or err.replace("_", " ")
        elif isinstance(err, dict):  # OpenAI-style {"error": {...}}
            if isinstance(err.get("message"), str):
                message = err["message"] or message
            if isinstance(err.get("code"), str):
                code = err["code"] or code
    elif isinstance(body, str) and body.strip():
        # A proxy's HTML error page can be long; keep the message readable.
        text = " ".join(body.split())
        message = text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 3] + "..."

    if not message:
        message = response.reason_phrase or f"HTTP {response.status_code}"

    status = response.status_code
    cls = _STATUS_ERRORS.get(status)
    if cls is None:
        cls = InternalServerError if status >= 500 else APIStatusError
    return cls(message, response=response, body=body, code=code)


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _int_field(body: Any, key: str) -> Optional[int]:
    if isinstance(body, dict):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None
