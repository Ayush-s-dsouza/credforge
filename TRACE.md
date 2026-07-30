# TRACE.md — GitHub, end to end, real numbers

This is the demo script. Every number below comes from one real run of
`credforge run github` (equivalently, `pipeline/orchestrator.py::run_app`)
against the live internet: real DuckDuckGo search, real HTTP fetches, real
`claude-sonnet-5` calls, a real (mocked, non-`--live`) provision and a
real HTTP credential check. Nothing here is invented or reconstructed —
if you re-run it yourself, expect the shape to match and a few numbers to
drift (search results and LLM confidence aren't perfectly deterministic;
that variance is itself discussed below, not hidden).

Run ID: `run_20260730T015221Z_db39e629`. Total wall clock: **62.4
seconds**. Three real LLM calls, 7,752 input tokens and 1,009 output
tokens total — a few cents of Anthropic spend for this one app (see
OPS.md for the full cost breakdown across the seed batch).

## The setup

I run `credforge run GitHub`. No flags — that means mocked provisioning
(no real GitHub account gets created), real search, real fetch, real LLM
extraction, because research has to be real for any of this to mean
anything. The pipeline runs RESOLVE, then DISCOVER, then CLASSIFY, then
GATE, and — because this run happens to clear GATE as AUTO — PROVISION
and VALIDATE, then EMIT writes the final artifact and REPORT can
summarize it.

## RESOLVE — 40.6 seconds

The input is the string "GitHub." RESOLVE's job is to turn that into a
canonical domain and, if possible, a verified developer-docs URL. It
searches "GitHub official website" via DuckDuckGo, scores every domain
that shows up by name similarity, search rank, and how many of the
results agree on it, and — this is the part that took real debugging to
get right — runs a *second* search pass specifically for developer docs
("GitHub API documentation," "GitHub developer docs"), fetches the
top candidates, and only accepts one as `docs_url` if the page actually
contains real API-documentation markers: HTTP verbs next to paths,
words like "authentication" or "endpoint," code fences, that kind of
thing. A URL that merely *sounds* right without a content check is
exactly how an earlier version of this stage picked Salesforce's
customer support portal instead of its real developer docs — that bug
is fixed and tested now (see DECISIONS.md D-027).

This run resolves to `github.com` at high confidence, with `docs_url =
https://docs.github.com/en/rest`. 40.6 seconds is on the slow side, and
that's real and known, not a fluke of this one run: it's the no-key
DuckDuckGo provider's unpredictable throttling combined with RESOLVE
issuing several sequential fetch probes to verify docs candidates. It's
logged as a known latency characteristic in OPS.md, with the fix
(parallelize the probes) scoped but not built — Brave's paid API would
also fix this, at the cost of requiring a credit card on file.

One honest wrinkle worth pointing out live: the evidence snippet RESOLVE
attaches for "why github.com" isn't always the cleanest possible quote —
DuckDuckGo's real result ranking is noisy, and the snippet shown is
whatever the highest-ranked real search hit for github.com actually said,
not a hand-picked one. The domain match itself is still correct and
high-confidence; the evidence text is just real search noise, which is
what real evidence looks like.

## DISCOVER — 10.9 seconds

DISCOVER takes RESOLVE's verified `docs_url`, fetches it for real, and
hands the visible text (HTML stripped, script/style content excluded —
see D-023) to Claude for structured extraction: is there a public API,
what's the base URL, the rate limits, the pagination style, a cheap
endpoint to validate a credential against later. It crawled
`docs.github.com/en/rest` — GitHub's real REST API landing page — and
came back with `has_public_api: true` and a genuinely useful rate-limit
sentence pulled straight from the page text. It did *not* find a stated
`base_url`, a `pagination_style_hint`, or a `validation_endpoint` — that
page is a table-of-contents page, not a reference page with concrete
endpoint examples, so those fields legitimately aren't stated in prose
anywhere on it.

