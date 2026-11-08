"""Шифрування секретів у базі: токени Slack-ботів, SECRET_KEY."""
import pytest
from cryptography.fernet import Fernet

from hoe_notifier.config import load_settings
from hoe_notifier.crypto import DecryptError, SecretBox, fingerprint, looks_encrypted
from hoe_notifier.db import Database
from hoe_notifier.sinks import ChannelError, SlackSink, TargetGone

TOKEN = "xoxb-1111-2222-abcdefghijkl"
KEY = Fernet.generate_key().decode()


def raw_tokens(db):
    return [r["token"] for r in db._conn.execute("SELECT token FROM slack_bots ORDER BY id")]


def test_box_roundtrip_and_wrong_key():
    box = SecretBox(KEY)
    enc = box.encrypt("секрет")
    assert looks_encrypted(enc) and box.decrypt(enc) == "секрет"
    with pytest.raises(DecryptError):
        SecretBox(Fernet.generate_key()).decrypt(enc)


def test_slack_token_encrypted_at_rest():
    db = Database(":memory:", box=SecretBox(KEY))
    bot, created = db.add_slack_bot(1, TOKEN, "Acme", "b")
    assert created and bot.token == TOKEN
    assert TOKEN not in raw_tokens(db)[0] and looks_encrypted(raw_tokens(db)[0])
    assert db.get_slack_bot(bot.id).token == TOKEN and db.list_slack_bots(1)[0].token == TOKEN
    again, created = db.add_slack_bot(1, TOKEN, "Acme", "b")      # дублікат знаходиться за відбитком
    assert not created and again.id == bot.id and len(db.list_slack_bots(1)) == 1
    other, created = db.add_slack_bot(2, TOKEN, "Acme", "b")      # інший користувач — окремий запис
    assert created and other.id != bot.id
    assert "xoxb" not in repr(bot)


def test_without_key_tokens_stay_plain():
    db = Database(":memory:")
    db.add_slack_bot(1, TOKEN, "Acme", "b")
    assert raw_tokens(db) == [TOKEN] and db.plaintext_slack_tokens() == 1


def test_existing_plain_tokens_get_encrypted_on_start(tmp_path):
    path = tmp_path / "db.sqlite3"
    old = Database(path)
    old._conn.execute("INSERT INTO slack_bots (owner_id,token,team,bot_name,created_at) VALUES (1,?,'A','b',0)",
                      (TOKEN,))          # як у базі старої версії: без token_hash
    old._conn.commit()
    old.close()

    db = Database(path, box=SecretBox(KEY))
    assert looks_encrypted(raw_tokens(db)[0]) and db.plaintext_slack_tokens() == 0
    assert db.list_slack_bots(1)[0].token == TOKEN
    hash_ = db._conn.execute("SELECT token_hash FROM slack_bots").fetchone()[0]
    assert hash_ == fingerprint(TOKEN)
    assert not db.add_slack_bot(1, TOKEN, "A", "b")[1]
    db.close()


async def test_lost_key_does_not_delete_subscriptions(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path, box=SecretBox(KEY))
    bot, _ = db.add_slack_bot(1, TOKEN, "Acme", "b")
    db.close()

    db = Database(path, box=SecretBox(Fernet.generate_key().decode()))   # ключ змінили
    assert db.get_slack_bot(bot.id).token == ""
    assert "не розшифровується" in db.get_slack_bot(bot.id).masked
    sink = SlackSink(None, db)
    with pytest.raises(RuntimeError) as info:
        await sink.send_text(f"{bot.id}/C0123ABCD", "s", "l", "b", silent=True)
    assert not isinstance(info.value, TargetGone)      # монітор лише залогує, підписки залишаться
    with pytest.raises(ChannelError):
        await sink.resolve(bot.id, "C0123ABCD")
    db.close()


def test_settings_secret_key_and_cabinet_switch(tmp_path, monkeypatch):
    for var in ("BOT_TOKEN", "SECRET_KEY", "CABINET_SECRET_KEY", "CABINET_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"BOT_TOKEN=1:x\nSECRET_KEY={KEY}\n")
    s = load_settings(env)
    assert s.secret_key == KEY and s.cabinet_enabled
    monkeypatch.setenv("CABINET_ENABLED", "false")
    assert not load_settings(env).cabinet_enabled