"""Real HTTP fetch client (FetchProvider).

This is the single choke point every stage's fetch goes through: robots.txt
check, then rate-limiter acquire, then the actual request -- in that
order, so a disallowed URL never even spends a rate-limit token. See
DECISIONS.md for why both live here rather than being left to the caller.

FetchResult.text is visible text, not raw markup, for anything served as
HTML -- found via a real fetch of GitHub's actual ToS page, whose raw HTML
was being handed to the extractors and to GATE's evidence-snippet quoting
directly. That meant the "evidence" attached to an AUTO decision was
literally `<!DOCTYPE html>...<head>` boilerplate -- a quote that quotes
nothing. See DECISIONS.md D-023.

Every response is streamed and size-capped (default 5 MB, `max_response_bytes`)
rather than buffered and decoded in one shot -- found via a real crawl that
hit a pathologically large third-party page and died with a MemoryError
inside `response.text`. See DECISIONS.md D-030.

A `application/pdf` response is extracted to text (`pypdf`), the same
size-capped `body` bytes every other content type already goes through --
found live, a real vendor's real ToS (Alpha Vantage) is served as a PDF,
and GATE could never confirm anything about it while `text` stayed empty
for every PDF response. See DECISIONS.md D-060.
"""

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal

import httpx

from ..net.rate_limiter import DomainRateLimiter
from ..net.robots import RobotsCache
from ..utils.domains import origin_of, registrable_domain
from .fetch import FetchError, FetchException, FetchResult

DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class HttpxFetchProvider:
    def __init__(
        self,
        *,
        rate_limiter: DomainRateLimiter,
        user_agent: str = "credforge-bot/0.1",
        robots_ttl_seconds: float = 3600.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._user_agent = user_agent
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._robots = RobotsCache(
            raw_fetch=self._raw_fetch_for_robots,
            ttl_seconds=robots_ttl_seconds,
            user_agent=user_agent,
        )

    async def fetch(
        self,
        url: str,
        *,
        method: Literal["GET", "HEAD"] = "GET",
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        domain = registrable_domain(url)
        origin = origin_of(url)

        allowed = await self._robots.is_allowed(url, origin)
        if not allowed:
            raise FetchException(FetchError(url=url, reason="robots_disallowed"))

        await self._rate_limiter.acquire_for_domain(domain)

        status_code, final_url, content_type, body = await self._stream_capped(
            method, url, headers={"User-Agent": self._user_agent, **(headers or {})}
        )

        text: str | None = None
        if _is_textual_content_type(content_type):
            try:
                text = _decode_body(bytes(body), content_type)
            except (MemoryError, UnicodeDecodeError) as exc:
                raise FetchException(
                    FetchError(url=url, reason="decode_error", status_code=status_code, detail=str(exc))
                ) from exc
        elif _is_pdf_content_type(content_type):
            # `body` is already size-capped by `_stream_capped` above --
            # exactly the same guard every other content type already
            # goes through, nothing PDF-specific added here.
            try:
                text = _extract_pdf_text(bytes(body))
            except Exception as exc:
                raise FetchException(
                    FetchError(url=url, reason="pdf_decode_error", status_code=status_code, detail=str(exc))
                ) from exc
        if text and "html" in content_type.lower():
            text = _html_to_text(text)

        return FetchResult(
            url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            text=text,
            fetched_at=datetime.now(timezone.utc),
        )

    async def _stream_capped(
        self, method: str, url: str, *, headers: dict[str, str]
    ) -> tuple[int, str, str, bytearray]:
        """Stream a response body with a hard size cap, never buffering an
        oversized body before deciding to reject it.

        Checks Content-Length up front (skips the download entirely when the
        server is honest about a too-large body) and also caps bytes as
        they're read from `aiter_bytes()` -- which yields *decompressed*
        content, so a small gzipped response that expands into a huge body
        is caught mid-stream even when Content-Length (the compressed size)
        was under the cap.
        """
        try:
            async with self._client.stream(method, url, headers=headers) as response:
                content_length = response.headers.get("content-length")
                if content_length is not None and content_length.isdigit():
                    if int(content_length) > self._max_response_bytes:
                        raise FetchException(
                            FetchError(
                                url=url,
                                reason="response_too_large",
                                status_code=response.status_code,
                                detail=(
                                    f"content-length {content_length} exceeds cap of "
                                    f"{self._max_response_bytes} bytes"
                                ),
                            )
                        )

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise FetchException(
                            FetchError(
                                url=url,
                                reason="response_too_large",
                                status_code=response.status_code,
                                detail=(
                                    f"streamed body exceeded cap of {self._max_response_bytes} bytes "
                                    "mid-read (decompressed size, not just content-length)"
                                ),
                            )
                        )

                return response.status_code, str(response.url), response.headers.get("content-type", ""), body
        except FetchException:
            raise
        except httpx.InvalidURL as exc:
            raise FetchException(FetchError(url=url, reason="http_error", detail=str(exc))) from exc
        except httpx.TimeoutException as exc:
            raise FetchException(FetchError(url=url, reason="timeout", detail=str(exc))) from exc
        except httpx.HTTPError as exc:
            raise FetchException(FetchError(url=url, reason="connection_error", detail=str(exc))) from exc

    async def _raw_fetch_for_robots(self, url: str) -> tuple[int, str]:
        # Deliberately bypasses is_allowed() (that would recurse forever) but
        # still goes through the rate limiter -- robots.txt fetches are
        # metered too, not a free pass -- and the same size cap, since a
        # malicious/misconfigured server can serve an oversized robots.txt
        # just as easily as an oversized page.
        domain = registrable_domain(url)
        await self._rate_limiter.acquire_for_domain(domain)
        status_code, _final_url, content_type, body = await self._stream_capped(
            "GET", url, headers={"User-Agent": self._user_agent}
        )
        text = _decode_body(bytes(body), content_type) if body else ""
        return status_code, text


def _is_textual_content_type(content_type: str) -> bool:
    return any(marker in content_type for marker in ("text/", "json", "xml"))


def _is_pdf_content_type(content_type: str) -> bool:
    return "application/pdf" in content_type.lower()


def _extract_pdf_text(raw: bytes) -> str:
    # Lazy import, same pattern as anthropic/playwright elsewhere in this
    # project -- pypdf is a real, always-installed base dependency here
    # (not gated behind an extra, unlike those two), but importing it only
    # where it's used keeps this module's own import cost at zero for
    # every fetch that isn't a PDF.
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)


def _decode_body(raw: bytes, content_type: str) -> str:
    match = _CHARSET_RE.search(content_type)
    charset = match.group(1) if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _VisibleTextExtractor(HTMLParser):
    _SKIPPED_TAGS = ("script", "style", "noscript")

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return " ".join(self._chunks)


def _html_to_text(raw_html: str) -> str:
    parser = _VisibleTextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:
        return raw_html  # malformed markup -- fall back to the raw string rather than losing the page
    text = parser.get_text()
    return html.unescape(text) if text else raw_html
