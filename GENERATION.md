# DISCOVER_SIGNUP — how recipe generation works

## Why this exists

Every credential-acquisition recipe credforge had before this — NASA,
OpenWeatherMap, Alpha Vantage, IPinfo — was written by a human who opened
that exact vendor's real signup page, read its DOM by hand, and hardcoded
the result as a `SignupRecipe`: a fixed set of CSS selectors and a
credential-extraction regex. That's honest, verified work, but it doesn't
scale. Four hand-written recipes prove the *mechanism* works; they don't
prove the *system* generalizes. "It works on four vendors I personally
looked at" is not an architecture.

DISCOVER_SIGNUP is what replaces hand-authorship. It explores an unseen
vendor once — real browser automation, a real LLM call to read the form,
a real submission — and instead of just acquiring one credential, it
**writes a recipe as its output**. That recipe is a plain JSON file, in
the exact same shape as the four hand-written ones. From that point on,
every future run against that vendor replays the recipe through the
identical deterministic code path a hand-written recipe already uses —
no LLM, no exploration, no non-determinism. Generation is the expensive,
one-time cost. Replay is cheap and repeatable forever after.

## Why the recipe is the output, not the credential

If DISCOVER_SIGNUP just produced a credential, every future run against
that vendor would need to repeat the whole expensive exploration —
another LLM call to re-read the form, another real signup, another
account. Emitting the recipe instead means the *investigation* is the
thing that gets reused, not just its one result. This mirrors how the
four existing recipes already work: the value of NASA's recipe isn't the
one API key it fetched the day it was written, it's the selectors and
regex that let it fetch a fresh key on every future run, deterministically,
forever.

## How it works, step by step

**1. Confirm the precondition.** DISCOVER_SIGNUP only ever runs after
GATE has independently decided a vendor is `AUTO` — eligible for
automated signup, ToS reviewed, no blocking policy found. It reuses
credforge's own real RESOLVE → DISCOVER → CLASSIFY → GATE pipeline to get
there; it never re-derives or second-guesses that verdict. If GATE says
`HITL` or `UNSUPPORTED`, generation stops immediately. Nothing below this
line ever runs for a vendor GATE hasn't cleared.

**2. Find the real signup page.** Starting from whatever URL DISCOVER's
own extraction found, and falling back to scanning the vendor's docs page
and homepage for a link that looks signup-shaped (filtered so it only
ever trusts a link on the vendor's *own* domain — never a third-party
signup page a docs page happens to link to). If that finds nothing, it
tries a short list of conventional paths like `/signup` and `/register`,
verified by actually fetching each one.

**3. Read the real, rendered form.** A real headless browser opens the
page and reads every input, select, textarea, and button — its name, id,
placeholder, associated label text, and whether it's required. This is
sent to the LLM as a compact, structured list, not the raw HTML of the
page — cheaper, and the model doesn't have to fight through unrelated
markup to find the form.

**4. Classify the form.** The LLM reads that structured list and decides:
which field is the email address, which is the password, which is a
first name — and, separately, whether anything about this form should
stop the whole attempt (see hard stops, below).

**5. Fill and submit — but only if `--live` was passed.** By default,
DISCOVER_SIGNUP does none of this: it stops after classification and
prints exactly what it *would* fill, into which field, without touching
the page. Only an explicit `--live` flag lets it actually type real
values and click submit.

**6. Find the credential.** After a real submission, the LLM reads the
resulting page (or, if the vendor emails the credential instead, a real
polled inbox message) and is asked one specific thing: what stable prose
sits right next to the credential value — not what the value itself looks
like. Code then builds a regex anchored on that exact prose and applies it
to the real page or email text. The actual credential that gets vaulted
always comes from that regex match against real content — never from the
LLM's own transcription of a long value it read.

**7. Emit the recipe.** Everything learned — the signup URL, the field
selectors, the extraction regex, whether the vendor requires email
verification — is written to disk as a `SignupRecipe`, tagged with when
and how it was generated. From that point forward, this vendor is
indistinguishable, to the rest of credforge, from one a human hand-wrote.

## What the LLM decides vs. what code decides

The honest split, because this is the question that actually matters for
trusting the system:

- **Locating the signup page** — entirely code. Anchor-link scanning and
  a guess list, both fully deterministic.
- **Classifying which field is which** — the LLM. This genuinely has no
  deterministic fallback; that's the entire reason this component exists
  instead of another hand-written recipe.
- **Whether to proceed at all** — code, reading the LLM's judgment, plus
  one check that never asks the LLM anything: a plain regex scan of the
  raw form for payment/card fields and CAPTCHA-response fields, checked
  independently of whatever the model concluded. Two separate checks for
  the same class of question, on purpose — the same reasoning credforge
  already applies to secret redaction elsewhere in the codebase.
- **Filling and submitting** — entirely code, driven by the LLM's
  classification but with a fixed table of what to type for each field
  purpose.
- **Finding the credential's location** — the LLM, but only ever asked
  for the *surrounding text*, never the value itself.
- **Actually extracting the credential** — entirely code: a regex built
  from that surrounding text, applied to the real page or email. The LLM
  never gets the final say on what the vaulted value is.

## The hard stops, and why each one exists

DISCOVER_SIGNUP refuses to submit a real form — even under `--live` — if
any of these fire:

- **A CAPTCHA or other bot-challenge is present.** Automating past a
  vendor's own anti-bot defense is exactly the kind of thing this project
  refuses to do anywhere else, and this generator is no exception.
- **A payment or card field is present.** credforge never provisions
  anything that costs money without a human in the loop.
- **The form's classification confidence is too low.** Submitting a real
  form to a real vendor based on a guess the model itself isn't confident
  about is exactly the kind of action that should require more certainty,
  not less, than a routine research call.
- **No field was identified as an email address.** There's no way to
  receive a credential without one.

The CAPTCHA and payment checks each run twice, deliberately: once as the
LLM's own judgment, and once as a plain, code-only pattern match over the
raw form that runs regardless of what the LLM concluded. Both have to be
clean. This is not redundancy for its own sake — a real CAPTCHA was found
live that left no visible trace at all in the rendered page, only a
hidden field the model was never even shown until this two-layer check
was added.

## What it cannot do

Every real anti-automation defense this project has ever run into applies
here too, without exception. Across every vendor tried this session that
involves creating a real account — eleven of them — every single one
turned out to be defended: nine showed a visible CAPTCHA or an
old-fashioned "solve this math problem" field before a form was ever
submitted. The other two looked completely clean right up until
submission, and only then revealed real, invisible defenses — one
vendor's registration backend rejected the request at the network level
with an explicit bot-blocking message, the other ran a CAPTCHA whose only
trace in the page was a hidden field, not a visible widget.

None of these are bugs to fix. A vendor that defends its signup form is
making a deliberate choice, and working around that choice is exactly the
line this project has never crossed for any other reason.

There's a second, different limitation that isn't about defenses at all:
some vendors' real API documentation is rendered entirely by JavaScript
after the page loads, with nothing but an empty placeholder in the raw
page source. credforge's research stages fetch pages directly, without
running a browser — by design, that's what keeps research cheap and fast
across hundreds of vendors. But it means a vendor whose docs are a
JavaScript shell can never be confirmed as having a real public API in
the first place, and DISCOVER_SIGNUP is never even triggered, because the
step that decides a vendor is eligible for automation never got to run.
This is a different, upstream problem from anything DISCOVER_SIGNUP
itself does — and correctly so: this generator's one hard rule is that it
never second-guesses that earlier decision, for any vendor, for any
reason.
