# OPS.md

Operational reality: where this runs, what triggers it, what happens when
it dies, how secrets get supplied, and (filled in at Stage 9, once
measurable) what it costs and what breaks first at scale.

## Process model

credforge is a **synchronous CLI process**, not a daemon or a service.
Each invocation (`credforge resolve|run|batch|report|revoke`) is a single
Python process that runs to completion (or to the point where every app in
a batch has either finished or hit a wall) and exits. There is no
persistent server component and nothing listens on a port.

Concurrency within one `batch` invocation is `asyncio`-based (an
`asyncio.Semaphore(--concurrency)` gating per-app pipelines), not
multi-process — one CLI invocation is one OS process regardless of how
many apps it's processing concurrently.

## Trigger

Manual, from a terminal, by an engineer who wants a new integration
researched: `credforge run <app>` for one app, `credforge batch apps.csv`
for many. Nothing about this build wires it to a cron job, a webhook, or a
queue consumer — it's a tool an engineer runs, not a background service.
(Nothing rules out wrapping it in a scheduled job later; see the cost/scale
section for what would need to change first.)

## What happens if it dies mid-batch

This is the point of the registry + resumability design (see
`DECISIONS.md` D-006): every stage completion is appended to
`.credforge/registry.jsonl` as it happens, not batched up and written at
the end. If the process is killed (Ctrl-C, OOM, host reboot) partway
through a batch:

- Apps that already reached a terminal `EMIT completed` entry are
  untouched — their artifact is already on disk.
- The app that was mid-stage when the process died has a `started` entry
  but no matching `completed` entry for that stage; on the next
  `credforge batch` invocation over the same CSV, that app resumes at the
  stage it was interrupted on, not from scratch.
- Nothing is left partially written in a way that corrupts state: the
  vault only ever gets a `store()` call after a stage's work is fully
  done, and the registry is append-only (D-006), so the worst a crash can
  do is leave one incomplete-but-harmless trailing bookkeeping line (see
  `test_corrupted_line_is_skipped_not_fatal`).

## How secrets are supplied

Every secret credforge needs is an environment variable (see
`.env.example`), never a CLI argument (which would land in shell history)
and never a config file committed to the repo:

- `CREDFORGE_VAULT_KEY` — the Fernet key protecting the credential vault.
- `BRAVE_API_KEY` — optional. Real web search (RESOLVE/DISCOVER) works
  with no key at all via the default `DdgSearchProvider` (D-024); set
  this only to switch to Brave's documented, metered API instead.
- `ANTHROPIC_API_KEY` — LLM-assisted extraction (DISCOVER/CLASSIFY).
  Absent → falls back to the deterministic heuristic extractor.
- `CREDFORGE_IMAP_*` — real email polling, only consulted under `--live`.

## What the Playwright dependency implies for the host (Stage 5, now implemented)

Playwright is an **optional extra** (`pip install credforge[live]`), not a
default dependency, specifically so that research-only usage (RESOLVE
through GATE, plus EMIT/REPORT) never needs a browser binary at all. Once
`--live` provisioning is actually exercised, the host needs: the
Playwright Python package *and* its browser binaries (`playwright install
chromium` — a separate ~150–300MB download per browser, not covered by pip
alone), and either a real X server / Xvfb for `--headed` debugging or
headless mode for unattended runs. This is real host setup, not just a pip
install — worth calling out explicitly, since it's the one dependency in
this project that isn't "add a line to `pyproject.toml` and move on."

As of Stage 5, `--live` also requires `CREDFORGE_IMAP_HOST` /
`CREDFORGE_IMAP_USER` / `CREDFORGE_IMAP_PASSWORD` to be set --
`build_providers(settings, live=True)` raises a clear `RuntimeError` up
front if any are missing, rather than constructing a browser driver that
would only fail later, mid-signup, when email verification is reached.
`ImapEmailProvider` itself needs no new dependency (stdlib `imaplib`) --
Playwright remains the only host-setup burden `--live` adds. See
DECISIONS.md D-031.

## Outbound network surface (Stage 1-4)

