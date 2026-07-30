"""PROVISION: creates a real developer account and captures its credential.

Unlike RESOLVE/DISCOVER/CLASSIFY/GATE -- pure research functions with no
persistent side effects beyond network calls -- PROVISION is the first
stage that mutates durable state: it writes to the credential vault and
appends to the append-only registry. That's why its signature carries
`vault`/`registry`/`run_id`/`settings_fingerprint`, none of which any
earlier stage needs. See DECISIONS.md D-031.

Account-level and API-level credentials are vaulted separately, under
separate refs, and never conflated in the artifact -- see DECISIONS.md
D-041. PROVISION generates the account password itself (not the browser
driver) because "generate a strong password" is not a vendor-specific
concern; the driver only decides whether to *use* it, based on whether
the vendor's flow has a password field at all.

An `auth_scheme` of NONE short-circuits immediately: some real APIs
(Open-Meteo) are genuinely open, and provisioning "succeeds" trivially --
no browser call, no email, nothing to vault. This is an explicit
no-credential-required state, not a null that looks like a failure.

The idempotency guard runs FIRST, before any provider call: if a
non-closed credential already exists for this identity_key, provision()
returns immediately with status="already_provisioned" and zero
email/browser calls. This is what makes provision() safe to call
repeatedly -- crash-resume, or simply re-running the same app -- without
ever creating a second live account for the same vendor. See OPS.md's
"what happens if it dies mid-batch" section and DECISIONS.md D-006.

Write ordering on the success path matters and is deliberate: every
vault store() call happens, and must succeed, before the registry entry
is appended. A crash between the two leaves an orphaned vault entry
(harmless -- never referenced by anything) rather than a registry entry
that claims a ref which was never actually written (which resume logic
would trust and then fail to read). See DECISIONS.md D-006's append-only
invariant.

Every real secret this stage handles -- the generated account password,
and each API-level secret the signup flow returns (api_key,
client_secret, bearer_token) -- is registered with the process-wide
redaction registry (`register_secret`) the moment it exists, before it's
ever passed to a provider, vaulted, or has any chance of reaching a log
line or an --explain message. See DECISIONS.md D-043.
"""

import secrets
from datetime import datetime, timezone

from ..enums import AuthScheme, CredentialType, PipelineStage, ReasonCode, StageStatus
from ..models.registry_entities import RegistryEntry
from ..models.state import ProvisionResult
from ..providers.browser import BrowserDriver
from ..providers.email import EmailProvider
from ..redaction import register_secret
from ..registry.store import AppendOnlyRegistry
from ..vault.crypto_vault import FernetVault
from ..vault.secret_ref import make_vault_ref

_PASSWORD_BYTES = 18  # ~24 chars of base64url -- comfortably strong, no vendor has ever rejected it for being too long in testing


def _generate_account_password() -> str:
    return secrets.token_urlsafe(_PASSWORD_BYTES)


def _result_from_registry_entry(entry: RegistryEntry) -> ProvisionResult:
    return ProvisionResult(
        status="already_provisioned",
        account_email=entry.email_alias,
        account_password_ref=entry.account_password_vault_ref,
        credential_type=entry.credential_type,
        api_key_ref=entry.api_key_ref,
        client_id=entry.client_id,
        client_secret_ref=entry.client_secret_ref,
        bearer_token_ref=entry.bearer_token_ref,
        console_url=entry.console_url,
    )


async def provision(
    identity_key: str,
    *,
    app_name: str,
    developer_portal_url: str,
    redirect_uris: list[str],
    auth_scheme: AuthScheme | None,
    email: EmailProvider,
    browser: BrowserDriver,
    vault: FernetVault,
    registry: AppendOnlyRegistry,
    run_id: str,
    settings_fingerprint: str,
    headed: bool = False,
) -> ProvisionResult:
    existing = registry.find_open_provision(identity_key)
    if existing is not None:
        return _result_from_registry_entry(existing)

    if auth_scheme == AuthScheme.NONE:
        # Nothing to acquire -- a genuinely open API. Still recorded in
        # the registry (so a re-run short-circuits here too, consistently
        # with every other outcome) but touches no email/browser/vault.
        registry.append(
            RegistryEntry(
                identity_key=identity_key,
                app_name=app_name,
                run_id=run_id,
                stage=PipelineStage.PROVISION,
                stage_status=StageStatus.COMPLETED,
                settings_fingerprint=settings_fingerprint,
                recorded_at=datetime.now(timezone.utc),
                credential_type=CredentialType.NONE,
            )
        )
        return ProvisionResult(status="provisioned", credential_type=CredentialType.NONE)

    email_alias = email.alias_for(identity_key)
    account_password = _generate_account_password()
    register_secret(account_password)  # before it's ever handed to the browser driver

    outcome = await browser.signup_and_create_app(
        developer_portal_url=developer_portal_url,
        email_alias=email_alias,
        email_provider=email,
        app_display_name=app_name,
        redirect_uris=redirect_uris,
        account_password=account_password,
        headed=headed,
    )

    if not outcome.success:
        return ProvisionResult(
            status="failed",
            reason_code=ReasonCode.PROVISION_FAILED,
            account_email=email_alias,
            steps=outcome.steps,
        )

    account_password_ref: str | None = None
    if outcome.account_password_used is not None:
        register_secret(outcome.account_password_used)  # defense in depth -- should equal `account_password` above
        account_password_ref = make_vault_ref(identity_key, "account_password")
        vault.store(account_password_ref, {"password": outcome.account_password_used})

    raw = outcome.raw_api_credential
    client_id = raw.get("client_id")  # not secret -- never vaulted, never registered

    api_key_ref: str | None = None
    if "api_key" in raw:
        register_secret(raw["api_key"])
        api_key_ref = make_vault_ref(identity_key, "api_key")
        vault.store(api_key_ref, {"api_key": raw["api_key"]})

    client_secret_ref: str | None = None
    if "client_secret" in raw:
        register_secret(raw["client_secret"])
        client_secret_ref = make_vault_ref(identity_key, "client_secret")
        vault.store(client_secret_ref, {"client_secret": raw["client_secret"]})

    bearer_token_ref: str | None = None
    if "bearer_token" in raw:
        register_secret(raw["bearer_token"])
        bearer_token_ref = make_vault_ref(identity_key, "bearer_token")
        vault.store(bearer_token_ref, {"bearer_token": raw["bearer_token"]})

    registry.append(
        RegistryEntry(
            identity_key=identity_key,
            app_name=app_name,
            run_id=run_id,
            stage=PipelineStage.PROVISION,
            stage_status=StageStatus.COMPLETED,
            settings_fingerprint=settings_fingerprint,
            recorded_at=datetime.now(timezone.utc),
            email_alias=email_alias,
            console_url=outcome.console_url,
            account_password_vault_ref=account_password_ref,
            credential_type=outcome.credential_type,
            api_key_ref=api_key_ref,
            client_id=client_id,
            client_secret_ref=client_secret_ref,
            bearer_token_ref=bearer_token_ref,
        )
    )

    return ProvisionResult(
        status="provisioned",
        account_email=email_alias,
        account_password_ref=account_password_ref,
        credential_type=outcome.credential_type,
        api_key_ref=api_key_ref,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        bearer_token_ref=bearer_token_ref,
        console_url=outcome.console_url,
        steps=outcome.steps,
    )
