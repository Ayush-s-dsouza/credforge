"""Provider protocols: the stable contracts pipeline stages code against.

Concrete implementations (real and mock/fixture) are added stage by stage
(Stage 1: search/fetch; Stage 2: extractor; Stage 5: email/browser). One
factory (providers/factory.py, added in Stage 2) is the single place that
chooses which concrete implementation backs each protocol for a given run.
"""

from .browser import BrowserDriver, ProvisionOutcome, ProvisionStepResult
from .email import EmailMessage, EmailProvider, EmailTimeoutError
from .fetch import FetchError, FetchException, FetchProvider, FetchResult
from .llm import ClassifyExtraction, DiscoveryExtraction, Extractor, TosGateExtraction
from .search import SearchProvider, SearchResult

__all__ = [
    "SearchProvider",
    "SearchResult",
    "FetchProvider",
    "FetchResult",
    "FetchError",
    "FetchException",
    "EmailProvider",
    "EmailMessage",
    "EmailTimeoutError",
    "Extractor",
    "DiscoveryExtraction",
    "ClassifyExtraction",
    "TosGateExtraction",
    "BrowserDriver",
    "ProvisionOutcome",
    "ProvisionStepResult",
]
