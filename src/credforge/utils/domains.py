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
