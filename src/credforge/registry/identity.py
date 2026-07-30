"""Two-level identity keying.

input_key is the raw slugified input string -- it only dedups a repeat
search of the *exact same spelling* within RESOLVE itself.

identity_key is the real identity used everywhere else (registry, vault
refs, revoke, resumability): the canonical domain once RESOLVE succeeds
(e.g. "github.com"), or "unresolved:<input_key>" if it hasn't resolved
yet. This is what guarantees two different spellings of the same app
("Github", "GitHub Inc", "github.com") never provision two accounts.
"""

import re


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def input_key(app_name: str) -> str:
    return slugify(app_name)


def identity_key_from_domain(domain: str) -> str:
    return domain.strip().lower().removeprefix("www.")


def unresolved_identity_key(app_name: str) -> str:
    return f"unresolved:{input_key(app_name)}"
