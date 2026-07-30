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

## Measured coverage — real seed batch, `run_20260730T023149Z_37a0e4fe`

**These are measured counts from one real run of `credforge batch
data/seed_apps.csv` against the live internet — real DuckDuckGo search,
real HTTP fetches, real `claude-sonnet-5` extraction, mocked (non-`--live`)
provisioning. Nothing below is projected or estimated; every count below
is read directly from this run's 20 artifact files and `report.json`.**

| status | count | share |
|---|---|---|
| AUTO | 2 | 10% |
| HITL | 17 | 85% |
| UNSUPPORTED | 1 | 5% |

All 20 seed apps produced a real artifact in this run — including the two
that failed at RESOLVE — so this table's total is exactly `report.json`'s
`total_apps: 20`, no reconciliation against CLI console output needed.
That's itself a measured result, not a given: it's what D-038/D-039 (EMIT
now builds a real HITL artifact from a failed RESOLVE or DISCOVER, instead
of returning early with nothing) were built to guarantee.

**The HITL bucket splits into two categories that mean very different
things, and collapsing them into one number hides the one that actually
matters:**

| category | count | what it means |
|---|---|---|
| **VENDOR_BLOCKED** — irreducible, a human must act | **2** | the vendor's own policy requires payment, business/phone verification, a CAPTCHA, SSO-only signup, or explicitly prohibits automation |
| **PIPELINE_LIMITED** — reducible, credforge just couldn't confirm | **13** | the vendor doesn't require a human here — credforge's own detection ran out of confidence or coverage before it could say so |

VENDOR_BLOCKED, by app and real evidence:

- **Etsy** — `tos_prohibits_automation`. developers.etsy.com's own docs
  state: *"Applications must not sidestep the API to retrieve or post
  Etsy data. Screen-scraping is not allowed."*
- **Open-Meteo** — `requires_payment`. open-meteo.com's own docs state:
  *"apikey ... Only required to commercial use to access reserved API
  resources for customers."*

PIPELINE_LIMITED, by reason code:

- `classify_low_confidence` (11): Auth0, Discord, GitHub, HubSpot,
  Monday.com, NASA API, Notion, Salesforce, Slack, Spotify, Stripe
- `discovery_failed` (1): Mailgun
- `tos_unverifiable` (1): OpenWeatherMap

Plus 2 apps that stopped at RESOLVE, before DISCOVER or GATE ever ran —
counted inside the HITL total above, not a separate bucket, since both
produced real HITL artifacts: `resolve_low_confidence` (Google Calendar)
and `resolve_ambiguous` (Twitter).

**AUTO: SendGrid, Trello (both `eligible_auto`).**

**The honest metric this points to: of the 17 apps a human was asked to
look at, 2 (11.8%) actually needed one.** That's a meaningfully different
claim than "the pipeline never blocks on a real vendor policy" — it shows
the VENDOR_BLOCKED/PIPELINE_LIMITED split doing real work: Etsy and
Open-Meteo have specific, quoted evidence backing a real human-required
stop, while the other 15 are credforge running out of confidence, not the
vendor saying no. There's still real, measurable headroom to recover apps
into AUTO without loosening anything that matters — just not from zero.

**Which single fix would move this number most:** source-authority
weighting on docs candidates (see "what's next") — it targets
`classify_low_confidence`'s dominant real cause directly: CLASSIFY
reading a landing/table-of-contents page instead of a detailed reference
page, exactly what happened to GitHub in this run and, one stage over, to
Salesforce's Trailhead-tutorial misread (see "what it gets wrong"). Upper
bound: all 11 `classify_low_confidence` apps. Realistic estimate,
qualitative, not a re-measured figure: **a majority, not all** — some of
these vendors' real docs may genuinely lack the specificity CLASSIFY
needs regardless of which page gets picked, so not every miss is a
page-selection problem. `classify_low_confidence` alone is 11 of the 13
PIPELINE_LIMITED apps — it's the dominant cause by a wide margin, whatever
fraction of it this specific fix recovers.

**Why fail-closed is the correct error direction, not just the cautious
one:** a false `AUTO` hands a downstream agent a credential-shaped
promise that doesn't exist — the Spotify soft-404 (below) is the proof,
not a hypothetical: GATE almost shipped an `AUTO` verdict for an app
whose real Terms of Service were never actually read. A false `HITL`
costs a human three minutes confirming what credforge couldn't. Those
two failure costs are not remotely symmetric, which is why every
uncertain case in this pipeline resolves toward HITL, never toward AUTO
— and the 2-of-17 figure above is what that conservatism is supposed to
produce: a small, evidence-backed VENDOR_BLOCKED count, with the rest
recoverable.

**The original seed list's own predicted outcomes (in the build plan)
turned out wrong for a majority of apps, once real research ran against
them — worth saying plainly rather than quietly editing the old
predictions away:**

- **Superhuman** was seeded specifically as the `UNSUPPORTED` /
  `NO_PUBLIC_API` test case, and in this run it landed exactly there —
  `no_public_api`, the prediction holding up. That's also evidence D-037's
  fix (making `UNSUPPORTED` reachable through the real pipeline at all,
  not just in a unit test — see DECISIONS.md D-037) works end to end, not
  just in the test that originally caught the bug.
- **Monday.com** was seeded as the disambiguation demo (an intentionally
  ambiguous name). It resolved past RESOLVE cleanly — the ambiguity
  itself wasn't a problem — but landed on `classify_low_confidence`, not
  `AUTO`; the disambiguation the seed list meant to test isn't what this
  run actually exercised.
- **Discord** was seeded as the real `TOS_PROHIBITS_AUTOMATION` example.
  It got past RESOLVE and DISCOVER this run and landed on
  `classify_low_confidence` — further than a `resolve_low_confidence`
  stop, but still not far enough to test the ToS-prohibition path. Etsy,
  not Discord, is this run's real `tos_prohibits_automation` case (see
  above), with its own quoted evidence.
- **Google Calendar**, not Discord, is this run's `resolve_low_confidence`
  case — RESOLVE scored `google.com` at 0.6357 against "Google Calendar,"
  just under the 0.7 threshold, because the name-similarity check compares
  the full app name against a domain label containing only part of it.
- Of the apps the plan marked "to verify": Mailgun and Etsy landed in the
  same status family the plan guessed (HITL), just not always the same
  reason code — Mailgun on `discovery_failed` rather than a
  payment/verification block, Etsy on a real ToS prohibition rather than
  app review. **SendGrid did not** — the plan guessed HITL
  (phone/manual review), and it landed on `AUTO` (`eligible_auto`)
  instead, a different status family, not just a different reason code.

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
- **`TOS_UNVERIFIABLE` is a real, current coverage gap, not just a safe
  fallback.** 1 of 20 seed apps (OpenWeatherMap) landed there this run
  because GATE's seven conventional ToS-path guesses didn't happen to hit
  the vendor's real terms page. That's the deliberately safe failure mode
  (D-021 — never assume a policy is clean because the page couldn't be
  found), but it means real apps that might be perfectly fine to automate
  can get routed to a human anyway, purely on findability. This bucket is
  small in this run specifically because D-040 (GATE now scans the
  developer-docs page it had already fetched, not just the dedicated ToS
  page) exists — Etsy's and Open-Meteo's real, evidence-backed
  VENDOR_BLOCKED findings above came from exactly that cross-source scan,
  which is the concrete signal D-040 was built to produce.
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
  what was rejected, and why. 46 entries.
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
