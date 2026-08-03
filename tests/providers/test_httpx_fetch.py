import gzip
from pathlib import Path

import httpx
import pytest
import respx

from credforge.net.rate_limiter import DomainRateLimiter
from credforge.providers.fetch import FetchException
from credforge.providers.httpx_fetch import HttpxFetchProvider, is_js_rendered_shell
from credforge.providers.httpx_fetch import _html_to_text

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "js_shell_detection"


def _provider(max_response_bytes: int | None = None) -> HttpxFetchProvider:
    limiter = DomainRateLimiter(default_rate_per_sec=100.0, default_burst=5)
    kwargs = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
    return HttpxFetchProvider(rate_limiter=limiter, user_agent="credforge-test/0.1", **kwargs)


def _minimal_pdf_with_text(text: str) -> bytes:
    """A real, valid, minimal single-page PDF with one text run -- built by
    hand (correct xref offsets computed here, not guessed) rather than
    depending on a static binary fixture file. Verified against a real
    pypdf parse before use in any test."""
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 100] /Contents 5 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        b"5 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_start = len(out)
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n".encode()
    out.extend(xref)
    out.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_start).encode() + b"\n%%EOF")
    return bytes(out)


@pytest.mark.asyncio
@respx.mock
async def test_successful_fetch_returns_result() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/docs").mock(
        return_value=httpx.Response(200, text="<html><body>hi</body></html>", headers={"content-type": "text/html"})
    )
    provider = _provider()
    result = await provider.fetch("https://example.com/docs")
    assert result.status_code == 200
    assert result.text == "hi"  # converted to visible text, not raw markup -- see D-023


@pytest.mark.asyncio
@respx.mock
async def test_robots_disallowed_raises_before_touching_target_url() -> None:
    # Failure drill (1/2): a disallowed path must never even reach the
    # target server, let alone spend a rate-limit token on it.
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    target_route = respx.get("https://example.com/private/x").mock(return_value=httpx.Response(200))

    provider = _provider()
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/private/x")

    assert exc_info.value.error.reason == "robots_disallowed"
    assert target_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_timeout_is_classified_correctly() -> None:
    # Failure drill (2/2): a real network timeout must surface as a typed,
    # classifiable FetchException -- not an unhandled httpx exception.
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/slow").mock(side_effect=httpx.TimeoutException("timed out"))

    provider = _provider()
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/slow")

    assert exc_info.value.error.reason == "timeout"


@pytest.mark.asyncio
@respx.mock
async def test_html_responses_are_converted_to_visible_text() -> None:
    # Failure drill / regression: found by fetching a real page (GitHub's
    # ToS) whose raw HTML was handed straight to the extractors, so an
    # "evidence" quote could literally be `<!DOCTYPE html>...<head>` --
    # a quote that quotes nothing. Script/style content must also be
    # excluded, not just the tags stripped.
    html_body = (
        "<html><head><style>.x{color:red}</style>"
        "<script>var flag = 'octocaptcha_secret';</script></head>"
        "<body><h1>Terms of Service</h1><p>You may not use automated means.</p></body></html>"
    )
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/tos").mock(
        return_value=httpx.Response(200, text=html_body, headers={"content-type": "text/html; charset=utf-8"})
    )

    provider = _provider()
    result = await provider.fetch("https://example.com/tos")

    assert "<html>" not in result.text
    assert "<script>" not in result.text
    assert "octocaptcha_secret" not in result.text  # script content excluded, not just untagged
    assert "color:red" not in result.text  # style content excluded
    assert "Terms of Service" in result.text
    assert "You may not use automated means." in result.text


@pytest.mark.asyncio
@respx.mock
async def test_connection_error_is_classified_correctly() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("refused"))

    provider = _provider()
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/down")

    assert exc_info.value.error.reason == "connection_error"


@pytest.mark.asyncio
@respx.mock
async def test_oversized_content_length_is_rejected_before_download() -> None:
    # A server that's honest about size (declares a huge Content-Length) must
    # be rejected before a single body byte is streamed, not after buffering
    # gigabytes and decoding them. D-030 -- found via a real crawl that hit a
    # MemoryError inside response.text with no size cap in place.
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/huge").mock(
        return_value=httpx.Response(
            200,
            content=b"tiny",
            headers={"content-length": "999999999", "content-type": "text/plain"},
        )
    )

    provider = _provider(max_response_bytes=5 * 1024 * 1024)
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/huge")

    assert exc_info.value.error.reason == "response_too_large"
    assert "999999999" in exc_info.value.error.detail


