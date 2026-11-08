"""Особистий кабінет: HTTP-клієнт, токени, шифрування, синхронізація черг, діалоги бота."""
import json
import sqlite3

import httpx
import pytest
from cryptography.fernet import Fernet

from hoe_notifier.cabinet import (
    CabinetAuthError, CabinetClient, CabinetService, CabinetUnavailable, OmsData, QueueEntry, SecretBox,
    TokenRejected, house_matches, mask_login, pick_queue, street_matches,
)
from hoe_notifier.config import ConfigError, load_settings
from hoe_notifier.db import CABINET_AUTH_FAILED, Database
from hoe_notifier.monitor import SUBJECT_CABINET_AUTH, SUBJECT_QUEUE_CHANGED, Monitor

from .test_monitor import FakeHoe, FakeSink

KEY = Fernet.generate_key().decode()
ADDRESS = ("Хмельницька область, Хмельницький район, м. Хмельницький (Хмельницька громада), "
           "вул. Мирного П. 21, кв.11")


class FakeSite:
    """Підставний cabinet.hoe.com.ua."""

    def __init__(self):
        self.login, self.password = "user@mail.com", "secret"
        self.contracts = [{"cabinetId": 16568, "contractNumber": "12100000902191",
                           "contractStatusName": "активний", "ownerName": "X", "address": ADDRESS}]
        self.queues = {16568: [{"community": "Хмельницька", "settlement": "м. Хмельницький",
                                "street": "вул. Мирного П.", "house": "21", "queue": "2.2", "queueGAV": "0"}]}
        self.logins = 0
        self.attempts = 0
        self.valid_tokens: set[str] = set()
        self.down = False
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503)
        path = request.url.path
        if path == "/Token":
            body = json.loads(request.content)
            self.attempts += 1
            if (body["username"], body["password"]) != (self.login, self.password):
                return httpx.Response(400, json={"message": "Невірний логін або пароль"})
            self.logins += 1
            token = f"tok{self.logins}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"accessToken": token, "tokenType": "bearer", "expiresIn": 86399})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_tokens:
            return httpx.Response(401)
        if path.endswith("/cabinet/get-by-user"):
            return httpx.Response(200, json=self.contracts)
        if "/get-oms-data/" in path:
            cid = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"shutdowns": [], "shutdownQueues": self.queues.get(cid, [])})
        return httpx.Response(404)


def make_client(site: FakeSite) -> CabinetClient:
    return CabinetClient("https://cab.test", retries=2, retry_delay=0,
                         client=httpx.AsyncClient(transport=httpx.MockTransport(site.handler)))


def make_service(db: Database, site: FakeSite) -> CabinetService:
    return CabinetService(db, make_client(site), SecretBox(KEY))


# --- клієнт ------------------------------------------------------------------

async def test_login_sends_json_and_returns_token():
    site = FakeSite()
    token, exp = await make_client(site).login("user@mail.com", "secret")
    assert token == "tok1" and exp > 0
    req = site.requests[0]
    assert req.method == "POST" and json.loads(req.content) == {
        "username": "user@mail.com", "password": "secret", "rememberme": True}


async def test_login_wrong_password():
    with pytest.raises(CabinetAuthError):
        await make_client(FakeSite()).login("user@mail.com", "bad")


async def test_contracts_and_oms_use_bearer():
    site = FakeSite()
    c = make_client(site)
    token, _ = await c.login("user@mail.com", "secret")
    contracts = await c.contracts(token)
    assert contracts[0].cabinet_id == 16568 and contracts[0].number == "12100000902191"
    oms = await c.oms_data(token, 16568)
    assert oms.queues[0].queue == "2.2" and oms.queues[0].house == "21"
    assert site.requests[-1].headers["authorization"] == "Bearer tok1"


async def test_rejected_token_and_site_down():
    site = FakeSite()
    c = make_client(site)
    with pytest.raises(TokenRejected):
        await c.contracts("nope")
    site.down = True
    with pytest.raises(CabinetUnavailable):
        await c.login("user@mail.com", "secret")


# --- сервіс ------------------------------------------------------------------

async def test_sign_in_stores_encrypted_and_reuses_token():
    db, site = Database(":memory:"), FakeSite()
    svc = make_service(db, site)
    acc = await svc.sign_in(42, "user@mail.com", "secret")
    raw = db._conn.execute("SELECT password, token FROM cabinet_accounts").fetchone()
    assert "secret" not in raw["password"] and "tok1" not in raw["token"]
    await svc.contracts(acc)
    await svc.oms(acc, 16568)
    assert site.logins == 1                          # токен із бази, без повторного входу


async def test_relogin_when_token_rejected():
    db, site = Database(":memory:"), FakeSite()
    svc = make_service(db, site)
    acc = await svc.sign_in(42, "user@mail.com", "secret")
    site.valid_tokens.clear()                        # сайт «забув» токен
    assert (await svc.oms(acc, 16568)).queues[0].queue == "2.2"
    assert site.logins == 2