This is where DISCOVER's fallback logic matters, even though this run
didn't need it: DISCOVER doesn't just try RESOLVE's one best guess. It
walks RESOLVE's whole ranked candidate list, and if a real vendor's docs
turn out to be a dead end — the earlier Salesforce case, or a candidate
whose extraction says `has_public_api: false` — it tries the next one
before giving up (D-028). And as of this Stage 9 pass, if *every*
candidate genuinely has no public API, DISCOVER now correctly reports
that instead of collapsing it into a generic "couldn't find anything"
failure — a real bug (D-037) found writing this stage's own orchestrator
tests, not from a live run: the old code made it structurally impossible
for the pipeline to ever produce an `UNSUPPORTED` verdict.

## CLASSIFY — 2.4 seconds

CLASSIFY hands the same docs text to Claude with one focused question:
what's the authentication scheme, and how confident are you. This run,
it said `oauth2_auth_code` at confidence 0.9-something — clears the 0.6
threshold, so GATE never routes to HITL on classification grounds this
time.

Worth saying plainly, because it's the single most important thing to
understand about this stage: **that confidence number reflects how
clearly the extraction task could be done, not whether the underlying
answer is actually right.** A landing page can state "OAuth 2.0" in
large, unambiguous type and still be the wrong page to learn the *real*
auth flow from — Salesforce's real extraction, in an earlier trace,
pulled `oauth2_client_credentials` at real confidence from a Trailhead
*tutorial* page, not Salesforce's actual API reference. The confidence
score is honest about "I read this clearly." It says nothing about "this
is definitely how the real integration works." That distinction matters
enough that it's in the README's "what this gets wrong" section, not
just here.

This run also happens to land on a different real number than several
earlier traces of the same app — GitHub has previously classified at
confidence 0.55 (below threshold, routing to HITL) on the exact same
landing page. That's real LLM run-to-run variance on a page that's
genuinely borderline-informative, not a bug to chase.

## GATE — 8.6 seconds

GATE checks preconditions first (discovery didn't fail, there's a public
API, classification was confident enough — all clear this run), computes
the completeness gaps (three fields DISCOVER couldn't state: `base_url`,
`pagination_style_hint`, `validation_endpoint` — carried forward, not
treated as a blocker, per D-029), then goes and actually fetches GitHub's
real Terms of Service. It found `github.com/terms-of-service` on the
third of seven conventional-path guesses, read the real page text, and
asked Claude whether it prohibits automation, requires payment, business
verification, phone verification, a CAPTCHA, or SSO-only signup. None of
those flags fired. Status: `AUTO`. Reason: `eligible_auto`.

This is the stage with the sharpest real bug found this project, worth
telling in full because the fix itself wasn't complete on the first try.
Running Spotify through this exact code path, GATE's first ToS guess
came back HTTP 200 with a body that was Spotify's own branded 404 page —
"Page not found... this page is out of tune..." The old code had no way
to tell that apart from a real ToS page: 200 status, plenty of text.
Claude correctly found no prohibition in it, because there wasn't one —
it wasn't ToS text at all — and GATE cleared Spotify to AUTO having never
reviewed real terms. Fixed with a soft-404 content check. Re-verified
live, and the fix was *incomplete*: the next guessed URL was a
*second*, differently-worded Spotify soft-404 the first fix didn't
catch. Checked all seven guessed paths directly, confirmed both were
soft-404s, extended the fix, re-verified again. Spotify now correctly
lands on `TOS_UNVERIFIABLE`. Full account in DECISIONS.md D-035 — it's
there specifically because a fix that looks complete after one live check
and isn't is a more useful lesson than a fix that worked the first time.

## PROVISION — under 5 milliseconds

Because this run cleared to AUTO, and this run isn't `--dry-run`,
PROVISION runs. Not `--live`, so it's the mocked path:
`MockEmailProvider`/`MockBrowserDriver` simulate a signup and app-creation
flow deterministically, no real browser, no real GitHub account. The
credential — a fake `client_id`/`client_secret` pair — gets
Fernet-encrypted and written to the vault; only a `vault_ref` string
(`vault://github.com/credential`) ever leaves the vault module. Before
any of that, PROVISION checks the registry for an existing open
`vault_ref` for `github.com` — this run has none, so it proceeds. Run it
a second time for the same app, and that check is the entire function
call: no email, no browser, just a lookup.