RESOLVE, DISCOVER, and GATE are the three stages that always make real
outbound network calls in a live run (`credforge run`/`batch` without
`--dry-run`) -- unlike PROVISION/VALIDATE, none of them are gated behind
`--live`, because research has to be real for the pipeline to mean
anything. Concretely, per app: RESOLVE issues 3 search calls (official-site
query, then 2 docs-URL query phrasings, all collected up front for ranking
-- see DECISIONS.md D-027) plus up to ~7 fetch calls verifying docs-URL
candidates in ranked order (stopping at the first that's both reachable
*and* content-verified as real API documentation); DISCOVER tries each of
RESOLVE's verified candidates in order, falling back through its own 3
subdomain guesses only if none of them work (D-028); GATE issues up to 7
fetch calls (one per common ToS-URL guess, stopping at the first success)
plus one more if a ToS page is found and needs extraction. All of it
funnels through the single rate-limiter/robots.txt choke point in
`HttpxFetchProvider` (Stage 0).

**Known latency characteristic, observed for real, not yet fixed:** a live
run against `"Salesforce"` took **24.7s for RESOLVE alone** -- the
combination of the no-key `ddgs` provider's aggressive, unpredictable
throttling (D-024) and RESOLVE's docs-candidate verification issuing its
fetch probes *sequentially*, one at a time, against up to 5 conventional
subdomain/path guesses per candidate batch. Every probe that fails still
costs a full request round trip before the next one starts. This is
logged here deliberately rather than fixed silently -- the fix belongs
with a real concurrency budget decision (Stage 9's batch/concurrency
design), not a quick patch to one stage:

- **Parallelize the probes.** The 5 conventional guesses (and the ToS
  guesses in GATE, similarly sequential today) have no dependency on each
  other -- they can be issued concurrently (bounded by the existing
  per-domain `DomainRateLimiter`, which already caps real concurrent load
  on any one vendor) instead of one-at-a-time.
- **Cache negative results within a run.** If `docs.<domain>` and
  `developer.<domain>` both fail for a vendor during RESOLVE's docs-URL
  discovery, DISCOVER's own fallback guessing (D-028) currently has no way
  to know that and may re-probe the same dead URLs a second time later in
  the same app's pipeline run.

Neither fix is implemented yet. This is the first concrete evidence that
per-app wall-clock time is dominated by sequential network round trips to
guessed/probed URLs, not by computation -- a real cost signal for Stage
9's batch/concurrency numbers, and worth revisiting before committing to a
default `--concurrency` value.

## LLM extraction dependency (Stage 2)

DISCOVER's field extraction and CLASSIFY's auth-scheme assignment (Stage 3)
both go through the same `Extractor` interface, backed by one of two
concrete implementations chosen independently of `--live` (see
DECISIONS.md D-013): `AnthropicExtractor` (real `claude-sonnet-5` calls,
selected automatically when `ANTHROPIC_API_KEY` is set) or
`HeuristicExtractor` (regex/keyword-based, zero dependencies, zero cost,
always available). The `anthropic` Python package itself is an optional
extra (`pip install credforge[llm]`) -- the default install never pulls it
in, and `anthropic_extractor.py` is only ever imported from inside the
`if settings.anthropic_api_key:` branch in `providers/factory.py`, so a
host with no key configured and the extra not installed never hits an
import error.

## VALIDATE's real bottleneck, observed for real (Stage 6/8)

Every real AUTO/near-AUTO app traced through Stages 6-8 this session
(Spotify, GitHub across two separate real runs) had `validation_endpoint:
null` in DISCOVER's extraction -- `completeness_gaps` lists it every
time. VALIDATE's classification logic (D-032) is exercised and correct
(see TRACE.md Stage 6's live 401 against GitHub's real API using a
placeholder `validation_endpoint`), but in every *organically* real run
so far, VALIDATE never actually had a real endpoint to check against at
all, landing on `VALIDATION_FAILED_UNKNOWN` with a null `checked_url` --
not because anything is broken, but because DISCOVER's prose-derived
extraction rarely states a concrete, cheap, read-only endpoint explicitly
enough to extract. This is a sample of two, not a conclusion -- logged
here as a pattern to watch during Stage 9's seed-list run, which will
say whether this generalizes or was a coincidence of which two apps got
traced.

## First 20-app seed batch: failure distribution (RESOLVE-GATE, real providers)

