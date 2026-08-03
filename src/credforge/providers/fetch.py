"""FetchProvider: the interface every stage uses to fetch a URL.

Every stage calls only FetchProvider.fetch() -- never httpx directly --
so rate limiting and robots.txt compliance (both enforced inside the
concrete HttpxFetchProvider, built in Stage 1) apply uniformly to every
fetch in the system, regardless of which stage issued it.
"""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

FetchErrorReason = Literal[
    "robots_disallowed",
    "timeout",
    "http_error",
    "connection_error",
    "rate_limited_wait_exceeded",
    "response_too_large",
    "decode_error",
    "pdf_decode_error",
]


class FetchResult(BaseModel):
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str | None
    fetched_at: datetime
    from_cache: bool = False


class FetchError(BaseModel):
    url: str
    reason: FetchErrorReason
    status_code: int | None = None
    detail: str | None = None


class FetchException(Exception):
    def __init__(self, error: FetchError) -> None:
        self.error = error
        super().__init__(f"{error.reason} fetching {error.url}: {error.detail or ''}")


class FetchProvider(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        method: Literal["GET", "HEAD"] = "GET",
        headers: dict[str, str] | None = None,
    ) -> FetchResult: ...
