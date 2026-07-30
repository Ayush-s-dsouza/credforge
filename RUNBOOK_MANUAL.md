# RUNBOOK_MANUAL.md — the process credforge replaces

This is the process a Composio engineer follows by hand today, before any
integration gets built, written as a numbered checklist. Every step is
tagged:

- **[AUTOMATED]** — credforge does this without a human.
- **[GATED]** — credforge attempts this, but routes to a human (HITL) when
  it detects a real blocker (ToS, payment, verification, etc.).
- **[MANUAL HANDOFF]** — outside credforge's scope; a human (or the
  downstream toolkit-generation agent) does this using credforge's output.

## 1. Vendor identification

1.1. Take the app name as given (often ambiguous — "Monday", "Sage",
"Atlas" could each mean several different companies). **[AUTOMATED —
RESOLVE]**

1.2. Search for the official vendor site and confirm it's the *right*
company, not a lookalike or a reseller. **[AUTOMATED — RESOLVE]**

1.3. If more than one plausible company matches, stop and ask someone who
knows which one was meant, rather than guessing. **[AUTOMATED — RESOLVE
emits a disambiguation prompt instead of guessing]**

## 2. Docs discovery

2.1. Find the developer/API docs (not just marketing pages). **[AUTOMATED
— DISCOVER]**

2.2. Determine whether a public API exists at all. **[AUTOMATED —
DISCOVER]**

2.3. Note the base URL, auth scheme, rate limits, pagination style, and a
cheap endpoint to sanity-check a credential against later. **[AUTOMATED —
DISCOVER]**

2.4. Find the developer-portal signup URL and check whether there's a free
tier / sandbox, or whether API access requires a sales conversation or
paid plan. **[AUTOMATED — DISCOVER + GATE]**

## 3. Eligibility check (the step humans usually skip, and shouldn't)

3.1. Read the actual Terms of Service / developer agreement — not just
assume it's fine — and check whether it prohibits automated account
creation. **[AUTOMATED — GATE, checked first, always]**

3.2. Check whether signup requires payment, business verification, or a
sales call. **[AUTOMATED — GATE]**

3.3. Check whether signup requires phone verification, a CAPTCHA, or is
SSO-only (no independent credential to extract). **[AUTOMATED — GATE]**

3.4. If any of 3.1–3.3 apply, or there's no public API at all, stop — this
integration needs a human, not more automation. **[GATED / UNSUPPORTED]**

## 4. Developer account creation

4.1. Sign up for a developer account using a company-controlled email (not
a personal one, so the account is findable and revocable later).
**[AUTOMATED — PROVISION; mocked by default, real under `--live`]**

4.2. Complete email verification. **[AUTOMATED — PROVISION polls IMAP
under `--live` (stdlib `imaplib`); a canned message by default]**

4.3. Navigate the developer console (which varies wildly per vendor, and
is the single least automatable step in this whole process). **[AUTOMATED
under `--live` via Playwright, but only for a vendor with an explicit
`SignupRecipe` registered — no recipe means a clear failure, never a
best-effort guess at an unknown form. Mocked by default.]**

## 5. OAuth app creation

5.1. Create an application/app registration in the developer console.
**[AUTOMATED — PROVISION; mocked by default, real under `--live`]**

5.2. Set the redirect URI(s) credforge/Composio needs. **[AUTOMATED —
PROVISION; mocked by default, real under `--live`]**

5.3. Select the scopes the integration will need. **[AUTOMATED —
PROVISION; scope selection logic is intentionally simple for now]**

5.4. Fill in required app metadata (name, logo, description) — some
vendors reject apps with placeholder metadata. **[MANUAL HANDOFF if the
vendor's review process inspects this]**

## 6. Credential extraction

6.1. Copy the client ID/secret, API key, or token out of the console.
**[AUTOMATED — PROVISION; mocked by default, real under `--live`]**

6.2. Store it somewhere that isn't a plaintext file or a Slack message.
**[AUTOMATED — VAULT, Fernet-encrypted, only a `vault_ref` ever leaves
this system]**

## 7. Verification submission (vendor-side app review)

7.1. Some vendors (Google sensitive scopes, Meta, Twitter/X elevated
access) require submitting the app for manual review before it can be used
beyond a small test audience. **[MANUAL HANDOFF — this is a multi-day
vendor process credforge cannot shortcut; GATE routes these to HITL rather
than pretending they're AUTO]**

## 8. Test round-trip, including token refresh

8.1. Make one real, read-only API call to prove the credential actually
works. **[AUTOMATED — VALIDATE]**

8.2. For OAuth2, confirm the refresh token actually refreshes the access
token (not just that the initial token works). **[MANUAL HANDOFF — out of
scope for this build; VALIDATE only proves the *initial* credential works.
See DECISIONS.md.]**

## 9. Endpoint enumeration

9.1. Catalog which endpoints the integration will actually call. **[MANUAL
HANDOFF — this is the downstream toolkit-generation agent's job, not
credforge's; credforge hands it a validated credential + base API facts,
not a full endpoint catalog]**

## 10. Tool schema authoring

10.1. Write the actual tool/action schemas Composio exposes for this
integration. **[MANUAL HANDOFF — downstream toolkit-generation agent]**

## 11. Pagination / rate-limit / error handling

11.1. Encode how the vendor paginates, rate-limits, and reports errors
into the integration's client code. **[MANUAL HANDOFF — downstream agent,
informed by credforge's DISCOVER output (`pagination_style`,
`rate_limits` fields)]**

## 12. Catalog registration

12.1. Register the finished integration in Composio's catalog. **[MANUAL
HANDOFF — entirely outside credforge's scope]**

## Where the handoff sits

credforge's output ends at step 8.1 (a validated credential) plus the raw
facts gathered in steps 2–3 (base URL, auth scheme, scopes, rate limits,
pagination style), packaged as the handoff artifact. Steps 9–12 are the
downstream toolkit-generation agent's job entirely; credforge deliberately
does not attempt them.
