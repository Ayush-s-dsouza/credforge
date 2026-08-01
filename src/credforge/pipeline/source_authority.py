"""Three-tier source-authority classification for docs-candidate URLs.

The real bug this exists to fix: the old ranking signal
(`developer`/`docs`/`api` present in the URL or not, a flat yes/no) scored
an official API reference and a tutorial or blog post that merely lives
on a matching-shaped URL identically. Authority has to live in the
RANKING that decides which candidate wins before DISCOVER/CLASSIFY ever
see it -- by the time CLASSIFY reads a page, the wrong page has already
been chosen, and no amount of downstream distrust recovers that. See
DECISIONS.md D-049.

Deliberately a standalone module, not folded into resolve.py: CLASSIFY
needs the exact same tier logic applied to whichever docs_url DISCOVER
actually ended up using (which can differ from RESOLVE's top pick if
that candidate didn't pan out, D-028) -- a shared function is what keeps
"HIGH means the same thing" true across both call sites, not two
independently-maintained copies that could drift.
"""

from urllib.parse import urlsplit

from ..enums import SourceTier
from ..utils.domains import subdomain_of

_TIER_RANK = {SourceTier.HIGH: 0, SourceTier.MEDIUM: 1, SourceTier.LOW: 2}

# Official API references: a dedicated reference/rest path, or a
# developer(s).* subdomain. Deliberately does NOT include a bare "api.*"
# subdomain -- that's frequently an API *endpoint* domain (base_url), not
# a docs page, and treating it as automatic HIGH-tier docs authority
# would be a category error.
_HIGH_PATH_SEGMENTS = frozenset({"api", "reference", "api-reference", "rest"})
_HIGH_SUBDOMAIN_PREFIXES = ("developer",)  # matches "developer" and "developers"

# Could be real API docs, could be a general user guide -- worth trying
# before anything in LOW, but not trusted the way HIGH is.
_MEDIUM_PATH_SEGMENTS = frozenset({"docs", "guide", "guides"})
_MEDIUM_SUBDOMAIN_PREFIXES = ("docs",)


def _path_segments(url: str) -> set[str]:
    path = urlsplit(url).path.strip("/")
    return {seg.lower() for seg in path.split("/") if seg}


def classify_source_tier(url: str) -> SourceTier:
    """HIGH: official API reference (path segment api/reference/api-reference/rest,
    or a developer(s).* subdomain). MEDIUM: general docs/guide (path segment
    docs/guide/guides, or a docs.* subdomain). LOW: everything else --
    blogs, tutorials, third-party write-ups, community forums, Stack
    Overflow, Medium. Checks every path segment, not just the first --
    docs.github.com/en/rest is a real vendor shape where the subdomain
    alone (MEDIUM) would undersell what the path segment ("rest") already
    tells you (HIGH)."""
    subdomain = subdomain_of(url)
    subdomain_label = subdomain.split(".")[0].lower() if subdomain else ""
    segments = _path_segments(url)

    if subdomain_label.startswith(_HIGH_SUBDOMAIN_PREFIXES) or segments & _HIGH_PATH_SEGMENTS:
        return SourceTier.HIGH
    if subdomain_label.startswith(_MEDIUM_SUBDOMAIN_PREFIXES) or segments & _MEDIUM_PATH_SEGMENTS:
        return SourceTier.MEDIUM
    return SourceTier.LOW


def tier_sort_key(url: str) -> int:
    """Lower sorts first. Use with a *stable* sort so "within a tier,
    existing ranking applies" -- this key alone decides tier placement,
    ties within a tier keep whatever relative order they arrived in."""
    return _TIER_RANK[classify_source_tier(url)]


def describe_tier_match(url: str) -> str:
    """Human-readable, auditable reason for the tier a URL landed in --
    names the specific matched segment/subdomain, not just the tier name."""
    tier = classify_source_tier(url)
    subdomain = subdomain_of(url)
    subdomain_label = subdomain.split(".")[0].lower() if subdomain else ""
    segments = _path_segments(url)

    if tier == SourceTier.HIGH:
        matched_segments = segments & _HIGH_PATH_SEGMENTS
        if matched_segments:
            return f"HIGH-tier (path segment {sorted(matched_segments)[0]!r})"
        return f"HIGH-tier ({subdomain_label!r} subdomain)"
    if tier == SourceTier.MEDIUM:
        matched_segments = segments & _MEDIUM_PATH_SEGMENTS
        if matched_segments:
            return f"MEDIUM-tier (path segment {sorted(matched_segments)[0]!r})"
        return f"MEDIUM-tier ({subdomain_label!r} subdomain)"
    return "LOW-tier (no official-reference or docs signal in URL shape)"