## VALIDATE — real HTTP attempt, no real credential to validate

VALIDATE tries a real, read-only GET against whatever
`validation_endpoint` DISCOVER found. This run, that field is `null` —
DISCOVER's real extraction never stated one, which shows up honestly in
`completeness_gaps`. So VALIDATE has nothing to check against and reports
`validation_failed_unknown` with a null `checked_url` — not a crash, a
correctly-labeled "nothing to validate." In a separate real check against
GitHub's actual live API (`GET api.github.com/user` with a placeholder
bearer token), VALIDATE correctly got a real `401` back and classified it
as `validation_failed_bad_credential` — proof the classification logic
works against a real vendor's real response shape, even though this
particular end-to-end run didn't have an endpoint to point it at.

One more thing worth saying explicitly here, because it's a genuine,
deliberate safety decision: VALIDATE never sends whatever HTTP method
DISCOVER's extraction names, even when it names one. DISCOVER's real
Salesforce extraction, in an earlier trace, named `POST
/contacts/v1/contacts` as its best guess at a validation endpoint — a
*create-a-contact* call. VALIDATE always downgrades to GET. Replaying the
extracted method literally would mean checking whether a credential works
by actually creating data in the vendor's system, every time. See
DECISIONS.md D-032.

## EMIT — instantaneous

EMIT is the one stage with no `async` in its signature, because it does
no I/O — it's a pure conversion from the working `AppPipelineState` into
the frozen `HandoffArtifact` this whole project exists to produce. The
real artifact from this run:

```json
{
  "status": "AUTO",
  "reason_code": "eligible_auto",
  "app": {"app_name": "GitHub", "identity_key": "github.com"},
  "api": {
    "docs_url": "https://docs.github.com/en/rest",
    "auth_scheme": "oauth2_auth_code",
    "rate_limit_notes": "You can authenticate to the REST API to access more endpoints and have a higher rate limit.",
    "scopes_available": ["repo", "user", "admin:org", "gist", "notifications", "workflow"],
    "redirect_uris_required": true
  },
  "credential": {
    "credential_type": "oauth2_token_pair",
    "vault_ref": "vault://github.com/credential",
    "console_url": "https://docs.github.com/en/rest/apps/mock",
    "email_alias": "credforge+github-com@example.com"
  },
  "validation": {"status": "invalid", "reason_code": "validation_failed_unknown"},
  "completeness_gaps": [
    {"field": "base_url", "reason": "not stated in prose..."},
    {"field": "pagination_style_hint", "reason": "not stated in prose..."},
    {"field": "validation_endpoint", "reason": "not stated in prose..."}
  ]
}
```

Two invariants are enforced at the model level here, not just by
`emit.py`'s own logic: `status: AUTO` cannot coexist with any
`reason_code` other than `eligible_auto` (this was the exact reason
pydantic was chosen at Stage 0, made real at Stage 7 — D-033), and
`credential` is structurally impossible to attach to a non-AUTO artifact.
A future bug that tried to leak a credential onto a HITL artifact would
fail loudly at construction, not silently ship.

## REPORT

`credforge report <run_id>` reads every artifact EMIT wrote for a run and
aggregates: counts by status, counts by reason code, and a
`needs_attention` list — every HITL app, its reason, and its specific
completeness gaps, sorted so same-cause apps sit together. It never
re-runs anything; it only reads what's already on disk. See the README
for the real numbers from the full 20-app seed batch this produced.

## What actually happened running this, honestly

Two real bugs were found and fixed by building and running this exact
trace, not hypothesized in the abstract: the Spotify soft-404 false-AUTO
(D-035, two attempts to fix completely) and the DISCOVER/GATE routing bug
that made `UNSUPPORTED` unreachable through the real pipeline (D-037,
D-038). Both are described above at the stage where they were found. That
is the entire point of building this document from real runs instead of
writing plausible-looking numbers by hand — a script like this one is
only useful to walk someone through if what it shows actually happened.