@pytest.mark.asyncio
@respx.mock
async def test_gzip_amplified_body_is_aborted_mid_stream_despite_small_content_length() -> None:
    # Content-encoding can amplify: a small gzipped Content-Length can still
    # decompress into gigabytes. httpx's aiter_bytes() yields *decompressed*
    # chunks, so the cap must apply to bytes read, not just the declared
    # (compressed) Content-Length -- a dishonest-by-omission server, not a
    # dishonest one.
    large_text = "A" * (2 * 1024 * 1024)  # 2 MiB decompressed
    compressed = gzip.compress(large_text.encode())
    assert len(compressed) < 10 * 1024  # highly compressible -- content-length stays tiny

    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/bomb").mock(
        return_value=httpx.Response(
            200,
            content=compressed,
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
        )
    )

    provider = _provider(max_response_bytes=1024 * 1024)  # 1 MiB cap, well under 2 MiB decompressed
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/bomb")

    assert exc_info.value.error.reason == "response_too_large"
    assert "mid-read" in exc_info.value.error.detail


@pytest.mark.asyncio
@respx.mock
async def test_decode_error_is_wrapped_not_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single bad URL must degrade one candidate, never kill the process --
    # even a decode-layer failure (MemoryError, bad codec) must come back as
    # a typed FetchException, not an unhandled exception.
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/bad-encoding").mock(
        return_value=httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
    )

    def _boom(raw: bytes, content_type: str) -> str:
        raise MemoryError("simulated allocation failure")

    monkeypatch.setattr("credforge.providers.httpx_fetch._decode_body", _boom)

    provider = _provider()
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/bad-encoding")

    assert exc_info.value.error.reason == "decode_error"


@pytest.mark.asyncio
@respx.mock
async def test_pdf_response_is_extracted_to_text() -> None:
    # D-060: found live -- Alpha Vantage's real ToS is served as
    # application/pdf, and GATE could never confirm anything about it
    # while a PDF response's `text` stayed None unconditionally.
    pdf_bytes = _minimal_pdf_with_text("Hello ToS PDF World")
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/terms.pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})
    )

    provider = _provider()
    result = await provider.fetch("https://example.com/terms.pdf")

    assert result.text is not None
    assert "Hello ToS PDF World" in result.text


@pytest.mark.asyncio
@respx.mock
async def test_pdf_still_goes_through_the_existing_size_cap() -> None:
    # The size cap is not a PDF-specific guard -- it's the same
    # _stream_capped path every content type already goes through. A
    # server honest about a too-large PDF must still be rejected before
    # any bytes are streamed or handed to pypdf, same as D-030.
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/huge.pdf").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.4 not a real full pdf but content-length lies about size anyway",
            headers={"content-length": "999999999", "content-type": "application/pdf"},
        )
    )

    provider = _provider(max_response_bytes=5 * 1024 * 1024)
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/huge.pdf")

    assert exc_info.value.error.reason == "response_too_large"


@pytest.mark.asyncio
@respx.mock
async def test_corrupted_pdf_raises_a_typed_pdf_decode_error_not_unhandled() -> None:
    # A real PDF-parsing failure (corrupted body, no real xref) must
    # degrade to a typed FetchException, the same principle
    # test_decode_error_is_wrapped_not_propagated already establishes for
    # text decoding -- never an unhandled pypdf exception.
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/corrupt.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4\nthis is not a real pdf body", headers={"content-type": "application/pdf"})
    )

    provider = _provider()
    with pytest.raises(FetchException) as exc_info:
        await provider.fetch("https://example.com/corrupt.pdf")

    assert exc_info.value.error.reason == "pdf_decode_error"


# --- D-066: JS-shell detection, calibrated against two real captured pages ---


def test_is_js_rendered_shell_true_on_a_real_captured_js_app_shell() -> None:
    # api.congress.gov's real raw HTML, captured live: 174,997 bytes, almost
    # entirely one inlined 170,736-byte <script> block, extracting to 79
    # characters of visible text. This is the exact page that produced a
    # real DISCOVERY_FAILED this session before this fix existed.
    raw_html = (_FIXTURES_DIR / "real_js_shell.html").read_text(encoding="utf-8")
    extracted = _html_to_text(raw_html)
    assert is_js_rendered_shell(raw_html=raw_html, extracted_text=extracted) is True


def test_is_js_rendered_shell_false_on_a_real_captured_static_docs_page() -> None:
    # Alpha Vantage's real, live documentation page: ~1MB raw HTML,
    # extracting to over 700,000 characters of real, substantial prose.
    raw_html = (_FIXTURES_DIR / "real_static_docs.html").read_text(encoding="utf-8")
    extracted = _html_to_text(raw_html)
    assert is_js_rendered_shell(raw_html=raw_html, extracted_text=extracted) is False


