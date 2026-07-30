import gzip

import httpx
import pytest
import respx

from credforge.net.rate_limiter import DomainRateLimiter
from credforge.providers.fetch import FetchException
from credforge.providers.httpx_fetch import HttpxFetchProvider


def _provider(max_response_bytes: int | None = None) -> HttpxFetchProvider:
    limiter = DomainRateLimiter(default_rate_per_sec=100.0, default_burst=5)
    kwargs = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
    return HttpxFetchProvider(rate_limiter=limiter, user_agent="credforge-test/0.1", **kwargs)


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