`scripts/scratch_run.py --batch` (added specifically to answer this
question -- runs every app independently, one app's crash/failure never
stops the batch, per-app JSON written to disk, summary table + reason_code
tally printed) against the working `data/seed_apps.csv` list, real DDG
search + real httpx fetch + real `claude-sonnet-5` extraction throughout:

| reason_code                | count | apps |
|-----------------------------|-------|------|
| `eligible_auto`             | 6     | HubSpot, Spotify, Stripe, SendGrid, Monday.com, Discord |
| `classify_low_confidence`   | 4     | GitHub, Slack, Auth0, NASA API |
| `tos_unverifiable`          | 4     | Etsy, Salesforce, Notion, Open-Meteo |
| `discovery_failed`          | 2     | Twitter, Mailgun |
| `internal_error`            | 2     | Trello, Superhuman |
| `resolve_low_confidence`    | 1     | Google Calendar |
| `resolve_ambiguous`         | 1     | OpenWeatherMap (fixed live during this run -- see below) |

**`resolve_ambiguous` on OpenWeatherMap was a real bug, fixed immediately:**
a SaaS-comparison directory page (`saashub.com`) scored close enough to
`openweathermap.org` to trip the ambiguity margin -- the same aggregator
problem D-025 already exists to solve, just a domain not yet on the list.
Added `saashub.com` to `_AGGREGATOR_BLOCKLIST`, added a regression test,
re-verified live: RESOLVE now clears at confidence 0.975 with no competing
candidate. See DECISIONS.md D-025 (updated in place, not a new entry --
this is exactly the "Revisit if" case that section already named).

**Three findings noted, deliberately not chased further right now** (each
one is a real signal from the batch, but investigating and fixing all
three properly is more than a quick patch, and the instruction for this
round was to get through this batch and move on to Stage 5, not iterate
again):

1. **`internal_error` (Trello, Superhuman) is `SearchProviderError`
   working as designed, exposing a retry-scope gap.** Both are the no-key
   `ddgs` backend failing for real (Trello: "No results found" for the
   official-site query; Superhuman: a timeout talking to the Yahoo backend
   `ddgs` proxies through) -- exactly the kind of failure D-024 designed
   `SearchProviderError` to surface clearly instead of masquerading as
   `RESOLVE_NOT_FOUND`, and exactly the kind of failure the batch harness
   (built for this) correctly isolated without killing the other 18 apps.
   But `providers/ddg_search.py`'s retry-with-backoff (`_MAX_ATTEMPTS=3`)
   is scoped *only* to `RatelimitException` -- a bare "no results" or a
   backend timeout hits the generic `except Exception` branch and fails on
   the first attempt, zero retries. 2/20 apps (10%) hit this in one run.
   Worth widening the retryable-error set at Stage 9 once there's a real
   concurrency/retry budget decision to make it part of (same reasoning as
   the RESOLVE-latency note above -- don't patch one call site piecemeal).

2. **RESOLVE's docs-path guess list is narrower than at least one real
   vendor's actual layout.** OpenWeatherMap's real API docs live at
   `openweathermap.org/api` -- `_DOCS_PATH_GUESSES = ("/docs",
   "/developers")` doesn't include `/api`, so the guess-based fallback
   never finds it, and the two search-query templates plus content
   verification (D-027) apparently didn't surface a passing candidate
   either (RESOLVE's own `docs_url_candidates` came back empty for this
   app). DISCOVER's own 3 subdomain guesses (D-028's safety net) also
   missed it, and the app landed on `discovery_failed`. Candidate fix:
   widen `_DOCS_PATH_GUESSES`, but a real fix needs to look at *why* the
   search-based path didn't catch it too, not just add another guess --
   Stage 9 seed-list work, not a one-line patch right now.