async def test_password_changed_marks_account():
    db, site = Database(":memory:"), FakeSite()
    svc = make_service(db, site)
    acc = await svc.sign_in(42, "user@mail.com", "secret")
    site.valid_tokens.clear()
    site.password = "new"
    with pytest.raises(CabinetAuthError):
        await svc.contracts(acc)
    assert db.get_cabinet_account(acc.id).status == CABINET_AUTH_FAILED
    acc = await svc.sign_in(42, "user@mail.com", "new")   # новий пароль повертає акаунт до роботи
    assert acc.ok and len(db.list_cabinet_accounts(42)) == 1


async def test_wrong_key_is_auth_error():
    db, site = Database(":memory:"), FakeSite()
    acc = await make_service(db, site).sign_in(42, "user@mail.com", "secret")
    db._conn.execute("UPDATE cabinet_accounts SET token_exp=0")
    other = CabinetService(db, make_client(site), SecretBox(Fernet.generate_key().decode()))
    with pytest.raises(CabinetAuthError):
        await other.contracts(acc)


# --- допоміжне ---------------------------------------------------------------

def test_matching_helpers():
    assert house_matches("12a", "12А") and house_matches("12 А", "12а") and not house_matches("12", "12А")
    assert street_matches("вул. Мирного П.", "Хмельницький, вул. Панаса Мирного, 21")
    assert not street_matches("вул. Мирного П.", "Хмельницький, вул. Проскурівська, 21")
    assert street_matches("", "будь-що")
    oms = OmsData(queues=[QueueEntry("м", "вул", "5", "1.1", "0"), QueueEntry("м", "вул", "21", "2.2", "0")])
    assert pick_queue(oms, "21").queue == "2.2" and pick_queue(oms, "99").queue == "1.1"
    assert pick_queue(OmsData(queues=[]), "1") is None
    assert mask_login("bushok@gmail.com") == "bu***@gmail.com" and mask_login("0961234567") == "096***67"


def test_migration_adds_cabinet_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL DEFAULT "
                 "'tg', target TEXT NOT NULL, street_id TEXT NOT NULL, house TEXT NOT NULL, label TEXT NOT NULL, "
                 "events INTEGER NOT NULL DEFAULT 1, graphs INTEGER NOT NULL DEFAULT 0, silent INTEGER NOT NULL "
                 "DEFAULT 1, source TEXT NOT NULL DEFAULT 'bot', owner_id INTEGER, created_at INTEGER NOT NULL, "
                 "UNIQUE (channel, target, street_id, house))")
    conn.execute("INSERT INTO subscriptions (target,street_id,house,label,created_at) VALUES ('1','10','5','Дім',0)")
    conn.commit()
    conn.close()
    db = Database(path)
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue is None and sub.cabinet_account_id is None and not sub.queue_from_cabinet


