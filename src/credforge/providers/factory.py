"""Single choice point for which concrete implementation backs each provider
Protocol on a given run.

search/fetch are always real outside of tests -- RESOLVE and DISCOVER need
real research regardless of --live, so there is no mock branch for them
here (tests inject Cassette*/Fake* providers directly instead of going
through this factory). email/browser are gated by --live (Stage 5) --
mock by default, real (IMAP + Playwright) only when --live is passed,
independently of every other axis here. extractor is gated independently
by ANTHROPIC_API_KEY presence -- a completely separate axis from --live,
and from each other. See DECISIONS.md D-031.

search is DdgSearchProvider (no API key, no billing) by default; Brave is
used instead whenever BRAVE_API_KEY is set. See DECISIONS.md D-024 for why
neither is hardcoded as "the" provider.
"""

from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..net.rate_limiter import DomainRateLimiter
from .brave_search import BraveSearchProvider
from .browser import BrowserDriver
from .ddg_search import DdgSearchProvider
from .email import EmailProvider
from .fetch import FetchProvider
from .heuristic_extractor import HeuristicExtractor
from .httpx_fetch import HttpxFetchProvider
from .llm import Extractor
from .mock_browser import MockBrowserDriver
from .mock_email import MockEmailProvider
from .search import SearchProvider


# D-075: `examples/` is checked into the repo (the Dockerfile COPYs it
# into the image at build time -- unlike `settings.data_dir`, which lives
# on Railway's ephemeral container filesystem and is gone on every
# redeploy). A generated recipe committed to `examples/generated_recipes/`
# is the one, deliberate way to make DISCOVER_SIGNUP's output survive a
# redeploy without re-generating it live every time. `parents[3]` from
# this file (providers -> credforge -> src -> repo root) mirrors the same
# REPO_ROOT computation webapp/main.py already does for itself.
_COMMITTED_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"


@dataclass
class ProviderBundle:
    search: SearchProvider
    fetch: FetchProvider
    extractor: Extractor
    email: EmailProvider
    browser: BrowserDriver


def build_providers(settings: Settings, *, live: bool = False) -> ProviderBundle:
    rate_limiter = DomainRateLimiter(
        default_rate_per_sec=settings.default_rate_per_sec,
        default_burst=settings.default_burst,
    )
    fetch = HttpxFetchProvider(
        rate_limiter=rate_limiter,
        user_agent=settings.user_agent,
        robots_ttl_seconds=settings.robots_ttl_seconds,
        max_response_bytes=settings.max_response_bytes,
    )

    search: SearchProvider
    if settings.brave_api_key:
        search = BraveSearchProvider(api_key=settings.brave_api_key.get_secret_value())
    else:
        search = DdgSearchProvider(rate_limiter=rate_limiter)

    extractor: Extractor
    if settings.anthropic_api_key:
        from .anthropic_extractor import AnthropicExtractor

        extractor = AnthropicExtractor(api_key=settings.anthropic_api_key.get_secret_value())
    else:
        extractor = HeuristicExtractor()

    email: EmailProvider
    browser: BrowserDriver
    if live:
        if not (settings.imap_host and settings.imap_user and settings.imap_password):
            raise RuntimeError(
                "--live requires CREDFORGE_IMAP_HOST, CREDFORGE_IMAP_USER, and "
                "CREDFORGE_IMAP_PASSWORD to be set (see .env.example)"
            )
        from ..pipeline.discover_signup import load_generated_recipes, merge_recipes
        from ..redaction import register_secret
        from .imap_email import ImapEmailProvider
        from .playwright_browser import PlaywrightBrowserDriver
        from .signup_recipes import LIVE_SIGNUP_RECIPES

        imap_password = settings.imap_password.get_secret_value()
        register_secret(imap_password)  # before it's ever passed anywhere -- see DECISIONS.md D-043
        email = ImapEmailProvider(
            host=settings.imap_host,
            port=settings.imap_port,
            username=settings.imap_user,
            password=imap_password,
            alias_domain=settings.email_alias_domain,
        )
        # D-065 (DISCOVER_SIGNUP): a generated recipe is loaded from disk and
        # merged in here -- the same PlaywrightBrowserDriver, same
        # `self._recipes.get(domain)` lookup, zero special-casing between a
        # recipe a human wrote and one DISCOVER_SIGNUP generated.
        #
        # D-075: two generated-recipe sources, not one. `settings.data_dir`
        # is THIS process's own ephemeral output (gone on redeploy --
        # Railway's filesystem doesn't persist); `_COMMITTED_EXAMPLES_DIR`
        # is the checked-in copy that survives redeploys, populated only by
        # a human deliberately committing a real DISCOVER_SIGNUP run's
        # output. Committed wins over ephemeral on collision (a
        # deliberately-reviewed recipe over a same-run scratch one), and
        # merge_recipes' own (now-reversed) precedence makes generated
        # (either source) win over hand-authored -- see its docstring.
        generated_recipes = {
            **load_generated_recipes(settings.data_dir),
            **load_generated_recipes(_COMMITTED_EXAMPLES_DIR),
        }
        recipes = merge_recipes(generated_recipes, LIVE_SIGNUP_RECIPES)
        browser = PlaywrightBrowserDriver(recipes=recipes)
    else:
        email = MockEmailProvider(alias_domain=settings.email_alias_domain)
        browser = MockBrowserDriver()

    return ProviderBundle(search=search, fetch=fetch, extractor=extractor, email=email, browser=browser)