3. **Multi-word product names nested under a parent company's bare domain
   score below threshold.** "Google Calendar" -> `google.com` landed at
   confidence 0.6357, just under the 0.7 threshold, because name-similarity
   scoring compares the full app name against a domain label that only
   contains part of it ("google", not "google calendar"). This is a
   genuine scoring limitation, not a blocklist gap -- correctly failing
   closed (`RESOLVE_LOW_CONFIDENCE`, not a wrong guess) rather than
   picking a plausible-but-wrong domain, which is the safer failure mode
   of the two, but it does mean a class of real apps (products living under
   a platform's shared domain) will systematically under-score. Worth
   revisiting at Stage 9 if the seed list's other platform-nested apps
   (Google APIs generally) show the same pattern.

## Cost and scale

Measured from two real sources: the 20-app seed batch
(`run_20260730T170035Z_4a8d3af4`, via `credforge batch data/seed_apps.csv`,
mocked provisioning, real search/fetch/LLM) and one separately-instrumented
real GitHub run (`pipeline/orchestrator.py::run_app`, `AnthropicExtractor`'s
per-call `usage` recorded directly from the Anthropic response — see
`providers/anthropic_extractor.py`'s `self.calls`).

**Wall clock, real and measured (20 of 20 seed apps -- every app in this
run has both a `resolved_at` and an `emitted_at` provenance timestamp):**
707.3 seconds total span, individual per-app durations (`resolved_at` to
`emitted_at`, i.e. processing-start to artifact-written) ranging from
2.1s (Discord, `resolve_not_found` -- fails almost instantly, stops
before DISCOVER/CLASSIFY/GATE ever run) to 83.8s (GitHub, the one AUTO
app that ran the full DISCOVER+CLASSIFY+GATE ToS-check path), averaging
**35.4 seconds/app**. This is sequential -- one app at a time, no
concurrency in the current `batch` command. Every app in this run has a
real duration; D-038/D-039 (every terminal outcome produces a real
artifact with real `provenance`) is what makes that true.

**These numbers moved from the previously measured run (43.2s -> 37.9s ->
35.4s/app average across three real runs of the same code) -- see "Run-to-
run coverage volatility" below before treating the per-app breakdown, or
even this average, as a stable measurement rather than one sample.**

**LLM calls and tokens, real and measured for one app (GitHub, full AUTO
path -- DISCOVER, CLASSIFY, and GATE's ToS check all ran):**

| call | input tokens | output tokens |
|---|---|---|
| `extract_discovery` | 3,045 | 846 |
| `extract_classification` | 2,662 | 72 |
| `extract_tos_gate_signals` | 2,045 | 91 |
| **total** | **7,752** | **1,009** |

At `claude-sonnet-5`'s current intro pricing ($2.00/1M input, $10.00/1M
output, in effect through 2026-08-31): **$0.0256** for this one app. At
standard pricing ($3.00/$15.00) after that date: **$0.0384**. Both
comfortably inside the "a handful of cents per app" estimate from D-014.

**Stated honestly: this per-call/per-token table is one real measurement,
not a batch-wide audit.** Call count varies per app -- DISCOVER issues one
`extract_discovery` call *per candidate it actually reads*, so an app
whose first docs candidate fails and falls back to a second or third (the
D-028 fallback path) makes more calls than GitHub's one-candidate case
here; GATE only calls `extract_tos_gate_signals` if a ToS page was
actually found. Token usage tracking (`AnthropicExtractor.calls`) was
added specifically for this section but wasn't wired into the CLI's
`batch` command in time to capture it across all 20 apps this pass --
a real, stated gap, not a rounding assumption. A rough per-app range of
2-4 real LLM calls (1 CLASSIFY, 1 GATE ToS check if found, 1-2+ DISCOVER
attempts) is a reasonable extrapolation from this one measured app, not
an independently confirmed average.

**Which resource binds first, reasoned from real numbers, not
speculation:**

- **At 100 apps:** DDG search reliability, not cost or wall clock. The
  earlier scratch-batch run documented above (see "First 20-app seed
  batch" -- a different, earlier run than the one this section's wall-clock
  numbers come from) hit `SearchProviderError` (a genuine search failure,
  not a rate-limit retry exhaustion -- see D-024) on **2 of 20 apps
  (10%)** -- a bare "no results" and a backend timeout, neither retried
  today since `ddg_search.py`'s retry logic is scoped only to
  `RatelimitException`. The canonical seed batch this section otherwise
  cites (`run_20260730T170035Z_4a8d3af4`) hit zero `SearchProviderError`
  failures this specific run -- both are real, single-run samples of an
  unpredictable, no-key backend, not a stable rate. Extrapolated at the
  earlier run's 10% rate, 100 apps
  means roughly 10 outright search failures with zero retry attempted --
  before LLM cost (~$2.56 at 100 apps, intro pricing) or wall clock
  (~59 minutes sequential at the measured 35.4s/app average) become the
  practical constraint.
- **At 1,000 apps:** wall clock becomes the harder constraint --
  ~9.8 hours sequential at the same per-app average, which is the concrete
  number that makes `batch`'s current lack of concurrency (D-036's
  scoped-down Stage 9 cut) the thing to fix first, not LLM cost (~$25.60
  at 1,000 apps, intro pricing -- genuinely cheap at this scale) or the
  local vault/registry (plain JSONL/file I/O, no real ceiling at this
  volume). DDG's reliability problem doesn't go away at this scale either,
  but by 1,000 apps it's competing with a ~10-hour sequential run as the
  more visible problem, which is exactly the "real concurrency budget
  decision" this document has flagged as deferred work since Stage 4.

## Run-to-run coverage volatility (found investigating the RESOLVE fix below)

**The seed batch's per-app coverage table is a single sample, and the
sample varies more than this project's docs have previously admitted.**
Investigating whether D-047/D-048 (RESOLVE fixes, see below) actually
moved the seed-batch numbers required running the full 20-app batch
twice, plus several isolated single-app re-runs, to tell a real fix
effect apart from ordinary noise. The result: **10 of 20 seed apps landed
on the identical status and reason code in both full batch runs; the
other 10 didn't** -- and for apps whose `resolve()` code path is
completely untouched by today's change (Discord, Etsy, Mailgun,
Monday.com, Notion, Open-Meteo, Trello -- none are bare-domain-shaped
input, none have a registered recipe), any difference between runs is
necessarily DDG search-result drift or LLM-extraction variance, not
credforge's own logic changing. Etsy alone produced three different
reason codes across three separate real runs:
`tos_prohibits_automation`, `resolve_not_found`, `tos_unverifiable`.

