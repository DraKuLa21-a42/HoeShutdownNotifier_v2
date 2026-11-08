"""Меню /settings: живі налаштування поверх .env, пароль, сесія, блокування."""
import time

import pytest
from cryptography.fernet import Fernet

from hoe_notifier.crypto import SecretBox, looks_encrypted
from hoe_notifier.db import Database
from hoe_notifier.runtime import (
    SPECS, RuntimeSettings, SettingError, hash_password, parse_value, verify_password,
)

from .test_bot import make_env, settings as make_settings


def rt(**over):
    db = Database(":memory:")
    return RuntimeSettings(make_settings(**over), db, SecretBox(Fernet.generate_key()), env_present={"poll_interval"})


# --- значення ------------------------------------------------------------------

def test_parse_values():
    assert parse_value(SPECS["poll_interval"], "600") == 600
    assert parse_value(SPECS["request_timeout"], "7,5") == 7.5
    assert parse_value(SPECS["default_silent"], "ні") is False
    assert parse_value(SPECS["allowed_chat_ids"], "1, -100 2") == frozenset({1, -100, 2})
    assert parse_value(SPECS["allowed_chat_ids"], "-") == frozenset()
    assert parse_value(SPECS["log_level"], "debug") == "DEBUG"
    assert parse_value(SPECS["slack_token"], "-") is None
    for name, raw in [("poll_interval", "10"), ("poll_interval", "abc"), ("default_silent", "може"),
                      ("allowed_chat_ids", "a,b"), ("log_level", "TRACE"), ("slack_token", "abc")]:
        with pytest.raises(SettingError):
            parse_value(SPECS[name], raw)


def test_override_reset_source_and_listeners():
    r = rt()
    seen = []
    r.on_change(lambda n, v: seen.append((n, v)))
    assert r.poll_interval == 900 and r.source("poll_interval") == "env" and r.source("concurrency") == "default"
    r.set("poll_interval", 120, user_id=42)
    assert r.poll_interval == 120 and r.source("poll_interval") == "bot" and seen[-1] == ("poll_interval", 120)
    assert r.bot_token == "1:x"                      # не-налаштування беруться з .env як є
    again = RuntimeSettings(r.base, r.db, r.box)     # після перезапуску значення з бази зберігається
    assert again.poll_interval == 120
    r.reset("poll_interval", user_id=42)
    assert r.poll_interval == 900 and seen[-1] == ("poll_interval", 900)
    assert [(e["name"], e["old"], e["new"], e["reset"]) for e in r.audit()] == [
        ("poll_interval", "900", "120", False), ("poll_interval", "120", "900", True)]


def test_secret_setting_encrypted_and_needs_key():
    r = rt()
    r.set("slack_token", "xoxb-1111-2222-abcdefghijkl", user_id=42)
    stored = r.db.kv_get("setting:slack_token")
    assert "abcdefghijkl" not in stored and looks_encrypted(stored.strip('"'))
    assert RuntimeSettings(r.base, r.db, r.box).slack_token == "xoxb-1111-2222-abcdefghijkl"
    assert "abcdefghijkl" not in str(r.audit())
    plain = RuntimeSettings(make_settings(), Database(":memory:"))
    with pytest.raises(SettingError):
        plain.set("slack_token", "xoxb-1111-2222-abcdefghijkl", user_id=42)


def test_password_hashing():
    h = hash_password("long-password")
    assert verify_password("long-password", h) and not verify_password("other", h)
    assert not verify_password("x", None) and not verify_password("x", "garbage")
    r = rt()
    with pytest.raises(SettingError):
        r.set_password("short")
    r.set_password("long-password")
    assert r.has_password and r.check_password("long-password")


# --- бот -------------------------------------------------------------------------

@pytest.fixture
def env():
    e = make_env(admin_ids=frozenset({42, 7}))
    yield e
    e[0].close()


def deletes(session):
    return sum(type(m).__name__ == "DeleteMessage" for m in session.methods)


def last_markup_data(session):
    for m in reversed(session.methods):
        if type(m).__name__ in {"SendMessage", "EditMessageText"} and m.reply_markup:
            return [b.callback_data for row in m.reply_markup.inline_keyboard for b in row]
    return []


async def unlock(send, msg, password="correct-horse"):
    await send(msg("/settings"))
    await send(msg(password))


async def create_password(send, msg, password="correct-horse"):
    await send(msg("/settings"))
    await send(msg(password))
    await send(msg(password))


