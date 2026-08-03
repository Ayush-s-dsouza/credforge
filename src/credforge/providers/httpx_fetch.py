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

A plain HTTP fetch that lands on a client-rendered app shell (a near-empty
root div, all real content injected by JavaScript after load) is silently
indistinguishable from a genuinely content-free page -- this is the real
cause behind DISCOVERY_FAILED for several real vendors whose actual public
API is well documented but served behind a JS framework (confirmed live,
D-066: api.congress.gov's raw HTML is 175KB, almost entirely one inlined
170KB <script> block, extracting to 79 characters of visible text; even
NASA's own hand-authored recipe was masking the identical limitation, not
avoiding it -- a fresh, unpinned resolve() this session hit the same
failure). `is_js_rendered_shell()` detects this deterministically (near-zero
extracted text AND substantial script content -- neither alone is proof;
a real page can legitimately be short, and a real page can legitimately
carry a lot of script for reasons that aren't "this is an app shell"), and
`fetch()` falls back to a real headless-browser render of the exact same
URL only when both are true. See DECISIONS.md D-066.
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

# D-066: thresholds behind is_js_rendered_shell(), sized against two real,
# captured pages (tests/fixtures/js_shell_detection/) -- a real JS app shell
# (api.congress.gov: 174,997 raw bytes -> 79 extracted chars, one 170,736-byte
# inline <script>) and a real, substantial static docs page (Alpha Vantage's
# real documentation: ~1MB raw -> 723,348 extracted chars). The gap between
# 79 and 723,348 is enormous; these numbers sit nowhere near either real
# observation, with wide margin on both sides.
_SHELL_TEXT_THRESHOLD = 300
_MIN_SCRIPT_BYTES_FOR_SHELL = 2_000
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)

# A slow or hung page must fail this specific fetch, not stall the whole run.
DEFAULT_BROWSER_RENDER_TIMEOUT_MS = 10_000


def is_js_rendered_shell(*, raw_html: str, extracted_text: str) -> bool:
    """True only if BOTH: the extracted visible text is near-empty (a real,
    genuinely short static page is not by itself proof of anything), AND the
    raw HTML carries a nontrivial amount of script content (ruling in the
    actual client-rendering signal, not just "this page didn't say much").
    Pure and deterministic -- no network, no heuristics beyond these two
    measured thresholds. See this module's docstring and DECISIONS.md D-066
    for the real fixtures this was calibrated against."""
    if len(extracted_text.strip()) >= _SHELL_TEXT_THRESHOLD:
        return False
    script_bytes = sum(len(match) for match in _SCRIPT_TAG_RE.findall(raw_html))
    return script_bytes >= _MIN_SCRIPT_BYTES_FOR_SHELL


class HttpxFetchProvider:
    def __init__(
        self,
        *,
        rate_limiter: DomainRateLimiter,
        user_agent: str = "credforge-bot/0.1",
        robots_ttl_seconds: float = 3600.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        browser_render_timeout_ms: int = DEFAULT_BROWSER_RENDER_TIMEOUT_MS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._user_agent = user_agent
        self._max_response_bytes = max_response_bytes
        self._browser_render_timeout_ms = browser_render_timeout_ms
        self._client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._robots = RobotsCache(
            raw_fetch=self._raw_fetch_for_robots,
            ttl_seconds=robots_ttl_seconds,
            user_agent=user_agent,
        )
        # D-066: one browser render per URL per provider instance, ever --
        # HttpxFetchProvider is constructed once per run (factory.py) and
        # reused across every stage, so this is genuinely "cache per run,"
        # not per-call. A browser render is expensive; RESOLVE's docs-URL
        # verification and DISCOVER's own crawl can both legitimately fetch
        # the same URL in the same run.
        self._render_cache: dict[str, str | None] = {}

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
        rendered_with_browser = False
        if text and "html" in content_type.lower():
            raw_html = text
            # The TRUE (possibly empty) extracted text -- not _html_to_text's
            # own raw-HTML-on-empty fallback, which would otherwise mask a
            # genuine JS shell as if it had large real text. See
            # _extract_visible_text's docstring.
            visible_text = _extract_visible_text(raw_html)
            if visible_text is not None and is_js_rendered_shell(raw_html=raw_html, extracted_text=visible_text):
                rendered = await self._render_with_browser(final_url, domain=domain)
                if rendered is not None:
                    text = rendered
                    rendered_with_browser = True
                else:
                    text = _html_to_text(raw_html)
            else:
                text = _html_to_text(raw_html)

        return FetchResult(
            url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            text=text,
            fetched_at=datetime.now(timezone.utc),
            rendered_with_browser=rendered_with_browser,
        )

    async def _render_with_browser(self, url: str, *, domain: str) -> str | None:
        """Re-fetch `url` with a real headless browser, only ever reached
        after `is_js_rendered_shell()` fired on the plain-HTTP result for
        this exact URL. Robots.txt is deliberately NOT re-checked here --
        `fetch()` already confirmed this exact URL is allowed before ANY
        request (plain or browser) against it was made. The rate limiter
        IS re-acquired: this is a second, real network round-trip to the
        same origin, and browser renders are expensive -- worth metering
        independently of the plain-HTTP request that triggered it.

        Returns None (never raises) on any failure -- a broken or
        pathologically slow page degrades this one fetch back to its
        (thin) plain-HTTP text, exactly the pre-D-066 behavior, rather
        than taking down the caller. `page.goto`'s own `timeout` is what
        makes "a slow page must fail, not hang" true -- capped explicitly,
        not left to whatever Playwright's default happens to be.
        """
        if url in self._render_cache:
            return self._render_cache[url]

        await self._rate_limiter.acquire_for_domain(domain)

        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(user_agent=self._user_agent)
                    await page.goto(url, wait_until="networkidle", timeout=self._browser_render_timeout_ms)
                    rendered_text = await page.inner_text("body")
                finally:
                    await browser.close()
        except Exception:
            self._render_cache[url] = None
            return None

        # Same size discipline every other fetch path already applies --
        # a rendered page's visible text is capped the same way a plain
        # response body is, not left unbounded just because it took a
        # different path to get here.
        rendered_text = rendered_text[: self._max_response_bytes]
        self._render_cache[url] = rendered_text
        return rendered_text

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


def _extract_visible_text(raw_html: str) -> str | None:
    """The real, possibly-empty parsed visible text -- None only if parsing
    itself failed outright (malformed enough to raise). Split out from
    `_html_to_text` (D-066) because that function's own fallback -- return
    the raw HTML back when extraction comes back empty, to avoid losing a
    genuinely malformed page -- would otherwise hide the exact signal
    `is_js_rendered_shell` needs: a page that parsed FINE but has
    legitimately near-zero visible text is precisely what a JS app shell
    looks like, and must be seen as empty, not silently swapped for the
    raw markup."""
    parser = _VisibleTextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:
        return None
    return html.unescape(parser.get_text())


def _html_to_text(raw_html: str) -> str:
    text = _extract_visible_text(raw_html)
    return text if text else raw_html  # malformed markup or empty extraction -- fall back to the raw string
