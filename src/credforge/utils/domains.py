"""Registrable-domain extraction for rate-limit keying and robots.txt origins.

Uses tldextract with its bundled public-suffix snapshot instead of hitting
the network for an up-to-date suffix list on first use -- credforge's own
rate limiter/robots layer should never have a hidden startup network
dependency of its own.
"""

from urllib.parse import urlsplit

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(url: str) -> str:
    parts = _extract(url)
    if not parts.domain:
        return urlsplit(url).hostname or url
    return ".".join(p for p in (parts.domain, parts.suffix) if p)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def subdomain_of(url: str) -> str:
    return _extract(url).subdomain


def parse_bare_domain(text: str) -> str | None:
    """If `text` -- exactly, no scheme, no path, no subdomain -- already IS a
    registrable domain (a real public suffix, per tldextract's snapshot,
    not a naive "has a dot" check), return it lowercased. Otherwise None.

    "nasa.gov" -> "nasa.gov". "api.nasa.gov" -> None (has a subdomain --
    not "bare", so it isn't trusted as-is). "Sage" -> None (no recognized
    suffix at all -- tldextract finds nothing to extract). "3.14" -> None
    ("14" isn't a real public suffix). See DECISIONS.md D-047.
    """
    candidate = text.strip().lower()
    parts = _extract(candidate)
    if not parts.domain or not parts.suffix or parts.subdomain:
        return None
    if f"{parts.domain}.{parts.suffix}" != candidate:
        return None
    return candidate
