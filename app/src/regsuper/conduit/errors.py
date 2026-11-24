"""Conduit error mapping between HTTP statuses (REST) and MCP error codes.

Both channels read one table, so they stay in step:

* 401: unauthenticated or bad token (audience is not this resource, or the `org_id` claim is
  missing). The response carries `WWW-Authenticate: Bearer resource_metadata=<URL>` pointing at
  the protected-resource metadata.
* 402: payment or upgrade required (feature not in tier, e.g. an org without API access, or
  `answer.generate` on a lower plan). Never used for quota.
* 403: not entitled (wrong org, or the token lacks the tool's scope).
* 429: quota or rate limit exhausted (`quota.exceeded`), raised before any engine call, with
  `Retry-After` where the reset time is known. This is the only quota-exhaustion code.
* 503: kill switch engaged or compliance guard unavailable; every tool returns 503.

The 402, 403 and 429 constants are re-exported from `regsuper.entitlements`, so each denial
code is defined exactly once across the app and Conduit.
"""

from __future__ import annotations

from enum import StrEnum

# Re-exported from the entitlements package (entitlements/guard.py defines them), so each
# denial code has a single definition.
from regsuper.entitlements import (
    HTTP_FORBIDDEN,  # 403: not entitled (wrong org or missing scope)
    HTTP_PAYMENT_REQUIRED,  # 402: payment or upgrade required (feature not in tier)
    HTTP_QUOTA_EXCEEDED,  # 429: quota or rate limit exhausted ('quota.exceeded')
)

_WP = "WP-062 (Stage-2 Conduit build)"

# Conduit-local HTTP statuses not owned by the entitlements package.
HTTP_UNAUTHORIZED = 401  # bad or missing token; sent with WWW-Authenticate
HTTP_SERVICE_UNAVAILABLE = 503  # kill switch engaged or guard unavailable


class ConduitErrorCode(StrEnum):
    """MCP error codes; the REST channel returns the paired HTTP status instead.

    The string values are stable codes that host models and REST clients branch on. Each pairs
    one-to-one with an HTTP status in `HTTP_STATUS_FOR`.
    """

    UNAUTHORIZED = "unauthorized"  # 401
    PAYMENT_REQUIRED = "payment_required"  # 402 (feature not in tier)
    FORBIDDEN = "forbidden"  # 403 (not entitled or missing scope)
    QUOTA_EXCEEDED = "quota_exceeded"  # 429 (quota or rate limit exhausted)
    UNAVAILABLE = "unavailable"  # 503 (kill switch or guard unavailable)


# The authoritative pairing of error code and HTTP status. The REST channel reads the status,
# the MCP channel returns the code; keeping it as data means both share one map.
HTTP_STATUS_FOR: dict[ConduitErrorCode, int] = {
    ConduitErrorCode.UNAUTHORIZED: HTTP_UNAUTHORIZED,
    ConduitErrorCode.PAYMENT_REQUIRED: HTTP_PAYMENT_REQUIRED,
    ConduitErrorCode.FORBIDDEN: HTTP_FORBIDDEN,
    ConduitErrorCode.QUOTA_EXCEEDED: HTTP_QUOTA_EXCEEDED,
    ConduitErrorCode.UNAVAILABLE: HTTP_SERVICE_UNAVAILABLE,
}


class ConduitError(Exception):
    """Base Conduit error carrying both an MCP `code` and the paired HTTP `status`.

    Raised in the middleware and tool path; the REST adapter renders `status` and the MCP
    adapter renders `code`. `retry_after` is set only for 429 (quota or rate limit).
    """

    def __init__(
        self,
        code: ConduitErrorCode,
        detail: str = "",
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.status = HTTP_STATUS_FOR[code]
        self.detail = detail
        self.retry_after = retry_after


# Reverse of HTTP_STATUS_FOR, built once at import (the pairing is one-to-one).
_CODE_FOR_STATUS: dict[int, ConduitErrorCode] = {
    status: code for code, status in HTTP_STATUS_FOR.items()
}


def mcp_error_for_status(http_status: int) -> ConduitErrorCode:
    """Map an HTTP status back to its MCP error code.

    Used when an engine or entitlements call raised an HTTP status (e.g. `QuotaExceeded` with
    429) that the MCP channel must express as an error code. An unknown status fails closed to
    `UNAVAILABLE` (503) rather than guessing a softer code.
    """
    return _CODE_FOR_STATUS.get(http_status, ConduitErrorCode.UNAVAILABLE)


__all__ = [
    "HTTP_FORBIDDEN",
    "HTTP_PAYMENT_REQUIRED",
    "HTTP_QUOTA_EXCEEDED",
    "HTTP_SERVICE_UNAVAILABLE",
    "HTTP_STATUS_FOR",
    "HTTP_UNAUTHORIZED",
    "ConduitError",
    "ConduitErrorCode",
    "mcp_error_for_status",
]