def test_config_validates_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1:x\nCABINET_SECRET_KEY=not-a-key\n")
    monkeypatch.delenv("CABINET_SECRET_KEY", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        load_settings(env)
    monkeypatch.setenv("CABINET_SECRET_KEY", KEY)
    assert load_settings(env).cabinet_enabled


# --- синхронізація в моніторі -------------------------------------------------

async def sync_env():
    db, site, sink = Database(":memory:"), FakeSite(), FakeSink()
    svc = make_service(db, site)
    mon = Monitor(db, FakeHoe(), {"tg": sink}, send_delay=0, cabinet=svc, cabinet_interval=3600)
    acc = await svc.sign_in(42, "user@mail.com", "secret")
    sub, _ = db.add_subscription("tg", "1", "10", "21", "Хмельницький, вул. Панаса Мирного, 21", owner_id=42,
                                 queue="2.2", cabinet_account_id=acc.id, cabinet_id=16568)
    return db, site, sink, mon, acc, sub


async def test_sync_applies_new_queue_and_notifies():
    db, site, sink, mon, acc, sub = await sync_env()
    report = await mon.sync_cabinet()
    assert report.ok == 1 and not report.changed and sink.texts == []

    site.queues[16568][0]["queue"] = "3.1"
    report = await mon.sync_cabinet()
    assert report.changed == [(sub.label, "2.2", "3.1")]
    updated = db.get_subscription(sub.id)
    assert updated.queue == "3.1" and updated.queue_from_cabinet       # прив'язка лишається
    target, subject, label, body = sink.texts[-1]
    assert target == "1" and subject == SUBJECT_QUEUE_CHANGED and "3.1" in body and "була 2.2" in body


async def test_sync_auth_failure_notifies_owner_once_and_pauses():
    db, site, sink, mon, acc, sub = await sync_env()
    site.valid_tokens.clear()
    site.password = "changed"
    await mon.sync_cabinet()
    await mon.sync_cabinet()                         # другий раз акаунт пропускається
    owner_msgs = [t for t in sink.texts if t[0] == "42"]
    assert len(owner_msgs) == 1 and owner_msgs[0][1] == SUBJECT_CABINET_AUTH
    assert site.attempts == 2                        # sign_in + одна невдала спроба, далі пауза
    assert db.get_subscription(sub.id).queue == "2.2"


async def test_sync_site_down_keeps_queue():
    db, site, sink, mon, acc, sub = await sync_env()
    site.valid_tokens.clear()
    site.down = True
    report = await mon.sync_cabinet()
    assert report.failed == 1 and db.get_subscription(sub.id).queue == "2.2"
    assert db.get_cabinet_account(acc.id).ok and db.get_cabinet_account(acc.id).last_error


async def test_poll_runs_cabinet_sync_on_interval():
    db, site, sink, mon, acc, sub = await sync_env()
    await mon.poll_once()
    await mon.poll_once()
    oms_calls = [r for r in site.requests if "/get-oms-data/" in r.url.path]
    assert len(oms_calls) == 1                       # другий цикл — у межах інтервалу


async def test_manual_queue_unlinks_and_account_delete():
    db, site, sink, mon, acc, sub = await sync_env()
    assert not db.set_queue(sub.id, "1.1").queue_from_cabinet
    db.link_cabinet(sub.id, acc.id, 16568, "2.2")
    assert db.delete_cabinet_account(acc.id) == 1
    left = db.get_subscription(sub.id)
    assert left.queue == "2.2" and not left.queue_from_cabinet and db.list_cabinet_accounts(42) == []


# --- бот -----------------------------------------------------------------------

@pytest.fixture
async def bot_env():
    from aiogram import Bot
    from hoe_notifier.bot import build_dispatcher
    from .test_bot import FakeSession, make_env, settings
    # беремо з make_env лише базу, підставний сайт і конструктори апдейтів; бота будуємо з кабінетом
    db, hoe, _, _, msg, cb = make_env()
    site = FakeSite()
    svc = make_service(db, site)
    mon = Monitor(db, hoe, {"tg": hoe.sink}, send_delay=0, cabinet=svc)
    session = FakeSession()
    bot = Bot("1:x", session=session)
    dp = build_dispatcher(settings(), db, hoe, mon)

    async def send2(update):
        await dp.feed_update(bot, update)

    yield db, site, session, send2, msg, cb
    db.close()


async def walk_to_house(send, msg, cb, house="21"):
    await send(msg("/add"))
    await send(msg("давид"))
    await send(cb("sp:0"))
    await send(msg("мирного"))
    await send(cb("st:0"))
    await send(msg(house))


def buttons(session):
    markup = [m for m in session.methods if type(m).__name__ == "SendMessage"][-1].reply_markup
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@pytest.fixture
def mirnogo(monkeypatch):
    """Вулиця в підставному пошуку має містити «Мирного», щоб адреса збігалася з кабінетом."""
    from hoe_notifier.hoe import Option
    from .test_bot import SearchHoe

    async def streets(self, settlement_id, q):
        return [Option("77", "вул. Панаса Мирного")]
    monkeypatch.setattr(SearchHoe, "search_streets", streets)
    monkeypatch.setattr(SearchHoe, "houses", ["21", "23"])


async def test_add_with_cabinet_links_queue(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    assert db.count_for_target("tg", "1") == 0
    assert buttons(session) == ["cq:new", "cq:manual", "cq:none"]
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    assert any(type(m).__name__ == "DeleteMessage" for m in session.methods)   # пароль прибрано з чату
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue == "2.2" and sub.queue_from_cabinet and sub.cabinet_id == 16568
    assert any("з особистого кабінету" in t for t in session.texts())
    assert all("secret" not in (t or "") for t in session.texts())


async def test_wrong_password_asks_login_again(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("bad"))
    assert "Невірний логін або пароль" in session.texts()[-1]
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    assert db.list_subscriptions("tg", "1")[0].queue == "2.2"


async def test_saved_account_offered_next_time(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    acc = db.list_cabinet_accounts(42)[0]
    await walk_to_house(send, msg, cb, house="23")
    assert buttons(session)[0] == f"cq:acc:{acc.id}"
    await send(cb(f"cq:acc:{acc.id}"))
    # будинок 23 не збігається з 21 у кабінеті → підтвердження
    assert "Адреса в кабінеті" in session.texts()[-1]
    await send(cb("cm:yes"))
    houses = {s.house: s for s in db.list_subscriptions("tg", "1")}
    assert houses["23"].queue == "2.2" and houses["23"].queue_from_cabinet


async def test_mismatch_manual_choice(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb, house="23")
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    await send(cb("cm:no"))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue is None and not sub.queue_from_cabinet
    assert any(d.startswith(f"q:{sub.id}:") for d in buttons(session))     # одразу вибір черги


async def test_multiple_contracts(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    site.contracts.append({"cabinetId": 2, "contractNumber": "999", "contractStatusName": "активний",
                           "address": "м. Хмельницький, вул. Інша 1"})
    site.queues[2] = [{"settlement": "м. Хмельницький", "street": "вул. Мирного П.", "house": "21",
                       "queue": "5.1", "queueGAV": "0"}]
    await walk_to_house(send, msg, cb)
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    assert buttons(session) == ["cc:0", "cc:1"]
    await send(cb("cc:1"))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue == "5.1" and sub.cabinet_id == 2


async def test_cabinet_down_still_adds_address(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    site.down = True
    await send(msg("secret"))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue is None and "не відповідає" in " ".join(session.texts())


async def test_no_queue_choice(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    await send(cb("cq:none"))
    assert db.list_subscriptions("tg", "1")[0].queue is None


async def test_link_existing_address_and_manual_override(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    sub, _ = db.add_subscription("tg", "1", "77", "21", "Хмельницький, вул. Панаса Мирного, 21", owner_id=42)
    await send(cb(f"sub:{sub.id}:queue"))
    edit = [m for m in session.methods if type(m).__name__ == "EditMessageReplyMarkup"][-1]
    assert f"q:{sub.id}:cab" in [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
    await send(cb(f"q:{sub.id}:cab"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    linked = db.get_subscription(sub.id)
    assert linked.queue == "2.2" and linked.queue_from_cabinet
    await send(cb(f"q:{sub.id}:1.1"))                # ручний вибір вимикає автооновлення
    manual = db.get_subscription(sub.id)
    assert manual.queue == "1.1" and not manual.queue_from_cabinet


async def test_cabinet_link_refused_in_group(bot_env):
    db, site, session, send, msg, cb = bot_env
    sub, _ = db.add_subscription("tg", "-5", "77", "21", "Адреса", owner_id=42)
    session.member_status = "creator"
    await send(cb(f"q:{sub.id}:cab", chat_type="group", chat_id=-5))
    assert not db.get_subscription(sub.id).queue_from_cabinet
    assert not any(type(m).__name__ == "SendMessage" for m in session.methods)


async def test_cabinet_menu_sync_repass_delete(bot_env, mirnogo):
    db, site, session, send, msg, cb = bot_env
    await walk_to_house(send, msg, cb)
    await send(cb("cq:new"))
    await send(msg("user@mail.com"))
    await send(msg("secret"))
    acc = db.list_cabinet_accounts(42)[0]

    await send(msg("/cabinet"))
    assert "us***@mail.com" in session.texts()[-1] and "черга 2.2" in session.texts()[-1]
    assert buttons(session) == [f"cab:sync:{acc.id}", f"cab:pass:{acc.id}", f"cab:del:{acc.id}"]

    site.queues[16568][0]["queue"] = "4.2"
    await send(cb(f"cab:sync:{acc.id}"))
    assert "2.2 → 4.2" in session.texts()[-1] and db.list_subscriptions("tg", "1")[0].queue == "4.2"

    site.password = "new"
    await send(cb(f"cab:pass:{acc.id}"))
    await send(msg("wrong"))
    assert "не підходить" in session.texts()[-1]
    await send(msg("new"))
    assert "оновлено" in session.texts()[-1]

    await send(cb(f"cab:del:{acc.id}"))
    await send(cb(f"cab:delyes:{acc.id}"))
    assert db.list_cabinet_accounts(42) == [] and not db.list_subscriptions("tg", "1")[0].queue_from_cabinet


async def test_cabinet_command_private_only_and_disabled():
    from .test_bot import make_env
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/cabinet"))
    assert "не ввімкнено" in session.texts()[-1]
    await send(msg("/add"))                          # без кабінету діалог як раніше
    db.close()


async def test_group_add_skips_cabinet_step(bot_env):
    db, site, session, send, msg, cb = bot_env
    session.member_status = "creator"
    await send(msg("/add", chat_type="group", chat_id=-7))
    await send(msg("давид", chat_type="group", chat_id=-7))
    await send(cb("sp:0", chat_type="group", chat_id=-7))
    await send(msg("проскур", chat_type="group", chat_id=-7))
    await send(cb("st:0", chat_type="group", chat_id=-7))
    await send(msg("12", chat_type="group", chat_id=-7))
    assert db.count_for_target("tg", "-7") == 1


async def test_sync_skipped_when_disabled_in_settings():
    db, site, sink, mon, acc, sub = await sync_env()
    mon.cabinet_enabled = False
    assert (await mon.sync_cabinet()).accounts == 0
    assert not [r for r in site.requests if "/get-oms-data/" in r.url.path]