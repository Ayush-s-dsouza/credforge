# DECISIONS.md

One entry per meaningful technical choice. Format: what was decided, what
was rejected and why, and what would make it worth revisiting. This is
maintained as study material — terse "we chose X" entries without the
rejected alternative are not useful here.

---

## Stage 0 — Foundations

### D-001: Schema/enum library — pydantic v2 (+ pydantic-settings for config)

**Decided:** pydantic v2 `BaseModel` for all schema types, `StrEnum` for
all enums, `pydantic-settings.BaseSettings` for config.

**Rejected:** (a) plain `dataclasses` + hand-written `__post_init__`
validation — less dependency weight, but the artifact schema needs a
cross-field validator (status/reason_code consistency) that pydantic gives
for near-free via `@model_validator`, and dataclasses would need that
hand-rolled; (b) `TypedDict` + `jsonschema` — lighter still, but loses
static type-checking on field access and requires maintaining a JSON
Schema by hand alongside the Python types, two sources of truth instead of
one.

**Why:** the spec hands us an explicit JSON schema for the handoff
artifact; pydantic v2's `model_json_schema()` gives that almost for free,
and `model_validator(mode="after")` is exactly the right tool for
"status == AUTO must imply reason_code == ELIGIBLE_AUTO"-style invariants.

**Revisit if:** pydantic's validation overhead ever becomes a measured
bottleneck at high concurrency (unlikely at this scale — validating a few
hundred small objects per batch run is not where time goes).

### D-002: Two model families instead of one — `AppPipelineState` (mutable) vs `HandoffArtifact` (frozen)

**Decided:** the per-app working record that stages fill in one field at a
time (`AppPipelineState`, arriving at Stage 1) is a *separate* pydantic
model from the frozen `HandoffArtifact` assembled at EMIT.

**Rejected:** one model used both ways — fields `Optional` during the
pipeline, "finalized" by convention at the end. Rejected because a model
meant to be read as "complete" downstream (by the toolkit-generation
agent) needs every field required and needs to be frozen so nothing
mutates it after handoff; a model meant to be filled in stage-by-stage
needs every field optional and needs to stay mutable. Those are
contradictory requirements on the same class.

**Why:** two small models plus one conversion function
(`emit.py::build_artifact(state)`) is simpler than one model fighting
itself, and the conversion function is a natural place to put the final
validation pass.

**Revisit if:** the duplication between the two schemas grows painful
(e.g. a third, differently-shaped view is needed) — at that point a shared
base/mixin might be worth it.

### D-003: Logging — stdlib `logging` + custom `JSONLFormatter`/`RedactionFilter`, not `structlog`

**Decided:** stdlib `logging`, one `JSONLFormatter` (renders every log
record as one JSON line) and one `RedactionFilter` (scrubs registered
secrets before formatting), both attached to every handler.

**Rejected:** `structlog`, with redaction as a pipeline processor. It's a
more powerful/composable option, and if this were a long-lived production
service I'd lean toward it. Rejected here specifically because a bespoke
~100-line filter is something I can walk through line-by-line and defend
completely, where "we use structlog's processor chain" pushes the actual
redaction logic into a framework I'd be explaining by reference to docs
rather than by reading the code. One fewer dependency too.

**Why:** see D-004 for the redaction mechanism itself.

**Revisit if:** log volume/structure needs grow past what a flat JSONL
line comfortably expresses (e.g. genuinely nested, high-cardinality
structured fields where a processor pipeline's composability would pay for
itself).

### D-004: Secret redaction — two independent layers, not one

**Decided:** (1) `RedactedSecret`, a wrapper type whose `__repr__`/`__str__`
always return a fixed placeholder regardless of what's inside it, and (2)
`RedactionFilter`, a `logging.Filter` that scrubs any value previously
registered via `register_secret()` out of a `LogRecord`'s message, %-args,
and exception text before formatting.

