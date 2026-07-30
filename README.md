# credforge

## What it does

credforge is a CLI agent that automates the pre-work an engineer does
before building a new SaaS integration: it finds the vendor's real
developer documentation, works out how its API is authenticated, and
checks — by actually reading the vendor's Terms of Service, not
guessing — whether an automated signup is even allowed, then (mocked by
default, real under `--live`) provisions a developer account and
validates the resulting credential with one real API call. Every run
emits a `HandoffArtifact`: a frozen, schema-validated JSON record of what
was found, what it costs to trust it (`completeness_gaps`), and either a
`vault_ref` pointing at an encrypted credential or the specific reason a
human needs to look at this one instead. That artifact is the contract —
it's what a downstream toolkit-generation agent consumes to actually
build the integration, without needing to re-derive any of the research
that went into it.

## Measured coverage — real seed batch, `run_20260730T170035Z_4a8d3af4`

**These are measured counts from one real run of `credforge batch
data/seed_apps.csv` against the live internet — real DuckDuckGo search,
real HTTP fetches, real `claude-sonnet-5` extraction, mocked (non-`--live`)
provisioning. Nothing below is projected or estimated; every count below
is read directly from this run's 20 artifact files and `report.json`.**

**Read this table with the volatility callout below it, not instead of
it.** A second full batch run, same code, same seed list, produced
materially different numbers for several apps for reasons that trace to
the search provider and LLM extraction, not to credforge's own logic —
see "What it gets wrong" for the full reproducibility investigation
before treating any single number here as a stable measurement of vendor
behavior.

| status | count | share |
|---|---|---|
| AUTO | 2 | 10% |
| HITL | 15 | 75% |
| UNSUPPORTED | 3 | 15% |

All 20 seed apps produced a real artifact in this run — including every
app that failed at RESOLVE — so this table's total is exactly
`report.json`'s `total_apps: 20`, no reconciliation against CLI console
output needed (D-038/D-039).

**The HITL bucket splits into two categories that mean very different
things, and collapsing them into one number hides the one that actually
matters:**

| category | count | what it means |
|---|---|---|
| **VENDOR_BLOCKED** — irreducible, a human must act | **0** | the vendor's own policy requires payment, business/phone verification, a CAPTCHA, SSO-only signup, or explicitly prohibits automation |
| **PIPELINE_LIMITED** — reducible, credforge just couldn't confirm | **10** | the vendor doesn't require a human here — credforge's own detection ran out of confidence or coverage before it could say so |

**That "0" is this run's real, literal count — and it is also
demonstrably not a reliable measurement of how often vendors actually
block.** Re-running three of this run's non-VENDOR_BLOCKED apps in
isolation immediately surfaced real, evidence-backed vendor blocks the
batch missed:

- **Etsy** landed on `resolve_not_found` in this run. Re-run alone
  moments later: `tos_prohibits_automation`, with developers.etsy.com's
  own docs quoted directly: *"Applications must not sidestep the API to
  retrieve or post Etsy data. Screen-scraping is not allowed."*
- **OpenWeatherMap** landed on `discovery_failed` in this run (and in a
  second full batch run — see below). Re-run alone, five separate times:
  five real VENDOR_BLOCKED results (`requires_payment` x2,
  `requires_sales_contact` x2, `tos_unverifiable` x1), zero failures.
- **Open-Meteo** landed on `tos_unverifiable` in this run (and in a
  second batch run too). An earlier real run landed on `requires_payment`
  with open-meteo.com's own docs quoted: *"apikey ... Only required to
  commercial use to access reserved API resources for customers."*

PIPELINE_LIMITED, by reason code, this run:

- `classify_low_confidence` (8): Auth0, HubSpot, Monday.com, Salesforce,
  Slack, Spotify, Stripe, Trello
- `discovery_failed` (1): OpenWeatherMap
- `tos_unverifiable` (1): Open-Meteo

Plus 5 apps that stopped at RESOLVE, before DISCOVER or GATE ever ran —
counted inside the HITL total above, not a separate bucket, since all
five produced real HITL artifacts: `resolve_not_found` (Discord, Etsy),
`resolve_low_confidence` (Google Calendar, Mailgun), `resolve_ambiguous`
(Twitter).

**AUTO: GitHub, SendGrid (both `eligible_auto`).**

**UNSUPPORTED (3, up from 1): NASA API, Notion, Superhuman — all
`no_public_api`.** NASA landing here is a direct, verified effect of
today's fix, not a coincidence — see "What it gets wrong."

