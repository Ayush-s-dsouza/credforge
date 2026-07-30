# HITL review needed: Etsy

**Status:** HITL &nbsp;|&nbsp; **Reason:** `tos_prohibits_automation` &nbsp;|&nbsp; **Bucket:** VENDOR_BLOCKED (irreducible -- a human must act)

## Why a human is needed here

credforge read Etsy's own developer documentation and found an explicit
statement prohibiting the kind of automation this tool does:

> "Applications must not sidestep the API to retrieve or post Etsy data.
> Screen-scraping is not allowed."
>
> -- https://developers.etsy.com/documentation/

This is not a low-confidence guess or a missing-page fallback -- it's a
real, quoted policy statement found on the vendor's own docs page. GATE
ranks a ToS/policy prohibition as the highest-precedence signal there is:
it overrides every other finding, including a working auth scheme.

## What credforge found before stopping

| Field | Value |
|---|---|
| Docs URL | https://developers.etsy.com/documentation/ |
| Auth scheme (best guess) | oauth2_auth_code |
| Scopes seen | transaction_r |
| Redirect URI required | yes |

## What's missing (would need a human to fill in even if this weren't blocked)

- `base_url`, `developer_portal_url`, `rate_limit_notes`,
  `pagination_style_hint`, `validation_endpoint` -- none stated in prose
  on the crawled page.

## What a human should actually do next

1. Read Etsy's real developer terms yourself (link above) and confirm
   the prohibition still applies to your intended use case -- policies
   change, and this snapshot is from one crawl at one point in time.
2. If automation truly isn't permitted, this integration needs a manual
   application/partnership process with Etsy directly -- credforge
   cannot and will not attempt to route around a vendor's explicit
   policy.
3. If you believe this flags a false positive (e.g. the quoted line
   refers to data-scraping, not developer signup), open an issue with
   the source URL above so the GATE heuristic can be reviewed.

---
*Generated from a real artifact in `examples/seed_batch/etsy-com.json`,
run `run_20260730T023149Z_37a0e4fe`. This exact rendering (evidence +
gaps + next steps from a HITL artifact) is what the web UI shows live
for any HITL result.*