**Concretely, this means "0 VENDOR_BLOCKED" in the current README table
undercounts the real rate, provably.** Re-running Etsy,
Open-Meteo, and OpenWeatherMap in isolation immediately surfaced real,
evidence-quoted vendor blocks the batch run missed -- see the README's
"Measured coverage" section for the specific quotes. This isn't a new
problem this session introduced; it's a pre-existing characteristic of a
no-key search provider plus LLM extraction that a single labeled run
number has always silently hidden. Worth fixing properly (running N
repeats per app and reporting a distribution, not a point estimate) as
future work -- not attempted here because it would 20x the real wall-clock
and API cost of every future coverage measurement, a real tradeoff, not
an oversight.

**Two specific, real findings surfaced by chasing this down, both a
direct consequence of D-048's identity-pinning fix actually working:**

1. **NASA API now resolves to the right domain (`api.nasa.gov`, not
   `sti.nasa.gov`) and immediately hits a different, real limitation:
   DISCOVER can't read a JavaScript-rendered page.** Fetching
   `api.nasa.gov` directly (a plain HTTP GET, exactly what
   `HttpxFetchProvider` does) returns a page whose actual API-key-signup
   widget is client-side rendered -- the raw HTML contains only
   `Loading signup form...` and a `<script>` config block, no real API
   documentation prose. `PlaywrightBrowserDriver` (PROVISION only) is the
   only component in this project that executes JavaScript. Result:
   NASA now lands on `UNSUPPORTED`/`no_public_api` reproducibly (four
   separate real runs, zero variance) -- correctly pointed at the right
   page, still can't read what's really there. `--live` still works
   for NASA (PROVISION renders the page for real, see below) -- this
   gap is specific to DISCOVER's research path, not provisioning.
2. **OpenWeatherMap's newly-pinned docs URL
   (`openweathermap.org/api`) is reliable in isolation and unreliable
   deep in a long batch.** Five separate isolated/short runs: five
   successes, real evidence every time (`requires_payment` x2,
   `requires_sales_contact` x2, `tos_unverifiable` x1 -- itself further
   evidence of the volatility above). Both full 20-app batch runs:
   `discovery_failed` on this exact URL, both times. Ruled out: the
   per-domain rate limiter (`net/rate_limiter.py` keys strictly per
   domain, no cross-domain state to exhaust) and "batch command
   mechanics" generally (a batch CSV containing only OpenWeatherMap
   succeeds). Not yet isolated: the actual mechanism by which being
   deep in a long sequential run specifically affects this one fetch --
   best current guess is `httpx`'s connection-pool behavior over many
   sequential cross-domain requests, not confirmed. Logged here as a
   real, reproducible-in-context gap rather than papered over as
   one-off flakiness.

