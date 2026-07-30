# examples/

Pre-computed, real output -- no search calls needed to view these.

- **`seed_batch/`** -- all 20 artifacts + `report.json` from the real seed
  batch run `run_20260730T023149Z_37a0e4fe` (real DuckDuckGo search, real
  HTTP fetches, real `claude-sonnet-5` extraction, mocked provisioning).
  Copied byte-for-byte from `.credforge/runs/.../artifacts/`; every
  credential-shaped field in every file is either `null` or a `vault://`
  reference, verified with a grep pass before this directory was
  committed -- never a raw secret.

- **`nasa_live_credential.json`** -- the one real, live-provisioned and
  live-validated credential this project produced (see DECISIONS.md and
  STUDY.md for the full account). **Provenance note, stated plainly:**
  the original live run (`run_20260730T033948Z_7107f21f`, visible in
  `.credforge/registry.jsonl`) provisioned the credential for real -- it's
  really in the vault, encrypted, under `vault://nasa.gov/api_key` -- but
  that ad hoc run never called EMIT, so no artifact file was ever written
  to disk for it. This JSON was reconstructed by hand from three real,
  independently-checkable sources: the real vault ref name, the real
  registry entry (`credential_type`, `console_url`), and a **fresh live
  re-validation performed just before this commit** (`GET
  api.nasa.gov/planetary/apod` using the actual decrypted vaulted key --
  HTTP 200, real JSON back). It is schema-validated against the real
  `HandoffArtifact` model, not hand-typed against a guess.

- **`hitl_task_etsy.md`** -- a human-readable rendering of a real HITL
  artifact (`seed_batch/etsy-com.json`), showing the format the web UI
  renders live for any HITL result: the quoted evidence, what's missing,
  and what a human should actually do next.
