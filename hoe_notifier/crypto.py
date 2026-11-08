"""Шифрування секретів у базі (паролі й токени кабінету, токени Slack-ботів) ключем SECRET_KEY."""
from __future__ import annotations

import hashlib

ENCRYPTED_PREFIX = "gAAAAA"   # так починається кожен токен Fernet (версія 0x80 у base64)


class DecryptError(RuntimeError):
    """Значення не вдалося розшифрувати (інший ключ або пошкоджені дані)."""


class SecretBox:
    def __init__(self, key: str | bytes) -> None:
        from cryptography.fernet import Fernet
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise DecryptError("Не вдалося розшифрувати збережені дані (змінився SECRET_KEY?)") from exc


def validate_key(key: str) -> None:
    """ValueError, якщо рядок не є ключем Fernet."""
    try:
        SecretBox(key)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def looks_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(ENCRYPTED_PREFIX)


def fingerprint(value: str) -> str:
    """Відбиток секрету для пошуку дублікатів без розшифрування (Fernet щоразу дає інший шифротекст)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()