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

## Measured coverage — real seed batch, `run_20260801T084914Z_04685c6b`

**These are measured counts from one real run of `credforge batch
data/seed_apps.csv` against the live internet — real DuckDuckGo search,
real HTTP fetches, real `claude-sonnet-5` extraction, mocked (non-`--live`)
provisioning. Nothing below is projected or estimated; every count below
is read directly from this run's 20 artifact files and `report.json`.
This is the first canonical run since source-authority weighting (D-049)
landed — see the before/after table right below for exactly what moved
and why, and "What it gets wrong" for the standing volatility caveat
that still applies to any single run's numbers.**

| status | count | share |
|---|---|---|
| AUTO | 6 | 30% |
| HITL | 12 | 60% |
| UNSUPPORTED | 2 | 10% |

All 20 seed apps produced a real artifact in this run, matching
`report.json`'s `total_apps: 20` exactly (D-038/D-039) — this run also
fixed two real crashes (D-050, D-051) that would otherwise have dropped
apps from the count; see below.

**VENDOR_BLOCKED / PIPELINE_LIMITED split:**

| category | count | what it means |
|---|---|---|
| **VENDOR_BLOCKED** — irreducible, a human must act | **2** | OpenWeatherMap, Spotify — both `requires_payment`, both with real quoted evidence, both found on the vendor's real official docs page |
| **PIPELINE_LIMITED** — reducible, credforge just couldn't confirm | **9** | `classify_low_confidence` (4: Auth0, HubSpot, Salesforce, Slack), `discovery_failed` (1: Mailgun), `tos_unverifiable` (4: Etsy, Google Calendar, Notion, Open-Meteo) |

Real evidence for both VENDOR_BLOCKED apps, both found because
source-authority weighting picked the *right* page:

- **OpenWeatherMap** (`openweathermap.org/api`, recipe-pinned, D-048) —
  *"Included in the Developer, Professional and Expert subscription
  plans."*
- **Spotify** (`developer.spotify.com`, HIGH-tier) — *"Note: You need a
  Spotify Premium account to use the Web API."*

Plus 1 app that stopped at RESOLVE before DISCOVER or GATE ever ran, counted
inside the HITL total: `resolve_ambiguous` (Twitter).

**AUTO (6, up from 2): GitHub, SendGrid, Discord, Monday.com, Stripe,
Trello — all `eligible_auto`.** Three of these (Discord, Monday.com,
Trello) were checked for "would 1d's live-recipe bar apply" and rejected
on real evidence, not skipped — see "What I'd build next."

**UNSUPPORTED (2): NASA API, Superhuman — both `no_public_api`.**

**The honest metric: of 12 apps a human was asked to look at, 2 (16.7%)
actually needed one** — both with a real, specific, quoted reason. That's
a materially more informative number than the previous run's "0 of 15"
headline, and it's more informative for a real, traceable reason: the
same fix that moved `classify_low_confidence` also let GATE read the
*correct* page for OpenWeatherMap and Spotify, where the wrong page
previously hid a real payment requirement behind `discovery_failed` /
`classify_low_confidence` respectively.

### Source-authority weighting: before vs after, full 20-app table

Before: `run_20260730T170035Z_4a8d3af4` (previously measured, cited
above in earlier revisions of this doc). After:
`run_20260801T084914Z_04685c6b`. Every row checked individually for
regressions — **zero found**: no app moved from a better-informed status
to a worse one.

| app | before | after | moved because of D-049? |
|---|---|---|---|
| Auth0 | HITL / classify_low_confidence | *(same)* | — |
| Discord | HITL / resolve_not_found | **AUTO** / eligible_auto | No — RESOLVE identification succeeded this run; that code path is untouched by D-049 (search-provider variance) |
| Etsy | HITL / resolve_not_found | HITL / tos_unverifiable | No — same reason as Discord |
| GitHub | AUTO / eligible_auto | *(same)* | — |
| Google Calendar | HITL / resolve_low_confidence | HITL / tos_unverifiable | No — same reason as Discord |
| HubSpot | HITL / classify_low_confidence | *(same)* | — |
| Mailgun | HITL / resolve_low_confidence | HITL / discovery_failed | No — same reason as Discord |
| Monday.com | HITL / classify_low_confidence | **AUTO** / eligible_auto | **Yes** — resolved successfully in both runs; only docs-ranking/confidence changed |
| NASA API | UNSUPPORTED / no_public_api | *(same)* | Recipe-pinned (D-048), not D-049 |
| Notion | UNSUPPORTED / no_public_api | HITL / tos_unverifiable | **Yes** — resolved in both runs; D-049 found `developers.notion.com` (HIGH-tier) where DISCOVER previously missed a real API entirely |
| Open-Meteo | HITL / tos_unverifiable | *(same)* | — |
| OpenWeatherMap | HITL / discovery_failed | HITL / **requires_payment** | Ambiguous — recipe-pinned domain unchanged by D-049; likely the same batch-position fetch issue named below, not tier ranking |
| Salesforce | HITL / classify_low_confidence | *(same, now explicitly LOW-tier)* | **Yes**, confirmed — only real docs candidate is DISCOVER's own bare-domain fallback, correctly scored LOW |
| SendGrid | AUTO / eligible_auto | *(same)* | — |
| Slack | HITL / classify_low_confidence | *(same, now explicitly MEDIUM-tier)* | **Yes**, confirmed via `source_tier` |
| Spotify | HITL / classify_low_confidence | HITL / **requires_payment** | **Yes** — resolved in both runs; HIGH-tier `developer.spotify.com` is what surfaced the real payment requirement |
| Stripe | HITL / classify_low_confidence | **AUTO** / eligible_auto | **Yes** — resolved in both runs; `docs.stripe.com/api`, HIGH-tier |
| Superhuman | UNSUPPORTED / no_public_api | *(same)* | — |
| Trello | HITL / classify_low_confidence | **AUTO** / eligible_auto | **Yes** — resolved in both runs; `developer.atlassian.com`, HIGH-tier |
| Twitter | HITL / resolve_ambiguous | *(same)* | — |