def test_is_js_rendered_shell_false_for_a_genuinely_short_but_real_page() -> None:
    # Thin text alone is not proof -- a real page can legitimately be short.
    # Only combined with substantial script content does it count as a shell.
    raw_html = "<html><body><h1>Service Unavailable</h1><p>Try again later.</p></body></html>"
    extracted = _html_to_text(raw_html)
    assert is_js_rendered_shell(raw_html=raw_html, extracted_text=extracted) is False


def test_is_js_rendered_shell_false_when_script_present_but_text_is_substantial() -> None:
    # A real page can carry plenty of script (analytics, widgets) without
    # being an app shell -- what matters is whether real content ALSO exists.
    raw_html = "<html><body>" + "<script>var x = 1;</script>" * 200 + "<p>" + ("Real content. " * 50) + "</p></body></html>"
    extracted = _html_to_text(raw_html)
    assert len(extracted) >= 300
    assert is_js_rendered_shell(raw_html=raw_html, extracted_text=extracted) is False


# --- D-066: the browser-render fallback, integration behavior -----------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_falls_back_to_browser_render_on_a_detected_js_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    shell_html = "<html><body><div id=\"root\"></div>" + "<script>" + ("x=1;" * 1000) + "</script></body></html>"
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/app").mock(
        return_value=httpx.Response(200, text=shell_html, headers={"content-type": "text/html"})
    )

    provider = _provider()
    calls: list[str] = []

    async def fake_render(url: str, *, domain: str) -> str:
        calls.append(url)
        return "This is the real, rendered content a browser would have seen."

    monkeypatch.setattr(provider, "_render_with_browser", fake_render)

    result = await provider.fetch("https://example.com/app")

    assert result.rendered_with_browser is True
    assert result.text == "This is the real, rendered content a browser would have seen."
    assert calls == ["https://example.com/app"]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_does_not_attempt_browser_render_on_a_normal_page(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/docs").mock(
        return_value=httpx.Response(200, text="<html><body><p>Real, substantial content. " * 20 + "</p></body></html>", headers={"content-type": "text/html"})
    )

    provider = _provider()
    calls: list[str] = []

    async def fake_render(url: str, *, domain: str) -> str:
        calls.append(url)
        return "should never be called"

    monkeypatch.setattr(provider, "_render_with_browser", fake_render)

    result = await provider.fetch("https://example.com/docs")

    assert result.rendered_with_browser is False
    assert calls == []


@pytest.mark.asyncio
@respx.mock
async def test_browser_render_failure_degrades_to_the_original_thin_text_not_a_crash() -> None:
    # A slow/broken browser render must never take the whole fetch down --
    # falls back to the (thin) plain-HTTP text, exactly pre-D-066 behavior.
    shell_html = "<html><body><div id=\"root\"></div>" + "<script>" + ("x=1;" * 1000) + "</script></body></html>"
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/app").mock(
        return_value=httpx.Response(200, text=shell_html, headers={"content-type": "text/html"})
    )

    provider = _provider()
    provider._browser_render_timeout_ms = 1  # force a real, immediate timeout, no mocking of playwright itself

    result = await provider.fetch("https://example.com/app")

    # Falls back to exactly what _html_to_text(raw_html) alone would have
    # produced pre-D-066 -- this shell has zero real visible text, so that
    # function's own existing fallback (return raw HTML rather than lose a
    # page) is what's expected here, not a new behavior this fix introduced.
    assert result.rendered_with_browser is False
    assert result.text == shell_html


@pytest.mark.asyncio
async def test_render_cache_short_circuits_before_ever_launching_a_browser() -> None:
    # A pre-populated cache entry must return immediately, never reaching
    # the `from playwright.async_api import ...` line at all -- proven here
    # by NOT mocking Playwright and asserting the cached value comes back
    # (a real launch attempt would either be slow or fail in this sandbox,
    # neither of which happens if the cache is actually consulted first).
    provider = _provider()
    provider._render_cache["https://example.com/shell"] = "cached rendered text"

    result = await provider._render_with_browser("https://example.com/shell", domain="example.com")

    assert result == "cached rendered text"


@pytest.mark.asyncio
async def test_render_cache_also_short_circuits_a_cached_failure() -> None:
    # A prior failed render (cached as None) must also not be retried within
    # the same run -- same "expensive, don't repeat" reasoning as a success.
    provider = _provider()
    provider._render_cache["https://example.com/broken"] = None

    result = await provider._render_with_browser("https://example.com/broken", domain="example.com")

    assert result is None
