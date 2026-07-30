"""Vault reference naming.

vault_ref strings are the only credential-shaped value that leaves the
vault module -- everything downstream (the artifact schema, logs, the
registry) stores this string, never the secret itself.
"""


def make_vault_ref(identity_key: str, credential_field: str) -> str:
    return f"vault://{identity_key}/{credential_field}"
