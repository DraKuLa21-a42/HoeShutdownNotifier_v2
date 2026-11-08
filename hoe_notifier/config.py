from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "так", "on"}


def _int_set(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    try:
        return frozenset(int(x) for x in re.split(r"[,\s]+", value.strip()) if x)
    except ValueError as exc:
        raise ConfigError(f"Очікував список чисел через кому, отримав: {value!r}") from exc


def _path(value: str | None, default: str) -> Path:
    p = Path(value or default)
    return p if p.is_absolute() else BASE_DIR / p


def normalize_bot_token(token: str | None) -> str | None:
    """Приймає і `123:ABC`, і старий формат зі скрипта `bot123:ABC`."""
    if not token:
        return None
    return re.sub(r"^bot(?=\d+:)", "", token.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    admin_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    slack_token: str | None
    poll_interval: int
    max_addresses_per_chat: int
    db_path: Path
    addresses_file: Path
    hoe_base_url: str
    request_timeout: float
    retries: int
    retry_delay: float
    concurrency: int
    default_silent: bool
    default_graphs: bool
    log_level: str
    cabinet_key: str | None = None            # CABINET_SECRET_KEY; None — інтеграцію з кабінетом вимкнено
    cabinet_base_url: str = "https://cabinet.hoe.com.ua"
    cabinet_sync_interval: int = 21600        # секунд між оновленнями черг із кабінету

    @property
    def cabinet_enabled(self) -> bool:
        return bool(self.cabinet_key)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or BASE_DIR / ".env")
    env = os.environ.get

    settings = Settings(
        bot_token=normalize_bot_token(env("BOT_TOKEN")),
        admin_ids=_int_set(env("ADMIN_IDS")),
        allowed_chat_ids=_int_set(env("ALLOWED_CHAT_IDS")),
        slack_token=(env("SLACK_TOKEN") or None),
        poll_interval=max(60, int(env("POLL_INTERVAL", "900"))),
        max_addresses_per_chat=int(env("MAX_ADDRESSES_PER_CHAT", "5")),
        db_path=_path(env("DB_PATH"), "data/hoe.sqlite3"),
        addresses_file=_path(env("ADDRESSES_FILE"), "addresses.toml"),
        hoe_base_url=(env("HOE_BASE_URL") or "https://hoe.com.ua").rstrip("/"),
        request_timeout=float(env("REQUEST_TIMEOUT", "15")),
        retries=max(1, int(env("RETRIES", "3"))),
        retry_delay=float(env("RETRY_DELAY", "5")),
        concurrency=max(1, int(env("CONCURRENCY", "3"))),
        default_silent=_bool(env("DEFAULT_SILENT"), True),
        default_graphs=_bool(env("DEFAULT_GRAPHS"), False),
        log_level=(env("LOG_LEVEL") or "INFO").upper(),
        cabinet_key=(env("CABINET_SECRET_KEY") or "").strip() or None,
        cabinet_base_url=(env("CABINET_BASE_URL") or "https://cabinet.hoe.com.ua").rstrip("/"),
        cabinet_sync_interval=max(900, int(env("CABINET_SYNC_INTERVAL", "21600"))),
    )
    if settings.cabinet_key:
        from .cabinet import validate_key
        try:
            validate_key(settings.cabinet_key)
        except ValueError as exc:
            raise ConfigError(
                "CABINET_SECRET_KEY має бути ключем Fernet. Згенеруй: python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\"") from exc
    if not settings.bot_token and not settings.slack_token:
        raise ConfigError("Потрібен BOT_TOKEN (Telegram) і/або SLACK_TOKEN. Див. .env.example")
    return settings