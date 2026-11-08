"""Наскрізні тести бота на підставному Telegram-сеансі (без мережі)."""
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery, Chat, ChatFullInfo, ChatMemberAdministrator, ChatMemberBanned, ChatMemberMember,
    ChatMemberOwner, ChatMemberUpdated, Message, MessageEntity, Update, User,
)

from hoe_notifier.bot import build_dispatcher
from hoe_notifier.config import Settings
from hoe_notifier.db import Database
from hoe_notifier.hoe import Option, SearchUnavailable
from hoe_notifier.monitor import Monitor

from .test_monitor import TABLE_A, FakeHoe, FakeSink

USER = User(id=42, is_bot=False, first_name="Tester")


BOT_ID = 1000


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []
        self.member_status = "member"
        self.members = {}      # (chat_id, user_id) -> ChatMember
        self.chats = {}        # chat_id або "@username" -> ChatFullInfo
        self.fail_send = set()  # chat_id, куди SendMessage дає Forbidden

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="bot", username="hoebot")
        if name == "GetChat":
            if method.chat_id in self.chats:
                return self.chats[method.chat_id]
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        if name == "SendMessage" and method.chat_id in self.fail_send:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot is not a member")
        if name in {"SendMessage", "EditMessageText", "SendPhoto"}:
            return Message(message_id=len(self.methods), date=datetime.now(), chat=Chat(id=1, type="private"))
        if name == "GetChatMember":
            key = (method.chat_id, method.user_id)
            if key in self.members:
                return self.members[key]
            if self.member_status == "creator":
                return ChatMemberOwner(status="creator", user=USER, is_anonymous=False)
            return ChatMemberMember(status="member", user=USER)
        return True

    def texts(self):
        return [m.text for m in self.methods if type(m).__name__ in {"SendMessage", "EditMessageText"}]

    def sent_to(self, chat_id):
        return [m.text for m in self.methods if type(m).__name__ == "SendMessage" and m.chat_id == chat_id]


class SearchHoe(FakeHoe):
    broken = False
    houses = ["1", "12", "12А", "12/1", "5Б"]
    houses_broken = False

    async def search_settlements(self, q):
        if self.broken:
            raise SearchUnavailable("x")
        return [Option("11", "Давидківці"), Option("12", "Давидківці (Хмельницький р-н)")]

    async def search_streets(self, settlement_id, q):
        if self.broken:
            raise SearchUnavailable("x")
        assert settlement_id in {"11", "12"}
        return [Option("77", "вул. Проскурівська"), Option("78", "вул. Проскурівського Підпілля")]

    async def get_houses(self, street_id):
        if self.houses_broken:
            raise SearchUnavailable("x")
        return list(self.houses)


def settings(**over):
    base = dict(
        bot_token="1:x", admin_ids=frozenset({42}), allowed_chat_ids=frozenset(), slack_token=None,
        poll_interval=900, max_addresses_per_chat=2, db_path=":memory:", addresses_file=None,
        hoe_base_url="", request_timeout=1, retries=1, retry_delay=0,
        concurrency=1, default_silent=True, default_graphs=False, log_level="INFO",
    )
    base.update(over)
    return Settings(**base)


def make_env(**settings_over):
    db, hoe, sink = Database(":memory:"), SearchHoe(), FakeSink()
    mon = Monitor(db, hoe, {"tg": sink}, send_delay=0)
    session = FakeSession()
    bot = Bot("1:x", session=session)
    dp = build_dispatcher(settings(**settings_over), db, hoe, mon)
    n = {"id": 0}

    def msg(text, chat_type="private", chat_id=1):
        n["id"] += 1
        ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
        m = Message(message_id=n["id"], date=datetime.now(), chat=Chat(id=chat_id, type=chat_type),
                    from_user=USER, text=text, entities=ents)
        return Update(update_id=n["id"], message=m)

    def cb(data, chat_type="private", chat_id=1):
        n["id"] += 1
        m = Message(message_id=999, date=datetime.now(), chat=Chat(id=chat_id, type=chat_type))
        return Update(update_id=n["id"], callback_query=CallbackQuery(
            id=str(n["id"]), from_user=USER, chat_instance="c", message=m, data=data))

    async def send(update):
        await dp.feed_update(bot, update)

    hoe.sink, hoe.monitor = sink, mon
    return db, hoe, session, send, msg, cb