**Rejected:** just the filter alone. A filter is a safety net for secrets
that arrive at the logger as bare strings — it can't help if application
code holds a live secret in a variable and something *other* than logging
prints it (an uncaught exception's `repr()` in a REPL, a debugger, etc.).
`RedactedSecret` closes that gap structurally, at the cost of remembering
to wrap secret values in it at the point they're minted.

**Why both, not one:** the filter catches carelessness (a secret leaking
into a message no one thought to wrap); the wrapper prevents the leak from
being possible in the first place for anything that *does* go through it.
Defense in depth for the one requirement the spec calls out with its own
mandated test.

**Non-obvious implementation detail worth flagging:** `RedactionFilter`
only replaces a logging arg with its scrubbed string form *if scrubbing
actually changed it* — otherwise the original value (and type) passes
through unmodified. Without that check, every `%d`-formatted integer arg
would get coerced to a string by the filter and then blow up at
`record.getMessage()` time with a `TypeError`, because `"%d" % "5"` is not
valid. See `tests/unit/test_redaction.py::test_numeric_args_survive_the_filter_unmodified`.

**Revisit if:** never, short of the spec's own requirement changing — this
is about as small and inspectable as the mechanism can be while still
covering the interpolation, exception, and dict-repr leak paths.

### D-005: Vault — `cryptography.fernet`, single static key from env, ciphertext-only JSON file

**Decided:** one Fernet key loaded from `CREDFORGE_VAULT_KEY` (env var),
vault contents on disk as `{vault_ref: ciphertext}` JSON, never anything
but ciphertext written.

**Rejected:** (a) OS keychain via `keyring` — no file on disk at all,
which sounds strictly better until it needs to run identically on a CI
runner or a headless Linux box without a keyring daemon, at which point it
either silently falls back to something worse or just fails; also much
harder to make deterministic in tests. (b) A per-secret key derived via a
KDF (e.g. scrypt) from a passphrase instead of one static key — stronger
against key-reuse cryptanalysis, but the added complexity (salt storage,
KDF parameter choices, migration if params change) is disproportionate to
the actual threat model of a local dev-credential vault, and the spec
explicitly names `cryptography.fernet` + "key from env" as the intended
shape.

**Why:** matches the spec's explicit ask, portable across any environment
that can set an env var, and trivially deterministic in tests (generate an
ephemeral key per test, never touch a real one).

**What breaks if the key is generated fresh per process instead of loaded
from env and persisted:** every restart makes every previously-vaulted
secret permanently undecryptable. This is the natural mistake to make, and
is exactly why `generate_key()` is a separate, explicitly-named function
from vault construction — it's for bootstrapping the env var once, not for
the vault to call on every startup.

**Revisit if:** this needs to run multi-tenant or shared — a single static
key protecting every vaulted secret is a real single point of failure at
that scale, and per-tenant keys or a real secrets manager (Vault, AWS
Secrets Manager) would be the honest answer.

### D-006: Registry — append-only JSONL, never mutated in place

**Decided:** `AppendOnlyRegistry` writes one JSON line per event (stage
completion, revoke), always via append, never rewrites an existing line.

**Rejected:** SQLite. More capable (transactions, queries, indexes), but
adds a schema-migration story for a table that's really just "one row per
(app, stage, outcome) event," is harder to hand-inspect or diff during a
demo, and — the deciding factor — a corrupted SQLite file is typically
just unusable, while a corrupted JSONL file degrades to "one bad line,"
which is both a more honest failure mode and the one the spec's
"append-only" wording implies.

**Why:** matches spec wording directly; the corrupted-line failure mode
(tested in `tests/unit/test_registry.py::test_corrupted_line_is_skipped_not_fatal`)
is naturally recoverable in a way a corrupted database file wouldn't be.

**What breaks if entries were mutated in place instead of appended:** a
crash mid-write could destroy the *only* record that an account was
already provisioned for a given app, which breaks the "never
duplicate-provision" guarantee the registry exists to provide in the first
place.

**Revisit if:** registry size or query patterns ever justify an index
(e.g. thousands of apps and frequent random-access lookups by
`identity_key` rather than "load everything, filter in memory").

### D-007: Rate limiting — token bucket per registrable domain

**Decided:** `DomainRateLimiter` keeps one `TokenBucket` per registrable
domain (via `tldextract`, offline-configured so it never phones home on
its own), created lazily on first use.

**Rejected:** (a) fixed-window counter ("N requests per rolling 60s
window") — simpler, but bursty at window boundaries, since up to 2N
requests can land in a short span straddling a window edge; (b) a flat
`asyncio.sleep(N)` between every outbound request regardless of domain —
trivial, but means one slow/strict vendor throttles every unrelated domain
in a batch run to its pace, which defeats the point of batching 20
different vendors concurrently.

**Why:** token bucket is the standard well-understood algorithm for smooth
rate limiting with burst tolerance, and per-domain keying is what makes
concurrent batch runs across many vendors actually parallel instead of
serialized behind whichever domain is strictest.

**Revisit if:** true fairness/priority between domains becomes a
requirement (e.g. one vendor's API is business-critical and should never
wait behind a low-priority one) — token buckets alone don't express
priority; that would need a scheduler layer on top.

### D-008: robots.txt handling — fail-closed on any non-2xx/non-404 response

**Decided:** `RobotsCache.is_allowed()` treats a 404 as "no restrictions"
(the industry-standard meaning of a missing robots.txt), a 2xx as "parse
and obey," and *everything else* — connection errors, timeouts, 403,
5xx — as "deny," not "assume allowed."

**Rejected:** failing open (assume allowed) on any fetch problem. Simpler,
and would have been the more common accidental default. Rejected because
the spec says "respect robots.txt for crawling," and assuming permission
when we can't actually confirm the policy inverts that requirement into
its opposite in exactly the cases where it matters most (a vendor
temporarily blocking us, or a network blip).

**Why:** a false "deny" costs one skipped fetch and a HITL/failure path; a
false "allow" is a policy violation. Those aren't symmetric costs, so the
tie-break goes to deny.

**Revisit if:** the false-deny rate in practice turns out high enough
(e.g. flaky robots.txt hosting on some vendor's CDN) that a short-lived
allow-and-retry grace period becomes worth the risk.

---

## Stage 1 — RESOLVE

### D-009: Confidence scoring — a small heuristic formula, not an LLM call

**Decided:** score each candidate domain as
`0.5 * name_similarity + 0.3 * rank_weight + 0.2 * consensus_fraction`,
where `name_similarity` is a `difflib.SequenceMatcher` ratio between the
app-name slug and the domain's label, `rank_weight = 1/best_rank`, and
`consensus_fraction` is the share of search results landing on that
domain.

**Rejected:** (a) handing RESOLVE to the LLM-assisted extraction path
(same mechanism as DISCOVER/CLASSIFY) -- rejected because that budget is
explicitly reserved for the genuinely unstructured task of reading
arbitrary docs pages; picking a domain out of a search-results list is a
much narrower, already-structured problem that a cheap deterministic
formula handles fine, and doing it that way keeps RESOLVE free and fully
deterministic; (b) just trusting the top search result unconditionally --
the spec explicitly requires refusing to guess on ambiguous names like
"Monday," which a top-result-only rule cannot detect at all.

**Why these three weights specifically:** name similarity gets the
largest share because it's the strongest standalone signal for "is this
domain even plausibly the company," but it's not sufficient alone (a
domain can accidentally string-match); rank and consensus are corroborating
signals from the search engine's own relevance ranking, not substitutes
for name similarity.

**What breaks if wrong:** too loose a margin between top and second
candidate silently auto-resolves an ambiguous name to the wrong company;
too strict and nearly everything routes to a human, defeating the point
of automating RESOLVE at all. See `MIN_PLAUSIBLE_CONFIDENCE` /
`AMBIGUITY_MARGIN` in `pipeline/resolve.py`.

**Revisit if:** the seed-list coverage run (Stage 9) shows a systematic
misclassification pattern the weights don't explain -- at that point
tuning the formula against real measured outcomes beats hand-picked
weights.

### D-010: docs-URL selection — prefer a docs/developer subdomain over "first same-domain hit by rank"

**Decided:** among same-domain search hits for the docs-URL query, prefer
one whose *subdomain* is `docs`/`developer`/`dev`/`api` over merely taking
the highest-ranked same-domain result.

**Rejected:** "first same-domain hit by search rank." This was the first
implementation, and it broke on the very first real app tried: running
RESOLVE against real search results for GitHub picked
`github.com/topics/api-documentation` (a topic/tag listing page that
happens to contain "api" in its path) over the real docs, because that
tag page outranked `developer.github.com` and `docs.github.com` in the
raw search results. See TRACE.md for the real run.

**Why:** a docs/developer subdomain is a much stronger structural signal
than a keyword appearing anywhere in a URL's path, and is cheap to check
(one more `tldextract` call, no new provider call).

**Known residual limitation, stated plainly rather than hidden:** on that
same real GitHub run, the fix picks `developer.github.com/changes/5/` (a
changelog subpage) over `docs.github.com/en/rest` (the actual REST API
docs) -- both have a qualifying subdomain, and the tiebreaker (lower
search rank wins) picks the changelog page since it happened to rank
higher. This is a real, imperfect heuristic, not a solved problem.

**Revisit if:** this residual case turns out common across the seed list
(Stage 9 will show it) -- a next refinement would prefer a *shorter path*
among same-subdomain candidates (a landing page over a deep subpage), but
that's speculative until there's evidence it's needed across more than
one app.

### D-011: Cassette design — content-addressed by hash(query) / hash(method+url), not stage-tagged filenames

**Decided:** `providers/cassette.py` keys a cassette file purely by a hash
of the query (search) or `method:url` (fetch), under a per-app directory.

**Rejected:** naming/keying cassette files by `(app, stage, sequence)` --
e.g. `resolve_call_1.json`. Rejected because the same URL can legitimately
be fetched by two different stages (DISCOVER's crawl and GATE's
supplemental ToS fetch might both hit the vendor's docs domain), and
stage-tagging would either duplicate that fixture or require the caller to
know which stage "owns" a given fetch, which the fixture layer shouldn't
need to care about.

**Why:** content-addressing gives correct, automatic dedup for free --
two stages asking the exact same question get the exact same cassette,
which is also just... correct (it's the same real-world fact either way).

**What breaks if wrong:** stage-tagged keys would mean recording the same
URL twice (once "for DISCOVER," once "for GATE") produces two cassette
files claiming two different answers if the vendor's page ever changed
between recordings -- a correctness bug hiding as a filing convention.

**Revisit if:** never expected, but if a single URL's *meaning* genuinely
differs by which stage fetches it (e.g. a stage-specific request header
changes the response), the hash key would need to include that
distinguishing input too.

### D-012: FetchProvider does not raise on non-2xx status codes

**Decided:** `HttpxFetchProvider.fetch()` returns a `FetchResult` with
whatever `status_code` it got (200, 404, 500, ...) for any response that
actually arrived; `FetchException` is reserved for cases where no
inspectable response exists at all (robots-disallowed, timeout,
connection error, invalid URL).

**Rejected:** `response.raise_for_status()` inside the provider, turning
every 4xx/5xx into an exception. Rejected because DISCOVER and GATE
routinely need to *distinguish* status codes rather than treat them all as
failure -- a 404 on a guessed docs URL means "wrong guess, try another,"
not "the fetch failed"; RESOLVE's own `_find_docs_url` relies on exactly
this (a non-2xx candidate docs URL just means `docs_url = None`, not a
crashed stage).

**What breaks if wrong:** if fetch raised on every 4xx, callers would need
to wrap every single fetch in a try/except just to read an ordinary 404,
turning a normal, expected outcome into exception-handling noise
throughout the codebase.

**Revisit if:** a future stage needs "raise on any non-2xx" as its default
behavior -- that would be a second, opt-in method or parameter, not a
change to this one's contract.

---

## Stage 2 — DISCOVER

### D-013: Extraction backend selection is a factory, keyed on env-var presence -- not on `--live`

**Decided:** `providers/factory.py::build_providers()` picks `AnthropicExtractor`
when `ANTHROPIC_API_KEY` is set and `HeuristicExtractor` otherwise -- a
completely independent axis from the `--live` flag that gates
email/browser providers (Stage 5).

**Rejected:** folding extractor selection into the same `--live` switch as
provisioning. Rejected because the two are genuinely independent
questions: "should credforge attempt to actually create accounts and drive
a browser" (a safety/authorization question, `--live`) vs. "is an LLM
available to help read messy docs pages" (a capability/cost question, an
API key). Coupling them would force a user with an Anthropic key but no
intention of provisioning anything to pass `--live` just to get better
extraction, or vice versa.

**Why:** matches how the two capabilities actually vary independently in
practice -- a CI job running research-only dry-runs might have an
Anthropic key but never `--live`; a provisioning run might have `--live`
but no key (falls back to heuristic extraction, still functional, just
less accurate on unusual docs formats).

**Revisit if:** a third extraction backend is added (e.g. a different LLM
vendor) -- at that point this becomes a small provider-selection enum
rather than an if/else on one env var.

### D-014: LLM extraction model -- `claude-sonnet-5`, not the flagship

**Decided:** `AnthropicExtractor` calls `claude-sonnet-5`.

**Rejected:** the Opus-tier flagship. Anthropic's own guidance is to
default to the most capable model unless there's a specific reason not
to -- the reason here is concrete: this call runs once or twice per app,
across a batch meant to eventually cover hundreds of vendors (see
`OPS.md`'s cost section, filled in at Stage 9), and the task itself
("read this docs page, fill in a structured form matching a known schema")
is squarely in the range Anthropic documents Sonnet-tier as handling well
for high-volume production workloads -- not the kind of open-ended,
long-horizon reasoning that justifies flagship cost.

**Rejected also:** Haiku-tier. Rejected specifically because one of the
three extraction calls (`extract_tos_gate_signals`) is a judgment call
with real consequences -- a false negative on `prohibits_automation` means
GATE would route an app to AUTO that should have been HITL, i.e. attempt
automated signup somewhere the vendor's terms actually forbid it. That's
exactly the failure mode this project's Hard Requirements exist to
prevent, and it's not a place to reach for the cheapest tier.

**Why `client.messages.parse()` specifically:** it validates the response
against the exact same pydantic models (`DiscoveryExtraction`,
`ClassifyExtraction`, `TosGateExtraction`) the `Extractor` protocol already
returns -- no hand-written JSON schema to keep in sync, no tool-use
plumbing, and a parse failure surfaces as a normal pydantic validation
error rather than a manually-parsed-and-hoped-for JSON blob.

**Revisit if:** the Stage 9 seed-list run shows the heuristic fallback and
the LLM path disagreeing often enough on `ClassifyExtraction`/
`TosGateExtraction` to warrant a real accuracy comparison between model
tiers -- right now this is a reasoned default, not a benchmarked one.

### D-015: HeuristicExtractor is a real, tested fallback -- not a stub

**Decided:** `HeuristicExtractor` implements all three `Extractor` methods
with genuine regex/keyword logic and is exercised by its own test file,
not left as a `NotImplementedError` placeholder for "the LLM path is the
real one."

**Rejected:** treating the heuristic path as a minimal stub since
`ANTHROPIC_API_KEY` is the "real" path. Rejected because the spec is
explicit that this is a deterministic fallback the system must function
without an API key at all -- a stub that always returns
`has_public_api=False` would silently route every app to `NO_PUBLIC_API`
whenever no key is configured, which is a much worse failure mode than an
imperfect-but-real heuristic.

**Why:** confidence on the heuristic classification path is deliberately
capped at 0.5 (`ClassifyExtraction.confidence`) regardless of how strong
the keyword match looks -- this is a signal to GATE/EMIT that heuristic
classifications are inherently less trustworthy than an LLM's, without
needing a separate "was this heuristic or LLM" flag threaded through the
rest of the pipeline.

**Revisit if:** the fixed 0.5 confidence value proves too coarse once
real seed-list data exists to calibrate against.

### D-016: DISCOVER tries docs-subdomain guesses when RESOLVE's hint fails

**Decided:** if the `docs_url` hint from RESOLVE is missing or fails to
fetch (or fetches something too short to be real docs -- see the failure
drill), DISCOVER tries `docs.<domain>`, `developer.<domain>`,
`developers.<domain>` in turn before giving up.

**Rejected:** treating a missing/failed hint as an immediate
`DISCOVERY_FAILED`. Rejected because RESOLVE's docs-URL confirmation is
explicitly best-effort (D-010) -- an unreachable hint doesn't mean the
vendor has no docs, just that RESOLVE's one search-and-fetch attempt
didn't land on them. A cheap, bounded set of common-convention guesses
recovers a meaningful fraction of those cases for the cost of a couple of
extra fetch calls (still governed by the same rate-limiter/robots choke
point as everything else).

**Why these three guesses specifically:** they're the overwhelmingly most
common developer-docs subdomain conventions in practice (confirmed by
Stage 1's own real GitHub trace, where both `docs.github.com` and
`developer.github.com` were real, live subdomains). Kept to three rather
than an open-ended list to bound worst-case latency/request volume per
app.

**Revisit if:** the seed-list run shows a different convention (e.g.
`api-docs.<domain>`) recurring often enough to be worth a fourth guess.

### D-017: `DISCOVERY_INCOMPLETE` vs `DISCOVERY_FAILED` -- distinguishing "couldn't crawl" from "crawled but got nothing useful"

**Decided:** `DISCOVERY_FAILED` means no usable docs page was ever fetched
(hint + all subdomain guesses failed). `DISCOVERY_INCOMPLETE` means a docs
page *was* fetched and extraction ran, but claimed `has_public_api=True`
while providing neither a `base_url` nor a `validation_endpoint` --
internally inconsistent, and not actionable by GATE or a human without
more digging.

**Rejected:** collapsing both into one `DISCOVERY_FAILED` code. Rejected
because the HITL task file (Stage 4/7) needs to tell a human two very
different things: "the docs URL doesn't work, go find the real one" vs.
"the docs exist and claim an API, but the extraction couldn't pin down
where it actually lives" -- different next steps, different estimated
minutes to resolve.

**Revisit if:** never expected to merge back -- if anything, this could
split further (e.g. a distinct code for "found the docs but the page was
clearly not in English" ) if that turns out to be a recurring case.

---

## Stage 3 — CLASSIFY

### D-018: `has_public_api=False` short-circuits CLASSIFY entirely -- no extractor call

**Decided:** if DISCOVER's extraction says `has_public_api=False`, CLASSIFY
returns `AuthScheme.NO_PUBLIC_API` at confidence `1.0` immediately, without
calling `Extractor.extract_classification()` at all.

**Rejected:** always calling the extractor and letting it decide. Rejected
because "is there an auth scheme" is not a real question once DISCOVER has
already established there's no API to authenticate against -- asking an
LLM (or running heuristics) to classify the auth scheme of a nonexistent
API is a wasted call in every sense: it costs money/latency on the LLM
path and it's just noise on the heuristic path, for a question whose
answer DISCOVER already determined.

**Why this is safe:** unlike RESOLVE's confidence scoring or CLASSIFY's
own low-confidence path below, this isn't a probabilistic judgment call --
it's a direct, deterministic consequence of an already-established fact
from the previous stage. There's no guess being skipped here.

**Revisit if:** DISCOVER's `has_public_api` signal itself proves
unreliable enough (via Stage 9's seed-list run) that CLASSIFY should
double-check it independently -- right now DISCOVER's finding is trusted
as-is.

### D-019: A fixed confidence threshold gates trust, not just the enum coercion

**Decided:** `CONFIDENCE_THRESHOLD = 0.6` in `pipeline/classify.py`. A
classification below it -- or one whose `auth_scheme` string doesn't map
to any `AuthScheme` member at all -- is flagged `CLASSIFY_LOW_CONFIDENCE`
rather than accepted as-is. `auth_scheme` is still set to the best
available guess when there is one (so a human reviewing the HITL task has
something concrete to check), and left `None` only when the extractor's
answer was entirely unparseable.

**Rejected:** trusting whatever the extractor returns outright, on the
theory that "the extractor already has its own `confidence` field, that's
enough." Rejected because a threshold has to live *somewhere* that
actually gates behavior -- a `confidence` field nobody reads is not a
safety mechanism, it's decoration. Putting the threshold in CLASSIFY
(rather than pushing it downstream to GATE) keeps the "is this
trustworthy" judgment next to the code that produced the number.

**What this threshold actually does in practice, observed for real:**
`HeuristicExtractor` always reports exactly `0.5` (D-015), which sits
below `0.6` unconditionally -- meaning every real-auth-scheme
classification made without `ANTHROPIC_API_KEY` configured gets flagged
low-confidence by design, not by accident. The Stage 3 real trace (see
`TRACE.md`) demonstrates exactly why this matters: the heuristic
classified GitHub's real API as `bearer_static` when the correct answer is
`oauth2_auth_code` -- a genuine wrong answer that the confidence gate
caught before it could reach GATE as if it were ground truth.

**Revisit if:** the Stage 9 seed-list run's real LLM-backed classifications
cluster in a way that suggests `0.6` is miscalibrated against the LLM's
own honest confidence reporting (as opposed to the heuristic path, whose
fixed `0.5` is deliberately, unconditionally below threshold and isn't
"recalibration" territory at all).

---

## Stage 4 — GATE

### D-020: "No public API" is checked as a precondition, not as the spec's literal last item

**Decided:** GATE checks, in order: (0) did DISCOVER even find usable
docs at all, (1) does a public API exist at all, (2) did CLASSIFY produce
a confident auth scheme, then finally (a)/(b)/(c) the ToS-derived checks
in the spec's given order, with AUTO only reachable after all of those
clear.

**Rejected:** the spec's literal ordering, which lists "no public API" as
item (d), *after* the ToS/payment/phone checks (a-c). Rejected because
(a)-(c) are all fundamentally questions about *developer-portal signup
gating* -- they presuppose a developer portal exists to gate. Checking ToS
prohibition on automated account creation for an app that has no
developer portal at all is a question that doesn't have a meaningful
answer; there's no signup flow to check terms against.

**Why this is a defensible reinterpretation, not a spec violation:** the
spec's four checks are still all present, in the given relative order,
and every one of them still produces the exact reason code the spec
names. What changed is only *when* the API-existence precondition is
evaluated relative to them -- as a gate on whether the ToS checks are even
meaningful, not as a fifth peer check.

**Revisit if:** a real vendor turns up (Stage 9) whose general
(non-developer) ToS explicitly prohibits automation in a way that should
still surface even for a `NO_PUBLIC_API` app -- that would argue for
checking ToS prohibition before the public-API precondition after all.

### D-021: An unfindable ToS page routes to HITL, not to AUTO

**Decided:** if none of GATE's common-URL guesses for a ToS/developer-
agreement page succeed, the result is `HITL` / `TOS_UNVERIFIABLE` -- not a
pass-through to AUTO on the theory that "no evidence of prohibition was
found."

**Rejected:** treating an unfindable ToS page as equivalent to a clean
ToS. Rejected because the spec is explicit: "Fetch and check the actual
terms; do not assume." Absence of evidence (couldn't find the page) is
not evidence of absence (the terms don't prohibit automation) -- conflating
the two is exactly the kind of assumption the spec calls out by name.

**Why this is the same principle as D-008 (robots.txt fail-closed):** both
cases ask "what do we do when we can't confirm a policy," and both answer
"don't guess permissive." The asymmetry is the same too: a false
`TOS_UNVERIFIABLE` costs one HITL task a human can clear in a couple of
minutes by finding the real ToS URL themselves; a false AUTO on an app
whose ToS actually prohibits automation is the exact failure mode this
whole project exists to prevent.

**What this costs in practice, observed for real:** GATE's guessed paths
for `github.com` initially missed on the first two guesses
(`/developers/terms`, `/developer-terms`) before landing on the real page
at `/terms-of-service` on the third try -- meaning a vendor whose real ToS
sits at a path outside this guess list would currently route to HITL
purely on findability, not on any actual policy signal. That's an honest,
stated limitation, not a hidden one.

**Revisit if:** the Stage 9 seed-list run shows `TOS_UNVERIFIABLE` firing
often enough (vs. genuine gating reasons) that the guess list needs
expanding, or that DISCOVER's own docs-page crawl should be mined for an
in-page link to the real ToS URL rather than guessing paths blind.

**Later finding (Stage 8, D-035):** "found" turned out to need its own
definition. A guessed URL that returns HTTP 200 with a soft-404 body (the
page technically exists, but says it doesn't) was passing this stage's
own reachability check and being treated as a found, clean ToS -- the
exact conflation this entry's own rejected-alternative paragraph already
names, just one layer deeper than "the fetch failed outright." See D-035
for the fix; this entry's principle didn't change, its enforcement did.

### D-022: Heuristic keyword matching uses word boundaries, not bare substrings

**Decided:** `HeuristicExtractor`'s hint matching (`_contains_hint`) uses
`\bhint\b` regex matching everywhere, not Python's `in` substring check.

**Rejected:** substring matching, which is what the original Stage 2
implementation used. Rejected because it's a real, demonstrated bug, not
a hypothetical: running GATE's real ToS check against GitHub's actual
`terms-of-service` page, the substring check on `"captcha"` matched inside
`"octocaptcha_origin_optimization"` -- a feature-flag identifier embedded
in the page -- and produced a false `REQUIRES_CAPTCHA` finding with a
nonsense "evidence" snippet.

**Why word boundaries fix this correctly, not just for this one case:**
`\b` requires a non-word character (or start/end of string) on each side
of the matched hint, so a hint can never match as a sub-word of a longer
identifier -- this generalizes to every hint list in the file (auth
scheme detection, pagination hints, ToS flags), not just the one that
happened to be caught first.

**What this cost to discover:** nothing extra -- it was found by the same
real-data trace this whole build has been running against every stage,
confirming the value of `TRACE.md`'s "no reconstruction after the fact"
rule: a synthetic test fixture would very likely never have included a
feature-flag string like `octocaptcha_origin_optimization` naturally.

**Revisit if:** never expected to regress -- this is a strict correctness
improvement with no real downside (word-boundary regex is negligibly
slower than substring search at these text sizes).

### D-023: Fetched HTML is converted to visible text at the fetch layer, not left as raw markup

**Decided:** `HttpxFetchProvider.fetch()` converts any response whose
content-type contains `html` into visible text (script/style content
excluded, tags stripped, entities unescaped) before returning it as
`FetchResult.text` -- every caller, not just GATE, sees clean text.

**Rejected:** leaving `FetchResult.text` as the raw HTTP response body and
letting each extractor deal with markup itself. Rejected because this
surfaced as a real, user-visible defect, not a theoretical one: GATE's
"evidence" for a real AUTO decision against `github.com` was, before this
fix, the literal string `<!DOCTYPE html>\n<html\n lang="en" ...`  -- a
"quoted snippet" that quotes nothing meaningful, directly failing the
spec's evidence requirement ("the evidence (URL + quoted snippet) that
produced it"). Fixing it per-extractor would mean fixing it three times
(DISCOVER, CLASSIFY, GATE) instead of once, and would leave RESOLVE's own
evidence quoting inconsistent with the rest.

**Why script/style content is explicitly excluded, not just tags
stripped:** a plain tag-strip (regex `<[^>]+>` or similar) would still
leak `<script>` bodies into the "text" -- exactly the kind of
JavaScript/feature-flag noise (`octocaptcha_origin_optimization`) that
motivated D-022's word-boundary fix in the first place. Excluding
`<script>`/`<style>`/`<noscript>` content at parse time closes both
problems from one root cause rather than patching each symptom.

**A genuine, measured side benefit, not just a cleanup:** re-running
Stage 2's real DISCOVER trace after this fix, `base_url` extraction
improved from a markup-artifact match (`https://developer.github.com`,
from a regex hit against raw HTML) to the actually-correct answer
(`https://api.github.com`, found in real visible prose). See `TRACE.md`.

**Revisit if:** a real XML/JSON docs format needs similar treatment --
right now only content-types containing `html` get this conversion.

### D-024: Search stays behind a protocol with two real, swappable backends -- neither is "the" provider

**Decided:** `SearchProvider` (Stage 0) had exactly one real implementation
(`BraveSearchProvider`) until Brave discontinued its free tier in February
2026 and started requiring a credit card with no spending cap even to
obtain a key. `DdgSearchProvider` (backed by the unofficial `ddgs`
package, no key, no billing) is now the **default** in
`providers/factory.py`; `BraveSearchProvider` is used instead whenever
`BRAVE_API_KEY` is set. Nothing above the factory -- not RESOLVE, not
DISCOVER, not GATE -- knows or cares which one is active.

**Rejected:** hardcoding Brave as *the* search backend and treating this
as a one-off patch when it stopped being free. Rejected because vendor
pricing/access policy is exactly the kind of external fact that changes
without warning and has nothing to do with credforge's own logic -- a
protocol boundary that didn't exist yet would have meant threading a
second backend through every call site that touches search, instead of
adding one file and one branch in one factory function. This is the same
argument that justified the protocol in the first place, now validated by
an external change nobody controlled.

**The real tradeoff, stated plainly, not glossed over:**

| | `ddgs` (default) | Brave Search API |
|---|---|---|
| Cost / setup | Free, no key, no billing | Requires a card on file, metered |
| Contract | Unofficial -- no SLA, no documented rate limit, subject to breaking if DuckDuckGo changes its page/API without notice | Official, documented, versioned API |
| Reliability | Throttles aggressively and unpredictably; mitigated with retry-with-backoff (below), not eliminated | Rate limits are documented and predictable |
| Async | No native async client -- wrapped via `asyncio.to_thread` | Native `httpx.AsyncClient` |

Neither is strictly better -- `ddgs` is the right default for anyone who
wants to run credforge without a billing relationship (research/demo/
take-home usage, exactly this project's context); Brave is the right
choice for a production deployment that needs a documented contract to
depend on and can afford the setup. The factory's job is to make that
choice a one-line branch on an env var, not a judgment credforge makes for
the user.

**Why the retry-with-backoff and `SearchProviderError` (not a silent empty
list) are load-bearing, not defensive boilerplate:** `ddgs` has no
documented rate limit to design around, so throttling has to be handled
empirically -- bounded exponential backoff (3 attempts, ~2s/4s base,
jittered) on `RatelimitException` specifically, and every other exception
wrapped immediately with no retry (retrying a non-rate-limit failure
would just be slower, not safer). The reason an exhausted retry raises
rather than returning `[]`: RESOLVE's whole ambiguity-detection logic
(D-009) depends on search results actually reflecting the world. A
throttled search returning `[]` is indistinguishable, from RESOLVE's
point of view, from "this app genuinely has no web presence" -- which
would silently produce a wrong `RESOLVE_NOT_FOUND` instead of a correct
"try again, the search provider was throttled" signal.

**Why the test suite is unaffected by any of this:** every pipeline-stage
test (`test_resolve.py`, `test_discover.py`, `test_gate.py`, ...) is
already written against `FakeSearchProvider`/`Cassette*` fixtures, not
against `BraveSearchProvider` or `DdgSearchProvider` directly (see D-011).
Swapping which real backend is the default changes zero test files --
`test_ddg_search.py` and `test_brave_search.py` are the only two that
touch either concrete implementation, and they mock at the boundary
(`_search_sync`, `respx`) rather than hitting a real network or a real
key. This is exactly what the cassette-fixture architecture was for.

**Revisit if:** `ddgs`'s throttling in practice proves severe enough that
the fixed 3-attempt/~2s backoff isn't enough for a real batch run across
many apps -- at that point a configurable retry budget (rather than a
hardcoded one) would be worth adding.

### D-025: An aggregator/reference-site blocklist excludes known non-vendor domains from candidacy entirely

**Decided:** `_AGGREGATOR_BLOCKLIST` (Wikipedia, the Wayback Machine,
Crunchbase, G2, Capterra, LinkedIn, Product Hunt, Glassdoor, Trustpilot,
Medium, GitHub Pages, app-store listings) is checked before a search hit
is ever allowed to become a `ResolveCandidate`. A blocklisted domain's
hits are dropped from `hits_by_domain` at grouping time -- they can never
win, never appear as an alternate, and never trigger `RESOLVE_AMBIGUOUS`
on their own account.

**`archive.org` was added after the fact, from a second real run, not
guessed up front:** re-testing "Sage" (deliberately, to confirm genuine
ambiguity still works after the fix below) surfaced a Wayback Machine
snapshot of a Wikipedia page about SAGE Publishing as a third competing
candidate -- the identical failure mode as raw `wikipedia.org`, just one
hop removed through an archival mirror. Added immediately, same category,
same reasoning.

**`saashub.com` was added the same way, from the first 20-app seed batch
run:** "OpenWeatherMap" -- an unambiguous, single-vendor app name -- came
back `RESOLVE_AMBIGUOUS`, with a SaaSHub comparison page
(`saashub.com/compare-accuweather-vs-openweathermap`) as a competing
candidate at 0.4202 against the real domain's 0.5625 -- inside the 0.15
ambiguity margin. Same category as G2/Capterra (a SaaS-comparison
directory, not a vendor), same fix: add it and move on. This is the
predicted failure mode this section's own "Revisit if" already named --
the list catching a new aggregator category from real batch data, exactly
as expected, not a surprise requiring new design.

**Rejected:** letting every domain that appears in search results compete
as a candidate on equal footing, trusting the scoring formula (name
similarity + rank + consensus) to naturally rank real vendor sites above
reference sites. Rejected because this was a real, demonstrated failure,
not a hypothetical one: running RESOLVE against real DuckDuckGo results
for `"GitHub"` -- the single least-ambiguous app in the entire seed
list -- returned `RESOLVE_AMBIGUOUS` with `en.wikipedia.org/wiki/GitHub`
as a competing candidate at confidence 0.39, because Wikipedia's SEO
authority is enough to rank well for "<company> official website" queries
regardless of actual relevance to *this specific task* (finding a
developer portal to sign up on).

**Why exclude entirely rather than just down-weight:** a reference site is
categorically not a candidate for "what domain do I create a developer
account on" -- no plausible weighting scheme scores it to zero reliably
across every vendor, and a blocklist is a strictly more legible,
auditable mechanism than tuning a formula to suppress a known-irrelevant
class of result. If Wikipedia should never win, say so directly.

**Why blocklisted hits stay in the result corpus rather than being deleted
outright:** they're removed from `hits_by_domain` (so they can't become a
candidate) but the raw `results` list -- and therefore `total` in the
consensus-fraction denominator -- is unchanged. A real candidate's
`consensus_fraction` is computed against the *actual* number of search
results returned, aggregator hits included, not against an artificially
shrunk pool. This is the (deliberately modest) sense in which a
blocklisted hit still "serves as evidence": its presence in the result set
still shapes the arithmetic surrounding a real candidate's score, even
though it can never become one itself. This build does not implement
anything more elaborate (e.g. parsing a Wikipedia snippet for a mention of
the real domain as corroborating text) -- that's a real possible
extension, not something claimed as done here.

**Revisit if:** the Stage 9 seed-list run surfaces a domain that should be
blocklisted but isn't (a new aggregator category) -- the list is a plain
`frozenset`, trivially extendable, and deliberately not claimed exhaustive.

### D-026: Same-brand TLD variants are merged into one candidate, not left to compete

**Decided:** before scoring, candidate domains are grouped by their
second-level label (`_domain_label`, e.g. `"github"` for both
`github.com` and `github.blog`) into families. All hits across a family's
domain variants are merged into one `ResolveCandidate`, with a single
canonical domain chosen (`.com` preferred; otherwise whichever variant has
the best search rank) -- never two separate, competing candidates for the
same brand.

**Rejected:** treating each registrable domain as an independent
candidate regardless of shared branding, and rejected specifically
because it produced the exact real failure this decision fixes: real
DuckDuckGo results for `"GitHub"` split evidence across `github.com`
(0.75) and `github.blog` (0.625) -- close enough to trip
`RESOLVE_AMBIGUOUS` even though both domains obviously belong to the same
company and nobody would consider them "two different Githubs." The
`AMBIGUITY_MARGIN` check has no way to know that on its own; it just sees
two candidates within 0.15 of each other.

**Why merge rather than just special-case GitHub:** the underlying
pattern -- a vendor's primary `.com` plus a secondary branded property on
a different TLD (`.blog`, `.dev`, `.io`, `.app`, ...) -- is common across
real companies, not a GitHub-specific quirk. Grouping by label rather than
hardcoding known pairs is what makes the fix general.

**Why the merge key is an *exact* label match, not fuzzy similarity:**
this is what keeps genuinely different companies that happen to share a
name (`sage.com` vs `sagepub.com` for "Sage") from ever being
incorrectly folded together -- `"sage"` and `"sagepub"` are different
`_domain_label` values, so they stay separate candidates and a real
`RESOLVE_AMBIGUOUS` for a genuinely ambiguous name is preserved. Confirmed
directly: `test_ambiguous_when_two_candidates_are_close` (the Atlas
two-company case) and `test_docs_url_discovery_is_never_attempted_when_ambiguous`
both still produce `RESOLVE_AMBIGUOUS` after this change, unaffected by
the merge logic, because `"atlascorp"` and `"atlasapp"` never share a
label.

**A closely related, separately load-bearing design point --
`AMBIGUITY_MARGIN` is checked on *unboosted* confidence, before the
docs-URL discovery/boost step ever runs:** this was a deliberate ordering
choice, not an accident of implementation order. A verified docs URL is
real evidence, but it must only ever be able to *rescue* an unambiguous
candidate whose score was too low, never *break a tie* between two
genuinely competing candidates -- otherwise the fix for "GitHub wrongly
flagged ambiguous" could silently become a bug where "Sage" (or any
genuinely two-company name) stops being flagged ambiguous just because one
of the two happens to have a more easily verifiable docs page. This is
directly tested (`test_docs_url_discovery_is_never_attempted_when_ambiguous`
asserts zero fetch calls occur for an ambiguous case, even when every
fetch would succeed if attempted) rather than left as an informal
invariant.

**Revisit if:** a real vendor turns up whose secondary property has a
*different* second-level label entirely (e.g. a rebrand where the old and
new names coexist) -- label-matching wouldn't catch that case, and it
isn't meant to; that's a genuinely different problem (name history, not
TLD variation).

### D-027: docs-URL selection is a ranking problem, not a lookup

**Decided:** `_discover_docs_candidates` collects *every* plausible docs
URL first (both search queries' same-domain hits, plus conventional
subdomain/path guesses) before evaluating any of them, then applies three
independent filters in sequence: (1) hard-exclude anything with a
help/support/community/status/knowledgebase signal, (2) rank what's left
by developer-signal strength (`developer`/`docs`/`api` subdomain or path
beats a neutral one), (3) fetch each in ranked order and accept only the
first whose *content* actually matches API-documentation markers (HTTP
verb + path, "API reference", "authentication"/"authorization",
OpenAPI/Swagger, `curl`, code fences -- at least two distinct categories,
not one incidental mention).

**Rejected:** the original design -- try one search query, take the first
same-domain hit, verify it merely returns 2xx. Rejected because it's a
real, demonstrated failure: real RESOLVE against `"Salesforce"` picked
`help.salesforce.com` (the end-user support portal) as `docs_url`, because
it was the *only* same-domain hit returned for that specific search query
and "reachable" was the only bar it had to clear. DISCOVER then correctly
found no API content there and the whole run ended in `DISCOVERY_FAILED`
-- a real vendor with real, well-documented developer docs
(`developer.salesforce.com/docs`) treated as unsupported because the
*first* URL tried happened to be wrong.

**Why first-match is structurally the wrong shape for this problem, not
just unlucky on this one vendor:** a same-domain hit for a docs-flavored
search query has no guarantee of being *the best* same-domain hit --
search ranking reflects the query engine's relevance model, not "is this
actually developer documentation." Any design that commits to the first
candidate that clears a low bar (2xx) will eventually pick a support
page, a marketing page, or a blog post for *some* vendor. Ranking the full
candidate set and requiring content verification is what makes the
failure mode "the second-best candidate gets picked sometimes" instead of
"the wrong category of page gets picked with no chance to recover."

**Why negative signals are an outright exclusion, not just a scoring
penalty:** a help/support/status page is categorically not developer
documentation, the same reasoning as the aggregator blocklist in D-025 --
no plausible weight makes it acceptable when it's the only candidate
available, which is exactly the situation that produced this bug.

**Why content verification exists as a second, independent check on top of
URL-shape ranking:** a URL's subdomain or path is a strong *prior*, not
proof. A neutral-looking URL can still be real docs (content check alone
would accept it); a `developer.*`-shaped URL can still be a marketing
landing page with no real reference content (URL ranking alone would
wrongly accept it). Requiring both is what makes the acceptance decision
robust to either kind of mismatch between a URL's shape and what's
actually on the page.

**Why the winning URL's acceptance reason is recorded
(`docs_url_reason`):** the spec's evidence requirement ("the evidence...
that produced it") applies to every decision GATE and EMIT will need to
defend, and a docs-URL pick silently accepted with no record of *why* is
exactly the kind of decision that's unauditable later -- especially once
this logic is tuned further and someone needs to understand why a specific
historical run picked what it picked.

**Revisit if:** the seed-list run (Stage 9) shows `_MIN_API_MARKER_HITS =
2` rejecting real docs pages that are unusually short or use non-English
terminology -- the marker patterns are English-only and prose-shaped by
construction, a known limitation stated plainly rather than hidden.

### D-028: DISCOVER needs candidate fallback, not just RESOLVE's best guess

**Decided:** RESOLVE now passes forward a full ranked, content-verified
list of docs-URL candidates (`docs_url_candidates`), not a single URL.
DISCOVER tries them in order; if a candidate is unreachable, or the real
`Extractor` concludes there's genuinely no public API on that page,
DISCOVER moves to the next candidate rather than returning
`DISCOVERY_FAILED` on the first miss. DISCOVER's own conventional
subdomain guesses (D-016) are still appended as a final safety net after
RESOLVE's candidates are exhausted.

**Rejected:** trusting RESOLVE's top-ranked candidate unconditionally now
that D-027 makes it much more likely to be right. Rejected because "much
more likely" is not "guaranteed" -- RESOLVE's content-verification check
is a cheap, generic heuristic (keyword/pattern matching against page
text), while DISCOVER's real extractor (LLM-assisted or heuristic, see
Stage 2) does a genuinely more careful read of the same page. A page can
pass RESOLVE's coarse filter and still turn out, on closer reading, not to
be real API documentation for this specific vendor. Treating RESOLVE's
pick as final would reintroduce a narrower version of exactly the failure
D-027 fixed -- just with a higher bar to clear before failing, not a
recovery path once it's cleared incorrectly.

**Why the retry criterion is `has_public_api is False`, specifically, and
not "any imperfection":** DISCOVER should retry when a candidate is
*wrong* (no real API there at all), not merely *incomplete* (found a real
API page missing some optional field) -- retrying on incompleteness would
mean burning additional real network and possibly LLM calls chasing a
"perfect" candidate for a question (missing fields) that D-029 has
separately decided isn't worth blocking on at all. Once a candidate
confirms a real API, DISCOVER stops there, imperfections and all.

**Why "how many candidates were tried" is recorded in `DiscoveryResult.detail`
rather than only implied by a bare `DISCOVERY_FAILED` code:** a human
looking at a failed run needs to know whether DISCOVER gave up after one
attempt or five before deciding whether the candidate-generation logic
itself needs work versus this specific vendor genuinely has no
machine-discoverable docs.

**Revisit if:** a real vendor's docs sit behind a candidate list long
enough that trying all of them meaningfully hurts batch latency -- see
OPS.md's Salesforce latency note for the same underlying tension (more
thoroughness costs more wall-clock time) and its proposed mitigation
(parallelize candidate probes rather than trying fewer of them).

### D-029: DISCOVERY_INCOMPLETE does not block AUTO -- incompleteness is carried forward, not escalated

**Decided:** GATE explicitly does not treat `DISCOVERY_INCOMPLETE` as a
precondition that routes to HITL, unlike `DISCOVERY_FAILED` (which still
does). Instead, GATE computes a `completeness_gaps` list -- every expected
`DiscoveryExtraction` field DISCOVER couldn't populate (`base_url`,
`developer_portal_url`, `rate_limit_notes`, `pagination_style_hint`,
`validation_endpoint`), each with a stated reason -- and attaches it to
its result regardless of what status that result ends up being. A gap
never changes the status/reason_code decision on its own.

**Rejected:** routing any app with `DISCOVERY_INCOMPLETE` to HITL, which
is what would have happened by default if GATE's precondition check had
been written to mirror `DISCOVERY_FAILED`'s handling exactly (both are
"DISCOVER didn't get everything," so treating them identically was the
easy, un-examined default). Rejected on a stated principle, not just a
convenience: **HITL exists for things a human must unblock -- a
prohibition, a payment wall, a phone-verification gate, a page nobody can
even find. A missing field in prose is not in that category.** A human
can't do anything about `base_url` being absent from a docs page's prose
that the docs page itself doesn't do (it's not there to find by looking
harder) -- the actual next step is for the downstream toolkit-generation
agent to derive it from concrete endpoint examples elsewhere in the same
reference docs, which is exactly the kind of derivation a human reviewer
wouldn't do any better than that agent already does.

**Why this matters beyond this one field:** routing incompleteness to
HITL inverts the metric this whole project exists to improve. The
project's stated purpose is to replace manual pre-work with automation
wherever a human isn't actually required; every `DISCOVERY_INCOMPLETE`
app routed to HITL "to be safe" is manual work re-introduced for a
question a human has no special ability to answer better than the
artifact's own downstream consumer. `AUTO` coverage measured against a
policy that escalates on imperfection, not just on real blockers, would
systematically undercount how much of this can actually run unattended.

**Why the gap is still recorded, not just silently accepted:** "doesn't
block AUTO" is not "doesn't matter." The downstream toolkit-generation
agent inherits an artifact that's missing a field it might reasonably
expect to be there; discovering that gap by field name up front (in
`completeness_gaps`) is categorically better than discovering it by a
`KeyError`-shaped surprise at build time. This is the same "evidence and
reasons belong in the artifact, not just in a log somewhere" instinct
behind every other reason-code and evidence field in this project --
completeness gaps are a first-class, structured part of the handoff, not
an afterthought.

**Where this is decided:** at GATE, explicitly, now -- not deferred to
EMIT (Stage 7). The instruction that produced this decision was specific
about that: decide the *policy* (does this block AUTO, and what gets
carried forward) at the stage that owns AUTO/HITL/UNSUPPORTED decisions,
so EMIT's job stays "assemble the artifact from what earlier stages
already decided," not "also decide policy that arguably belongs
upstream."

**Revisit if:** a specific missing field turns out, empirically (Stage 9's
seed-list run), to correlate with a downstream toolkit-generation failure
often enough that it should be promoted to a real blocking condition --
that would be a deliberate, evidence-based escalation of one specific
field, not a reversal of the general principle.

### D-030: Fetch responses are streamed and size-capped, never buffered-then-decoded

**Decided:** `HttpxFetchProvider` no longer calls `self._client.request(...)`
and then `response.text` on whatever comes back. It uses `self._client.stream(...)`,
checks `Content-Length` up front and rejects before downloading a single
body byte if the server declares a size over `max_response_bytes` (default
5 MB, `CREDFORGE_MAX_RESPONSE_BYTES`), then reads via `aiter_bytes()` with a
running total checked on every chunk, aborting mid-stream the moment the cap
is crossed. The decode step itself (`_decode_body`, bytes -> str using the
Content-Type charset, falling back to utf-8) is wrapped in a `try/except`
catching `MemoryError` and `UnicodeDecodeError` and converting either into a
typed `FetchException(reason="response_too_large"/"decode_error")` rather
than letting it propagate. `_raw_fetch_for_robots` goes through the same
streaming/capped path -- a robots.txt fetch is still a fetch of a
third-party URL, and there's no reason to trust it more than any other page.

**Found by:** a real crawl against a live third-party page that died with
an unhandled `MemoryError` inside `httpx_fetch.py`, traced to `response.text`
decoding an arbitrarily large buffered body. Nothing upstream of that line
had any notion of a size limit -- `HttpxFetchProvider.fetch()` would
buffer and decode whatever bytes the server chose to send, full stop.

**Rejected:** relying on `response.iter_bytes()`'s buffering combined with
httpx's own default `Content-Length`-based limits -- httpx does not impose
one; it will happily buffer a multi-gigabyte body if the server sends one
and the caller awaits `.text`/`.content`. Also rejected: checking only
`Content-Length` and skipping the mid-stream cap. `Content-Length` is what
the server *claims*, and it describes the wire size, not the decoded size
-- a small gzipped body can still decompress into gigabytes (a "zip bomb"
in miniature). httpx's `aiter_bytes()` yields already-decompressed chunks,
so capping cumulative bytes read from that iterator catches amplification
that a Content-Length-only check would miss entirely. Both checks are kept:
the `Content-Length` check is a cheap, mandatory-nothing rejection that
skips the network read outright for a server that's honest about size; the
streaming check is the one that actually holds under an amplifying or
lying server.

**Why the cap belongs in the fetch provider, not at each call site:**
`HttpxFetchProvider.fetch()` is already the single choke point every
stage's fetch goes through -- robots.txt and rate-limiting live here for
exactly the same reason (see the module docstring and D-023). RESOLVE,
DISCOVER, and GATE all fetch arbitrary third-party URLs (search results,
guessed subdomains/paths, ToS pages) that credforge does not control and
has no reason to trust. Pushing a size check into each of those three
call sites would mean three chances to forget it, three slightly different
implementations, and a fourth stage added later that forgets it entirely.
One cap, enforced once, at the only place that actually reads bytes off
the wire, is the same reasoning that put rate-limiting and robots.txt
compliance here instead of in `resolve.py`/`discover.py`/`gate.py`.

**Why this is a general availability risk, not a one-off bug:** any
process that fetches arbitrary third-party URLs -- which is what a
research/crawling tool fundamentally does -- is exposed to this class of
failure by construction, whether the oversized response is a
misconfigured server, a deliberately hostile one, or just a very large
real page (a rendered SPA bundle, a changelog with years of entries). The
fix is not "handle this one bad URL"; it's "assume every URL might be
this one," which is what a cap enforced unconditionally, on every fetch,
actually buys.

**Consequence for callers:** a single oversized or amplifying URL now
degrades to one failed candidate (`FetchException` with a specific,
loggable reason) instead of taking down the whole process -- consistent
with the batch-resilience principle everywhere else in this project (one
app's failure never kills a run; the same logic now applies one level
down, to one fetch never killing an app).

## Stage 5 — PROVISION

### D-031: PROVISION is the first stage with durable side effects; mocked by default, an idempotency guard first, a recipe-gated real browser driver

**Decided, in three linked parts:**

**(1) `provision()`'s signature is deliberately different in shape from
every earlier stage.** RESOLVE/DISCOVER/CLASSIFY/GATE are pure research
functions: given providers and inputs, they return a result, with no
observable effect beyond the network calls made along the way. PROVISION
is not -- it creates a real developer account and must remember that it
did, durably, across process restarts. So it alone takes `vault`,
`registry`, `run_id`, and `settings_fingerprint` in addition to the usual
provider protocols. This is a real, visible architectural seam between
"research stages" and "the one stage that mutates the world," not an
oversight to be smoothed over -- a caller reading the signature can tell
which kind of stage this is without reading the body.

**(2) The idempotency guard runs before anything else, unconditionally.**
`provision()`'s first line is `registry.find_open_provision(identity_key)`
-- before `email.alias_for()`, before touching the browser at all. If a
non-closed `vault_ref` already exists, it returns `already_provisioned`
immediately. This was specified as a hard invariant at Stage 0 (OPS.md's
"what happens if it dies mid-batch" section, D-006) before PROVISION
existed to implement it; this stage is where that promise becomes real
code, not new scope.

**(3) `PlaywrightBrowserDriver` requires an explicit per-vendor
`SignupRecipe` (CSS selectors); no recipe -> `PROVISION_FAILED`, never a
best-effort guess.**

**Rejected (1):** giving PROVISION the same narrow signature as the
earlier stages and reaching for a module-level global or a class instance
to hold the vault/registry. Rejected because it would hide a real
dependency behind implicit state -- the exact thing this project's
provider-Protocol pattern (every stage takes its providers as explicit
parameters, never reaches for a global; see D-024's "neither provider is
hardcoded" framing for the same instinct applied to search) exists to
avoid everywhere else. An explicit, wider parameter list is more honest
about what this stage actually needs than pretending it fits the same
shape as GATE.

**Rejected (2):** checking for an existing provisioned account only at
the orchestrator level (Stage 9), leaving `provision()` itself naive and
re-provisioning on every call. Rejected because the guard's whole value is
that it holds *even if the orchestrator never gets built or is bypassed*
(e.g. this scratch-testing phase, where `provision()` may be called
directly). Pushing the check up a layer would make correctness depend on
every caller remembering to do it first -- exactly the kind of thing that
gets forgotten once, in production, against a real vendor, which is a
duplicate live account, not a test failure.

**Rejected (3a) -- attempting generic, vendor-agnostic form-filling
(guessing common field names/ids, or an LLM-driven "find the email field
and type into it" loop):** this is a real, open problem in web automation,
not a small gap to wave off. A guess that's wrong doesn't fail loudly --
it can silently type an app name into an email field, or click the wrong
submit button, and a signup form is exactly the kind of place where a
wrong click has a real-world side effect (an account created with garbage
data, a support ticket generated, in the worst case a real charge
attempted). Claiming to solve this generically would be dishonest about
what's actually been built.

**Rejected (3b) -- hardcoding per-vendor logic as Python `if domain ==
...` branches inside the driver itself:** works, but couples the driver's
code to every vendor it knows about, makes adding a new vendor a code
change + review + deploy instead of a data change, and makes the
"vendor not supported yet" case implicit (falls through to some default
branch) instead of an explicit, structured absence (`recipes.get(domain)`
returning `None`). A `SignupRecipe` dict is the same information,
externalized to data.

**Why mocked by default rather than real-by-default with tests
mocking it out:** this mirrors the locked scoping decision from the
original build plan (Provisioning stubbed by default, `--live` swaps in
the real thing) -- `MockEmailProvider`/`MockBrowserDriver` are not test
doubles that only exist inside `tests/`, they're the actual default
runtime behavior, satisfying the same `EmailProvider`/`BrowserDriver`
Protocols `ImapEmailProvider`/`PlaywrightBrowserDriver` do. This keeps
`credforge run <app>` safe to run against real vendors without creating a
real, unwanted account by accident -- provisioning a real account is
something an operator opts into with `--live`, never a side effect of
running the tool at all.

**Why the vault write happens before the registry append, specifically:**
a crash between the two leaves an orphaned vault entry -- harmless, never
referenced by anything, silently ignorable. The reverse ordering would
leave a registry entry that claims a `vault_ref` which was never actually
written; the idempotency guard (which trusts `find_open_provision()`'s
result) would then hand back a `vault_ref` that `vault.retrieve()` raises
`VaultRefNotFoundError` on. One ordering fails safe; the other doesn't.

**Revisit if:** a real `SignupRecipe` gets written against a real vendor
(Stage 9+, needs actual `--live` testing against a throwaway account) and
turns up a step this design doesn't account for yet -- e.g. a CAPTCHA, a
multi-page signup flow, or a verification link that must be visited
(`PlaywrightBrowserDriver`'s `verify_email` step currently only *waits
for* the message via `EmailProvider`, not yet click-through the link
inside it -- called out as a known next step in the code itself, not
silently skipped).

## Stage 6 — VALIDATE

### D-032: Credential validation is HTTP-status-code classification, and it never honors a non-GET validation_endpoint

**Decided, in two linked parts:**

**(1) Status classification is a small deterministic heuristic
(`_classify_status`), not an LLM call.** 2xx -> valid. 401 -> bad
credential, or expired if the response body contains the word "expired"
(word-boundary-safe, same `\b...\b` pattern D-022's fix already
established for hint matching elsewhere in this codebase). 403 ->
insufficient scope. 404 -> wrong base URL. 429 -> rate limited. Anything
else -> unknown.

**(2) VALIDATE always issues GET, regardless of what HTTP method
DISCOVER's `validation_endpoint` field names.** The leading method token
(if present) is stripped and discarded, never honored --
`_resolve_check_url` only ever uses it to find where the path starts.

**Rejected (1):** an LLM call to interpret the response and decide
validity/failure-reason. Rejected for the same reason D-009 rejected an
LLM call for RESOLVE's confidence scoring: HTTP status codes are already
an effectively standardized, enumerable signal across virtually every
real-world REST API (401/403/404/429 mean roughly the same thing
everywhere) -- an LLM call would add latency and cost to reclassify
information that's already structured and cheap to pattern-match
directly. Body inspection is used only for the one case status code alone
can't resolve (expired vs. merely-invalid, both surfacing as 401 on most
vendors), and even there it's a plain regex, not a model call.

**Rejected (2) -- honoring whatever method `validation_endpoint`
specifies (i.e. actually sending a POST if that's what was extracted):**
this is not a hypothetical risk. DISCOVER's real, live extraction against
Salesforce (see TRACE.md/OPS.md's Stage 2-4 sections) returned
`validation_endpoint: "POST /contacts/v1/contacts"` -- a *create-a-contact*
endpoint, because that's genuinely the cheapest concrete example
Salesforce's own docs prose gave the extractor to point at, not a mistake
in the extraction. If VALIDATE replayed that method literally, every
credential check against that app would create a real contact record in
whatever Salesforce org the credential belongs to, as a side effect of
"just checking the credential works." That is precisely the kind of
unrequested write this project's repeated fail-safe pattern (robots.txt
fail-closed on ambiguity, the response size cap, the bounded/documented
ToS-URL guess list) exists to prevent -- do the safe minimal thing, and
say so explicitly, rather than trust an upstream field that was never
guaranteed to be read-only in the first place.

**Why not widen `FetchProvider.fetch()` to support POST instead, gated
behind some "I really mean it" flag on the caller:** considered and
rejected as unnecessary scope for what VALIDATE actually needs to prove
("this credential is accepted"), which a read-only endpoint answers just
as well as a write endpoint would, without the side-effect risk. Nothing
about `FetchProvider`'s Protocol (`method: Literal["GET", "HEAD"]`, fixed
since Stage 1) needed to change for this stage at all -- a genuine
"this stage doesn't need what it might look like it needs" outcome, not
an oversight.

**Where "no validation_endpoint" or "a relative path with no base_url to
resolve it against" show up:** both return `VALIDATION_FAILED_UNKNOWN`
with a specific `detail` string rather than raising. The second case is
an expected, not exceptional, situation -- D-029 already established that
`base_url` can legitimately be missing from an otherwise-AUTO app
(`completeness_gaps`, not a blocker). VALIDATE has to degrade gracefully
here, not assume every AUTO app arrives with every field populated.

**Revisit if:** a real vendor's only cheap, safe validation path turns
out to require a specific non-GET, side-effect-free method (rare, but
e.g. some GraphQL-only APIs expose introspection only via POST) -- that
would be a deliberate, narrow allowlist extension (a documented list of
"POST is safe for these specific known-idempotent GraphQL introspection
queries"), not a reversal of "never trust the extracted method blindly."

## Stage 7 — EMIT

### D-033: EMIT is synchronous, the status/reason_code invariant is enforced in the model not just in emit.py, and credential is structurally impossible on a non-AUTO artifact

**Decided, in three parts:**

**(1) `build_artifact()` is a plain synchronous function**, the first
stage in this project that isn't `async def`. Every earlier stage
(RESOLVE through VALIDATE) does real I/O -- search, fetch, an LLM call, a
browser, an inbox poll -- and is `async` because of it. EMIT does none of
that: it's a pure transformation over data every earlier stage already
computed into `AppPipelineState`. The signature itself documents this --
a reader doesn't need to open the file to know EMIT touches no network or
disk.

**(2) The `status`/`reason_code` consistency invariant lives on
`HandoffArtifact.model_validator`, not as a check inside `emit.py`.**
`HandoffArtifact` cannot be constructed at all with `status=AUTO` and any
`reason_code` other than `eligible_auto` (symmetric checks for
`UNSUPPORTED`/`no_public_api`, and for `HITL` never carrying either
singleton value) -- this is exactly the invariant D-001 named, all the way
back at Stage 0, as the reason pydantic was chosen over dataclasses in the
first place. This stage is where that promise gets kept, not new scope.

**(3) `credential must be None unless status=AUTO` is also a model-level
invariant, not just an emit.py convention.** `emit.py` never even
attempts to construct a `CredentialInfo` unless `state.provision.status ==
"provisioned"` (which itself only happens after an AUTO gate, per D-031),
so in normal operation the model-level check never fires -- it exists as
a second, independent line of defense: even a future bug in `emit.py`
that mistakenly tried to attach a credential to a HITL/UNSUPPORTED
artifact would fail loudly at construction, not silently produce a
handoff artifact that hands a downstream agent a working credential for
an app a human hasn't cleared yet.

**Rejected (2) -- validating status/reason_code consistency only in
`emit.py`, as a plain `if` before returning:** would work for the one
call site that exists today, but it's a weaker guarantee than "this
object cannot exist in an inconsistent state" -- any other code path that
ever constructs a `HandoffArtifact` directly (a test fixture, a future
CLI command that loads one back from disk with `model_validate_json` and
re-saves it, Stage 9's `report` command) gets the check for free instead
of needing to remember to re-run it. The model is the single source of
truth for "is this a valid artifact," not a convention every caller has
to honor.

**Rejected (3) -- letting `emit.py`'s own conditional logic be the only
thing preventing a credential from leaking onto a non-AUTO artifact:**
same reasoning as (2), specifically applied to the one field where a bug
would be worst: a `CredentialInfo` leaking onto an artifact a human is
supposed to review first isn't a cosmetic inconsistency, it's a
credential handed to a downstream agent before GATE's human-review gate
was actually satisfied. This is exactly the kind of invariant worth
paying for twice.

**Rejected -- computing `api`/`credential`/`validation` presence from a
single explicit "how far did this run get" enum on `AppPipelineState`,
rather than checking each field's own state (`state.gate is None`,
`state.provision.status == "provisioned"`, `state.validation is not
None`):** an explicit progress enum is one more piece of state that has
to be kept in sync with reality by every stage that touches it -- a stage
that populates its result but forgets to advance the enum produces a
subtly wrong artifact. Deriving presence from the actual populated
fields means there's nothing to forget to update; the state IS the
progress marker.

**Why `api.docs_url` is kept even for an `UNSUPPORTED` artifact:** a
`NO_PUBLIC_API` verdict is DISCOVER successfully crawling a real page and
concluding there's no API there -- the page it looked at is itself
evidence supporting the verdict, not something to discard just because
the outcome was negative. Only `credential`/`validation` are gated on
`status == AUTO`, because those specifically represent state changes
(a real account created, a real credential checked) that only happen
after a human-review gate has been cleared, unlike `api`, which is just a
record of what DISCOVER found.

**Revisit if:** Stage 9's orchestrator introduces a scenario where
`build_artifact()` needs to run on a state where GATE completed but a
later stage crashed mid-write in a way that leaves `state.provision`
populated with contradictory data (e.g. `status="failed"` but a stray
`vault_ref` somehow set) -- current behavior trusts `state.provision.status`
as the single source of truth for whether to treat a `ProvisionResult` as
real, which should already handle this, but worth confirming against a
real chaos-suite scenario once Stage 9 exists to write one.

## Stage 8 — REPORT

### D-034: REPORT is a pure aggregation over already-emitted artifacts; a corrupted artifact file is skipped, not fatal

**Decided:** `build_report(run_id, artifacts)` is a pure function (list of
`HandoffArtifact` in, `RunReport` out, zero I/O) -- same split as EMIT
(D-033), for the same reason: the aggregation logic (count by status,
count by reason_code, build the HITL attention list) is fully unit-
testable without touching a filesystem. `load_artifacts()`/
`generate_report()` do the actual file I/O (`.credforge/runs/<run_id>/
artifacts/*.json` in, `report.json` out) and are the only two functions
in this file that know a filesystem exists.

REPORT never re-runs any earlier stage to gather its numbers -- it only
reads what EMIT already wrote to disk. A run that was interrupted before
EMIT finished for some apps simply reports on however many artifacts
exist yet; it does not attempt to reconstruct or estimate the rest.

**Rejected:** letting a single corrupted/unreadable artifact file raise
and abort the whole report. Rejected on the exact same principle as
`AppendOnlyRegistry.load_all()` skipping a corrupted registry line
(D-006): a single bad file is data about that one file, not a reason to
withhold the summary for every other app that emitted cleanly.
`load_artifacts()` counts and logs the skip (`skipped_files` on the
report) rather than silently dropping it, so a report that's missing
data says so rather than looking complete.

**Why `needs_attention` exists as its own list, not just
`reason_code_counts`:** a count answers "how many," not "which ones."
The whole point of REPORT is to make a batch actionable for the human
who has to clear the HITL apps -- a count alone would still require
opening every artifact file by hand to find out which apps and why.
`needs_attention` carries `identity_key`, `reason_code`, and the
completeness-gap field names directly, sorted by `reason_code` so
same-cause apps sit together, because that's the shape a human triaging
a batch actually scans.

**Revisit if:** Stage 9's CLI wants `report.json`'s shape to also drive a
rendered console table (`rich.table`, the same library `scratch_run.py`'s
batch mode already uses) -- `RunReport` as a plain pydantic model is
already the right input for that, no format change needed, just a
presentation layer on top.

### D-035: GATE's ToS-page check now rejects soft-404s -- HTTP 200 is necessary but not sufficient for "found"

**Decided:** `_find_tos_page()` no longer accepts a guessed URL just
because it returned `2xx` with enough text (`_MIN_USABLE_TEXT_LENGTH`).
It also checks the body doesn't match a small set of soft-404 markers
("page not found", "we can't find", "doesn't exist", "no longer
available", "404 error", "404 not found") via plain substring match on
the first 500 characters. A guess that fails this check is treated
exactly like one that returned a network error or a 404 status: skipped,
try the next guess.

**Found live, running Spotify through the real end-to-end pipeline
(TRACE.md Stage 8):** GATE's first ToS guess,
`https://spotify.com/developers/terms`, returned a genuine HTTP `200`
with body text starting "Page not found... This page is out of tune...
We can't find the page you're looking for." -- Spotify's actual branded
404 page, served with a 200 status (common enough on JS-rendered sites
that the server can't distinguish a valid client-side route from an
invalid one at the HTTP layer). The old `_find_tos_page()` had no way to
tell this apart from a real ToS page: it was 200, and it had well over
200 characters of text. The extractor was then handed this text, found
no prohibition/payment/phone signals in it (correctly -- there aren't
any, because it isn't ToS content), and GATE cleared Spotify to `AUTO`
having never actually reviewed real Terms of Service.

**Why this is a correctness bug, not a coverage gap (compare to the
RESOLVE/DISCOVER findings logged-not-fixed in OPS.md's seed-batch
section):** this is not a case of "GATE could do more here." D-021
already established the operating
principle -- *absence of evidence (couldn't find the page) is not
evidence of absence (the terms are clean); when in doubt, HITL, never
AUTO.* A soft-404 is a page that, in every sense that matters, was **not
found** -- the site simply says so in prose instead of via a 404 status
code. Accepting it as "found" was always a bug relative to D-021's
already-stated intent, exposed by real content rather than introduced by
this fix. It happened to surface on the exact stage/finding this
document was being written for, which is why it's fixed immediately
rather than deferred like the RESOLVE/DISCOVER findings from the
20-app batch (OPS.md) -- a soundness bug in the AUTO-clearance path
outranks a coverage gap in RESOLVE's docs-path guessing, given what AUTO
is trusted to mean.

**Rejected:** word-boundary regex matching (`\bmarker\b`), the fix D-022
used for the `captcha`/`octocaptcha` collision. Rejected here because
the soft-404 markers are multi-word phrases ("page not found," "we can't
find") -- there's no realistic way for "page not found" to appear as an
unwanted substring inside an unrelated word the way a single token like
"captcha" can. Plain substring matching is simpler and loses nothing for
phrases this specific.

**Rejected:** an LLM call to judge whether a fetched page is "really" a
ToS page before extracting signals from it. Rejected for the same D-009/
D-032 reasoning already applied twice in this project: a soft-404 is
already a cheaply, deterministically detectable pattern (a fixed, short
list of common phrases) -- adding a model call to decide something a
substring check already decides correctly is cost and latency with no
accuracy gain for this specific, narrow judgment.

**This was tested against its own blind spot, immediately, not assumed
fixed:** re-running Spotify live after the first marker set landed showed
the fix was real but incomplete -- the first guess (`/developers/terms`)
was correctly skipped, but GATE fell through to `/legal/developer-terms`,
which turned out to be a *second*, differently-worded Spotify soft-404
("Oh no! ... This page got tripped up ... there was an error and we
couldn't load the page") that the first marker list didn't catch, so
Spotify still cleared to a false `AUTO` on the second guess. Fetching all
seven guessed paths directly confirmed **all seven** are one of these two
soft-404 templates -- Spotify's real developer ToS is not at any
conventional guess path at all. Markers for the second template were
added, and the live re-run then correctly landed on `HITL` /
`TOS_UNVERIFIABLE`. Left in the record as what it is: the first patch
looked complete after one live check and wasn't -- a second live check is
what actually confirmed it, which is the entire reason this project
re-verifies live rather than trusting a fix after the first green run.

**Revisit if:** a real vendor's soft-404 copy doesn't match any of the
current markers and slips through -- extend the list (same "grow the
list from real evidence" pattern as D-025's aggregator blocklist), not a
redesign, as a first response. But the Spotify evidence above also
surfaced a strictly more general signal worth real consideration before
the next keyword gets added by hand: **all seven of Spotify's guesses
returned one of only two distinct response bodies.** A same-run,
content-based dedup check ("if this guess's body matches one already
seen for an earlier guess in this same `_find_tos_page()` call, treat it
as the site's generic catch-all, not a distinct real page") would have
caught both templates with zero vendor-specific keywords, and generalizes
to soft-404 copy no one has seen yet. Not implemented now because it has
a real false-negative risk of its own -- a vendor that legitimately
serves identical content at two aliased real URLs (e.g. `/terms` and
`/tos` both rendering the same actual ToS) would incorrectly have the
second one rejected -- and resolving that tradeoff properly (e.g. only
treating a match as a catch-all once 3+ guesses collapse to the same
body) deserves its own design pass, not a rushed addition under the
finding that motivated it. If soft-404s keep showing up across the
Stage 9 seed-list run, that's the signal to build it properly.

## Stage 9 — Capstone

### D-036: The CLI is a thin wiring layer over pipeline/orchestrator.py; resumability is deliberately scoped down under the Stage 9 time budget

**Decided:** `pipeline/orchestrator.py::run_app()` is the only place that
sequences RESOLVE -> ... -> EMIT; `cli/__init__.py`'s five commands
(`resolve`, `run`, `batch`, `report`, `revoke`) do argument parsing,
provider/vault/registry setup, and printing -- no pipeline logic of their
own. `--explain` was built in and left in (it turned out to be
essentially free: every stage has accepted an `ExplainSink` since it was
written, so wiring it was one `ConsoleExplainSink` class and a bool flag).

**Two deliberate scope reductions under the 2.5-3 hour Stage 9 budget,
stated explicitly rather than silently shipped as if they were the full
original design:**

1. **`settings_fingerprint` is a coarse hash over two fields** (`live`,
   `resolve_confidence_threshold`), not a fingerprint that captures every
   setting that could affect every stage's decision, which is what the
   original plan described. It's enough for PROVISION's own idempotency
   guard (D-031, which is the one place a fingerprint is actually
   consulted) to work correctly; it is not a complete per-stage resume
   system.
2. **`batch`'s skip-already-done check is keyed on exact `app_name` string
   match** against completed `EMIT` registry entries, not on
   `identity_key` (the canonical, spelling-independent identity the
   registry and PROVISION's own guard use everywhere else). Two different
   spellings of the same app in the same CSV would each run and provision
   independently -- the full two-level-identity dedup the plan originally
   specified isn't implemented at the batch-skip layer, only inside
   PROVISION itself (which *is* identity_key-keyed, and is the guard that
   actually matters most: it's the one preventing a duplicate *live*
   account, not just a duplicate research pass).

**Rejected:** building the full per-stage settings-fingerprint and
identity_key-based batch dedup as originally scoped. Rejected purely on
time -- both are real, sound designs, not corners cut for lack of a
better idea -- and cutting them here (a resumability nicety) was chosen
over cutting seed-batch coverage, TRACE.md, or the README, which the time
budget explicitly prioritized higher.

**Revisit if:** a real batch run is re-run over a CSV with genuine
spelling duplicates ("GitHub" and "Github Inc" in the same file) and
double-provisions -- that's the concrete scenario that would justify
building the identity_key-based version instead of the name-based one.

### D-037: DISCOVER must distinguish "no candidate was ever readable" from "every candidate was read and genuinely has no public API"

**Decided:** `discover()` now tracks the last `DiscoveryResult` produced
by a candidate that was actually fetched and extracted, even when that
extraction said `has_public_api=False`. After exhausting every candidate,
if at least one such result exists, it's returned as-is (`reason_code=None`,
`extraction.has_public_api=False`) instead of `DISCOVERY_FAILED`.
`DISCOVERY_FAILED` is now reserved for the case where literally no
candidate ever yielded readable content at all.

**Found while writing `tests/pipeline/test_orchestrator.py`, not via a
live run:** a test meant to exercise GATE's `UNSUPPORTED` path through
the real orchestrator (not a hand-built `DiscoveryResult`, as
`test_gate.py`'s existing unit test does) failed -- `discover()`'s D-028
fallback logic retries the *next* candidate on `has_public_api=False`
unconditionally, and after exhausting every candidate it always returned
`DISCOVERY_FAILED`, regardless of whether real content had actually been
read and judged. There was no code path by which `discover()` could ever
return a successful result with `has_public_api=False`. Since GATE's
`UNSUPPORTED`/`NO_PUBLIC_API` branch is only reachable when
`discovery.extraction.has_public_api` is `False` on a *non*-failed
`DiscoveryResult`, that branch was unreachable through the real pipeline
end to end -- every genuinely-no-public-API vendor would instead land on
`DISCOVERY_FAILED` -> `HITL`, misrepresenting a fully-decided "this app
cannot be automated" case as "credforge couldn't find the docs, ask a
human to look."

**Why this mattered enough to fix immediately, mid-Stage-9, rather than
log and move to the seed batch run as planned:** the seed batch run's
entire purpose is to *measure* the real status distribution for the
README. Running it against this bug would have produced a coverage table
with zero `UNSUPPORTED` apps no matter what was in the seed list (the
seed list specifically includes an app expected to land there), silently
converting a real, meaningful category into `HITL` noise. Fixing a
measurement-corrupting bug before taking the measurement isn't optional
scope -- it's a precondition for the measurement being honest.

**Rejected:** distinguishing the two cases by inspecting *why* each
candidate was rejected (unreachable vs. too-short vs. real-content-no-api)
and only falling back to `DISCOVERY_FAILED` if literally every rejection
was "unreachable." Rejected as unnecessary complexity for what a single
`last_no_api_result` variable already captures correctly: the only
distinction that matters for GATE's decision is "was any content ever
actually read," not the specific reason every unread candidate failed.

**Revisit if:** the seed-list run shows this new `has_public_api=False`
path firing on a vendor that plausibly does have a public API RESOLVE/
DISCOVER just couldn't find (as opposed to a genuine no-API vendor like
Superhuman) -- that would suggest the docs-candidate list itself is too
narrow for that vendor, not that this fix's logic is wrong.

### D-038: `DISCOVERY_FAILED` flows into GATE, not around it -- every app that's run gets an artifact

**Decided:** `orchestrator.py::run_app()` no longer returns early when
`discovery.reason_code == DISCOVERY_FAILED`. It now passes through to
`gate()` (with a placeholder `ClassifyResult` that `gate()`'s own
precondition check never actually inspects -- see the code comment and
`test_discovery_failed_short_circuits_with_zero_fetch_calls`), which
already has a correct, tested branch for exactly this case: `HITL` /
`DISCOVERY_FAILED`, zero fetch calls. `build_artifact()` then runs
normally, producing a real `HandoffArtifact`, a written artifact file,
and an `EMIT`/`completed` registry entry -- same as every other outcome.

**Found running the seed batch for real, immediately after fixing D-037:**
the first full 20-app batch run through the new CLI showed 7 apps as
"stopped before GATE" with no artifact and no line in the aggregate
report at all -- silently absent, not counted as a failure of any kind.
Tracing why: `run_app()` special-cased `DISCOVERY_FAILED` with an early
`return state, None`, written by copying the pattern from
`scripts/scratch_run.py` (an interactive debugging script, where "stop
and print what happened so far" is exactly the right UX) into the
orchestrator (where it's the wrong UX -- a batch run needs every app to
resolve to *some* recorded outcome, not silently vanish from the count).
This is the same category of bug as D-037, found minutes after fixing
D-37: a stage's own correctly-designed precondition logic
(`gate()`'s `DISCOVERY_FAILED` branch, in this case) made unreachable by
an orchestration layer that short-circuits before ever calling it.

**Why this couldn't wait for a "logged, not fixed" treatment like the
OPS.md seed-batch findings from Stage 4/8:** those findings were about
*quality* of a result RESOLVE/DISCOVER already produced (a docs-path
guess list that's too narrow, a scoring formula that under-scores
platform-nested products). This is about *coverage of the measurement
itself* -- the seed batch run's entire purpose, right now, is to produce
real numbers for the README. A bug that makes some fraction of apps
silently invisible to that count isn't a quality nit to note for later;
it's a defect in the instrument being used to take the measurement,
exactly like D-037. Both were fixed before trusting any number that
came out of this batch run.

**What still legitimately produces no artifact, and why that's different:**
a RESOLVE failure (`resolved=False`) still returns early with no
artifact. Unlike DISCOVER, GATE has no precondition for "RESOLVE never
completed" -- its signature doesn't take a `ResolveResult` at all, only
`discovery`/`classify`, and extending GATE's contract to cover a stage
two steps upstream of it is a real design change, not a one-line
routing fix, and wasn't undertaken under this stage's time budget.
This is a known, stated gap, not a silent one: RESOLVE-stage failures
(`resolve_ambiguous`/`resolve_low_confidence`/`resolve_not_found`/
`malformed_input`) are undercounted in `RunReport`'s totals, tracked
only in the CLI's console output ("stopped before GATE"), not in
`report.json`. See the README's coverage section for the real count this
produced in the seed batch, stated explicitly rather than folded
silently into a total that would misrepresent what was measured.

**Revisit if:** RESOLVE-stage failures turn out to be a large enough
fraction of real seed-list runs that leaving them out of `RunReport`
materially misleads whoever reads it -- at that point, extending GATE
(or, more cleanly, extending EMIT to build a minimal artifact directly
from a failed `ResolveResult` without involving GATE at all, since GATE's
own checks are moot when there's no docs to check) is the real fix, not
a routing patch like this one was.

### D-039: EMIT builds an artifact directly from a failed RESOLVE, without involving GATE -- every app that's run gets one

**Decided:** `build_artifact()` now has two entry paths, checked in order:
if `state.gate is not None`, build from it as before (`_build_from_gate`);
else if `state.resolve is not None and not state.resolve.resolved`, build
a minimal artifact directly from the failed `ResolveResult`
(`_build_from_failed_resolve`) -- `status=HITL`, `reason_code` copied
straight from RESOLVE's own reason code (`resolve_ambiguous`/
`resolve_low_confidence`/`resolve_not_found`/`malformed_input`), `api`/
`credential`/`validation` all `None` (there's nothing to report --
DISCOVER/CLASSIFY/GATE never ran), and `evidence` built from RESOLVE's
`alternates` list (one `EvidenceItem` per candidate it considered, so a
human reviewing this artifact can see *why* it was ambiguous or
low-confidence, not just that it was). Only if neither a GATE result nor
a failed RESOLVE result is available does `build_artifact()` still raise
`EmitError`. `orchestrator.py::run_app()` no longer returns `(state,
None)` on a RESOLVE failure -- it calls `build_artifact()`, persists the
artifact, and appends the same `EMIT`/`completed` registry entry every
other outcome gets.

**Found the same way D-038 was, by the same reasoning, one stage
earlier:** D-038 fixed `DISCOVERY_FAILED` silently producing no artifact
by routing it into GATE's already-correct precondition. RESOLVE failures
had the identical bug, one stage further upstream, for the identical
reason -- `run_app()`'s early `return state, None` on `not
state.resolve.resolved` was written the same way, copying the same
"stop and print what happened" pattern from the interactive scratch
script. This was already named explicitly as the next thing to fix in
D-038's own "Revisit if" note, not a surprise.

**Rejected:** extending GATE's own signature to accept an optional
`ResolveResult` and check it as a new first precondition, so the
*existing* GATE-based path could handle this case too. Rejected because
GATE's actual checks (ToS review, payment/verification signals) are
categorically moot when there's no resolved domain to check anything
about -- adding a parameter to GATE that 95% of its real invocations
would never use, just to reuse `_build_from_gate`'s formatting, would
make GATE's contract wider for no real benefit. A RESOLVE failure and a
GATE verdict are different kinds of terminal outcomes; EMIT is the right
place to reconcile that difference (it already builds `HandoffArtifact`
from whatever shape of state it's given), not GATE.

**Why `resolved=True` in this branch is asserted away, not handled:**
`_build_from_failed_resolve` is only ever reached via `build_artifact()`'s
own `not state.resolve.resolved` check, so `state.resolve.reason_code`
is guaranteed non-`None` by `ResolveResult`'s own contract ("reason_code
set only when resolved is False"). The `assert` documents that guarantee
at the point it's relied on rather than silently trusting it.

**What this fixes concretely:** a `credforge batch` run over N apps now
always produces N artifacts (or, if an app fails at DISCOVER/RESOLVE, N
artifacts covering every reachable outcome) -- verified directly by
`test_a_batch_of_n_apps_always_produces_n_artifacts`, not just by the two
individual-stage tests. Before D-038/D-039, a batch's true completeness
depended on which stage each app happened to fail at, invisibly.

**Revisit if:** a future stage between RESOLVE and DISCOVER is added and
also needs this same "terminal failure still gets an artifact" treatment
-- at that point, a shared `_build_from_failure(state, *, status,
reason_code, evidence)` helper is probably worth extracting instead of a
third near-identical private function in `emit.py`.

### D-040: GATE collects vendor-policy signals from every source it has, not just the dedicated ToS page; TOS_UNVERIFIABLE ranks lowest

**Decided:** GATE now scans up to two real sources for the same seven
`TosGateExtraction` flags (prohibition, payment, business verification,
sales contact, phone verification, CAPTCHA, SSO-only), not one:
DISCOVER's already-crawled docs page (`discovery.docs_text` -- always
scanned when present, zero extra fetch cost, already in hand) and the
dedicated ToS/developer-agreement page (scanned if `_find_tos_page` finds
one, as before). `_blocking_gate_result` now takes a list of
`(source_label, source_url, TosGateExtraction)` tuples, checks every flag
across every source (first source with that flag true wins, evidence
attributed to that source), and applies the spec's mandated precedence
order across the *union* -- not per source, not on the first source
scanned. `TOS_UNVERIFIABLE` moved to dead last in the actual control
flow: it's returned only after the combined signal check comes back
clean AND the dedicated ToS page specifically couldn't be found. A signal
found on the docs page alone -- even with the ToS page totally
unfindable -- now produces its real, specific reason code, never
`TOS_UNVERIFIABLE`.

**Rejected: leaving `TOS_UNVERIFIABLE` as an early return based solely on
"was the ToS page found," with no other signal source consulted at
all** (the pre-D-040 behavior). This was flagged directly: a vendor's own
developer docs page routinely states pricing or verification requirements
in plain prose -- "you must be on a paid plan to access the API,"
"contact sales for API access" -- often more directly than a legal ToS
document does. Under the old control flow, if `_find_tos_page`'s seven
URL guesses all missed (a real, already-measured ~15% of the seed batch),
GATE would report `TOS_UNVERIFIABLE` even when a real, actionable
blocking signal was sitting unread in text GATE had already fetched for
DISCOVER. That's a detection failure masking a real finding, not a safe
fallback -- the two are not the same kind of "couldn't confirm AUTO,"
and conflating them was exactly the same category of bug the ToS-check
stage had already been corrected for once (D-035's soft-404 fix), just
one layer up: D-035 fixed "a page that responds isn't necessarily a page
that's really there"; D-040 fixes "the absence of one source's signal
isn't the absence of every source's signal."

**Rejected: keeping `_blocking_gate_result`'s original single-extraction
signature and calling it twice (once per source), returning on whichever
call finds something first.** This would still be "return on the first
hit" one level up -- if the docs-page call happened to return clean and
the ToS-page call was never reached because of some earlier-return
structure, a real ToS-page signal could still be missed depending on call
order. Collecting every source's full flag set into one list *before*
applying precedence is what actually guarantees the highest-precedence
real signal wins regardless of which source it came from or which
source was scanned first -- proven directly by
`test_docs_page_prohibition_outranks_tos_page_payment_signal_across_sources`
and its mirror-image test with the sources swapped.

**Cost, stated plainly:** this is one additional real LLM call
(`extract_tos_gate_signals` on `discovery.docs_text`) on every single app
that reaches this point in GATE, not just the ones where the ToS page is
unfindable -- a real, deliberate cost increase, not a free refactor. Given
`extract_tos_gate_signals`'s real measured size (~2,045 input / ~91 output
tokens per call, from TRACE.md's GitHub run), this adds roughly $0.004-
$0.006/app at current Sonnet pricing -- judged worth it because the
alternative is a known, already-measured class of missed finding, and the
existing GATE-reached apps in the 20-app seed batch already number in the
teens, not the thousands, where this would start to matter.

**What stays exactly as before, and is still tested for it:** a real
`TOS_PROHIBITS_AUTOMATION` still outranks a real `REQUIRES_PAYMENT` when
both fire, whether both come from the same source (the original
`test_tos_prohibition_outranks_payment_when_both_signals_fire`, untouched)
or from two different sources (the new cross-source tests). Precedence
ordering itself did not change -- only which sources feed into it.

**Revisit if:** the Stage 9 seed batch, re-run with this fix, shows
`VENDOR_BLOCKED` (a real payment/verification/CAPTCHA/prohibition finding)
appearing where it was previously masked by `TOS_UNVERIFIABLE` -- that's
the concrete signal this fix was built to produce, and the number is
worth reporting explicitly rather than assumed.

## Stage 9+ — Real `--live` provisioning

### D-041: Account-level and API-level credentials are separate, typed fields -- never one generic `vault_ref`

**Decided:** the credential shape changed everywhere it appears
(`ProvisionOutcome`, `RegistryEntry`, `ProvisionResult`, `CredentialInfo`)
from one generic `vault_ref: str` to explicit, named fields split by what
they're actually for:

- **Account-level** -- the developer-portal login credforge itself
  created, if the vendor's flow has one at all: `account_email` (plain,
  not secret -- just an address) and `account_password_ref` (a vault
  ref -- a real secret). Both `None` together means this vendor's flow
  has no login/password concept whatsoever (NASA's key-by-email flow),
  which is a real, distinct state from "we have one but something went
  wrong."
- **API-level** -- what the integration actually authenticates requests
  with: `api_key_ref`, `client_secret_ref`, `bearer_token_ref` (all vault
  refs), and `client_id` (plaintext -- OAuth client IDs are meant to be
  public, so vaulting one would be security theater, not more security).

**Why this was the priority, ahead of everything else queued for this
stage:** the deliverable this whole project exists to produce is a
credential, not an eligibility verdict. `AUTO` meaning "provisioning was
never attempted, mocked, or ambiguous about what got created" undersells
the thing being handed to a downstream agent. `AUTO` now means "a real
credential was acquired and its shape is explicit in the artifact" --
account vs. API, secret vs. not-secret, all named, not one opaque string
a consumer has to guess the meaning of.

**Rejected:** one `vault_ref` per provisioned app, with the *shape* of
what it points to implied by `credential_type` alone. This is what the
codebase had through Stage 8, and it was adequate when "provisioned"
meant a single mocked client_id/secret pair with no distinct account
identity at all. It stops being adequate the moment PROVISION creates a
*real* account with its *own* login separate from the API credential the
account then generates -- exactly NASA/OpenWeatherMap's real shape (sign
up with an email+password to get a dashboard identity; the API key is a
second, later artifact of that account, not the same secret). Cramming
both into one ref would force a downstream consumer to know, out of
band, which vault entry means "log into the vendor's website" versus
"authenticate an API request" -- the exact ambiguity this change removes.

**Why PROVISION generates the account password, not the browser driver:**
"generate a strong password" is a pure, vendor-agnostic operation --
`secrets.token_urlsafe`, no browser, no per-vendor knowledge needed. Only
*whether to use it* is vendor-specific (does this signup form have a
password field at all). Keeping generation in `provision.py` and letting
`ProvisionOutcome.account_password_used` be the driver's yes/no-and-echo
keeps `SignupRecipe`/`PlaywrightBrowserDriver` focused on the one thing
that's genuinely per-vendor (which fields exist, in what order), not
duplicating password-generation logic into every recipe.

**Why "already_provisioned" now builds a full `CredentialInfo`, where it
previously built none at all:** a real bug, found while making this
change, not a hypothetical -- the old `emit.py` only checked
`state.provision.status == "provisioned"`, so re-running an app whose
credential already existed (the idempotency-guard reuse path) produced
`credential: null` in the artifact, even though a real, valid credential
existed in the vault the whole time. Fixed as part of this same change
since the new field set made the gap visible; not a separate stage.

**Revisit if:** a vendor's real flow needs a credential shape none of
`api_key_ref`/`client_id`+`client_secret_ref`/`bearer_token_ref` covers
(e.g. a signed JWT, a certificate) -- extend with another named field
when a real recipe needs it, not speculatively now.

### D-042: SignupRecipe declares its own `credential_type`; credential extraction from a page and from an email body are two distinct, explicit mechanisms

**Decided:** `SignupRecipe` now requires `credential_type: CredentialType`
(the old code hardcoded `OAUTH2_TOKEN_PAIR` for every recipe, which was
never actually true for anything but the mocked path) and supports two
mutually-exclusive credential-extraction mechanisms, chosen per recipe,
not guessed at runtime: `client_id_selector`/`client_secret_selector`/
`api_key_page_selector` read the credential off a page after signup;
`api_key_email_regex` extracts it from the verification email's body
instead, for vendors (NASA) that deliver the credential purely by email
with no page to scrape at all. `first_name_field_selector`/
`last_name_field_selector` and `password_field_selector`/
`password_confirm_field_selector` were also added -- real form fields
NASA's and (expected) OpenWeatherMap's actual signup forms have that the
original recipe model, built before any real vendor was inspected,
didn't anticipate.

**Found by actually inspecting a real form, not by guessing ahead of
time:** `api.nasa.gov`'s signup widget is injected by third-party
JavaScript (`api.data.gov`'s embed script) with no static HTML to read --
fetching the page's raw source shows only a loading placeholder. Real
selectors (`#user_first_name`, `#user_last_name`, `#user_email`,
`#api_umbrella_signup_form`) were only visible after actually rendering
the page with Playwright and dumping the live DOM. This is the concrete
form of "no guessing at unknown forms" this stage was built around --
every selector in the NASA recipe was read off the real, rendered page,
not inferred from convention.

**Why email-based extraction is a distinct mechanism, not a special case
of page-based extraction:** NASA's form has no password field, no login,
and the response after submit is just a "check your email" message --
there is no page state to scrape a credential from at all. Trying to
force this through the existing `client_id_selector`-style page-reading
path would mean pointing a selector at nothing. `api_key_email_regex`
makes the actual mechanism (wait for the message, extract by pattern)
explicit in the recipe itself, so a reader of `SignupRecipe.NASA` (or
whichever variable holds it) can tell which flow they're looking at
without reading `playwright_browser.py`'s control flow first.

**Rejected:** keeping the recipe model minimal and hardcoding
NASA-specific logic into the driver (`if domain == "api.nasa.gov":
...`). Rejected for the exact reason D-031 already rejected this pattern
for signup generally -- it recreates the "generic automation that
secretly isn't generic" problem one layer down, coupling the driver's
code to a specific vendor instead of externalizing that knowledge into
data. The whole point of the recipe pattern is that adding a vendor is a
data change, not a code change; special-casing one vendor in the driver
would be the exact regression that pattern exists to prevent.

**Revisit if:** OpenWeatherMap's real, inspected form (next) needs a
mechanism neither `client_id_selector`-style page extraction nor
`api_key_email_regex` covers -- e.g. a verification *link* that must be
clicked before the key ever appears anywhere, rather than a key
delivered directly in the email body. That's a real, different
mechanism from what NASA's flow needed and should be added when (and if)
OpenWeatherMap's actual form is confirmed to need it, not assumed now.

### D-043: Every real secret --live introduces is registered for redaction the moment it exists, and `scrub_secrets()` extends that protection past `logging`

**Decided:** three concrete secrets are now registered with the
process-wide redaction registry (`register_secret`, D-004) at the exact
point each first exists, before it's passed anywhere else: the real IMAP
password (`factory.py`, the moment `settings.imap_password` is read to
build `ImapEmailProvider`), the generated account password
(`provision.py`, immediately after `_generate_account_password()`
returns, before it's handed to the browser driver), and each real
API-level secret a live signup flow returns (`api_key`/`client_secret`/
`bearer_token`, registered individually as each is read out of
`outcome.raw_api_credential`, before vaulting). `client_id` is
deliberately never registered -- it isn't secret (OAuth client IDs are
meant to be public), so redacting it would just make debug output
harder to read for no real protection.

A second function, `scrub_secrets(text) -> text`, was added alongside the
existing `RedactionFilter`. `RedactionFilter` is a `logging.Filter` --
it only ever sees output that goes through the `logging` module.
`--explain`'s `ConsoleExplainSink` never did: it prints straight to
`rich.console.Console`, bypassing `logging` entirely, so a secret
reaching an `ExplainEvent.message` would have sailed past
`RedactionFilter` untouched. `scrub_secrets()` exposes the exact same
scrubbing logic `RedactionFilter` already uses internally, callable
directly by any surface that isn't `logging`-based -- `ConsoleExplainSink.emit()`
now calls it on every message before printing.

**Why this couldn't wait to be discovered by an actual secret leaking
first:** `--live` is the first point in this project where a real
secret (a real Gmail app password, a real generated account password, a
real vendor API key) exists in the process at all -- every earlier
stage's "secrets" were either mocked strings or never touched logging in
a way that mattered. The moment real secrets entered the picture, the
redaction guarantee this project has claimed since Stage 0 (D-004) had
to actually hold for them, not just for the hypothetical secrets the
Stage 0 test used. Checking `--explain` specifically (not just
`logging`) mattered because `--explain` is precisely the feature built
to print verbose, human-readable detail about what each stage is
doing -- exactly the kind of surface a secret is likely to end up on by
accident (a well-meaning `detail=f"...{token}..."` in some future stage),
and exactly the surface a user is likely to have on and be watching
during a `--live` run, since that's when they'd want to see what's
happening.

**Rejected:** routing `--explain` output through the `logging` module
instead of adding a second scrubbing entry point, so `RedactionFilter`
alone would cover both surfaces. Rejected because `--explain` and
`logging` serve different purposes with different audiences --
`--explain` is real-time, human-facing narration of *this run*, printed
to the console the operator is already watching; `logging`'s JSONL
output is a structured record for later inspection/tooling. Conflating
them so one mechanism could reuse the other's filter would couple two
things that should stay independently designed. A five-line pure
function shared by both call sites is a smaller, more honest fix than
reshaping `--explain`'s whole output path to fit through a
`logging.Filter`.

**Verified directly, not just asserted:** `.env` was confirmed never
committed by checking `git rev-list --all` (0 commits exist in this
repo's entire history, on any ref) rather than trusting `.gitignore`
alone -- a `.gitignore` entry only prevents *future* accidental commits;
it says nothing about whether a secret was ever actually committed
before the ignore rule existed. For a repo with real history, the
correct check is `git log --all -- .env` (any commit that ever touched
the path, on any branch) or `git log --all --diff-filter=A --name-only`
(any commit that ever added it), not a `.gitignore` read.

**Revisit if:** a real vendor's live recipe needs a secret shape none of
the three registration points here covers (e.g. a 2FA backup code, a
signed assertion) -- register it at the point it's minted, following the
same pattern, not retrofitted after the fact.

### D-044: VALIDATE tries both real conventional placements for API_KEY -- query parameter first, then header

**Decided:** `auth_scheme == "api_key"` now tries two request shapes in
order before giving up: `?api_key=<key>` as a query parameter, then
`X-API-Key: <key>` as a header. It moves to the second only if the first
attempt specifically comes back as a bad-credential rejection (401) --
any other outcome (a real 2xx, a 404, a rate limit, a network error) is
returned immediately, since a different placement of the same key
wouldn't change a wrong-base-URL or rate-limit outcome.

**Found immediately before the first live `--live` run, not after a
wasted one:** about to validate a real NASA API key for real, a check of
NASA's own actual API convention (`exampleApiUrl:
'https://api.nasa.gov/planetary/apod?api_key={{api_key}}'`, visible in
the page's own signup-widget config) showed the key belongs in a query
parameter -- `_build_auth_headers`'s original `api_key` branch only ever
sent `X-API-Key`, a header NASA's real API doesn't look at at all.
Running VALIDATE unchanged against a real, genuinely valid key would
have produced a false `VALIDATION_FAILED_BAD_CREDENTIAL` -- not because
the credential was bad, but because VALIDATE was checking in the wrong
place. Caught by checking the real vendor's documented convention before
spending a real signup+email cycle on a misleading result, not by
running it once and being surprised.

**Rejected:** leaving `api_key` as a single-header attempt and accepting
that some real vendors would validate incorrectly. Rejected because this
isn't a rare edge case -- query-parameter API keys are extremely common
across real public APIs (this is precisely why NASA's own example URL
uses one), at least as common as header-based ones. A validation stage
that's wrong for a large, common class of real vendors isn't a
acceptable known limitation; it's a stage that doesn't do its one job
for a predictable fraction of the apps it'll see.

**Rejected:** trying every plausible placement (query param under
several different names -- `api_key`, `key`, `apikey`; multiple header
names) exhaustively. Rejected as unbounded guessing, the same category
of thing D-031 already declined to do for signup forms and D-032 already
kept narrow for method selection. Two placements, both independently
confirmed as real, common conventions (not guessed), is the same
"bounded, evidence-based list" discipline as the ToS-URL guesses and the
docs-subdomain guesses elsewhere in this codebase -- not "try
everything until something works."

**Why query-param is tried first, not header:** no principled reason to
prefer one over the other in the abstract, but query-param happens to be
the convention of the first real vendor this project actually validated
a credential against (NASA) -- ordering the bounded list by "most
recently confirmed real" rather than an arbitrary a priori guess.

**Revisit if:** a real vendor's API_KEY convention needs a placement
neither variant covers (e.g. a custom query param name, a cookie) --
extend the bounded list when a real recipe's VALIDATE run needs it, same
as every other guess-list in this project.

### D-045: `checked_url` must never carry the raw credential; found from a real leak during NASA's first live VALIDATE run

**Decided, in two parts, both found in the same live run:**

**(1) VALIDATE now scrubs every stored `checked_url`/`detail` string
through `scrub_secrets()` before returning it, never the raw request URL
directly.** D-044's query-param variant (`?api_key=<key>`) embeds the
real credential directly in the URL used for the HTTP request; that same
URL was being stored verbatim in `ValidateResult.checked_url`, which
flows straight into `HandoffArtifact.validation.checked_url` --
un-vaulted, and not guaranteed to pass through `logging`'s
`RedactionFilter` at all (it's a plain model field, serialized by
`model_dump_json()`, not a log line). Every credential value passed into
`validate()` is now also registered with the redaction registry
(`register_secret`) the moment `validate()` receives it, as defense in
depth alongside the URL-scrubbing itself.

**(2) `register_secret()` now silently ignores any value shorter than 8
characters, rather than registering it.** Found by the same live run,
one step later: fixing (1) and then running the existing test suite
broke `test_network_failure_is_wrapped_not_propagated`, whose fixture
credential was the single character `"t"` -- registering it turned
`scrub_secrets()`/`RedactionFilter` into a filter that redacts every
letter "t" in *any* subsequent string, including unrelated text like
"connection_error" (-> "connec***REDACTED***ion_error"). A real
credential this project ever generates or accepts is nowhere near this
short (generated account passwords are 24+ characters; every real
API-level secret seen so far, including NASA's real issued key, is 40);
a value this short reaching `register_secret()` is test data, not a
usable secret, and letting it through was a real, demonstrated way for
the redaction system to corrupt unrelated output.

**Found live, in this exact order, not hypothesized or found by code
review:** running the real, first-ever `--live` VALIDATE call against
NASA's actual API (see TRACE.md) printed the artifact's `checked_url`
straight to the terminal -- and it contained NASA's real, freshly-issued
API key in plain text, embedded in the query string. This was caught
immediately (before the key was ever persisted to a file -- the demo
script only printed, never called `build_artifact()`), fixed, and while
fixing it, a *second* real bug surfaced from the test suite itself
(the redaction guard). Both are fixed in the same change because the
second was only discoverable by actually attempting the first fix and
running the tests -- exactly the "real testing surfaces real bugs, fix
immediately, verify live" pattern this whole project has followed since
Stage 4, now applied to a bug the pattern itself helped find, not one a
human reviewer flagged first.

**Why this is more serious than the earlier VALIDATE findings (D-032,
D-044) despite being found the same session:** those were both about
VALIDATE's *classification* being wrong or incomplete -- a real, but
contained, correctness gap. This was a real secret, minted for a real
external account, written in plain text to a place the whole vaulting
design (D-041) exists specifically to prevent. Had this run gone through
the normal CLI path (`credforge run ... --live`, not the standalone demo
script used to first exercise the mechanism) instead of a script that
only prints, the plaintext key would have been written to
`.credforge/runs/<run_id>/artifacts/*.json` on disk, defeating the
purpose of vaulting it in the first place. Treated as a real incident
for this session's own key: NASA's real issued key was visible in
terminal output before this fix landed, and revoking/reissuing it (NASA
allows requesting a new key freely) was called out explicitly to the
user rather than assuming a low-stakes free-tier key makes the exposure
not worth mentioning.

**Rejected:** relying on `register_secret()` + `RedactionFilter`/
`scrub_secrets()` alone, without also actively scrubbing `checked_url`
at the point it's constructed. Rejected because registration only
protects surfaces that actually call `scrub_secrets()` or go through
`logging` -- a plain pydantic field written straight to a JSON file via
`model_dump_json()` is neither. Belt AND suspenders here specifically:
active scrubbing at the one call site known to embed a secret in a URL,
plus registration as a backstop for every other surface that might
reference the same credential value some other way.

**Revisit if:** another stage is found to construct a URL or string that
embeds a raw credential value the way VALIDATE's query-param variant
does -- the same active-scrub-at-the-source treatment applies, not just
registration.

### D-046: OpenWeatherMap's real signup form is field-complete and registered, but not provisionable -- a visible reCAPTCHA blocks it, confirmed with one live attempt, not thirty minutes of retries

**Decided:** `OPENWEATHERMAP` (`signup_recipes.py`) is built from the
real, rendered form (`home.openweathermap.org/users/sign_up`, real
selectors for username/email/password/password-confirmation and both
required consent checkboxes) and registered in `LIVE_SIGNUP_RECIPES`, but
is not currently provisionable: the form also has a visible Google
reCAPTCHA v2 widget (`div.g-recaptcha[data-sitekey]` + the standard
`#g-recaptcha-response` hidden textarea), and one real, careful
confirmation submission -- every other field filled correctly, both
checkboxes checked -- was rejected server-side with the exact text
**"reCAPTCHA verification failed, please try again."** No attempt was
made to solve, bypass, or work around the CAPTCHA; recognizing it and
stopping is the correct outcome here, not a shortfall to fix.

**Registered anyway, not omitted, deliberately:** a vendor this project
can't currently provision still gets a real, specific
`PROVISION_FAILED` (the "no recipe registered" path, or -- once
attempted -- the driver's own exception-wrapping around the CAPTCHA
rejection) instead of looking un-investigated. The recipe itself is real,
correct, useful data: it's exactly what a future CAPTCHA-solving
integration (a human-in-the-loop step, or a paid solving service, if
this project ever decided that was in scope -- it currently is not) would
need to finish the job. Throwing away the field-mapping work because the
last step is blocked would lose real information for no reason.

**Why one confirmation attempt, not "fighting it for thirty minutes":**
the explicit instruction for this recipe was to timebox real attempts
and treat a precisely-diagnosed failure as the actual deliverable, not a
working third app at any cost. Once the reCAPTCHA widget was found in
the rendered DOM (before any submission was even attempted), the outcome
was already structurally certain -- reCAPTCHA v2's server-side check
requires a token produced by a human solving the visible challenge or an
invisible risk-score challenge actually running; neither happens in a
fresh, non-interactive headless browser session with no solve attempted.
The one live submission wasn't a retry-until-it-works attempt; it was
confirmation that every *other* field was correct and the CAPTCHA truly
was the sole blocker, isolated on purpose (fill everything correctly
first) rather than left ambiguous ("maybe a selector was also wrong").

**Rejected:** attempting a CAPTCHA-solving service, or any other bypass.
Not a real option this project ever considered -- automating past a
vendor's own explicit anti-automation measure is precisely the
"detection evasion" category this project's own operating constraints
rule out, independent of whether it would technically work.

**Rejected:** building the login + console-navigation mechanism
(retrieving the key from a dashboard after email verification) purely
speculatively, without ever seeing the real post-signup pages. Rejected
because every other recipe in this project is built from an actually-
rendered real page, never guessed from convention (D-042) -- and the
login/dashboard pages were never reachable, since signup itself never
succeeded. Building selectors against a page never observed would be
exactly the kind of guessing this project's recipes exist to avoid.
This mechanism remains a real, honest gap: the account-password
generation-and-vaulting *mechanism* is proven correct in isolation
(`test_successful_provision_stores_account_and_api_credentials_separately`,
and the same code path NASA's live run exercised for the API-level
credential), but was never exercised end-to-end against a real vendor
requiring login + navigation, because none of the three vendors
attempted this session has that exact shape reachable.

**What this is worth, concretely:** a second real, precisely-diagnosed
data point on what makes a vendor recipe-able at all -- see OPS.md's
"What makes a vendor recipe-able" section, written directly from this
finding plus NASA's success, which is the actual point of attempting a
second vendor rather than stopping after the first.

**Revisit if:** this project's scope ever changes to include a
human-in-the-loop CAPTCHA-solving step (an operator solves it once,
live, while `--headed` is set) -- OpenWeatherMap's recipe is otherwise
ready for that; nothing about the field-mapping needs to change, only
the CAPTCHA step itself.

## Stage 9+ — Fixing RESOLVE's real live-provisioning gap

### D-047: RESOLVE trusts a bare registrable domain as input directly, skipping candidate scoring entirely

**Decided:** if the (validated, stripped) input already parses as a bare
registrable domain -- "nasa.gov", "github.com": a real public suffix, no
scheme, no path, no subdomain (`utils/domains.py::parse_bare_domain()`,
built on tldextract's real public-suffix snapshot, not a naive "has a
dot" regex) -- `resolve()` trusts it directly at confidence 1.0 and skips
the "`<app> official website`" identification search entirely. Docs-URL
discovery still runs for real afterward (a bare domain says nothing about
where its own docs live); only the *identity* half of RESOLVE is skipped.

**Found live, reconfirmed after the fact:** passing `nasa.gov` itself
into `resolve()` still went through the generic scoring path and could in
principle be out-scored or misranked by an unrelated same-label
candidate -- searching for the identity of a vendor whose domain was
just handed to you is answering a question already answered. This
wasn't a crash or a wrong-domain bug in isolation; it was pure wasted
search calls and needless risk for input that was never ambiguous to
begin with.

**Rejected: trusting any input containing a dot.** A naive `"." in text`
check would wrongly accept "3.14" or "v2.1" as domains (no real public
suffix backs either). `parse_bare_domain()` uses tldextract's actual
public-suffix list, so only a string that really is `label.suffix` (or
`label.co.uk`-shaped multi-part suffixes) qualifies -- verified directly:
`parse_bare_domain("3.14")` is `None`.

**Rejected: trusting a subdomain the same way** (`api.nasa.gov`).
`parse_bare_domain()` explicitly requires `parts.subdomain` to be empty.
A subdomain wasn't handed to us as "the vendor's identity" the way a bare
registrable domain was -- it could be a CDN, a regional mirror, or
anything else, and trusting it blindly would reintroduce exactly the
kind of unverified guessing this whole stage exists to avoid.

**Guard, tested directly:** this must never weaken real ambiguity
detection for input that *isn't* already a domain.
`test_bare_domain_trust_does_not_weaken_ambiguity_a_real_company_name_still_halts`
runs "Sage" (a real company name, several real Sage-brand product domains
genuinely close in score) through the unchanged normal path and asserts
it still halts on `RESOLVE_AMBIGUOUS` -- `parse_bare_domain("Sage")` is
`None` (no recognized suffix at all), so this path is structurally
unreachable for non-domain-shaped input, not just empirically unreached.

**Revisit if:** a real vendor's canonical identity is better expressed as
a subdomain than a bare domain (unlikely, but worth naming) -- would need
a deliberate, separate decision to trust subdomains too, not a silent
widening of this one.

### D-048: A registered SignupRecipe pins RESOLVE's identity (domain + docs URL) -- never its GATE verdict

**Decided:** `resolve()` checks `LIVE_SIGNUP_RECIPES` (lazily imported,
see below) before anything else. If the input names a vendor with a
registered recipe -- exact domain match, or the domain's own label
appearing in the slugified input ("NASA API" -> "nasa-api" contains
"nasa", the label of "nasa.gov") -- RESOLVE pins straight to that
recipe's known `domain` and `docs_url` at confidence 1.0, with zero
search or fetch calls at all. A recipe only exists because someone
already read that vendor's real signup page *and* its real docs page
(D-042) to build it; re-deriving an already-known fact via search is
pure waste, and for NASA specifically, actively harmful (see below).

**The concrete bug this fixes:** even passing the literal string
`"nasa.gov"` through the *old* RESOLVE, the docs-URL discovery step (a
real search, not the identity search D-047 also skips) reliably landed
on `sti.nasa.gov` (NASA's scientific/technical information server, a real
but wrong page) instead of `api.nasa.gov` -- confirmed live, twice, both
before and after D-047 landed. `api.nasa.gov` never won the generic
"developer API documentation" search ranking against a domain this
broad. `SignupRecipe.docs_url` is a new required field precisely to
short-circuit past this: NASA's recipe states the answer is
`https://api.nasa.gov/` because that's the page the recipe's own
selectors were captured from -- it isn't a guess to verify, it's a fact
already on hand. Same reasoning fixed a second, independent, previously
undiscovered gap: OpenWeatherMap's real API docs live at
`https://openweathermap.org/api`, a path RESOLVE's own
`_DOCS_PATH_GUESSES` (`"/docs"`, `"/developers"`) has never included and
so could never have found on its own (noted as an open gap in OPS.md
before this fix; reverified live -- HTTP 200 -- before being hardcoded).

**Critical constraint, and the actual design point worth writing down:**
this pins IDENTITY ONLY. `resolve()` returns exactly the same
`ResolveResult` shape any other path returns -- DISCOVER, CLASSIFY, and
GATE run against the pinned `docs_url` exactly as they would against any
other candidate's, with no awareness that identity was pinned rather
than derived, and GATE's own verdict is the only thing that decides
AUTO/HITL/UNSUPPORTED. A recipe knowing "this is definitely nasa.gov" is
a completely different claim from "this vendor is definitely safe to
automate" -- the first is a fact about naming, the second is a policy
judgment only GATE is positioned to make, on real evidence, every time.
Conflating the two would let a recipe silently force AUTO, which is
precisely the false-`AUTO` failure mode D-035 already exists to prevent
one layer up. Proven, not just asserted:
`test_recipe_pinned_identity_can_still_land_hitl_gate_verdict_remains_binding`
(`tests/pipeline/test_orchestrator.py`) pins a fake recipe's identity at
confidence 1.0 and drives a real `TosGateExtraction` signal through the
real `gate()`, and asserts the final artifact is `HITL` /
`tos_prohibits_automation`, never `AUTO` -- through the actual
orchestrator, not a hand-built shortcut.

**Rejected: matching purely on exact domain, no name variant.** Would
miss the more common real input shape (a vendor's name, "NASA API," not
its domain) entirely, defeating half the point -- a user who already
knows to type `nasa.gov` gets D-047's bare-domain trust regardless; D-048
exists specifically to also cover the name a person would actually type.

**Rejected: a second, separate hand-maintained registry mapping names to
(domain, docs_url) pairs.** `LIVE_SIGNUP_RECIPES` already is that
registry; adding a parallel one keyed differently is exactly the kind of
two-sources-of-truth drift risk this project avoids everywhere else
(the same reasoning as reusing `LIVE_SIGNUP_RECIPES` directly in
`webapp/main.py` instead of the ad hoc keyword dict it used before this
fix -- one registry, matched the same way, wherever the question "does
this vendor have a recipe" is asked).

**On the lazy import:** `signup_recipes.py` itself has no forced
dependency on the `live` extra (`playwright` is only ever imported inside
a method body, never at module load), so importing it from `resolve.py`
doesn't newly require Playwright on a default install. The import is
still kept local to `_match_recipe_identity()`, not hoisted to module
level, so that guarantee stays visible at the one call site that
actually needs it, rather than true only by accident of how
`signup_recipes.py` happens to be written today.

**Revisit if:** the recipe-backed vendor set grows past a size where
label-substring matching starts producing false positives on unrelated
app names -- worth switching to an explicit alias list per recipe at that
point, not a larger blocklist bolted onto the substring check.

### D-049: Three-tier source authority in docs-candidate ranking, and why authority lives there, not in CLASSIFY

**Decided:** `pipeline/source_authority.py` classifies any docs-candidate
URL into HIGH (an official API reference -- path segment `api`,
`reference`, `api-reference`, or `rest`, or a `developer(s).*` subdomain),
MEDIUM (general docs/guide -- path segment `docs`, `guide`, `guides`, or a
`docs.*` subdomain), or LOW (everything else: blogs, tutorials,
third-party write-ups, forums, Stack Overflow, Medium). Two call sites:

1. `resolve.py::_rank_and_verify_docs_candidates` sorts candidates by tier
   (`tier_sort_key`, a *stable* sort -- within a tier, whatever order
   candidates arrived in, i.e. search rank then conventional guesses,
   still applies) before fetching/verifying any of them. This replaces
   the old `_docs_signal_score`, a flat two-level "developer/docs/api
   signal present or not" that scored a Trailhead tutorial and a real API
   reference identically the moment neither had a negative signal
   (`help`/`support`/etc.). The winning URL's `docs_url_reason` now names
   the specific tier and matched signal (`"HIGH-tier (path segment
   'rest')"`), and -- a real, separate bug fixed in the same pass --
   this reason was being computed all along and never once surfaced in
   the final artifact; `emit.py` now adds a real evidence item for it.
2. `classify.py` computes the tier fresh from whichever `docs_url`
   DISCOVER actually used (which can differ from RESOLVE's top pick,
   D-028's fallback), and applies a real, asymmetric adjustment to the
   extractor's raw confidence before comparing it to
   `CONFIDENCE_THRESHOLD`: HIGH +0.05, MEDIUM +0.0, LOW -0.15. `source_tier`
   is recorded on `ClassifyResult` and surfaced on the artifact's
   `ApiInfo` so a downstream consumer can tell a HIGH-tier-derived 0.6
   apart from a LOW-tier-derived 0.6 -- previously indistinguishable in
   the artifact, which was the actual bug this exists to fix.

**Measured, real seed-batch effect** (`run_20260801T084914Z_04685c6b`
against the previously-measured `run_20260730T170035Z_4a8d3af4`; full
before/after table in README.md): `classify_low_confidence` went from 8
apps to 4 -- Monday.com, Stripe, and Trello moved straight to AUTO;
Spotify moved to a real, evidence-backed `requires_payment` GATE finding.
AUTO went from 2 apps to 6. Of the 16 apps that reached a real docs page
this run, 12 landed HIGH-tier, 3 MEDIUM, 1 LOW (Salesforce, on DISCOVER's
own bare-domain fallback -- correctly penalized, since a marketing
homepage genuinely isn't a docs page). Zero regressions found checking
all 20 apps individually -- no app moved from a better-informed status to
a worse one. Caveat stated as plainly as the result: several other
apps that changed between these two runs (Discord, Etsy, Google Calendar,
Mailgun) did so at the RESOLVE *identification* step, a code path this
decision never touches -- those are pre-existing search-provider
non-determinism (see OPS.md's "Run-to-run coverage volatility"), not an
effect of source-authority weighting, and are not counted among the
results above.

**Why authority lives in ranking, not in CLASSIFY:** by the time CLASSIFY
reads a page, the wrong page has already been chosen -- no amount of
downstream distrust of a confidently-written tutorial recovers a docs
candidate that should never have outranked the real reference in the
first place. Fixing this in CLASSIFY alone (e.g. "distrust tutorial-
flavored prose") would still let a genuinely well-written, wrong page win
the ranking and get read; fixing it in ranking means the right page gets
picked more often, which is strictly more valuable than making CLASSIFY
merely more suspicious of whatever it's handed. CLASSIFY's own tier-aware
confidence adjustment (item 2 above) exists as a second, independent
safety net for the residual case -- ranking picked the best *available*
candidate, but that candidate still turned out to be LOW-tier because
nothing better existed -- not as a substitute for fixing the ranking
itself.

**The tradeoff, named plainly:** a HIGH-tier page that's sparse (real,
minimal endpoint reference, thin on prose) will still outrank a MEDIUM or
LOW-tier page that's rich (a detailed, well-written tutorial covering the
same ground more thoroughly) -- tier is about the URL's *shape* and what
that shape structurally implies about the page's role, not the page's
actual information density, which ranking has no way to measure ahead of
a real fetch. This is a deliberate bet: a thin official reference is
still more likely to be *correct* about auth scheme, endpoints, and
scopes than a thorough unofficial writeup is, even when the writeup reads
better. `_looks_like_api_docs`'s content-verification check (unchanged
by this decision) still rejects a HIGH-tier candidate outright if its
actual content doesn't look like API documentation at all -- tier ranks
among candidates that already passed that bar, it doesn't replace it.

**Rejected: scoring authority as a continuous function of matched-signal
count, not three discrete tiers.** A URL matching both a `developer.*`
subdomain and a `/reference/` path isn't meaningfully more authoritative
than one matching only one of those -- both are clearly official
references. Discrete tiers avoid manufacturing false precision (a score
of "3" vs "2" implying a confidence gradient the URL shape alone can't
actually support) and keep the ranking auditable in plain language
(`docs_url_reason` names one tier and one matched signal, not a composite
number).

**Rejected: applying the same numeric adjustment symmetrically (e.g.
HIGH +0.15, LOW -0.15).** The chosen asymmetry (+0.05 / +0.0 / -0.15) is
deliberate: a HIGH-tier source earns only a small reward, because ranking
should already be steering most real cases there -- CLASSIFY's job isn't
to further reward what ranking already got right. A LOW-tier source takes
the real, threshold-crossing penalty, because that's the actual failure
mode this whole decision exists to fix: a confidently-extracted tutorial
passing the same bar as a confidently-extracted reference. Rewarding HIGH
as heavily as LOW is penalized would just re-inflate confidence for
everything, which isn't the problem being solved.

**Rejected: `"api.*"` as an automatic HIGH-tier subdomain signal.** Common
real shape: `api.<vendor>.com` is often the API *endpoint* domain
(`base_url`), not a docs page -- NASA's `api.nasa.gov` happens to serve
both, but that's a coincidence of one vendor's setup, not a pattern to
generalize from. Only an explicit path segment (`/api/`) or the
`developer(s).*` subdomain earn HIGH on their own.

**Revisit if:** the confidence-adjustment magnitudes turn out too weak or
too aggressive once measured against a larger, more diverse app set than
the 20-app seed list -- these are principled starting values (asymmetric
by design, see above), not independently tuned against real classification
outcomes at scale.

### D-050: `batch` runs the whole batch inside one event loop, not one `asyncio.run()` per app

**Decided:** `cli/__init__.py::batch()` now wraps the entire per-app loop
in a single `asyncio.run(_run_batch(...))` call, instead of calling
`asyncio.run(run_app(...))` fresh inside the loop for every app.

**Found live, re-running the seed batch to measure D-049:** app 16 of 20
crashed with `RuntimeError: Event loop is closed`, non-deterministically
(the same batch had run cleanly through 15 apps first). Root cause: the
`ProviderBundle` built once, before the loop, holds one
`HttpxFetchProvider` with one shared `httpx.AsyncClient` -- httpx binds a
client's connection pool to whichever event loop is running on its first
real request. The old code created and destroyed a *new* event loop every
iteration (`asyncio.run()` inside the `for` loop); the shared client's
pool, bound to app 1's loop, becomes invalid the moment that loop closes,
and reusing it from app 2's (new) loop raises exactly this error --
timing-dependent on real connection-pool state, which is why it didn't
reproduce on every run.

**Rejected: building a fresh `ProviderBundle` (and therefore a fresh
httpx client) for every app instead.** Would work, but throws away
keep-alive connection reuse across apps for no reason -- the actual bug
is calling `asyncio.run()` per app at all, not that providers are shared.
One event loop for the whole batch is both the correct fix and a real,
if secondary, performance win (no repeated event-loop startup/teardown).

**Rejected: leaving `run()` (single-app) and `resolve()` (single-app)
untouched.** Already correct -- each already calls `asyncio.run()`
exactly once for the one app they process; only `batch()`'s per-iteration
pattern was wrong.

### D-051: RESOLVE's docs-URL search now degrades on provider failure, the same way its identification search already did

**Decided:** `_collect_docs_candidate_urls()` (the *second* search RESOLVE
issues, looking for developer docs once a company identity is already
confirmed) now catches `SearchProviderError` per query template and
continues with conventional-guess URLs only, instead of letting the
exception propagate out of `resolve()` entirely.

**Found in the same batch re-run as D-050:** a different app crashed with
an uncaught `SearchProviderError` from `search.search(query, count=8)`
inside docs-URL discovery -- DDG genuinely timed out against Mojeek's
backend. `resolve()`'s *first* search call (`"<app> official website"`)
already has exactly this fallback (D-024's `SearchProviderError` split
and `_resolve_via_search_failure_fallback`), but that protection never
covered the second, later search call docs-URL discovery makes -- an
oversight in D-024's original scope, not a new failure mode.

**Effect on the batch harness specifically:** `batch()`'s outer
`except Exception` already prevents one app's crash from stopping the
whole batch (D-036) -- but "caught by the outer handler" still meant
zero artifact, zero registry entry, and the app silently missing from
`report.json`'s counts, exactly the class of measurement-integrity bug
D-038/D-039 exist to prevent one stage earlier. Degrading gracefully
inside `resolve()` itself, so every app still reaches EMIT, is the
correct fix, not a wider try/except further out.

### D-052: `SignupRecipe` gains `requires_email_verification` and `api_key_page_regex` -- page-based extraction was never actually two shapes, it was three

**Decided:** `SignupRecipe` (`providers/playwright_browser.py`) gains two
fields: `requires_email_verification: bool = False` (does this vendor's
page-based flow need an email-verification wait before the credential is
visible, or does it render immediately) and `api_key_page_regex: str |
None` (an optional regex to extract just the credential out of a page
selector's raw text, mirroring what `api_key_email_regex` already does
for the email path). `signup_and_create_app()`'s page-based extraction
branch now checks `requires_email_verification` before deciding whether
to wait on `email_provider.wait_for_message()` at all, and -- when a page
selector is set without an email wait -- polls for the *regex match*
specifically, not just non-empty selector text.

**Found live, building Alpha Vantage's recipe:** the two page-based
recipes that existed before this (NASA is email-only; OpenWeatherMap
never got past its CAPTCHA to reach extraction, D-046) meant page-based
extraction's actual behavior -- unconditionally wait for an
email-verification message, then read a selector -- had never been
exercised by a real vendor. Alpha Vantage's real flow renders the key
into the page via AJAX a few hundred milliseconds after submit, with *no*
email step of any kind: the AJAX response fires whether or not the
address is real, deliverable, or ever checked. The old code would have
hung waiting on an email that was never going to arrive. Separately, the
key's exact DOM container was never reliably identifiable by inspection
(`#results` stayed empty on `innerHTML` probing even after the key was
visibly rendered elsewhere in the page) -- the only reliable selector was
`"body"`, and a bare "selector has any text" poll exits immediately
against a selector that broad, since ordinary page chrome is never empty.
Polling for the regex match itself, not just non-empty text, is what
makes a broad fallback selector usable at all.

**Rejected: a separate boolean like `has_no_email_step` inverted from
`requires_email_verification`.** Functionally identical; phrased as
"requires verification" (default `False`) because that's the assumption
being corrected -- every recipe written before this one implicitly
assumed page-based extraction always needs an email wait, and that
assumption was simply wrong for a vendor shape nobody had tried yet.

**Revisit if:** a future vendor needs email verification *and* has no
identifiable credential-page selector -- the two new fields are
independent today, but a vendor combining "waits for email" with "regex
extraction only, no reliable container" hasn't been observed and isn't
tested.

### D-053: Page-based extraction with zero extraction mechanism must fail explicitly, not silently report an empty "success"

**Decided:** `signup_and_create_app()` now checks, immediately after
submit, whether `credential_type != CredentialType.NONE` and *none* of
`api_key_email_regex` / `client_id_selector` / `client_secret_selector` /
`api_key_page_selector` are set on the recipe. If so, it returns a real
`ProvisionOutcome(success=False, ...)` with a specific `failure_reason`,
instead of falling through the extraction `if`/`elif` chain untouched and
reaching the success return at the bottom with an empty
`raw_api_credential`.

**Found live, writing IPinfo's recipe for the "attempt it anyway, despite
the flagged invisible reCAPTCHA" stress test (see OPS.md and README.md):**
IPinfo's real signup form was fully field-complete (first name, last
name, email, password -- a genuine `account_email` +
`account_password_ref` archetype, the one the seed batch had never
exercised live), but a real submission -- ordinary headless Chromium, no
retries, no evasion attempted -- tripped Google's invisible reCAPTCHA v2
risk score and escalated to a full interactive "Select all images with
cars" challenge, confirmed by screenshot. Since the credential page was
never reached, there was nothing to write an `api_key_page_selector`
against, and the recipe was registered with no extraction mechanism at
all -- the same shape OpenWeatherMap's recipe (D-046) has had all along.
Tracing `signup_and_create_app()`'s control flow for this exact shape
found the bug: with every extraction field unset, none of the
`if`/`elif` branches ever execute, and execution falls straight through
to `return ProvisionOutcome(success=True, ..., raw_api_credential={})` --
a false "provisioned" that vaults nothing. `PROVISION` would have
recorded `status="provisioned"` with `credential_type=API_KEY` and
`api_key_ref=None`, silently misreporting a CAPTCHA-blocked signup as
successful. This means OpenWeatherMap's recipe comment ("Registered here
... so the failure is `PROVISION_FAILED` with a real, specific reason")
was never actually true until this fix -- confirmed by re-running IPinfo
through the real `provision()` pipeline after the fix: `status="failed"`,
`api_key_ref=None`, with an `extract_credential` step explicitly marked
`success=False` and the real reason recorded.

**Rejected: a Pydantic `model_validator` on `SignupRecipe` requiring at
least one extraction mechanism whenever `credential_type != NONE`.**
Would catch this at recipe-definition time instead of at run time, which
sounds strictly better -- except it would also make it *impossible* to
register a recipe for a vendor that's fully field-complete but
confirmed-blocked before the credential page, exactly IPinfo and
OpenWeatherMap's actual, legitimate shape. Registering those recipes
anyway (with a `PROVISION_FAILED` at run time, not a validation error at
import time) is deliberate: it's what turns "someone already investigated
this vendor and hit a specific, named wall" into a fact the next
`--live` attempt discovers immediately, instead of a silent "no recipe"
that looks unresearched. The runtime check preserves that while fixing
the actual defect (the false success), which is the narrower, correct
fix.

**Verified:** `tests/providers/test_playwright_browser.py::test_recipe_with_no_extraction_mechanism_fails_instead_of_reporting_empty_success`
drives a real Playwright browser against a local `data:` URL form (no
network, no vendor dependency) with a recipe that has a real
`credential_type` and no extraction selectors, and asserts the outcome is
an explicit failure with an empty credential -- not a silent success.
214/214 tests pass including this one.

### D-054: A new failure class -- the artifact contradicting itself, not failing silently. Source tier is now computed exactly once, in DISCOVER, from the URL that's actually used

**A different bug shape than D-050/D-051/D-053, worth naming as its own
class:** every prior fix this run has caught the pipeline claiming
*success it hadn't earned* (an empty credential reported as
"provisioned," an app silently dropped from a batch's count, a soft-404
cleared to `AUTO`). This one is not that. A real Linear run produced a
*complete, internally consistent-looking* artifact that nonetheless
asserted two different things about the same fact: the evidence array
said *"docs source selected: HIGH-tier ('developers' subdomain)"*, and
`api.source_tier` said `"low"`. Neither claim was individually wrong in
isolation -- each was computed correctly from *some* input -- the bug is
that there were two separate computations of "the tier of this run's docs
page" and nothing forced them to agree.

**Root cause, traced to two distinct, compounding bugs, not one:**

1. **A stale pre-redirect description.** `_rank_and_verify_docs_candidates()`
   (resolve.py) fetched each candidate `url`, then computed
   `tier_desc = describe_tier_match(url)` -- the *pre-redirect* URL --
   but stored that reason against `fetched.final_url`, the *post-redirect*
   URL, whenever they differed. Linear's real conventional guess
   `https://developer.linear.app` (a HIGH-tier subdomain) redirects to
   `https://linear.app/developers` (a path) -- RESOLVE recorded "HIGH-tier
   ('developer' subdomain)" as the reason for a URL that was never
   actually a subdomain at all.
2. **A real asymmetry in the tier rule itself**, which is what turned
   bug #1 from a cosmetic mislabel into an actual tier mismatch:
   `_HIGH_PATH_SEGMENTS` recognized `/api`, `/reference`,
   `/api-reference`, `/rest` as HIGH-tier paths, but not `/developer` or
   `/developers` -- even though the *subdomain* form
   (`developer(s).example.com`) already earned HIGH. `/docs` had its own
   MEDIUM-tier path entry; the developer(s) path form had none at all.
   `https://linear.app/developers` -- the real, final URL -- therefore
   fell all the way through to LOW under the old rule, independently of
   bug #1.
3. **CLASSIFY then independently recomputed the tier a second time**,
   from `state.discovery.docs_url` (the URL DISCOVER actually settled
   on, D-028) -- correctly, on its own terms, but from a URL that could
   differ from whatever RESOLVE's evidence-building had already committed
   to text. Two correct computations, two different inputs, one artifact.

**Fixed, three parts:**

- **Rule symmetry** (`pipeline/source_authority.py`): `_HIGH_PATH_SEGMENTS`
  now includes `developer`, `developers`, `api-docs`, `rest-api` alongside
  the existing `api`/`reference`/`api-reference`/`rest` -- a vendor whose
  real docs live at `/developers` (a path) is exactly as official as one
  at `developers.*` (a subdomain); which shape a vendor happens to use
  was never itself a signal of authority.
- **Tier description now reads the post-redirect URL**
  (`_verify_one_candidate`, resolve.py): `describe_tier_match(fetched.final_url)`,
  not `describe_tier_match(url)` -- the reason always describes the exact
  URL it's stored against.
- **Single source of truth, end to end** (`pipeline/discover.py`,
  `models/state.py`): `source_tier`/`source_tier_reason` are computed
  exactly once, in `discover()`, from whichever `docs_url` this stage
  actually settles on -- the same URL that becomes `api.docs_url`.
  `DiscoveryResult` carries both fields. `classify()` no longer calls
  `classify_source_tier()` at all; it takes `source_tier` as a parameter
  and asserts it's set whenever `has_public_api` is true. EMIT's
  "docs source selected" evidence item now reads
  `state.discovery.source_tier_reason`, never
  `state.resolve.chosen.docs_url_reason` (which still exists, and is
  still accurate for RESOLVE's own internal ranking/explain-log purposes
  -- it just no longer feeds the artifact). The evidence item's `claim`
  and `snippet` are also no longer the same string repeated (a real,
  separate, minor bug caught in the same pass): `snippet` is now a real
  200-character quote from `discovery.docs_text`, matching every other
  evidence item's shape.

**Rejected: "compute source tier once, in RESOLVE" (the fix as first
proposed).** RESOLVE ranks and content-verifies candidates before
DISCOVER ever runs, but it does not know which candidate DISCOVER will
actually settle on -- DISCOVER falls back through RESOLVE's ranked list
when the top pick isn't reachable or doesn't pan out (D-028), *and*
appends its own conventional subdomain/path guesses that were never in
RESOLVE's list at all (`_candidate_list` in discover.py). Computing the
tier once in RESOLVE and having CLASSIFY read it verbatim would have
been correct only when DISCOVER's fallback never triggers -- silently
reintroducing a narrower version of the exact bug this fix exists to
remove, for any app where D-028's fallback actually fires. Computing it
once in DISCOVER, at the point the real, final `docs_url` is known, is
the version of "single source of truth" that's actually true for every
app, not just the common case.

**Measured live, re-running Linear after the fix:** the evidence array
and `api.source_tier` now agree (`HIGH`) -- confirmed directly, not by
inspection of the code alone. The corrected adjusted confidence (0.6 raw
+ 0.05 HIGH bonus = 0.65) does clear `CONFIDENCE_THRESHOLD` (0.6) --
`CLASSIFY_LOW_CONFIDENCE` no longer fires for Linear. **This was not a
false HITL, though**: GATE's own ToS check independently finds Linear's
real terms state *"Customer will pay for access to and use of the
Service"* -- `requires_payment` fires regardless of CLASSIFY's
confidence, and Linear correctly lands on HITL either way. What the bug
actually cost, concretely: before this fix, `classify.reason_code ==
CLASSIFY_LOW_CONFIDENCE` short-circuits `gate()` *before* its ToS/payment
signal collection ever runs (gate.py's precondition check, ordered before
D-040's cross-source scan) -- so the artifact a human would have actually
seen said `reason_code: classify_low_confidence` ("credforge wasn't
confident enough," implying a re-run might help) instead of the real,
correct, and immutable `reason_code: requires_payment` ("Linear is a paid
API, full stop"). Same final status, materially different and less
actionable reason -- itself a smaller instance of the same "artifact
asserts something that isn't quite true" family this entry exists to
name, not a coincidence.

**Verified:** `tests/unit/test_source_authority.py::test_high_tier_developer_path_matches_the_developer_subdomain_symmetrically`
and `test_high_tier_api_docs_and_rest_api_paths` cover the rule fix;
`tests/pipeline/test_resolve.py::test_docs_url_reason_describes_the_post_redirect_url_not_the_pre_redirect_guess`
reproduces the exact redirect-mismatch shape with a fake fetch whose
`final_url` differs from the requested `url`; `tests/pipeline/test_emit.py::test_evidence_tier_never_contradicts_the_api_block_tier`
is parametrized across all three tiers with RESOLVE's own
`docs_url_reason` deliberately set to the *wrong* tier, proving EMIT
never reads it for this claim regardless of what it says.

### D-055: `api_style` (REST/GraphQL/unknown) -- the schema assumes REST, and that's now an honest, visible limitation instead of a silent one

**Decided:** DISCOVER now classifies whichever API it actually crawled as
`ApiStyle.REST`, `GRAPHQL`, or `UNKNOWN` (`enums.py`), via a cheap
keyword heuristic (`_detect_api_style` in `discover.py`, checking the
docs URL and the first 5000 characters of crawled text for markers like
`/graphql` / `"graphql api"` vs. `"rest api"` / `/openapi` / `swagger`)
-- not an LLM call, matching every other cheap-heuristic-first pattern in
this stage (soft-404 detection, negative-signal filtering). `UNKNOWN` is
the deliberately conservative default: it changes nothing about existing
behavior, unlike a *confirmed* `GRAPHQL` classification. The field is
carried on `DiscoveryResult` and surfaced on the artifact's `ApiInfo.api_style`.

**Found live, running Linear:** credforge's schema was built assuming
REST throughout -- `base_url` + resource paths, a `validation_endpoint`
VALIDATE always resolves as a bare "METHOD /path" and always checks with
a plain GET (pipeline/validate.py, D-032). Linear's real API is GraphQL:
one endpoint, cursor-based Relay connections, no REST-style multi-path
shape at all. `gate.py`'s `_completeness_gaps()` was reporting missing
`pagination_style_hint` and `validation_endpoint` as if they were
ordinary extraction misses on a REST vendor, when for a GraphQL vendor
neither field's *current design* has real meaning to miss.

**Fix, deliberately narrow (`Don't over-engineer this`):** only two of
the five tracked completeness fields are treated as REST-specific and
suppressed for a confirmed GraphQL API --
`pagination_style_hint` (a prose hint the extractor looks for in
REST-shaped terms: query params, page numbers -- not Relay's
`pageInfo`/`endCursor` shape) and `validation_endpoint` (consumed by
VALIDATE as a path resolved against `base_url` with an always-GET
request, which has no meaning for a GraphQL API's single
POST-with-a-query-body endpoint). `base_url`, `developer_portal_url`, and
`rate_limit_notes` stay universal -- a GraphQL vendor has exactly one of
the first and (usually) real prose about the other two; a missing one is
still a real, actionable gap, not a REST assumption. Linear's own real
run had zero completeness gaps regardless (DISCOVER's extraction found
every field), so this specific suppression wasn't exercised by the live
run that motivated it -- verified instead by a direct unit test
(`tests/pipeline/test_gate.py::test_graphql_api_style_suppresses_only_the_rest_specific_completeness_gaps`)
constructing the missing-fields case directly.

**Rejected: teaching VALIDATE to actually validate a GraphQL
credential** (a POST with an introspection or cheap query body, instead
of the current always-GET). Real, worthwhile future work, but a
materially bigger change than "stop reporting a false gap" -- explicitly
out of scope per "don't over-engineer this." Documented as a known,
honest limitation (see README) rather than silently left unmentioned:
credforge's schema and VALIDATE both currently assume REST; GraphQL is
discovered and classified correctly, but a GraphQL AUTO app would still
fail VALIDATE today.

**Rejected: an LLM call to classify API style.** The two-marker-set
heuristic is enough to catch the common, explicit case (a vendor's own
docs almost always say "GraphQL" or "REST API" somewhere prominent, or
the URL itself names it, as Linear's `/developers/graphql` does) without
spending an extra extraction call on every app just to set one field
that's `UNKNOWN`-safe by default when it can't tell.

### D-056: RESOLVE's docs-candidate probes now run with bounded concurrency -- a real, but smaller-than-hoped-for, latency win

**Decided:** `_rank_and_verify_docs_candidates()` (resolve.py) now
fetches every filtered, tier-sorted candidate through `asyncio.gather`
under a `asyncio.Semaphore(5)` bound, instead of `await`-ing each one in
a strict `for` loop. `asyncio.gather` preserves input order in its
results regardless of completion order, so the existing
tier-then-search-rank ordering is unaffected -- this is a pure
concurrency change, not a ranking change.

**Measured, honestly, and the result is smaller than "parallelize a
sequential loop" usually implies:** every candidate `_rank_and_verify_docs_candidates`
fetches targets the *same* app's registrable domain (RESOLVE filters
search hits to `registrable_domain(r.url) == domain` and adds only
same-domain conventional guesses) -- and `net/rate_limiter.py`'s
`DomainRateLimiter` keys its token bucket by registrable domain too
(`HttpxFetchProvider.fetch()`, confirmed by reading the call site). At
the default `0.5 req/sec, burst=2` (no per-domain overrides are actually
configured anywhere in this codebase today), every candidate for a
single app shares one bucket -- concurrent requests to it still acquire
their token one at a time, so parallelizing the *loop* does not
parallelize the *rate limit itself*, by design (the user's own
instruction: "respecting the per-domain rate limiter"). Real
before/after, git-worktree-isolated (old code at commit `323e2b5`, new
code current), same real DDG search/fetch calls:

| app | before | after | candidates verified (before -> after) |
|---|---|---|---|
| Linear | 42.88s | 34.52s | 4 -> 3 |
| Stripe | 40.14s | 36.80s | 3 -> 1 |
| GitHub | 41.32s | 50.80s | 2 -> 6 |

**Read with the confound stated plainly, not hidden:** candidate *count*
differs before vs. after for every app above -- real DDG search-result
variance (the same run-to-run volatility this project has documented
since Stage 4/8, unrelated to this change) means the before and after
runs are not doing identical amounts of work. GitHub's *after* run is
slower in raw wall-clock despite the fix, purely because it had three
times as many real candidates to verify. Normalizing to seconds-per-
verified-candidate (a fairer, though still imperfect, unit): Linear
10.7s/candidate -> 11.5s/candidate (flat, within noise -- consistent with
every candidate sharing one maximally-serialized bucket regardless of
concurrency); Stripe 13.4s/candidate -> 36.8s/candidate (the *after* run
had exactly one candidate -- concurrency has nothing to overlap with one
item, so this is the expected floor case, not a regression); GitHub
20.7s/candidate -> 8.5s/candidate (a real, meaningful drop -- the case
with the most candidates to overlap shows the clearest win). **Honest
summary: the fix is real and measurably helps when there's more than a
handful of same-domain candidates to check, but the shared per-domain
rate limiter -- by design, and per the explicit instruction to respect it
-- caps how much it can ever help, and an isolated single before/after
run per app is too noisy on its own to prove the effect size cleanly.**

**Rejected: raising the per-domain rate limit specifically for
RESOLVE's docs-probing phase.** Would unlock more of the theoretical
concurrency win, but changes real outbound request pacing against
vendor infrastructure -- a defaults change with a different risk profile
(politeness/abuse-avoidance, D-007) than a pure code-concurrency change,
and out of scope for what was asked here.

**Verified:** `tests/pipeline/test_resolve.py::test_docs_candidate_probes_run_concurrently_not_strictly_sequentially`
uses five candidates with a simulated 100ms round-trip each and asserts
both real overlap (`max_in_flight > 1`) and a wall-clock ceiling
(<300ms) that a strictly sequential loop could not meet (>=500ms) --
proves the concurrency mechanism itself, independent of the rate
limiter's separate, dominant effect measured above.

### D-057: A false HITL -- GATE's `requires_payment` didn't distinguish payment language scoped to one endpoint from payment language scoped to API access itself

**A third failure class, distinct from both families named so far.**
D-038/D-039/D-053 were the pipeline reporting success it hadn't earned.
D-054 was the artifact contradicting itself about the same fact. This one
is neither: GATE produced a single, internally-consistent, wrong verdict
-- `requires_payment`, blocking PROVISION on a vendor that has a real,
working free tier. Alpha Vantage's own real docs state: *"This is a
premium endpoint. If you would like to access realtime, 15-minute
delayed, and/or historical intraday data, please subscribe to a premium
membership plan."* That sentence scopes to specific realtime/historical
endpoints and data tiers -- it does not say API access requires payment.
Proof it's false, not a judgment call: a real Alpha Vantage API key was
acquired live in 5.59s two days before this bug was found (D-052/D-053),
and the artifact's own `developer_portal_url` is
`alphavantage.co/support/#api-key` -- the free-key signup page, extracted
from the same docs page GATE flagged.

**Root cause:** `TosGateExtraction.requires_payment` is a single boolean
-- the extractor (heuristic or Anthropic) reports whether *any* payment
language was found, with no distinction between "this specific endpoint
costs money" (true of nearly every real API with a paid tier, including
ones with a genuinely free key) and "you cannot get a credential at all
without paying." GATE trusted the raw flag unconditionally.

**Likely why this was never caught until now, and why it's fallout from
the GATE precedence fix, not a new regression:** before D-040 (GATE
reading every real signal source, not just a first-found one) and before
D-049/D-054 (the source-tier fixes that materially reduced how often
`CLASSIFY_LOW_CONFIDENCE` fires), a real fraction of apps never reached
GATE's payment-scanning code at all -- they short-circuited earlier,
at RESOLVE or CLASSIFY. D-049's own measured effect was VENDOR_BLOCKED
going from 0 to 2 in the seed batch specifically *because* GATE started
reaching the real payment/business signals on pages it previously never
got to read. That fix was correct and the two blocks it found
(OpenWeatherMap, Spotify) were real. But widening how often GATE's real
signal-scan runs to completion also widens exposure to any latent gap
*inside* that scan -- and the scoped-vs-unscoped ambiguity was always
there in the extraction layer, simply never exercised against a page
shaped like Alpha Vantage's until a live run actually reached it. Same
mechanism as D-054's Linear finding (a fix that reaches GATE's real
checks more often surfaces both real blocks it was missing AND latent
bugs in those checks themselves) -- not a coincidence that both showed up
in the same pass of live-testing previously-unreached code paths.

**Fixed:** `gate.py` gains `_payment_signal_is_false_positive()`, applied
to `requires_payment` from *every* signal source (the already-crawled
docs page and the dedicated ToS page, D-040) against that source's own
raw text -- not the extractor's evidence snippet, which mixes evidence
for all seven flags together and can't be cleanly attributed to just
this one. Three marker sets, checked in this precedence:

1. **UNSCOPED** (`"api access requires a paid plan"`, `"you must
   subscribe to use the api"`, `"no free tier"`, `"a paid account is
   required to obtain an api key"`, `"credit card required to sign
   up"`) -- checked first; if any matches, `requires_payment` is left
   alone, full stop, regardless of anything else on the page.
2. **SCOPED** (`"this is a premium endpoint"`, `"premium endpoint"`,
   `"this endpoint requires"`, `"realtime data"`, `"historical data"`,
   `"intraday data"`, `"delayed data"`, `"for commercial use"`,
   `"premium membership plan"`, `"upgrade to access"`) -- a match alone
   is sufficient to suppress; no additional free-tier confirmation is
   required.
3. **FREE-TIER OVERRIDE** (`"free api key"`, `"claim your free api
   key"`, `"free tier"`, `"get started for free"`, `"no credit card
   required"`, or a `developer_portal_url` containing `"api-key"`) -- an
   independent second path to suppression, not a precondition layered on
   top of SCOPED. Covers a page whose payment language doesn't literally
   match a SCOPED marker but where free-tier evidence is still present.

**A deliberate interpretation call, stated explicitly because the
request supports more than one reading:** "SCOPED... must not trigger
requires_payment on its own" could mean scoped language alone suffices
to suppress, or that it needs the free-tier override as corroboration.
Chosen: alone suffices -- SCOPED and FREE-TIER OVERRIDE are two
independent, OR'd paths to the same suppression, both beaten by UNSCOPED.
This makes every listed marker category load-bearing rather than leaving
FREE-TIER OVERRIDE as dead-code reinforcement of a rule SCOPED already
covers on its own.

**Rejected: fixing this in the extraction prompt instead of GATE.**
Would help the Anthropic path specifically, but this project's own
established rule is that tests never call a live LLM -- a prompt-only fix
would be fundamentally unverifiable by the test suite, and would do
nothing for the heuristic extractor (which doesn't have this distinction
either, and is the default with no `ANTHROPIC_API_KEY`). A deterministic
override in GATE, applied uniformly regardless of which extractor
produced the raw flag, is the only version of this fix that's actually
testable and that helps both paths. The prompt is unchanged; a real
future improvement, not attempted here.

**Rejected: applying the same scoped/unscoped distinction to the other
six `TosGateExtraction` flags** (business verification, sales contact,
phone verification, CAPTCHA, SSO-only, prohibition). Real HITL found this
specific false positive on this specific flag; generalizing to flags with
no observed false positive would be scope creep past what was actually
reported.

**Verified:** `tests/pipeline/test_gate.py` -- scoped language alone
clears to AUTO; genuinely unscoped language still blocks; the free-tier
override suppresses even with no SCOPED marker present; a
`developer_portal_url` containing `api-key` suppresses with no free-tier
text marker at all; unscoped wins even when scoped and free-tier language
both appear in the same text (the real Open-Meteo shape); the override
applies to both signal sources, not just one. Two explicit regression
guards: an Etsy-shaped unscoped automation prohibition and an
Open-Meteo-shaped unscoped payment requirement both still produce their
real, correct HITL verdicts -- this fix cannot silently clear a genuine
block. 236/236 tests pass including all of the above.

### D-058: D-057 generalized from `requires_payment` alone to every scope-suppressible flag, plus a structural guard for recipe-backed vendors

**Whack-a-mole, immediately, on the very next live run.** Re-running
Alpha Vantage after D-057, `requires_payment` no longer fired -- but GATE
blocked on `requires_sales_contact` instead, from the *very next clause*
in the *same real sentence*: *"This is a premium endpoint... please
subscribe to a premium membership plan for your personal use. **For
commercial use, please contact sales.**"* Same scope qualifier ("for
commercial use"), different flag. Fixing flags one at a time as each
false positive surfaces doesn't converge -- it just moves the bug to
whichever adjacent flag the same scoped clause happens to also trip.

**Fixed two ways, deliberately layered:**

1. **The scoped/unscoped check moved upstream of individual flags.**
   `_adjust_for_scoped_gate_signals()` replaces D-057's
   payment-only `_adjust_for_scoped_payment_language()`: it runs *once*
   per signal source, against that source's whole raw text, and clears
   *every* one of four flags the extractor found true --
   `requires_payment`, `requires_business_verification`,
   `requires_sales_contact`, `requires_phone_verification` -- if the text
   is explained by a scope qualifier or free-tier evidence and no
   unscoped-access language is present. One check, N flags, not N
   checks. `prohibits_automation`, `requires_captcha`, and
   `requires_sso_only` are deliberately excluded from this list --
   argued and rejected below.
2. **A structural guard for recipe-backed vendors (the real safety net
   against the *next* differently-worded clause).** A registered
   `SignupRecipe` is independent, out-of-band proof this vendor's free
   signup flow already works end to end (D-052/D-053: a real Alpha
   Vantage key acquired live in 5.59s, two days before this bug was
   found). For a recipe-backed vendor specifically, if a
   scope-suppressible flag blocks with *no* recognized scope-qualifier
   or free-tier marker *and no unscoped-access language either*, that's
   treated as a structural false positive and suppressed anyway --
   logged loudly (`D-058 STRUCTURAL FALSE POSITIVE` in the explain
   stream), not silently. This is what actually "stops the whack-a-mole"
   for these four vendors: the next unanticipated phrasing of the same
   scoped-commercial-tier idea doesn't need its own keyword added first.

**Why `prohibits_automation`/`requires_captcha`/`requires_sso_only` are
excluded, considered and rejected as candidates for the same treatment:**
these three are structural facts about the signup *mechanism* itself, not
claims about a pricing or usage tier. A signup form either has a CAPTCHA
widget or it doesn't; login is either SSO-only or it isn't; automated
account creation is either prohibited or it isn't -- none of these are
coherently "true only for commercial/premium use." A vendor cannot have a
CAPTCHA that only appears for paid customers and call that "scoped"
payment language; the CAPTCHA is either on the signup form credforge
actually has to submit, or it isn't. Generalizing scope-suppression to
them would mean actively working around exactly the class of real,
structural, principled-to-respect blocks D-046/OpenWeatherMap already
established this project won't try to defeat.

**A real, honest limit this generalization exposed, not fixed:** with
*both* flags correctly suppressed, Alpha Vantage's real live run still
does not reach `AUTO` -- it lands on `TOS_UNVERIFIABLE`. GATE never finds
a dedicated ToS page under any of its guessed paths for this vendor
(`could not locate a dedicated ToS/developer-agreement page`, confirmed
in the live run's own explain log), and D-021's principle -- a clean
docs-page scan is never sufficient evidence of a clean ToS on its own --
is untouched by this fix, correctly. This is a *separate*, pre-existing
gap (GATE's `_TOS_URL_GUESSES` list not including whatever path, if any,
Alpha Vantage's real ToS actually lives at) that D-058 did not touch and
was not asked to fix. Documented honestly rather than quietly patched in
under the same commit: see the live-verification report for the exact
outcome and OPS.md/README.md for whether this is worth a dedicated fix.

**Rejected: extending the structural guard to `TOS_UNVERIFIABLE`
itself** (i.e., a recipe-backed vendor with no blocking flags and no
findable ToS page clears to `AUTO` anyway). Tempting, since it would have
gotten Alpha Vantage to `AUTO` in this same pass -- but `TOS_UNVERIFIABLE`
is not a scope-suppressible flag's false positive, it's GATE genuinely
never having read *any* ToS text at all. "We already know this vendor's
signup form works" is not evidence about what its ToS says, and treating
recipe-backing as license to skip GATE's ToS check entirely would
contradict D-048's own explicit rule that a recipe pins identity only,
never the GATE verdict, and GATE must "still evaluate independently."
Not attempted here; would need its own decision, not inherited from this
one.

**Verified:** `tests/pipeline/test_gate.py` -- the reported
`requires_sales_contact` case is fixed directly; the structural guard
suppresses an unrecognized phrasing for `alphavantage.co` specifically
and is proven *not* to apply to an otherwise-identical non-recipe-backed
domain (a real negative test, not just the positive case); the guard is
proven not to override genuine unscoped-access language even for a
recipe-backed vendor; the Etsy and Open-Meteo regression guards from
D-057 still pass unchanged. Two Alpha Vantage end-to-end tests, not one:
the real shape (no findable ToS page) honestly asserts
`TOS_UNVERIFIABLE`, not `AUTO`; a second, otherwise-identical case with a
findable clean ToS page asserts the flag-suppression mechanism itself
does reach `AUTO` when nothing else is in the way. 242/242 tests pass.

### D-059: Real anchor-link discovery for the ToS page, not a bigger guess list -- Alpha Vantage's real ToS uses underscores, which none of seven hyphenated guesses ever matched

**The actual reason `TOS_UNVERIFIABLE` fired for Alpha Vantage after
D-058:** not a missing keyword, a missing URL. Its real ToS is at
`alphavantage.co/terms_of_service/` -- underscore-separated -- and every
one of `_TOS_URL_GUESSES`'s seven entries was hyphenated or bare
(`/terms-of-service`, `/terms`, `/tos`, ...). GATE never fetched the page
that would have told it anything at all.

**Two fixes, one narrow and one structural, both requested and both
done:**

1. **Narrow:** four underscore variants added to `_TOS_URL_GUESSES` --
   `/terms_of_service/`, `/terms_and_conditions/`, `/terms_of_use/`,
   `/privacy_policy/`. Fixes this one vendor. Does not fix the next one
   that guesses a different shape again.
2. **Structural:** `_find_tos_page()` now tries real anchor-link
   discovery *first* -- `_extract_tos_candidate_links()` parses real
   page HTML (stdlib `html.parser.HTMLParser`, no new dependency) for
   every `<a>` whose `href` or visible text contains `terms`, `tos`,
   `legal`, `conditions`, or `agreement`, resolves each to an absolute
   URL, and GATE fetches those candidates before ever touching the guess
   list. Two real sources are scanned: the docs page DISCOVER already
   crawled (`discovery.docs_text`, zero extra fetch cost, now passed
   into `_find_tos_page`) and the resolved homepage (one new fetch --
   the other place a footer commonly lives, and the only one for a
   vendor whose docs page itself has none). This is literally how a
   human finds a ToS page: they look at the footer, they don't guess
   URL shapes. The guess list (now with the underscore variants) is kept
   as a fallback, not replaced -- link discovery finds nothing on a page
   with no footer at all, and the fallback is what still finds Alpha
   Vantage's real page even without any link discovery, per fix #1.

**Why the narrow fix was still worth keeping, not just superseded by the
general one:** the general fix depends on the docs page or homepage
actually containing a real footer link -- a vendor whose only readable
page has no navigational footer at all (a bare API-endpoint domain, a
single-page app that renders its footer client-side and DISCOVER's plain
HTTP fetch never sees) still falls through to the guess list, and a
guess list that only covers hyphenated shapes would still miss an
underscore-shaped vendor in that case. Keeping both means the miss this
session actually found can never recur even in link-discovery's own
blind spot.

**Rejected: a full HTML/CSS selector library (e.g., adding
BeautifulSoup) instead of stdlib `HTMLParser`.** The task is bounded and
well-defined -- collect `(href, text)` pairs for every `<a>` tag, nothing
more -- and this project has never added a dependency where the stdlib
already does the job (see `heuristic_extractor.py`'s own regex-based
extraction, no parsing library at all). `HTMLParser` is stateful and a
little more verbose to drive than a selector call, but it's zero new
supply chain and zero new install step for a task this narrow.

**Rejected: treating every candidate link as equally worth trying, in
whatever order the HTML happened to list them, without any dedup.**
Implemented dedup (`seen` set) because the same URL commonly appears
twice on a real page (a footer link and a signup-flow "by continuing you
agree to our Terms" link, for instance) -- fetching it twice would waste
a real request for no benefit. Order itself is left as document order
(footer links, which is where the real candidates usually live, typically
come later in the page) -- not re-sorted by any authority signal, unlike
D-049's docs-candidate tier ranking; a ToS page is a ToS page, there's no
"more official" version of it to prefer among real candidates.

**Rejected: fetching every discovered candidate concurrently** (the
D-056 pattern). Real ToS pages are usually one or two candidates per
vendor, not RESOLVE's much longer per-app docs-candidate list D-056 was
written for -- the concurrency-management complexity isn't earning its
keep at this scale, and sequential-with-early-exit (stop at the first
usable page) is both simpler and, for the common one-candidate case,
identical in real latency.

**Verified:** `tests/pipeline/test_gate.py` -- direct unit coverage of
`_extract_tos_candidate_links` (matches on href, matches on visible text,
resolves a root-relative href correctly via `urljoin`, excludes
`mailto:`/`javascript:`/pure-anchor links, degrades to an empty list on
malformed HTML rather than raising); GATE finds Alpha Vantage's real
`/terms_of_service/` shape via docs-page link discovery; a separate case
proves homepage-only discovery works when the docs page itself has no
footer; link discovery finding nothing correctly falls back to the guess
list unchanged; the new underscore guesses are exercised directly when
link discovery has nothing to offer at all. Every pre-D-059 `test_gate.py`
test (47 in the file, 248 project-wide) still passes unchanged -- none of
the existing fixtures populate a homepage response, so they all correctly
fall through link discovery (finds nothing) straight to the same guess-
list behavior they tested before this change, proving the fallback is
truly backward compatible, not just theoretically so.

**Correction, found live before this was ever reported as verified --
two real bugs in the mechanism just described, both caught by fetching
Alpha Vantage's actual real docs page directly, outside the pipeline,
before trusting the synthetic test suite alone:**

1. **No domain filter -- link discovery would have trusted a third
   party's ToS as the vendor's own.** Alpha Vantage's real docs page
   cites the Federal Reserve (FRED), the IMF, and Investopedia as
   data-source attribution, each with a real, `terms`-keyword-matching
   link, all appearing *before* Alpha Vantage's own real ToS link in
   document order. `_extract_tos_candidate_links` had no concept of
   "the vendor being resolved" at all -- it would have fetched
   `fred.stlouisfed.org`'s Terms of Use (a real, live, fetchable page)
   and GATE would have evaluated *that* as if it were Alpha Vantage's
   policy. Fixed: every candidate link is now filtered to
   `registrable_domain(url) == vendor_domain` before it's ever fetched
   -- a subdomain of the vendor's own domain still counts (e.g. a
   `legal.example.com` link), an unrelated domain never does.
2. **`"tos"` matched as a bare substring inside `"ULTOSC"`** (Alpha
   Vantage's real Ultimate Oscillator API function name, appearing
   throughout its own example query URLs on the same docs page) --
   producing false-positive candidate links pointing at API query
   examples, not a ToS page. Identical bug class to D-022's
   `octocaptcha_origin_optimization`/`captcha` collision in
   `heuristic_extractor.py`, same fix: `_contains_tos_keyword()` now
   requires a `\b...\b` word-boundary match, not a bare `in` check.

**Why these were only found by fetching the real page directly, not by
the synthetic test suite:** every hand-written fixture in
`test_gate.py` was, by construction, shaped like a clean, minimal
example -- a footer with exactly one plausible link. Real vendor pages
are not minimal; they cite third parties and reuse ordinary English
words as API/function names, and only a genuinely messy real page
exercises either failure mode. Both are now covered by explicit
regression tests built from the real matched strings
(`test_extract_tos_candidate_links_excludes_third_party_domains`,
`test_extract_tos_candidate_links_does_not_match_tos_as_a_bare_substring`),
not just inferred from the live finding.

**The honest result after both corrections, verified live:** link
discovery now correctly finds exactly one candidate --
`https://www.alphavantage.co/terms_of_service/` -- and no others. GATE
still lands on `TOS_UNVERIFIABLE`, for a third, different, legitimate
reason: Alpha Vantage's real ToS is served as `application/pdf`, and
`HttpxFetchProvider` already, correctly, never decodes non-textual
content into `FetchResult.text` (`_is_textual_content_type()` only
allows `text/`, `json`, `xml` -- checked directly against the real
response). `_fetch_usable_page`'s `result.text` check then correctly
treats the PDF as unusable, the same way it already treats a soft-404
or a too-short page -- not a new gap, an existing, deliberate one
(fail closed on unreadable content, don't guess) doing exactly its job
against a real vendor for the first time. Reading a PDF ToS is real,
plausible future work (a `pdf` optional extra and a text-extraction
step at the fetch layer), explicitly not attempted here -- out of scope
for "find the right URL," which is what was asked and what's now fixed.

### D-060: PDF responses are extracted to text at the fetch layer -- the same size-capped bytes every content type already goes through

**Decided:** `HttpxFetchProvider.fetch()` gains a third content-handling
branch alongside "textual" and "everything else stays `text=None`":
`application/pdf` responses are extracted via `pypdf` (`_extract_pdf_text`,
`PdfReader` over the already-streamed, already-size-capped `body` bytes --
no new fetch, no new size check, the exact same `_stream_capped` guard
every other content type goes through). A genuine parse failure (a
corrupted or non-PDF body under that content type) raises `FetchException`
with a new, typed reason, `pdf_decode_error` -- added to
`FetchErrorReason`'s `Literal`, mirroring `decode_error`'s existing
treatment of a bad text codec exactly, not a new failure philosophy.
`pypdf` is a real, base dependency (`pyproject.toml`), not gated behind an
optional extra -- unlike `playwright`/`anthropic`, it has no heavy binary
or network-SDK weight, and this is core fetch-layer behavior every stage
already depends on unconditionally.

**Found live, the direct continuation of D-059:** Alpha Vantage's real
ToS -- now correctly located by D-059's link discovery -- is served as
`Content-Type: application/pdf`. Before this fix, `_is_textual_content_type()`
correctly (by original design) never decoded it, so `text` stayed `None`
and GATE could never confirm anything about the one vendor this whole
chain of fixes (D-057 through D-059) was working to unblock.

**Rejected: gating this behind a new `pdf` optional extra**, matching
`playwright`/`anthropic`'s pattern. Those are heavy (a browser binary
download, a network SDK) and genuinely optional -- most of this
project's default, offline-capable path never needs them. PDF handling
at the fetch layer is neither: `pypdf` is a small, pure pip install, and
"can this stage read the page it just fetched" is not a feature any
caller opts into, it's baseline fetch-layer behavior every stage already
assumes works for whatever content type a real vendor happens to serve.

**Rejected: OCR or scanned-image PDF support.** `pypdf`'s `extract_text()`
reads the PDF's real text layer; a scanned, image-only PDF has none and
would still come back empty (not raise -- `page.extract_text() or ""`
degrades gracefully per page). Out of scope; not the case found live.

**Verified:** `tests/providers/test_httpx_fetch.py` -- a real, hand-built,
valid minimal PDF (correct object offsets, computed programmatically, not
guessed) round-trips through a real `pypdf` parse and the extracted text
reaches `FetchResult.text`; the existing size cap still rejects an
oversized PDF before any bytes are streamed, unchanged from D-030; a
corrupted PDF body raises the new typed `pdf_decode_error`, not an
unhandled `pypdf` exception. 253/253 tests pass.

### D-061: A plausible hypothesis (vendor-side dedupe) tested directly and found false -- the real cause was a stale extraction regex, one word different

**The premise going in:** after D-057 through D-060 cleared every GATE
block, Alpha Vantage's PROVISION step still failed extraction
(`api_key_page_regex did not match the page content`). The most obvious
explanation -- the same plus-addressed email alias (`alias_for()` is
stable per identity_key) had already been used against this vendor
several times this session, so maybe Alpha Vantage's own system was
refusing to issue a second key to an email it already recognized, and
showing a "you already have a key" page instead of a fresh one. Ephemeral
Railway storage means credforge's own registry guard (`provision()`'s
`find_open_provision` idempotency check) can't ever observe this --
every deployed container starts with an empty registry, so it would
retry the real vendor signup fresh every time regardless of what the
vendor itself remembers about that email.

**Tested directly, not assumed:** attached a Playwright response listener
to the real signup submission (the *actual* network response, not a
guess at page text) using the exact same reused alias. The real response
from `POST https://www.alphavantage.co/create_post/` was:

```json
{"result": "Create post successful!", "text": "Welcome to Alpha Vantage! Your API key is: IE5ONPJ8S23PVPGA. Please record this API key at a safe place for future data access."}
```

A brand-new, real, valid key -- issued on a repeat submission with an
email already used multiple times this session. **There is no observed
vendor-side dedupe.** The actual cause was simpler and less interesting:
Alpha Vantage's success copy changed from *"Your dedicated access key
is: X"* (the wording the recipe's regex was written against, and the
wording a real key earlier this session actually used) to *"Your API
key is: X"* -- a one-word copy change on the vendor's side, sometime
between then and now, that the old regex simply didn't match. Ordinary
selector/regex drift -- exactly the class of risk already named, before
this incident, in README's "What I'd build next" (selector-drift
detection) -- not a new phenomenon and not vendor-side idempotency.

**Decided, given the tested-false premise:** no `ALREADY_PROVISIONED_AT_VENDOR`
outcome, no dedupe-detection code. Building a detector for wording that
was directly checked and does not appear would be speculative code with
no real backing -- exactly what this project's evidentiary standard
exists to prevent. Instead: `ALPHA_VANTAGE.api_key_page_regex` now
matches *either* observed real wording --
`r"Your (?:dedicated access key|API key) is:\s*([A-Z0-9]{16})"` -- since
both are independently confirmed real (at different points this
session), not just the newest one, in case the vendor is inconsistent
or reverts. Re-verified live with a fresh registry (bypassing the local
idempotency guard, matching how the real ephemeral Railway deployment
always starts): `provision()` → `status="provisioned"`, `validate()` →
`status="valid"`, HTTP 200, ~7.7s total, real key vaulted and validated.
Alpha Vantage is now the first vendor in this project to go from app
name to a live-acquired, live-validated credential through a real
`--live` deployment run *and* clear every GATE check for real, not
mocked -- NASA's earlier live credential (D-052/D-053) predates the
tier-contradiction and GATE-scoping fixes this chain of work (D-057
through D-060) exists to correct.

**Rejected: leaving the regex matching only the newest wording.** Both
phrasings are independently, separately confirmed real from this same
vendor at different points -- narrowing to just the latest one repeats
the exact mistake (assuming a vendor's copy is stable) that caused this
incident in the first place.

**Verified:** the fix is the regex change itself, verified against a
real captured network response (not a synthetic fixture) and a real,
fresh, live `provision()` + `validate()` run. No new automated test was
added for this specific regex string (a live vendor's exact copy isn't
something a unit test can pin without becoming exactly the kind of
brittle, guessed fixture this investigation just warned against); the
existing `PlaywrightBrowserDriver` regex-matching tests
(`test_recipe_with_no_extraction_mechanism_fails_instead_of_reporting_empty_success`
and the extraction-branch logic it exercises) are unaffected. 253/253
tests pass.
