"""Налаштування, які адміністратор змінює з бота (меню /settings), поверх значень із .env.

Порядок пріоритету: значення з бота (таблиця kv) → .env → вбудоване за замовчуванням.
`RuntimeSettings` поводиться як `Settings`: `rt.poll_interval` повертає актуальне значення, тож код, який
читає налаштування щоразу, бачить зміни одразу. Те, що закладається один раз (інтервали монітора, таймаути
HTTP-клієнтів), оновлюють слухачі `on_change` — їх підключає __main__.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .config import Settings

if TYPE_CHECKING:
    from .crypto import SecretBox
    from .db import Database

log = logging.getLogger(__name__)

KV_PREFIX = "setting:"
KV_PASSWORD = "settings_password"
KV_AUDIT = "settings_audit"
AUDIT_KEEP = 30
MIN_PASSWORD_LEN = 8
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class SettingError(ValueError):
    """Неприйнятне значення; текст показується адміністратору."""


@dataclass(frozen=True)
class Spec:
    name: str            # атрибут Settings
    env: str             # змінна в .env
    kind: str            # int | float | bool | ids | choice | secret
    label: str
    help: str
    group: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()


GROUPS = (
    ("behaviour", "Поведінка"),
    ("access", "Доступ"),
    ("cabinet", "Особистий кабінет"),
    ("tech", "Технічні"),
    ("integration", "Інтеграції"),
)

SPECS: dict[str, Spec] = {s.name: s for s in (
    Spec("poll_interval", "POLL_INTERVAL", "int", "Інтервал опитування, с",
         "Як часто перевіряти сайт. Нове значення діє з наступного циклу.", "behaviour", 60, 86400),
    Spec("max_addresses_per_chat", "MAX_ADDRESSES_PER_CHAT", "int", "Адрес на чат",
         "Скільки адрес може мати один чат чи канал (наявні понад ліміт не видаляються).", "behaviour", 1, 100),
    Spec("default_silent", "DEFAULT_SILENT", "bool", "Нові адреси без звуку",
         "Типове значення «Звук» для нових адрес.", "behaviour"),
    Spec("default_graphs", "DEFAULT_GRAPHS", "bool", "Нові адреси з графіком",
         "Типове значення «Графік» для нових адрес.", "behaviour"),
    Spec("allowed_chat_ids", "ALLOWED_CHAT_IDS", "ids", "Дозволені чати",
         "Telegram ID чатів і користувачів через кому або пробіл. Порожньо (надішли «-») — бот відкритий для всіх. "
         "Адміністратори мають доступ завжди.", "access"),
    Spec("cabinet", "CABINET_ENABLED", "bool", "Інтеграція з кабінетом",
         "Чи пропонувати користувачам чергу з особистого кабінету (потрібен SECRET_KEY у .env).", "cabinet"),
    Spec("cabinet_sync_interval", "CABINET_SYNC_INTERVAL", "int", "Оновлення черг із кабінету, с",
         "Як часто перечитувати черги з кабінету.", "cabinet", 900, 604800),
    Spec("request_timeout", "REQUEST_TIMEOUT", "float", "Таймаут запиту, с",
         "Скільки чекати на відповідь сайту.", "tech", 1, 120),
    Spec("retries", "RETRIES", "int", "Спроб на запит", "Скільки разів повторювати невдалий запит.", "tech", 1, 10),
    Spec("retry_delay", "RETRY_DELAY", "float", "Пауза між спробами, с",
         "Початкова пауза між повторами (далі подвоюється).", "tech", 0, 120),
    Spec("concurrency", "CONCURRENCY", "int", "Одночасних запитів",
         "Скільки адрес опитувати одночасно.", "tech", 1, 20),
    Spec("log_level", "LOG_LEVEL", "choice", "Рівень журналу", "Докладність журналу сервісу.", "tech",
         choices=LOG_LEVELS),
    Spec("slack_token", "SLACK_TOKEN", "secret", "Спільний токен Slack",
         "Токен xoxb-… лише для записів з addresses.toml (користувачі підключають своїх ботів через /slack). "
         "Зберігається зашифрованим. Порожньо (надішли «-») — вимкнути.", "integration"),
)}


def parse_value(spec: Spec, raw: str) -> Any:
    """Текст від адміністратора → значення потрібного типу (SettingError, якщо не підходить)."""
    raw = (raw or "").strip()
    if spec.kind in {"int", "float"}:
        try:
            value = int(raw) if spec.kind == "int" else float(raw.replace(",", "."))
        except ValueError:
            raise SettingError("Потрібне число.") from None
        if (spec.lo is not None and value < spec.lo) or (spec.hi is not None and value > spec.hi):
            raise SettingError(f"Допустимо від {format_number(spec.lo)} до {format_number(spec.hi)}.")
        return value
    if spec.kind == "bool":
        low = raw.lower()
        if low in {"1", "true", "yes", "y", "так", "on", "увімк"}:
            return True
        if low in {"0", "false", "no", "n", "ні", "off", "вимк"}:
            return False
        raise SettingError("Потрібно «так» або «ні».")
    if spec.kind == "ids":
        if raw in {"", "-", "—"}:
            return frozenset()
        try:
            return frozenset(int(x) for x in re.split(r"[,\s]+", raw) if x)
        except ValueError:
            raise SettingError("Потрібні числові ID через кому, наприклад: 123456789, -1001234567890") from None
    if spec.kind == "choice":
        value = raw.upper()
        if value not in spec.choices:
            raise SettingError("Обери одне з: " + ", ".join(spec.choices))
        return value
    if spec.kind == "secret":
        if raw in {"", "-", "—"}:
            return None
        if not re.fullmatch(r"xoxb-[A-Za-z0-9-]{10,}", raw):
            raise SettingError("Це не схоже на токен бота Slack (xoxb-…).")
        return raw
    raise SettingError("Невідомий тип налаштування.")


def format_number(value) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def format_value(spec: Spec, value: Any) -> str:
    if spec.kind == "bool":
        return "так" if value else "ні"
    if spec.kind == "ids":
        return ", ".join(str(x) for x in sorted(value)) if value else "усі (без обмежень)"
    if spec.kind == "secret":
        return f"xoxb-…{value[-4:]}" if value else "не задано"
    if spec.kind in {"int", "float"}:
        return format_number(value)
    return str(value)


def _encode(spec: Spec, value: Any) -> Any:
    return sorted(value) if spec.kind == "ids" else value


def _decode(spec: Spec, raw: Any) -> Any:
    return frozenset(raw) if spec.kind == "ids" else raw


# --- пароль меню налаштувань ----------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        scheme, salt_hex, digest_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1,
                                dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# --- живі налаштування ------------------------------------------------------

class RuntimeSettings:
    def __init__(self, base: Settings, db: Database, box: SecretBox | None = None,
                 env_present: set[str] | None = None) -> None:
        self.base = base
        self.db = db
        self.box = box
        # які змінні справді задано в .env (щоб показати джерело значення)
        self.env_present = env_present if env_present is not None else {
            s.name for s in SPECS.values() if (os.environ.get(s.env) or "").strip()}
        self._listeners: list[Callable[[str, Any], None]] = []
        self._overrides: dict[str, Any] = {}
        self._load()

    # Settings-сумісний доступ: rt.poll_interval, rt.bot_token …
    def __getattr__(self, name: str):
        if name.startswith("_") or name in {"base", "db", "box", "env_present"}:
            raise AttributeError(name)
        if name in SPECS:
            return self._overrides.get(name, getattr(self.base, name))
        return getattr(self.base, name)

    @property
    def cabinet_enabled(self) -> bool:
        return bool(self.base.secret_key) and self.cabinet

    def _load(self) -> None:
        for spec in SPECS.values():
            raw = self.db.kv_get(KV_PREFIX + spec.name)
            if raw is None:
                continue
            try:
                value = _decode(spec, json.loads(raw))
                if spec.kind == "secret" and value:
                    if self.box is None:
                        log.error("Налаштування %s збережено зашифрованим, але SECRET_KEY не задано — ігнорую",
                                  spec.env)
                        continue
                    value = self.box.decrypt(value)
            except Exception:
                log.exception("Не вдалося прочитати налаштування %s з бази — ігнорую", spec.env)
                continue
            self._overrides[spec.name] = value

    def on_change(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.append(listener)

    def apply_all(self) -> None:
        """Викликає слухачів для кожного налаштування (при запуску — щоб застосувати значення з бази)."""
        for name in SPECS:
            self._notify(name)

    def _notify(self, name: str) -> None:
        value = getattr(self, name)
        for listener in self._listeners:
            try:
                listener(name, value)
            except Exception:
                log.exception("Не вдалося застосувати налаштування %s", name)

    def source(self, name: str) -> str:
        if name in self._overrides:
            return "bot"
        return "env" if name in self.env_present else "default"

    def is_overridden(self, name: str) -> bool:
        return name in self._overrides

    def set(self, name: str, value: Any, user_id: int) -> tuple[Any, Any]:
        """Зберігає значення з бота. Повертає (було, стало)."""
        spec = SPECS[name]
        if spec.kind == "secret" and value and self.box is None:
            raise SettingError("Щоб зберігати секрети в базі, задай SECRET_KEY у .env.")
        old = getattr(self, name)
        stored = self.box.encrypt(value) if spec.kind == "secret" and value else value
        self.db.kv_set(KV_PREFIX + name, json.dumps(_encode(spec, stored)))
        self._overrides[name] = value
        self._audit(user_id, name, old, value)
        self._notify(name)
        return old, value

    def reset(self, name: str, user_id: int) -> tuple[Any, Any]:
        """Прибирає значення з бота — діє значення з .env (або за замовчуванням)."""
        old = getattr(self, name)
        self.db.kv_delete(KV_PREFIX + name)
        self._overrides.pop(name, None)
        new = getattr(self, name)
        self._audit(user_id, name, old, new, reset=True)
        self._notify(name)
        return old, new

    def _audit(self, user_id: int, name: str, old: Any, new: Any, *, reset: bool = False) -> None:
        spec = SPECS[name]
        entry = {"t": int(time.time()), "user": user_id, "name": name, "old": format_value(spec, old),
                 "new": format_value(spec, new), "reset": reset}
        log.warning("Налаштування %s змінено адміністратором %s: %s → %s%s", spec.env, user_id, entry["old"],
                    entry["new"], " (скинуто до .env)" if reset else "")
        try:
            items = json.loads(self.db.kv_get(KV_AUDIT) or "[]")
        except ValueError:
            items = []
        items.append(entry)
        self.db.kv_set(KV_AUDIT, json.dumps(items[-AUDIT_KEEP:], ensure_ascii=False))

    def audit(self) -> list[dict]:
        try:
            return json.loads(self.db.kv_get(KV_AUDIT) or "[]")
        except ValueError:
            return []

    # --- пароль ---
    @property
    def has_password(self) -> bool:
        return bool(self.db.kv_get(KV_PASSWORD))

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LEN:
            raise SettingError(f"Пароль має бути не коротший за {MIN_PASSWORD_LEN} символів.")
        self.db.kv_set(KV_PASSWORD, hash_password(password))

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.db.kv_get(KV_PASSWORD))