@pytest.fixture
async def env():
    e = make_env()
    yield e
    e[0].close()


async def test_add_id_command(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add_id 10 5 Дім"))
    subs = db.list_subscriptions("tg", "1")
    assert [(s.street_id, s.house, s.label) for s in subs] == [("10", "5", "Дім")]
    assert any("Додано" in t and "Черга: 1.1" in t for t in session.texts())


async def test_add_id_bad_usage(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add_id 10"))
    assert db.count_for_target("tg", "1") == 0
    assert "Формат" in session.texts()[-1]


async def walk_dialog(send, msg, cb, house="12А", first="/add"):
    await send(msg(first))
    await send(msg("давид"))
    await send(cb("sp:0"))
    await send(msg("проскур"))
    await send(cb("st:1"))
    await send(msg(house))


async def test_full_add_dialog(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb)
    sub = db.list_subscriptions("tg", "1")[0]
    assert (sub.street_id, sub.house) == ("78", "12А")
    assert sub.label == "Давидківці, вул. Проскурівського Підпілля, 12А"


async def test_house_latin_letter_normalised_to_site_spelling(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb, house="12a")   # латинська a
    assert db.list_subscriptions("tg", "1")[0].house == "12А"   # кирилична, як на сайті


async def test_house_with_inner_space(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb, house="12 а")
    assert db.list_subscriptions("tg", "1")[0].house == "12А"


async def test_house_too_long_rejected(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb, house="1234567890123")
    assert "Некоректний" in session.texts()[-1] and db.stats()["subscriptions"] == 0


async def test_unknown_house_offers_suggestions_and_force(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb, house="12В")
    assert db.count_for_target("tg", "1") == 0
    assert "немає у списку сайту" in session.texts()[-1]
    markup = [m for m in session.methods if type(m).__name__ == "SendMessage"][-1].reply_markup
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert "12А" in buttons and any("Все одно" in b for b in buttons)

    await send(cb("hs:0"))                      # обрав підказку
    assert db.list_subscriptions("tg", "1")[0].house == "12"   # перша за порядком підказка


async def test_unknown_house_force_add(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb, house="77")
    assert "Схожих не знайшов" in session.texts()[-1]
    await send(cb("hs:force"))
    assert db.list_subscriptions("tg", "1")[0].house == "77"


async def test_house_list_unavailable_accepts_typed_value(env):
    db, hoe, session, send, msg, cb = env
    hoe.houses_broken = True
    await walk_dialog(send, msg, cb, house="99")
    assert db.list_subscriptions("tg", "1")[0].house == "99"


async def test_house_hint_shows_short_list(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add"))
    await send(msg("давид"))
    await send(cb("sp:0"))
    await send(msg("проскур"))
    await send(cb("st:0"))
    assert "Є такі будинки: 1, 12, 12А, 12/1, 5Б" in session.texts()[-1]


async def test_last_settlement_shortcut(env):
    db, hoe, session, send, msg, cb = env
    await walk_dialog(send, msg, cb)             # перший раз — пошук, місто запам'ятовується
    session.methods.clear()
    await send(msg("/add"))
    markup = [m for m in session.methods if type(m).__name__ == "SendMessage"][-1].reply_markup
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["sp:last"]
    await send(cb("sp:last"))
    assert "Давидківці" in session.texts()[-1] and "назву вулиці" in session.texts()[-1]
    await send(msg("проскур"))
    await send(cb("st:0"))
    await send(msg("5б"))
    assert sorted(s.house for s in db.list_subscriptions("tg", "1")) == ["12А", "5Б"]


async def test_search_unavailable_ends_dialog_with_hint(env):
    db, hoe, session, send, msg, cb = env
    hoe.broken = True
    await send(msg("/add"))
    await send(msg("давид"))
    assert "недоступний" in session.texts()[-1] and "add_id" not in session.texts()[-1]
    await send(msg("ще раз"))                    # діалог завершено — текст ігнорується
    assert db.stats()["subscriptions"] == 0


async def test_settlement_not_found_keeps_asking(env):
    db, hoe, session, send, msg, cb = env

    async def empty(q):
        return []
    hoe.search_settlements = empty
    await send(msg("/add"))
    await send(msg("нема такого"))
    assert "не знайдено" in session.texts()[-1]
    await send(msg("ще"))
    assert "не знайдено" in session.texts()[-1]


async def test_stale_buttons_do_not_crash(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add"))
    await send(cb("sp:5"))                       # такого пункту в списку немає
    assert db.stats()["subscriptions"] == 0


async def test_limit_per_chat(env):
    db, hoe, session, send, msg, cb = env
    for i in range(3):
        await send(msg(f"/add_id {i} 1"))
    assert db.count_for_target("tg", "1") == 2
    assert "Ліміт" in session.texts()[-1]


async def test_duplicate_address(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add_id 10 5"))
    await send(msg("/add_id 10 5"))
    assert db.count_for_target("tg", "1") == 1
    assert "вже є" in session.texts()[-1]


async def test_list_toggle_and_delete(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add_id 10 5 Дім"))
    sub = db.list_subscriptions("tg", "1")[0]
    await send(msg("/list"))
    await send(cb(f"sub:{sub.id}:graphs"))
    assert db.get_subscription(sub.id).graphs is True
    await send(cb(f"sub:{sub.id}:silent"))
    assert db.get_subscription(sub.id).silent is False
    await send(cb(f"sub:{sub.id}:del"))
    assert db.list_subscriptions("tg", "1") == []


async def test_cannot_touch_other_chats_subscription(env):
    db, hoe, session, send, msg, cb = env
    sub, _ = db.add_subscription("tg", "555", "10", "5", "Чужа")
    await send(cb(f"sub:{sub.id}:del", chat_id=1))
    assert db.get_subscription(sub.id) is not None


async def test_config_entries_are_read_only(env):
    db, hoe, session, send, msg, cb = env
    db.sync_config_subscriptions([{"channel": "tg", "target": "1", "street_id": "1", "house": "1",
                                   "label": "cfg", "events": True, "graphs": False, "silent": True}])
    sub = db.list_subscriptions("tg", "1")[0]
    await send(cb(f"sub:{sub.id}:del"))
    assert db.get_subscription(sub.id) is not None


async def test_group_requires_admin():
    db, hoe, sink = Database(":memory:"), SearchHoe(), FakeSink()
    mon = Monitor(db, hoe, {"tg": sink}, send_delay=0)
    session = FakeSession()
    bot = Bot("1:x", session=session)
    # користувач 42 — адмін бота; для перевірки групових прав беремо іншого користувача
    dp = build_dispatcher(settings(admin_ids=frozenset()), db, hoe, mon)

    def group_cmd(text):
        m = Message(message_id=1, date=datetime.now(), chat=Chat(id=-100, type="supergroup"), from_user=USER,
                    text=text, entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))])
        return Update(update_id=1, message=m)

    await dp.feed_update(bot, group_cmd("/add_id 10 5"))
    assert db.count_for_target("tg", "-100") == 0
    assert "адміністратори" in session.texts()[-1]

    session.member_status = "creator"
    await dp.feed_update(bot, group_cmd("/add_id 10 5"))
    assert db.count_for_target("tg", "-100") == 1


async def test_private_bot_whitelist():
    db, hoe, sink = Database(":memory:"), SearchHoe(), FakeSink()
    mon = Monitor(db, hoe, {"tg": sink}, send_delay=0)
    session = FakeSession()
    bot = Bot("1:x", session=session)
    dp = build_dispatcher(settings(admin_ids=frozenset(), allowed_chat_ids=frozenset({999})), db, hoe, mon)
    m = Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"), from_user=USER,
                text="/add_id 10 5", entities=[MessageEntity(type="bot_command", offset=0, length=7)])
    await dp.feed_update(bot, Update(update_id=1, message=m))
    assert db.count_for_target("tg", "1") == 0
    assert "приватний" in session.texts()[-1]


# ---------------------------------------------------------------- канали
CH = -1001234567890


def channel(title="Світло", username=None):
    return ChatFullInfo.model_construct(id=CH, type="channel", title=title, username=username)


def bot_admin(can_post=True):
    return ChatMemberAdministrator.model_construct(status="administrator", user=USER, can_post_messages=can_post)


def user_admin():
    return ChatMemberAdministrator.model_construct(status="administrator", user=USER, can_post_messages=True)


@pytest.fixture
def ch():
    """Бот — адмін каналу з правом публікації, користувач 42 — адмін каналу, але не адмін бота."""
    e = make_env(admin_ids=frozenset())
    db, hoe, session, send, msg, cb = e
    session.chats[CH] = channel()
    session.chats["@svitlo_hoe"] = channel(username="svitlo_hoe")
    session.members[(CH, BOT_ID)] = bot_admin()
    session.members[(CH, 42)] = user_admin()
    yield e
    db.close()


async def test_add_id_to_channel(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 Дім --to={CH}"))
    sub = db.list_subscriptions("tg", str(CH))[0]
    assert (sub.street_id, sub.house, sub.label, sub.owner_id) == ("10", "5", "Дім", 42)
    assert db.list_subscriptions("tg", "1") == []              # у приватний чат нічого не додано
    assert any("Черга: 1.1" in t for t in session.sent_to(CH))  # у канал пішов поточний стан
    assert "Додано в «Світло»" in session.texts()[-1]


async def test_to_by_username_and_em_dash(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg("/add_id 10 5 Дім —to=@svitlo_hoe"))   # iOS підміняє «--» на «—»
    assert db.count_for_target("tg", str(CH)) == 1


async def test_to_with_space_and_name_with_spaces(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 Мій новий дім --to {CH}"))
    assert db.list_subscriptions("tg", str(CH))[0].label == "Мій новий дім"


async def test_channel_bot_not_admin(ch):
    db, hoe, session, send, msg, cb = ch
    session.members[(CH, BOT_ID)] = bot_admin(can_post=False)
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert db.count_for_target("tg", str(CH)) == 0
    assert "Публікація повідомлень" in session.texts()[-1]


async def test_channel_bot_not_member(ch):
    db, hoe, session, send, msg, cb = ch
    session.members[(CH, BOT_ID)] = ChatMemberMember.model_construct(status="left", user=USER)
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert db.count_for_target("tg", str(CH)) == 0
    assert "не є учасником" in session.texts()[-1]


async def test_user_must_be_channel_admin(ch):
    db, hoe, session, send, msg, cb = ch
    session.members[(CH, 42)] = ChatMemberMember(status="member", user=USER)
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert db.count_for_target("tg", str(CH)) == 0
    assert "лише його адміністратори" in session.texts()[-1]
    assert session.sent_to(CH) == []   # у чужий канал нічого не відправлено


async def test_bot_admin_may_add_without_channel_rights():
    db, hoe, session, send, msg, cb = make_env(admin_ids=frozenset({42}))
    session.chats[CH] = channel()
    session.members[(CH, BOT_ID)] = bot_admin()
    session.members[(CH, 42)] = ChatMemberMember(status="member", user=USER)
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert db.count_for_target("tg", str(CH)) == 1
    db.close()


async def test_unknown_or_malformed_target(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg("/add_id 10 5 --to=-100999"))
    assert "Не знайшов" in session.texts()[-1]
    await send(msg("/add_id 10 5 --to=канал"))
    assert "числовий ID" in session.texts()[-1]
    assert db.stats()["subscriptions"] == 0


async def test_private_chat_as_target_rejected(ch):
    db, hoe, session, send, msg, cb = ch
    session.chats[777] = ChatFullInfo.model_construct(id=777, type="private")
    await send(msg("/add_id 10 5 --to=777"))
    assert "особистий чат" in session.texts()[-1]


async def test_send_failure_does_not_subscribe(ch):
    db, hoe, session, send, msg, cb = ch
    session.fail_send.add(CH)
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert db.count_for_target("tg", str(CH)) == 0
    assert "Адресу не додано" in session.texts()[-1]


async def test_channel_duplicate_and_limit(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 --to={CH}"))
    await send(msg(f"/add_id 10 5 --to={CH}"))
    assert "вже є" in session.texts()[-1] and db.count_for_target("tg", str(CH)) == 1
    await send(msg(f"/add_id 11 5 --to={CH}"))
    await send(msg(f"/add_id 12 5 --to={CH}"))     # ліміт у тестових налаштуваннях = 2
    assert db.count_for_target("tg", str(CH)) == 2
    assert "Ліміт" in session.texts()[-1]


async def test_same_address_in_own_chat_and_channel(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg("/add_id 10 5 Дім"))
    await send(msg(f"/add_id 10 5 Дім --to={CH}"))
    assert db.count_for_target("tg", "1") == 1 and db.count_for_target("tg", str(CH)) == 1


async def test_dialog_add_to_channel(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add --to={CH}"))
    assert "Світло" in session.texts()[-1]
    await send(msg("давид"))
    await send(cb("sp:0"))
    await send(msg("проскур"))
    await send(cb("st:0"))
    await send(msg("12"))
    assert [s.street_id for s in db.list_subscriptions("tg", str(CH))] == ["77"]
    assert db.list_subscriptions("tg", "1") == []


async def test_dialog_with_bad_target_does_not_start(ch):
    db, hoe, session, send, msg, cb = ch
    session.members[(CH, BOT_ID)] = bot_admin(can_post=False)
    await send(msg(f"/add --to={CH}"))
    await send(msg("давид"))        # без активного діалогу текст ігнорується
    assert "Публікація повідомлень" in session.texts()[-1]
    assert db.stats()["subscriptions"] == 0


async def test_list_shows_channel_subscriptions_and_manages_them(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 Дім --to={CH}"))
    sub = db.list_subscriptions("tg", str(CH))[0]
    session.methods.clear()
    await send(msg("/list"))
    assert any("Світло" in (t or "") and "Дім" in t for t in session.texts())

    await send(cb(f"sub:{sub.id}:graphs"))                  # ключі з приватного чату
    assert db.get_subscription(sub.id).graphs is True
    await send(cb(f"sub:{sub.id}:del"))
    assert db.get_subscription(sub.id) is None


async def test_channel_subscription_not_manageable_after_losing_admin_rights(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 Дім --to={CH}"))
    sub = db.list_subscriptions("tg", str(CH))[0]
    session.members[(CH, 42)] = ChatMemberMember(status="member", user=USER)  # знято з адмінів
    await send(cb(f"sub:{sub.id}:del"))
    assert db.get_subscription(sub.id) is not None


async def test_other_user_cannot_manage_someone_elses_channel_sub(ch):
    db, hoe, session, send, msg, cb = ch
    sub, _ = db.add_subscription("tg", str(CH), "10", "5", "Чужа", owner_id=999)
    await send(cb(f"sub:{sub.id}:del"))
    assert db.get_subscription(sub.id) is not None


async def test_list_not_leaking_owned_subs_into_group_chats(ch):
    db, hoe, session, send, msg, cb = ch
    await send(msg(f"/add_id 10 5 Дім --to={CH}"))
    session.methods.clear()
    session.member_status = "creator"
    await send(msg("/list", chat_type="supergroup", chat_id=-555))
    assert session.texts() == ["Адрес ще немає. Додай через /add."]


# ---------------------------------------------------------------- захист і прибирання
async def test_check_command_does_not_hammer_the_site(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add_id 10 5 Дім"))
    before = hoe.calls
    for _ in range(5):
        await send(msg("/check"))
    assert hoe.calls == before                      # відповіді з кешу (<60 с)
    assert sum("Черга: 1.1" in (t or "") for t in session.texts()) >= 5


def kicked_update(chat_id, chat_type="private", status="kicked"):
    bot_user = User(id=BOT_ID, is_bot=True, first_name="bot")
    new = (ChatMemberBanned(status="kicked", user=bot_user, until_date=0) if status == "kicked"
           else ChatMemberMember(status="member", user=bot_user))
    return Update(update_id=1, my_chat_member=ChatMemberUpdated(
        chat=Chat(id=chat_id, type=chat_type), from_user=USER, date=datetime.now(),
        old_chat_member=ChatMemberMember(status="member", user=bot_user), new_chat_member=new))


async def test_subscriptions_removed_when_bot_kicked(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "-100", "10", "5", "A")
    db.add_subscription("tg", "-200", "10", "5", "B")
    db.sync_config_subscriptions([{"channel": "tg", "target": "-100", "street_id": "1", "house": "1",
                                   "label": "cfg", "events": True, "graphs": False, "silent": True}])
    await send(kicked_update(-100, "supergroup"))
    assert [s.label for s in db.list_subscriptions("tg", "-100")] == ["cfg"]   # запис із файлу лишається
    assert db.count_for_target("tg", "-200") == 1                              # інші чати не зачеплено


async def test_member_status_change_keeps_subscriptions(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "-100", "10", "5", "A")
    await send(kicked_update(-100, "supergroup", status="member"))
    assert db.count_for_target("tg", "-100") == 1


# ---------------------------------------------------------------- графік (текст / картинка) та адмін-панель
SCHEDULE = "За розпорядженням 07 листопада:\n\nПідчерга 1.1 – 00:00-04:00; 09:00-13:00."


def last_send_message(session):
    return [m for m in session.methods if type(m).__name__ == "SendMessage"][-1]


def alerts(session):
    return [m for m in session.methods if type(m).__name__ == "AnswerCallbackQuery" and m.show_alert]


def button_texts(method):
    return [b.text for row in method.reply_markup.inline_keyboard for b in row]


async def test_graph_command_shows_text_schedule(env):
    from hoe_notifier.hoe import Graphs
    db, hoe, session, send, msg, cb = env
    hoe.graph_text = SCHEDULE
    await send(msg("/graph"))
    out = last_send_message(session).text
    assert out.startswith("<b>Графік погодинних відключень</b>\n\n") and "Підчерга 1.1" in out
    assert "<pre>" not in out                       # звичайним текстом, не блоком коду


async def test_graph_command_shows_image(env):
    db, hoe, session, send, msg, cb = env
    hoe.graph_url = "https://x/1.png"
    await send(msg("/graph"))
    assert any(type(m).__name__ == "SendPhoto" for m in session.methods)


async def test_graph_command_shows_both(env):
    db, hoe, session, send, msg, cb = env
    hoe.graph_url, hoe.graph_text = "https://x/1.png", SCHEDULE
    await send(msg("/graph"))
    names = [type(m).__name__ for m in session.methods]
    assert "SendPhoto" in names and any("Підчерга" in (t or "") for t in session.texts())


async def test_graph_command_nothing_published_and_page_down(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/graph"))
    assert "немає графіка" in session.texts()[-1]
    hoe.page_down = True
    await send(msg("/graph"))
    assert "Не вдалося завантажити сторінку" in session.texts()[-1]


async def test_admin_panel_has_buttons(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/admin"))
    labels = button_texts(last_send_message(session))
    for expected in ("📊 Статистика", "🔄 Опитати зараз", "📋 Показати текстовий графік",
                     "🖼 Показати картинку-графік", "📤 Розіслати текстовий графік", "📤 Розіслати картинку-графік"):
        assert expected in labels


async def test_admin_panel_hidden_from_non_admins_and_groups():
    db, hoe, session, send, msg, cb = make_env(admin_ids=frozenset())
    await send(msg("/admin"))
    assert session.methods == []                    # мовчки ігнорує
    db.close()
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/admin", chat_type="supergroup", chat_id=-5))
    assert session.methods == []
    db.close()


async def test_admin_buttons_rejected_for_non_admins():
    db, hoe, session, send, msg, cb = make_env(admin_ids=frozenset())
    hoe.graph_text = SCHEDULE
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    await send(cb("adm:send_text:yes"))
    assert alerts(session) and hoe.sink.texts == []
    db.close()


async def test_admin_show_text_goes_only_to_admin(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await send(cb("adm:show_text"))
    assert any("Підчерга 1.1" in (t or "") for t in session.texts())
    assert hoe.sink.texts == []                      # підписникам нічого не пішло


async def test_admin_show_when_missing(env):
    db, hoe, session, send, msg, cb = env
    await send(cb("adm:show_text"))
    assert "немає текстового графіка" in session.texts()[-1]
    await send(cb("adm:show_img"))
    assert "немає картинки-графіка" in session.texts()[-1]


async def test_admin_send_text_requires_confirmation_then_broadcasts(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "8", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "9", "10", "5", "A", graphs=False)    # не підписаний на графік
    hoe.graph_text = SCHEDULE

    await send(cb("adm:send_text"))
    prompt = last_send_message(session)
    assert "<b>2</b> чатам" in prompt.text and "Підчерга 1.1" in prompt.text     # кількість і попередній перегляд
    assert hoe.sink.texts == []                                                  # ще нічого не розіслано
    assert "✅ Так, розіслати" in button_texts(prompt)

    await send(cb("adm:send_text:yes"))
    sent = [t for t in hoe.sink.texts if t[1] == "Графік погодинних відключень"]
    assert sorted(t[0] for t in sent) == ["7", "8"]
    assert session.texts()[-1] == "✅ Надіслано 2 з 2 чатів."


async def test_admin_send_text_cancel(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await send(cb("adm:send_text"))
    await send(cb("adm:send_text:no"))
    assert session.texts()[-1] == "Розсилку скасовано." and hoe.sink.texts == []


async def test_admin_send_text_unavailable_shows_alert(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    await send(cb("adm:send_text"))                  # на сайті немає тексту
    assert alerts(session) and "немає" in alerts(session)[-1].text
    hoe.page_down = True
    await send(cb("adm:send_text"))
    assert "не вдалося завантажити" in alerts(session)[-1].text


async def test_admin_send_image(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    hoe.graph_url = "https://x/1.png"
    await send(cb("adm:send_img"))
    assert "картинку-графік" in last_send_message(session).text
    await send(cb("adm:send_img:yes"))
    assert hoe.sink.photos == ["7"] and session.texts()[-1] == "✅ Надіслано 1 з 1 чатів."


async def test_admin_manual_send_does_not_cause_auto_duplicate(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await send(cb("adm:send_text"))
    await send(cb("adm:send_text:yes"))
    await hoe.monitor.poll_once()
    assert len([t for t in hoe.sink.texts if t[1] == "Графік погодинних відключень"]) == 1


async def test_admin_stats_and_poll_buttons(env):
    db, hoe, session, send, msg, cb = env
    db.add_subscription("tg", "7", "10", "5", "A", graphs=True)
    await send(cb("adm:stats"))
    assert "Чатів із графіком: 1" in session.texts()[-1]
    await send(cb("adm:poll"))
    assert "Готово: 1/1 адрес оновлено" in session.texts()[-1]
    assert hoe.calls == 1


async def test_stats_command_still_works(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/stats"))
    assert "Унікальних адрес" in session.texts()[-1]


async def test_add_with_settlement_argument_skips_first_question(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/add давид --queue=2.1"))
    assert "Обери населений пункт" in session.texts()[-1]       # одразу результати пошуку
    await send(cb("sp:0"))
    await send(msg("проскур"))
    await send(cb("st:0"))
    await send(msg("12А"))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue == "2.1" and sub.street_id == "77" and sub.house == "12А"


async def test_help_does_not_require_street_id(env):
    db, hoe, session, send, msg, cb = env
    await send(msg("/help"))
    text = session.texts()[-1]
    assert "add_id" not in text and "streetId" not in text and "/add" in text