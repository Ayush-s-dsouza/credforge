from pathlib import Path

import pytest

from credforge.registry.store import AppendOnlyRegistry
from credforge.vault.crypto_vault import FernetVault, generate_key


@pytest.fixture
def vault_key() -> str:
    return generate_key()


@pytest.fixture
def vault(tmp_path: Path, vault_key: str) -> FernetVault:
    return FernetVault(key=vault_key, path=tmp_path / "vault" / "secrets.vault")


@pytest.fixture
def registry(tmp_path: Path) -> AppendOnlyRegistry:
    return AppendOnlyRegistry(tmp_path / "registry.jsonl")
