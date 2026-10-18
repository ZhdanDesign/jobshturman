from __future__ import annotations

from .models import ErrorInfo

RETRYABLE = {"service_unreachable", "rate_limited", "internal_error"}


class SourceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "read",
        can_retry: bool | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.stage = stage
        self.can_retry = code in RETRYABLE if can_retry is None else can_retry
        self.retry_after = retry_after

    def info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,  # type: ignore[arg-type]
            message=self.message,
            stage=self.stage,
            can_retry=self.can_retry,
            retry_after_seconds=self.retry_after,
        )