**The honest metric this run points to — "of 15 apps a human was asked to
look at, 0 actually needed one" — is real but misleading on its own,**
because it's built from a run whose own reason codes for exactly the
apps that would answer this question (Etsy, Open-Meteo, OpenWeatherMap)
are shown above to be unstable. The truer statement, backed by repeated
real runs: **at least 3 of these 20 vendors have a real, quotable,
evidence-backed policy blocking automation** — this run's batch execution
simply didn't land on all three at once. Fail-closed toward HITL is still
the correct default (a false `AUTO` hands a downstream agent a
credential-shaped promise that doesn't exist — see the Spotify soft-404
below), but "the coverage table" as a single-run snapshot is a weaker
instrument than this project has previously presented it as.

**Which single fix would move `classify_low_confidence` most:**
source-authority weighting on docs candidates (see "what's next") —
CLASSIFY reading a landing/table-of-contents page instead of a detailed
reference page, the same failure mode as Salesforce's Trailhead-tutorial
misread (see "what it gets wrong"). `classify_low_confidence` is 8 of the
10 PIPELINE_LIMITED apps this run — still the dominant cause, though the
exact count moves between runs (11 in the previous measured run).

**The original seed list's own predicted outcomes (in the build plan)
continue to turn out wrong for a majority of apps, run to run, not just
once** — Superhuman is the one seed prediction that has now held twice
running (`UNSUPPORTED`/`no_public_api`, both this run and the prior
measured one); everything else has landed differently across the two
most recent real runs alone, which is itself the volatility finding
below, not a separate story per app.

## What it gets wrong