## Source-authority weighting (D-049): measured effect, plus two crashes it exposed

Re-running the seed batch to measure D-049 (three-tier docs-candidate
ranking + tier-aware CLASSIFY confidence) against the volatility already
documented above required getting a *clean* 20/20 run at all, which
surfaced two real, previously-latent crash bugs -- neither caused by
D-049 itself, both fixed before a trustworthy before/after comparison
was possible:

- **D-050**: `batch` opened a fresh `asyncio` event loop per app while
  sharing one `HttpxFetchProvider` (and its one `httpx.AsyncClient`)
  across the whole run. httpx binds a client's connection pool to
  whichever loop is active on its first real request; once that loop
  closed (end of app N's `asyncio.run()`), reusing the pool from app
  N+1's *new* loop raised `RuntimeError: Event loop is closed` --
  observed live, app 16 of 20, non-deterministic (timing-dependent on
  real connection-pool state, which is why it hadn't shown up in every
  prior run). Fixed by wrapping the entire batch in one `asyncio.run()`,
  not one per app.
- **D-051**: RESOLVE's *docs-URL* search (the second search call,
  looking for developer docs once identity is confirmed) had no
  `SearchProviderError` fallback -- only the *identification* search
  (`"<app> official website"`) had one, from D-024. A real DDG timeout
  against Mojeek's backend crashed that app's entire run, zero artifact,
  `batch`'s outer exception handler swallowing it into a bare "ERROR"
  line with no registry entry. Fixed the same way D-024 fixed the first
  search call: degrade to conventional-guess URLs, never raise.

**Measured result once both were fixed** (`run_20260801T084914Z_04685c6b`
against `run_20260730T170035Z_4a8d3af4`; full per-app table in README.md):
`classify_low_confidence` 8 apps -> 4. AUTO 2 apps -> 6. Of 16 apps that
reached a real docs page, source_tier breaks down 12 HIGH / 3 MEDIUM / 1
LOW (Salesforce, correctly -- DISCOVER's own bare-domain fallback isn't a
docs page). Checked all 20 apps individually for regressions: none found.
Several other apps that also changed between these two runs (Discord,
Etsy, Google Calendar, Mailgun) did so at the RESOLVE identification
step -- a code path D-049 never touches -- and are attributed to the
same pre-existing search-provider volatility documented above, not to
this fix; README's before/after table marks each row accordingly rather
than crediting D-049 for changes it structurally couldn't have caused.

## What makes a vendor recipe-able: two real vendors, two real outcomes

Two real `--live` attempts, both against real vendor infrastructure, both
producing definitive, not ambiguous, results:

| | NASA API (`api.nasa.gov`) | OpenWeatherMap (`home.openweathermap.org`) |
|---|---|---|
| Signup fields | email, first name, last name | username, email, password, password confirmation, 2 consent checkboxes |
| Anti-automation defense on the form | none | visible Google reCAPTCHA v2 |
| Result | provisioned + validated, 21.3s total, first attempt | `PROVISION_FAILED`, confirmed in one attempt: *"reCAPTCHA verification failed, please try again."* |
| Credential delivery | emailed directly, extracted by regex on the first try | never reached |
| Login required | no | would have been (unconfirmed -- never reached) |

**The distinction that actually decided the outcome, isolated on
purpose:** before submitting anything to OpenWeatherMap live, every field
credforge would need to fill was already confirmed correct from the
rendered DOM -- username, email, password, password confirmation, both
consent checkboxes. The one live submission filled all of them correctly
and was still rejected, with an explicit, specific reason. That's what
"precisely diagnosed" means here: the failure isn't "the recipe might
have a wrong selector somewhere," it's "every selector was right, and
the vendor's own anti-automation control did exactly its job." Those are
different findings, and only one of them is fixable by writing better
automation.

**Recipe-able, based on what was actually observed across both vendors:**