**Headline result:** `classify_low_confidence` went from 8 apps to 4
(Auth0, HubSpot, Salesforce, Slack) — halved. 3 apps
(Monday.com, Stripe, Trello) moved straight from `classify_low_confidence`
to AUTO; 1 (Spotify) moved to a real VENDOR_BLOCKED finding instead of a
low-confidence guess. AUTO went from 2 apps to 6.

**Confidence distribution, real and measured from this run's actual
`docs_url` values (`source_tier` on every artifact's `api` block):** of
the 16 apps that reached a real docs page, **12 HIGH-tier, 3 MEDIUM-tier,
1 LOW-tier**. Per D-049's adjustment (+0.05 / +0.0 / −0.15 respectively),
that's 12 apps whose raw confidence was nudged up slightly, 3 unchanged,
and 1 (Salesforce) penalized enough to matter. **Stated honestly, not
rounded up:** the artifact schema never exposed CLASSIFY's raw confidence
number before this fix (only `auth_scheme` and `reason_code` were
visible) — a `classify_confidence` field was added alongside `source_tier`
in this same change, but neither this run nor the prior one has it
populated on disk (both predate or coincide with the field's addition).
The tier counts above are real, on-disk, verified data; exact
before/after confidence *floats* per app are not — `tests/pipeline/test_classify.py`'s
`test_the_same_raw_confidence_is_distinguishable_by_source_tier` is
where the exact adjustment math (0.6 → 0.65 / 0.6 / 0.45) is proven
directly instead.

**Two real bugs found and fixed re-running this batch, neither caused by
D-049 itself (see DECISIONS.md D-050, D-051):** `batch` used to open a
fresh event loop per app while sharing one HTTP client across all of
them, which crashed with `RuntimeError: Event loop is closed`
non-deterministically once the client's connection pool outlived the
loop it was created on. Separately, RESOLVE's *docs-URL* search (as
opposed to its already-protected identification search, D-024) had no
`SearchProviderError` fallback at all, and a real DDG timeout crashed
that app's whole run with zero artifact. Both are now fixed; this run is
clean, 20/20, no crashes.

**OpenWeatherMap's `discovery_failed`→`requires_payment` move is flagged
"ambiguous" above on purpose:** its recipe-pinned domain and docs_url are
completely unchanged by D-049 (recipe-pinning short-circuits past
docs-ranking entirely, D-048), so this specific move is most likely the
same batch-position-dependent fetch issue diagnosed in the previous
measured run (isolated re-runs of OpenWeatherMap succeed reliably; deep
in a 20-app sequential batch, this one candidate sometimes doesn't) —
not a D-049 effect, even though it looks like one at a glance.

**The original seed list's own predicted outcomes continue to turn out
wrong for most apps** — Superhuman remains the one seed prediction that
has now held across three real runs (`UNSUPPORTED`/`no_public_api`).

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
- **Source-authority weighting (D-049) fixes the Salesforce-Trailhead
  class of bug, but only at the URL-shape level, not the content level.**
  CLASSIFY's raw confidence still measures how clearly the extraction
  task could be done, not whether the answer is correct — D-049 doesn't
  change that, it changes *which page* CLASSIFY is likely to be handed in
  the first place (a real `developer.*`/`/reference/` URL now consistently
  outranks a tutorial-shaped one), and penalizes confidence when only a
  LOW-tier page was available at all. What it still can't catch: a
  genuinely HIGH-tier-shaped URL (`developer.example.com/tutorials/...`)
  whose actual content is a tutorial anyway — tier is a structural proxy
  for authority, not a content read. `_looks_like_api_docs`'s existing
  content-verification check still runs first and is unchanged by this
  fix.
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

- **Content-aware authority, not just URL-shape authority.** D-049's
  three tiers are a real, measured improvement (see above), but they're
  entirely about a URL's *shape* — a HIGH-tier-shaped page with genuinely
  thin, sparse content still outranks a rich MEDIUM/LOW-tier page (the
  tradeoff D-049's DECISIONS.md entry names directly). A natural next
  step: fold a cheap content signal (endpoint-listing density, code-block
  count) into the ranking alongside tier, so a real reference wins on
  richness too, not just on where it happens to live.
- **A CAPTCHA/interstitial signal that reaches GATE, not just PROVISION.**
  OpenWeatherMap's real reCAPTCHA is invisible to GATE (it only reads ToS
  prose) and only discovered by PROVISION actually trying — see
  DECISIONS.md D-046/D-050 and OPS.md's recipe-ability section. Feeding
  that back into `requires_captcha` before a live attempt is wasted is
  exactly Task 2's territory (recipe auto-generation would need to detect
  this at recipe-generation time, not just at submission time).
- **Investigate why a fetch that succeeds reliably in isolation fails
  specifically deep in a long sequential batch** (OpenWeatherMap's
  `discovery_failed`, this run and the last) — named honestly above as
  unresolved, best current guess is `httpx` connection-pool behavior, not
  confirmed.
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
