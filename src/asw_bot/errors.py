"""Exception hierarchy. Each class maps to one row of the error table (spec 3.5).

The caller decides what to do from the *type* of the error, never by parsing
strings: retry, log in again, try the next preference, or abort.
"""

from enum import StrEnum


class AswError(Exception):
    """Base class for every error raised by the bot."""


class AuthError(AswError):
    """401 from the API, or a login that did not produce a token."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class BusinessErrorKind(StrEnum):
    """Known business rejections. Grows as we observe real messages."""

    NO_PENDING_TOPICS = "no_pending_topics"
    UNKNOWN = "unknown"


def classify_business_message(message: str) -> BusinessErrorKind:
    """Map the server's free-text error to a kind the strategy can act on."""
    if "temas pendientes" in message.lower():
        return BusinessErrorKind.NO_PENDING_TOPICS
    return BusinessErrorKind.UNKNOWN


class BusinessError(AswError):
    """The server understood the request and refused it: ``{"error": "..."}``.

    Never retried blindly: the same request would be refused again.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = classify_business_message(message)


class ServerError(AswError):
    """5xx without a business error body: the server itself failed."""

    def __init__(self, endpoint: str, status: int) -> None:
        super().__init__(f"{endpoint}: server failure (HTTP {status})")
        self.endpoint = endpoint
        self.status = status


class RouteError(AswError):
    """404: wrong path or method. A programming error, so we abort."""

    def __init__(self, endpoint: str) -> None:
        super().__init__(f"{endpoint}: route not found (HTTP 404)")
        self.endpoint = endpoint


class NetworkError(AswError):
    """The request failed at the transport level (connect, timeout, reset).

    ``maybe_delivered`` is False only when we know the request never left
    (connection could not be opened). When True, the server may have processed
    it, which matters for non-idempotent calls such as ``agendar``.
    """

    def __init__(self, endpoint: str, cause: str, *, maybe_delivered: bool) -> None:
        super().__init__(f"{endpoint}: network failure ({cause})")
        self.endpoint = endpoint
        self.cause = cause
        self.maybe_delivered = maybe_delivered


class UnexpectedResponseError(AswError):
    """The response does not look like anything in the spec."""

    def __init__(self, endpoint: str, detail: str) -> None:
        super().__init__(f"{endpoint}: unexpected response ({detail})")
        self.endpoint = endpoint
        self.detail = detail


class DryRunError(AswError):
    """A write endpoint was called while DRY_RUN is on. Nothing was sent."""

    def __init__(self, endpoint: str) -> None:
        super().__init__(f"{endpoint}: blocked because DRY_RUN is enabled")
        self.endpoint = endpoint
