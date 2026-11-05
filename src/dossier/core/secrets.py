"""Secrets in the OS keyring (macOS Keychain, Windows Credential Manager).

Lookup order: ``DOSSIER_SECRET_<NAME>`` environment variable (CI, containers),
then the keyring entry ``dossier/<name>``. Values are never logged or printed.
"""

from __future__ import annotations

import os
import re

SERVICE = "dossier"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _check(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"invalid secret name {name!r}: use lowercase letters, digits, . _ -")
    return name


def env_var(name: str) -> str:
    return "DOSSIER_SECRET_" + re.sub(r"[^A-Z0-9]", "_", _check(name).upper())


def get_secret(name: str) -> str | None:
    value = os.environ.get(env_var(name))
    if value:
        return value
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - keyring is a base dependency
        return None
    try:
        stored = keyring.get_password(SERVICE, name)
    except KeyringError:
        return None
    return stored or None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, _check(name), value)


def delete_secret(name: str) -> bool:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, _check(name))
    except PasswordDeleteError:
        return False
    return True
