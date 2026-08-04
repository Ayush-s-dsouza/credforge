"""LLM-assisted extraction backed by the real Anthropic API.

Used when ANTHROPIC_API_KEY is set (see providers/factory.py). This module
is only ever imported from inside that conditional branch -- never from a
package __init__ or any eagerly-imported module -- so the `anthropic`
package (an optional extra, `pip install credforge[llm]`) is never required
for the default heuristic-only path, and never errors at import time for
users who haven't installed it.

Uses client.messages.parse() with the exact same pydantic models the
Extractor protocol already returns (DiscoveryExtraction, ClassifyExtraction,
TosGateExtraction) passed as output_format -- no hand-written JSON schema,
no tool definition, and the SDK validates+parses the response for us.

Model choice: claude-sonnet-5, not the Opus-tier flagship. This extraction
call runs once or twice per app across a batch that may eventually cover
hundreds of vendors, and the flagship's cost/latency isn't needed for
"read this docs page and fill in a structured form" -- Sonnet-tier is
Anthropic's documented recommendation for exactly this shape of high-volume
production workload. See DECISIONS.md.
"""

try:
    import anthropic
except ImportError as exc:  # pragma: no cover -- exercised only without the optional extra installed
    raise ImportError(
        "ANTHROPIC_API_KEY is set but the 'anthropic' package isn't installed -- "
        "run `pip install credforge[llm]`"
    ) from exc

from .llm import ClassifyExtraction, DiscoveryExtraction, TosGateExtraction

_MODEL = "claude-sonnet-5"

# Keeps per-app extraction cost and latency bounded regardless of how large a
# real docs page turns out to be -- a docs site's full HTML-to-text dump can
# run to hundreds of KB; we only need the portion that actually discusses
# auth/signup/rate limits, and diminishing returns set in well before that.
_MAX_DOCS_CHARS = 15_000


class AnthropicExtractor:
    def __init__(self, *, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        # Real per-call token usage, for OPS.md's cost/scale section --
        # added specifically so Stage 9's cost numbers are measured, not
        # estimated. Not persisted anywhere; a caller inspects this
        # in-process after a run (see scripts/scratch_run.py-style usage).
        self.calls: list[dict[str, int | str | None]] = []

    def _record(self, kind: str, response) -> None:
        usage = getattr(response, "usage", None)
        self.calls.append(
            {
                "kind": kind,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }
        )

    async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction:
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You are extracting facts about a SaaS vendor's public API "
                        f"from its developer documentation page ({docs_url}). Only "
                        "report what the text actually states; leave a field unset "
                        "rather than guessing. Quote the exact supporting text for "
                        "anything you find as an evidence snippet.\n\n"
                        f"--- docs text ---\n{docs_text[:_MAX_DOCS_CHARS]}"
                    ),
                }
            ],
            output_format=DiscoveryExtraction,
        )
        self._record("discovery", response)
        return response.parsed_output

    async def extract_classification(
        self, *, docs_text: str, docs_url: str, discovery: DiscoveryExtraction
    ) -> ClassifyExtraction:
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Classify this API's authentication scheme from its "
                        f"developer docs ({docs_url}). Valid auth_scheme values: "
                        "oauth2_auth_code, oauth2_client_credentials, oauth2_pkce, "
                        "oauth1, api_key, basic, bearer_static, none, no_public_api. "
                        "auth_scheme answers \"what credential, if any, is there to "
                        "acquire\" -- NOT \"is a credential strictly required for basic "
                        "access.\" Some real APIs permit fully anonymous use but ALSO "
                        "offer a real, optional credential for higher rate limits or "
                        "more features (e.g. NASA's api.nasa.gov: docs explicitly say "
                        "\"you should sign up for a NASA developer key\" -- a concrete "
                        "acquisition instruction) -- classify auth_scheme as that "
                        "credential's real scheme (e.g. api_key) in this case, never "
                        "\"none\". BUT only when the docs show actual evidence a "
                        "credential can be ACQUIRED: a signup/registration page, a "
                        "\"request a key\"/\"contact for access\" mechanism, or an "
                        "example token/key shown in the docs. Merely documenting a "
                        "key's PARAMETER FORMAT for existing customers (e.g. \"apikey: "
                        "String, only required for commercial use, see pricing for "
                        "more information\") is NOT acquisition evidence on its own if "
                        "the docs never say how one would actually obtain that key -- "
                        "that is describing usage for someone who already has a key "
                        "through an out-of-band/sales process, not a credential this "
                        "project could go acquire. Classify auth_scheme=\"none\" in "
                        "that case even though a key parameter is mentioned. Reserve "
                        "auth_scheme=\"none\" for an API with no credential mechanism "
                        "to acquire at all, OR one mentioned without any acquisition "
                        "path described. Separately, report auth_required: \"required\" "
                        "(a credential is mandatory for any access), \"optional\" (the "
                        "API works without one, but a real, ACQUIRABLE credential also "
                        "exists), or \"none\" (matches auth_scheme=\"none\"). A prior "
                        "discovery pass found: "
                        f"auth_scheme_hint={discovery.auth_scheme_hint!r}, "
                        f"has_public_api={discovery.has_public_api}. List any OAuth "
                        "redirect URI requirements and available scopes if the docs "
                        "state them.\n\n"
                        f"--- docs text ---\n{docs_text[:_MAX_DOCS_CHARS]}"
                    ),
                }
            ],
            output_format=ClassifyExtraction,
        )
        self._record("classification", response)
        return response.parsed_output

    async def extract_tos_gate_signals(self, *, tos_text: str, tos_url: str) -> TosGateExtraction:
        response = await self._client.messages.parse(
            model=_MODEL,
            max_tokens=1536,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Read this Terms of Service / developer agreement text "
                        f"({tos_url}) and report, for each flag, only what the text "
                        "actually states -- do not infer from silence. Quote the "
                        "exact clause as an evidence snippet for anything you flag "
                        "true.\n\n"
                        f"--- ToS text ---\n{tos_text[:_MAX_DOCS_CHARS]}"
                    ),
                }
            ],
            output_format=TosGateExtraction,
        )
        self._record("tos_gate", response)
        return response.parsed_output