async def test_first_open_creates_password(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/settings"))
    assert "ще не створено" in session.texts()[-1]
    await send(msg("short"))
    assert "закороткий" in session.texts()[-1]
    await send(msg("correct-horse"))
    await send(msg("other-horse"))
    assert "не збігаються" in session.texts()[-1]
    await send(msg("correct-horse"))
    await send(msg("correct-horse"))
    assert "Пароль створено" in " ".join(session.texts())
    assert "⚙️ <b>Налаштування</b>" in session.texts()[-1]
    assert deletes(session) == 5                         # кожне повідомлення з паролем видалено
    assert db.kv_get("settings_password").startswith("scrypt$")
    assert any("створив пароль" in t for t in session.sent_to(7))   # інший адмін дізнався


async def test_non_admin_and_group_ignored(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/settings", chat_type="group", chat_id=-5))
    assert session.texts() == []
    await send(cb("set:home"))                            # без сесії
    assert db.kv_get("settings_password") is None


async def test_lock_and_wrong_password_lockout(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    await send(cb("set:lock"))
    await send(cb("set:g:behaviour"))                     # сесію закрито — кнопки не працюють
    assert "Меню налаштувань закрито" in session.texts()[-1]
    await send(msg("/settings"))
    assert "Введи пароль" in session.texts()[-1]
    for _ in range(4):
        await send(msg("wrong-pass"))
    assert "Залишилось спроб: 1" in session.texts()[-1]
    await send(msg("wrong-pass"))
    assert "заблоковано" in session.sent_to(1)[-1]
    assert any("невдалих спроб" in t for t in session.sent_to(7))
    await send(msg("/settings"))
    assert "Забагато невдалих спроб" in session.texts()[-1]
    await send(msg("correct-horse"))                      # блокування — навіть правильний пароль не діє
    assert "⚙️ <b>Налаштування</b>" not in session.texts()[-1]


async def test_session_expires(env, monkeypatch):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 16 * 60)
    await send(msg("/settings"))
    assert "Введи пароль" in session.texts()[-1]


async def test_change_int_setting_applies_to_bot(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    await send(cb("set:g:behaviour"))
    assert "set:e:max_addresses_per_chat" in last_markup_data(session)
    await send(cb("set:e:max_addresses_per_chat"))
    await send(cb("set:v:max_addresses_per_chat"))
    await send(msg("abc"))
    assert "Потрібне число" in session.texts()[-1]
    await send(msg("1"))
    assert "Адрес на чат: 2 → 1" in " ".join(session.texts())
    assert "set:r:max_addresses_per_chat" in last_markup_data(session)
    assert any("змінив налаштування" in t for t in session.sent_to(7))

    await send(msg("/add_id 10 5 Дім"))
    await send(msg("/add_id 10 6 Дача"))
    assert db.count_for_target("tg", "1") == 1 and "Ліміт: 1" in session.texts()[-1]

    await send(cb("set:r:max_addresses_per_chat"))       # скинути до .env
    await send(msg("/add_id 10 6 Дача"))
    assert db.count_for_target("tg", "1") == 2


async def test_toggle_choice_and_journal(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    await send(cb("set:t:default_silent"))
    await send(cb("set:c:log_level:DEBUG"))
    await send(cb("set:log"))
    journal = session.texts()[-1]
    assert "Нові адреси без звуку: так → ні" in journal and "Рівень журналу: INFO → DEBUG" in journal
    await send(msg("/add_id 10 5 Дім"))
    assert not db.list_subscriptions("tg", "1")[0].silent   # нове типове значення вже діє


async def test_allowed_chats_setting(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    await send(cb("set:v:allowed_chat_ids"))
    await send(msg("555"))
    assert "Дозволені чати: усі (без обмежень) → 555" in " ".join(session.texts())


async def test_secret_setting_message_deleted(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    before = deletes(session)
    await send(cb("set:v:slack_token"))
    await send(msg("xoxb-1111-2222-abcdefghijkl"))
    assert deletes(session) == before + 1
    # без SECRET_KEY секрет не зберігається
    assert "SECRET_KEY" in session.texts()[-1] and db.kv_get("setting:slack_token") is None


async def test_change_password_from_menu(env):
    db, hoe, session, send, msg, cb = env
    await create_password(send, msg)
    await send(cb("set:pw"))
    await send(msg("new-password-1"))
    await send(msg("new-password-1"))
    assert "Пароль змінено" in " ".join(session.texts())
    await send(cb("set:lock"))
    await unlock(send, msg, "new-password-1")
    assert "⚙️ <b>Налаштування</b>" in session.texts()[-1]


async def test_admin_panel_links_settings(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/admin"))
    assert "set:open" in last_markup_data(session)
    await send(cb("set:open"))
    assert "ще не створено" in session.texts()[-1]