- **The signup form is real HTML with stable selectors** -- whether
  server-rendered (OpenWeatherMap's Rails-style `simple_form`) or
  injected by third-party JS after page load (NASA's `api_umbrella`
  embed widget, D-042) doesn't matter; Playwright can wait for and read
  either. What matters is that the *resulting* DOM has real, addressable
  elements -- not that the markup was present on page load.
- **The credential delivery mechanism is mechanical, not adversarial.**
  NASA emails the key directly, in a predictable format, with no human
  judgment call involved on the vendor's side. A regex against a real
  email body is a completely reliable extraction mechanism *when nothing
  upstream of it required a human first.*
- **There is no active challenge designed specifically to distinguish a
  human from a script.** A required checkbox, a required field, even a
  multi-step form -- all mechanical, all just "more selectors to fill
  correctly." None of these are adversarial; they don't get *harder* to
  automate the more precisely you automate them.

**Not recipe-able, based on the same two data points:**

- **Any real CAPTCHA (reCAPTCHA, hCaptcha, or similar), full stop.** This
  is the one category this project's own constraints already rule out
  attempting to defeat (detection evasion is out of scope on principle,
  not just difficulty), so "not recipe-able" here isn't "harder than the
  other one" -- it's categorical. No amount of better selector-finding
  closes this gap; it isn't a selector problem.
- By extension (not directly observed this session, but the same
  category): **SMS/phone verification, and any step that requires
  receiving something on a channel credforge has no access to** --
  already recognized elsewhere in this project as `REQUIRES_PHONE_VERIFICATION`
  (GATE's own reason code for exactly this), and now confirmed by a real
  example of the sibling case (`REQUIRES_CAPTCHA`) at the *provisioning*
  stage instead of the *policy-reading* stage.

**The real, generalizable finding this comparison surfaces -- and it's a
gap in this project, not just an observation about vendors:** GATE
already has a `REQUIRES_CAPTCHA` reason code, and already detects it --
but only by reading ToS/developer-docs *prose* for a stated mention of a
CAPTCHA requirement (`extract_tos_gate_signals`, D-032/D-040). OpenWeatherMap's
real ToS never says anything about a CAPTCHA -- there is nothing to read
that would tell GATE this vendor's signup form has one. In the actual
20-app seed batch, `openweathermap.org` landed on `tos_unverifiable`, not
`requires_captcha` -- a real miss, not a hypothetical one. GATE's
text-based detection and PROVISION's structural discovery (a
`div.g-recaptcha[data-sitekey]` actually present in the rendered DOM) are
two *independent* signals that currently never talk to each other. A
vendor whose CAPTCHA is never mentioned in writing anywhere -- which,
reasonably, is most of them, since a CAPTCHA is usually just implemented,
not documented -- is invisible to GATE's check entirely, and only
surfaces once `--live` PROVISION actually tries the form and fails.
Worth building, not yet built: PROVISION detecting a CAPTCHA widget in
the real DOM (before ever attempting to fill or submit anything) and
reporting it back as its own reason code, so this class of app can route
to HITL/`REQUIRES_CAPTCHA` *before* wasting a live attempt, the same way
GATE's text-based detection already tries to -- and so the artifact's
reason code reflects the real, structural cause, not `PROVISION_FAILED`,
a generic label that doesn't tell a human reviewer *why* without opening
the logs.

**This is what explains why the HITL bucket exists, concretely, not
abstractly:** some fraction of HITL is credforge's own detection running
out of confidence or coverage (`PIPELINE_LIMITED`, see the README's
coverage section) -- genuinely reducible, genuinely worth improving.
Another fraction is a vendor's own explicit, deliberate anti-automation
control, working exactly as its designer intended. No amount of better
engineering closes that second gap, on principle -- a real CAPTCHA
succeeding against automation isn't a bug in the vendor's system, it's
the system working. `REQUIRES_CAPTCHA` (and its siblings --
`REQUIRES_PHONE_VERIFICATION`, `REQUIRES_SSO_ONLY`) exist as their own
reason codes, distinct from every other HITL cause, specifically because
this second category needs a human for a structural reason no amount of
better automation removes -- which is exactly the distinction the
VENDOR_BLOCKED / PIPELINE_LIMITED split (see README) was already drawing
from the policy side; OpenWeatherMap is the same distinction observed
from the provisioning side, live, for real.
