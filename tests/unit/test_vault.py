import json

import pytest

from credforge.vault.crypto_vault import (
    FernetVault,
    VaultError,
    VaultKeyMissingError,
    VaultRefNotFoundError,
    generate_key,
)


def test_store_and_retrieve_roundtrip(vault: FernetVault) -> None:
    vault.store("vault://github.com/oauth_client_secret", {"client_secret": "sk_live_abc123"})
    secret = vault.retrieve("vault://github.com/oauth_client_secret")
    assert secret == {"client_secret": "sk_live_abc123"}


def test_ciphertext_on_disk_never_contains_the_plaintext(vault: FernetVault) -> None:
    vault.store("vault://x", {"api_key": "sk_test_super_secret_value"})
    on_disk = vault.path.read_text(encoding="utf-8")
    assert "sk_test_super_secret_value" not in on_disk


def test_missing_ref_raises(vault: FernetVault) -> None:
    with pytest.raises(VaultRefNotFoundError):
        vault.retrieve("vault://nope")


def test_missing_key_raises(tmp_path) -> None:
    # Failure drill (1/2): no CREDFORGE_VAULT_KEY set at all.
    with pytest.raises(VaultKeyMissingError):
        FernetVault(key="", path=tmp_path / "v.vault")


def test_tampered_ciphertext_is_rejected(tmp_path, vault_key: str) -> None:
    # Failure drill (2/2): an on-disk vault file edited outside of credforge
    # (or corrupted by a partial disk write) must not silently return
    # garbage as if it were a real secret -- it must fail loudly.
    path = tmp_path / "vault" / "secrets.vault"
    vault = FernetVault(key=vault_key, path=path)
    vault.store("vault://x", {"api_key": "sk_test_1"})

    data = json.loads(path.read_text(encoding="utf-8"))
    data["vault://x"] = data["vault://x"][:-4] + "abcd"  # flip the token's tail
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(VaultError):
        vault.retrieve("vault://x")


def test_wrong_key_cannot_decrypt_another_vaults_secret(tmp_path) -> None:
    # A stale/wrong CREDFORGE_VAULT_KEY (e.g. after a botched key rotation)
    # must fail loudly, not return corrupted plaintext.
    path = tmp_path / "vault" / "secrets.vault"
    vault_a = FernetVault(key=generate_key(), path=path)
    vault_a.store("vault://x", {"api_key": "sk_test_1"})

    vault_b = FernetVault(key=generate_key(), path=path)
    with pytest.raises(VaultError):
        vault_b.retrieve("vault://x")
