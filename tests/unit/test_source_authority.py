from credforge.enums import SourceTier
from credforge.pipeline.source_authority import classify_source_tier, describe_tier_match, tier_sort_key


def test_high_tier_path_segments() -> None:
    for path in ("/api/reference", "/reference/getting-started", "/api-reference/v2", "/rest/v1"):
        assert classify_source_tier(f"https://example.com{path}") == SourceTier.HIGH


def test_high_tier_developer_subdomain() -> None:
    assert classify_source_tier("https://developer.example.com/") == SourceTier.HIGH
    assert classify_source_tier("https://developers.example.com/") == SourceTier.HIGH


def test_medium_tier_docs_path_and_subdomain() -> None:
    assert classify_source_tier("https://example.com/docs/intro") == SourceTier.MEDIUM
    assert classify_source_tier("https://example.com/guide/setup") == SourceTier.MEDIUM
    assert classify_source_tier("https://docs.example.com/") == SourceTier.MEDIUM


def test_low_tier_everything_else() -> None:
    for url in (
        "https://blog.example.com/how-to",
        "https://stackoverflow.com/questions/123",
        "https://medium.com/@someone/tutorial",
        "https://trailhead.example.com/some-tutorial",
        "https://example.com/",
    ):
        assert classify_source_tier(url) == SourceTier.LOW


def test_path_segment_beats_a_medium_subdomain() -> None:
    # The real vendor shape that motivated checking every path segment,
    # not just the first: docs.github.com/en/rest -- "docs" subdomain
    # alone is MEDIUM, but "rest" is a real HIGH-tier reference signal.
    assert classify_source_tier("https://docs.github.com/en/rest") == SourceTier.HIGH


def test_api_subdomain_alone_is_not_automatically_high_tier() -> None:
    # Deliberate: "api.*" is often an endpoint domain (base_url), not a
    # docs page -- only a path segment or the developer.* subdomain earns
    # HIGH on their own.
    assert classify_source_tier("https://api.example.com/") == SourceTier.LOW


def test_tier_sort_key_orders_high_before_medium_before_low() -> None:
    urls = [
        "https://blog.example.com/post",
        "https://example.com/api/reference",
        "https://docs.example.com/",
    ]
    ordered = sorted(urls, key=tier_sort_key)
    assert ordered == [
        "https://example.com/api/reference",
        "https://docs.example.com/",
        "https://blog.example.com/post",
    ]


def test_describe_tier_match_names_the_specific_signal() -> None:
    assert "'api'" in describe_tier_match("https://example.com/api/widgets")
    desc = describe_tier_match("https://developer.example.com/")
    assert "HIGH-tier" in desc
    assert "developer" in desc
