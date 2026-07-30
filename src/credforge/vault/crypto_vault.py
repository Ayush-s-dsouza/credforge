"""Encrypted local credential vault.

Secrets never touch disk in plaintext: every value passed to store() is
Fernet-encrypted before the JSON file on disk is written, and the
decryption key never lives anywhere but an environment variable (or, in
tests, an ephemeral key generated per test). The file on disk maps
vault_ref -> ciphertext; nothing about the plaintext credential is
recoverable from that file without the key. See DECISIONS.md D-005.
"""

import json
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    """Base class for all vault failures."""


class VaultKeyMissingError(VaultError):
    """Raised when no vault key was supplied at all."""


class VaultRefNotFoundError(VaultError):
    """Raised when retrieve() is called with a vault_ref that was never stored."""


def generate_key() -> str:
    """Generate a new Fernet key, for bootstrapping CREDFORGE_VAULT_KEY or for tests.

    This is deliberately a separate, explicitly-named function -- the vault
    itself never calls this on your behalf. If it did, a missing env var
    would silently generate a fresh key on every process start, and every
    restart would turn every previously-vaulted secret into undecryptable
    garbage. See DECISIONS.md D-005.
    """
    return Fernet.generate_key().decode("ascii")


class FernetVault:
    def __init__(self, *, key: str, path: Path) -> None:
        if not key:
            raise VaultKeyMissingError(
                "no vault key supplied -- set CREDFORGE_VAULT_KEY (see .env.example)"
            )
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def store(self, vault_ref: str, secret: dict[str, str]) -> None:
        token = self._fernet.encrypt(json.dumps(secret).encode("utf-8"))
        with self._lock:
            data = self._read_all()
            data[vault_ref] = token.decode("ascii")
            self._write_all(data)

    def retrieve(self, vault_ref: str) -> dict[str, str]:
        with self._lock:
            data = self._read_all()
        if vault_ref not in data:
            raise VaultRefNotFoundError(vault_ref)
        try:
            payload = self._fernet.decrypt(data[vault_ref].encode("ascii"))
        except InvalidToken as exc:
            raise VaultError(
                f"vault entry {vault_ref!r} could not be decrypted -- wrong key or corrupted ciphertext"
            ) from exc
        return json.loads(payload.decode("utf-8"))

    def _read_all(self) -> dict[str, str]:
        raw = self._path.read_text(encoding="utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _write_all(self, data: dict[str, str]) -> None:
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