- **A soft-404 produced a false `AUTO`, and the first fix for it was
  incomplete.** Running Spotify through GATE, a guessed ToS URL returned
  HTTP 200 with Spotify's own branded "page not found" copy. Nothing
  distinguished that from a real ToS page, GATE found no prohibition in
  it (correctly — it wasn't ToS text), and cleared Spotify to `AUTO`
  having never reviewed real terms. The first fix added a soft-404
  keyword check and looked complete after one live re-run — it wasn't.
  The *next* guessed URL was a second, differently-worded Spotify
  soft-404 the first keyword list didn't catch, and Spotify still cleared
  to `AUTO`, from a different URL. Only fetching all seven guessed paths
  directly and comparing them by hand caught that both were soft-404s.
  Full account in DECISIONS.md D-035. The underlying detection is still a
  hand-maintained keyword list, not a general solution — see "what's
  next."
- **The coverage table above is a noisier instrument than a single run
  makes it look, confirmed by running the exact same batch twice.** Two
  full 20-app batch runs, same code, same seed list, back to back:
  10 of 20 apps landed on the *same* status and reason code both times;
  the other 10 didn't, for reasons with zero connection to any code
  changed today (their RESOLVE call is byte-identical run to run — the
  difference is DDG's returned results and the LLM extraction's read of
  them, not credforge's own logic). Etsy alone produced three different
  reason codes across three separate real runs
  (`tos_prohibits_automation` / `resolve_not_found` / `tos_unverifiable`).
  `TOS_UNVERIFIABLE` specifically is real and current (D-021's
  deliberately safe fallback when GATE's ToS-path guesses miss), but
  *which* apps land there is itself unstable run to run — treat any
  single run's per-app breakdown as one sample, not a stable
  classification of the vendor.
- **RESOLVE now correctly points at a vendor's real docs — and that
  surfaced a second, separate, previously-hidden problem: DISCOVER can't
  read a JavaScript-rendered page.** NASA API's `docs_url` now correctly
  pins to `api.nasa.gov` (D-048) instead of the wrong `sti.nasa.gov` — but
  DISCOVER (a plain HTTP fetch, no JS execution) sees only a
  `Loading signup form...` placeholder shell there, since the real content
  loads client-side. Confirmed directly: the raw HTML has no API
  documentation prose at all. Only `PlaywrightBrowserDriver` (PROVISION)
  renders JS in this project. NASA now lands on `UNSUPPORTED`/
  `no_public_api` — reproducibly, four runs straight — a real, honest
  regression in *what DISCOVER can see*, traded for a real fix in *where
  RESOLVE points*. Separately: OpenWeatherMap's newly-pinned docs URL
  (`openweathermap.org/api`) succeeds reliably in isolation (5 of 5 runs)
  but failed in both full 20-app batch runs specifically — ruled out the
  per-domain rate limiter (no cross-domain interference) and ruled out
  "batch command mechanics" (a batch containing only OpenWeatherMap
  succeeds); root cause not fully isolated, most likely something in
  httpx's connection handling over a long sequential run. Both are real,
  current, unresolved gaps, not swept into the coverage table's "0
  VENDOR_BLOCKED" without comment.
- **CLASSIFY's confidence score measures how clearly the extraction task
  could be done, not whether the answer is actually correct.** A page can
  state an auth scheme in unambiguous prose and still be the wrong page
  to learn the real flow from. In one real trace, Salesforce's extraction
  confidently returned `oauth2_client_credentials` — pulled from a
  Trailhead *tutorial* page, not Salesforce's actual API reference. High
  confidence there means "the tutorial was clearly written," not "this is
  definitely how the production integration authenticates." Nothing in
  the current pipeline separates "confidently read" from "read from an
  authoritative source" — see "what's next."
- **The default search provider (no-key `ddgs`/DuckDuckGo) trades
  reliability for zero billing setup.** It throttles unpredictably and
  has no documented rate-limit contract, unlike Brave's paid API — this
  is very likely a real contributor to both RESOLVE's ~40-second median
  latency in this run and to some fraction of the `resolve_low_confidence`
  /`resolve_ambiguous` outcomes above being search-quality artifacts
  rather than genuine ambiguity. Brave is a one-env-var swap
  (`BRAVE_API_KEY`) but requires a card on file, which is exactly the
  constraint that made DDG the default in the first place (D-024).
- **The chaos properties described in the original build plan are real,
  passing tests — but scattered across the normal test suite, not
  consolidated into the dedicated `tests/chaos/` directory the plan
  specified.** A corrupted registry line being skipped, not fatal
  (`registry/store.py`'s tests), a corrupted artifact file not sinking a
  whole report (`test_report.py`), a failed provisioning attempt never
  leaving a partial vault write (`test_provision.py`) — the underlying
  behavior is tested and correct. What's missing is the presentation:
  one file per named scenario, in one place, as its own deliverable. That
  was cut for time this stage, not silently dropped from scope.

## What I'd build next

- **Source-authority weighting on docs candidates.** Right now a docs
  candidate is accepted or rejected by content pattern-matching alone
  (D-027) — a marketing page with enough incidental API vocabulary can
  pass the same bar as an actual reference page. The Salesforce-Trailhead
  case above is exactly this failure mode one layer downstream, in
  CLASSIFY instead of DISCOVER. Weighting candidates by *where* they live
  (a `/reference` or `/api-reference` path outranks a `/tutorials` or
  `/guides` path, a `developer.` subdomain outranks a general docs
  subdomain) would catch this before CLASSIFY ever sees the wrong page,
  which is a more durable fix than trying to make CLASSIFY itself
  distrust tutorial-flavored prose.
- **Parallelized subdomain/path probes.** RESOLVE and GATE both issue
  their conventional-guess fetches (docs subdomains, ToS paths)
  sequentially, one at a time, bounded only by the existing per-domain
  rate limiter — logged as a known latency characteristic since Stage 4
  and directly visible in this run's 40-second RESOLVE. Issuing them
  concurrently (still rate-limited per domain) is a real, scoped fix that
  was deliberately deferred to a real concurrency-budget decision rather
  than patched into one stage at a time.
- **Selector-drift detection on provisioning.** `PlaywrightBrowserDriver`
  requires an explicit per-vendor `SignupRecipe` (CSS selectors) rather
  than attempting generic form-filling (D-031) — the right call for
  correctness, but a recipe silently breaks the moment a vendor redesigns
  their signup page, and nothing today would notice except a confusing
  `PROVISION_FAILED`. Detecting drift (the expected selector exists but
  the page structure around it changed materially since the recipe was
  written) and surfacing that as a distinct, actionable signal — rather
  than the same generic failure a wrong password would produce — is real
  work worth doing before `--live` provisioning runs unattended against
  more than a handful of hand-verified vendors.

---

## The rest of the docs

- [`RUNBOOK_MANUAL.md`](RUNBOOK_MANUAL.md) — the human process this
  replaces, step by step, marked with what's automated vs. handed off.
- [`DECISIONS.md`](DECISIONS.md) — every meaningful technical choice,
  what was rejected, and why. 48 entries.
- [`TRACE.md`](TRACE.md) — GitHub, followed end to end, real numbers,
  written to be read aloud.
- [`OPS.md`](OPS.md) — where this runs, what happens if it dies
  mid-batch, and (now measured) what it costs.

## Install

    pip install -e ".[dev]"
    # optional: pip install -e ".[llm]"   -- real claude-sonnet-5 extraction
    # optional: pip install -e ".[live]"  -- real IMAP + Playwright provisioning

## Quick start

    credforge resolve "Stripe"                    # RESOLVE only, no other stage
    credforge run "Stripe" --dry-run --explain     # full research pipeline, stops at GATE
    credforge run "Stripe"                         # + mocked PROVISION/VALIDATE if AUTO
    credforge batch data/seed_apps.csv --dry-run   # every app in a CSV, one failure never stops the batch
    credforge report <run_id>                      # aggregate a run's artifacts
    credforge revoke stripe.com                    # mark a provisioned account closed

Nothing above requires an API key: search defaults to the no-key `ddgs`
provider, extraction defaults to a deterministic heuristic extractor
without `ANTHROPIC_API_KEY`. See [`.env.example`](.env.example) for what
each optional key unlocks.
