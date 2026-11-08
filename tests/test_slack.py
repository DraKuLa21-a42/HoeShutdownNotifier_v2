"""Slack: кожен користувач підключає власного Slack-бота через /slack і додає адреси з --slack=…"""
import pytest

from hoe_notifier.db import Database
from hoe_notifier.monitor import Monitor
from hoe_notifier.sinks import ChannelError, SlackSink, TargetGone

from .test_bot import alerts, button_texts, last_send_message, make_env
from .test_monitor import FakeHoe, FakeSink

TOKEN = "xoxb-1111111111-2222222222-AbCdEfGhIjKl"
TOKEN2 = "xoxb-3333333333-4444444444-ZzZzZzZzZzZz"


class FakeSlack(FakeSink):
    channels = {"C0123ABCD": "#svitlo", "C0999ZZZZ": "#other"}

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.not_in = set()
        self.bad_tokens = set()

    async def inspect(self, token):
        if token in self.bad_tokens:
            raise ChannelError("Slack не прийняв цей токен")
        return ("Acme", "hoebot") if token == TOKEN else ("Beta", "otherbot")

    async def resolve(self, bot_id, ref):
        ref = ref.strip()
        if ref in self.channels:
            return ref, self.channels[ref]
        for cid, name in self.channels.items():
            if name == "#" + ref.lstrip("#").lower():
                return cid, name
        raise ChannelError("Не знайшов такий канал у Slack.")

    async def send_text(self, target, subject, label, body, *, silent, pre=True):
        if target.split("/")[-1] in self.not_in:
            raise TargetGone("not_in_channel")
        await super().send_text(target, subject, label, body, silent=silent, pre=pre)


def slack_env(**over):
    db, hoe, session, send, msg, cb = make_env(**over)
    slack = FakeSlack(db)
    hoe.monitor.sinks["slack"] = slack
    return db, hoe, session, send, msg, cb, slack


def deleted(session):
    return [m for m in session.methods if type(m).__name__ == "DeleteMessage"]


async def connect(send, msg, token=TOKEN):
    await send(msg(f"/slack {token}"))


# --- підключення бота -------------------------------------------------------------------

async def test_connect_bot_with_inline_token_deletes_message_and_hides_token():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    bots = db.list_slack_bots(42)
    assert len(bots) == 1 and bots[0].token == TOKEN and bots[0].team == "Acme" and bots[0].bot_name == "hoebot"
    assert deleted(session)                                           # повідомлення з токеном видалено
    assert all(TOKEN not in (t or "") for t in session.texts())       # і не повторюється у відповідях
    assert "Підключено" in session.texts()[-1] and "/invite @hoebot" in session.texts()[-1]


async def test_connect_via_dialog():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await send(msg("/slack"))
    assert "Slack-ботів ще немає" in session.texts()[-1]
    await send(cb("slk:add"))
    assert "Bot User OAuth Token" in session.texts()[-1]
    await send(msg("привіт"))                                         # не токен — діалог триває
    assert "не схоже" in session.texts()[-1] and not db.list_slack_bots(42)
    await send(msg(TOKEN))
    assert db.list_slack_bots(42) and deleted(session)


async def test_invalid_token_rejected_and_dialog_continues():
    db, hoe, session, send, msg, cb, slack = slack_env()
    slack.bad_tokens.add(TOKEN)
    await send(msg("/slack"))
    await send(cb("slk:add"))
    await send(msg(TOKEN))
    assert "не прийняв" in session.texts()[-1] and not db.list_slack_bots(42)
    await send(msg(TOKEN2))
    assert db.list_slack_bots(42)[0].team == "Beta"


async def test_connect_only_in_private_and_limit_and_duplicate():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await send(msg(f"/slack {TOKEN}", chat_type="supergroup", chat_id=-100))
    assert "приватному чаті" in session.texts()[-1] and not db.list_slack_bots(42)
    await send(msg("/slack xoxb-bad"))
    assert "не схоже" in session.texts()[-1]
    await connect(send, msg)
    await connect(send, msg)
    assert "уже підключено" in session.texts()[-1] and len(db.list_slack_bots(42)) == 1
    for i in range(2):
        db.add_slack_bot(42, f"xoxb-{i}-{i}-aaaaaaaaaaaa", "T", "b")
    await connect(send, msg, TOKEN2)
    assert "Максимум" in session.texts()[-1] and len(db.list_slack_bots(42)) == 3


async def test_bots_are_private_to_their_owner():
    db, hoe, session, send, msg, cb, slack = slack_env()
    bot, _ = db.add_slack_bot(99, TOKEN, "Acme", "hoebot")             # бот іншого користувача
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    assert "Спершу підключи" in session.texts()[-1]
    await send(msg(f"/add_id 10 5 Дім --slack={bot.id}:C0123ABCD"))
    assert db.stats()["subscriptions"] == 0
    await send(cb(f"slk:delyes:{bot.id}"))                              # чужого бота видалити не можна
    assert db.get_slack_bot(bot.id) is not None


# --- адреси ---------------------------------------------------------------------------

async def test_add_id_with_single_bot_by_id_and_name():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    bot = db.list_slack_bots(42)[0]
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD --queue=1.1"))
    target = f"{bot.id}/C0123ABCD"
    sub = db.list_subscriptions("slack", target)[0]
    assert sub.queue == "1.1" and sub.label == "Дім" and sub.owner_id == 42
    assert slack.texts and slack.texts[0][0] == target and "черга 1.1" in slack.texts[0][2]
    assert "Acme #svitlo" in session.texts()[-1]
    assert db.list_subscriptions("tg", "1") == []
    await send(msg("/add_id 11 6 Офіс --slack=#svitlo"))
    assert db.find_subscription("slack", target, "11", "6")


async def test_several_bots_require_number():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    await connect(send, msg, TOKEN2)
    first, second = db.list_slack_bots(42)
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    assert "кілька Slack-ботів" in session.texts()[-1] and db.stats()["subscriptions"] == 0
    await send(msg(f"/add_id 10 5 Дім --slack={second.id}:C0123ABCD"))
    assert db.find_subscription("slack", f"{second.id}/C0123ABCD", "10", "5")
    assert db.find_subscription("slack", f"{first.id}/C0123ABCD", "10", "5") is None


async def test_dash_variant_and_both_targets_rejected():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    bot = db.list_slack_bots(42)[0]
    await send(msg("/add_id 10 5 Дім —slack C0123ABCD"))               # тире з мобільної клавіатури
    assert db.find_subscription("slack", f"{bot.id}/C0123ABCD", "10", "5")
    await send(msg("/add_id 11 6 Х --slack=C0123ABCD --to=@chan"))
    assert "не обидва" in session.texts()[-1]


async def test_unknown_channel_and_not_invited():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    await send(msg("/add_id 10 5 Дім --slack=#nope"))
    assert "Не знайшов" in session.texts()[-1] and db.stats()["subscriptions"] == 0
    slack.not_in.add("C0123ABCD")
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    assert "/invite" in session.texts()[-1] and db.stats()["subscriptions"] == 0


async def test_slack_limit_and_duplicate():
    db, hoe, session, send, msg, cb, slack = slack_env(max_addresses_per_chat=2)
    await connect(send, msg)
    await send(msg("/add_id 10 5 А --slack=C0123ABCD"))
    await send(msg("/add_id 10 5 А --slack=C0123ABCD"))
    assert "вже є" in session.texts()[-1]
    await send(msg("/add_id 12 7 В --slack=C0123ABCD"))
    await send(msg("/add_id 11 6 Б --slack=C0123ABCD"))
    assert "Ліміт" in session.texts()[-1]
    await send(msg("/add_id 11 6 Б --slack=C0999ZZZZ"))                # інший канал має власний ліміт
    assert db.stats()["subscriptions"] == 3


async def test_dialog_add_to_slack():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    await send(msg("/add --slack=#svitlo"))
    assert "Acme #svitlo" in session.texts()[-1]
    await send(msg("давид"))
    await send(cb("sp:0"))
    await send(msg("проскур"))
    await send(cb("st:0"))
    await send(msg("12А"))
    assert db.stats()["subscriptions"] == 1 and db.all_subscriptions()[0].channel == "slack"


# --- керування ------------------------------------------------------------------------

async def test_owner_manages_slack_subscription_any_user_not_only_admin():
    db, hoe, session, send, msg, cb, slack = slack_env(admin_ids=frozenset({7}))   # 42 — не адмін
    await connect(send, msg)
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    sub = db.all_subscriptions()[0]
    await send(msg("/list"))
    assert "Slack: Acme #svitlo" in session.texts()[-1]
    await send(cb(f"q:{sub.id}:2.1"))
    assert db.get_subscription(sub.id).queue == "2.1"
    await send(cb(f"sub:{sub.id}:graphs"))
    assert db.get_subscription(sub.id).graphs is True
    await send(cb(f"sub:{sub.id}:del"))
    assert db.get_subscription(sub.id) is None


async def test_slack_subscription_not_manageable_from_group_or_by_stranger():
    db, hoe, session, send, msg, cb, slack = slack_env(admin_ids=frozenset({7}))
    bot, _ = db.add_slack_bot(42, TOKEN, "Acme", "hoebot")
    mine, _ = db.add_subscription("slack", f"{bot.id}/C0123ABCD", "10", "5", "Дім", owner_id=42)
    await send(cb(f"sub:{mine.id}:del", chat_type="supergroup", chat_id=-100))
    assert db.get_subscription(mine.id) is not None and alerts(session)
    foreign, _ = db.add_subscription("slack", f"{bot.id}/C0123ABCD", "11", "6", "Чужа", owner_id=99)
    await send(cb(f"sub:{foreign.id}:del"))
    assert db.get_subscription(foreign.id) is not None


async def test_delete_bot_removes_its_subscriptions_with_confirmation():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    bot = db.list_slack_bots(42)[0]
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    await send(msg("/slack"))
    assert f"№{bot.id}" in session.texts()[-1] and "підписок: 1" in session.texts()[-1]
    assert TOKEN not in session.texts()[-1] and "xoxb-…" in session.texts()[-1]
    await send(cb(f"slk:del:{bot.id}"))
    assert db.get_slack_bot(bot.id) is not None                         # ще лише питання
    await send(cb("slk:no"))
    assert db.get_slack_bot(bot.id) is not None
    await send(cb(f"slk:delyes:{bot.id}"))
    assert db.get_slack_bot(bot.id) is None and db.stats()["subscriptions"] == 0


async def test_slack_updates_delivered_to_the_users_bot_target():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await connect(send, msg)
    bot = db.list_slack_bots(42)[0]
    await send(msg("/add_id 10 5 Дім --slack=C0123ABCD"))
    slack.texts.clear()
    hoe.html = "<table><thead><tr><th>Черга (ГПВ)</th></tr></thead><tbody><tr><td>3.3</td></tr></tbody></table>"
    await hoe.monitor.poll_once()
    assert slack.texts and slack.texts[-1][0] == f"{bot.id}/C0123ABCD"


async def test_help_mentions_slack_and_subs_shows_slack():
    db, hoe, session, send, msg, cb, slack = slack_env()
    await send(msg("/help"))
    assert "/slack" in session.texts()[-1] and "--slack" in session.texts()[-1]
    db.add_subscription("slack", "3/C0123ABCD", "10", "5", "Дім")
    await send(msg("/subs"))
    assert "slack:3/C0123ABCD" in session.texts()[-1]


def test_db_slack_bot_cascade():
    db = Database(":memory:")
    a, _ = db.add_slack_bot(1, TOKEN, "Acme", "b")
    b, _ = db.add_slack_bot(1, TOKEN2, "Beta", "b")
    db.add_subscription("slack", f"{a.id}/C1", "1", "1", "A")
    db.add_subscription("slack", f"{b.id}/C1", "1", "1", "B")
    db.add_subscription("slack", f"{a.id}0/C1", "1", "1", "C")          # id 10 ≠ id 1
    assert db.delete_slack_bot(a.id) == 1
    assert [s.label for s in db.all_subscriptions()] == ["B", "C"] or {s.label for s in db.all_subscriptions()} == {"B", "C"}
    assert db.get_slack_bot(b.id) is not None


# --- SlackSink.resolve (підставний клієнт) -------------------------------------------

class FakeClient:
    def __init__(self, channels, scopes=True):
        self.channels, self.scopes = channels, scopes

    def _err(self, code):
        from slack_sdk.errors import SlackApiError
        return SlackApiError("x", {"ok": False, "error": code})

    def conversations_info(self, channel):
        if not self.scopes:
            raise self._err("missing_scope")
        for c in self.channels:
            if c["id"] == channel:
                return {"channel": c}
        raise self._err("channel_not_found")

    def conversations_list(self, types, exclude_archived, limit, cursor):
        if not self.scopes:
            raise self._err("missing_scope")
        half = self.channels[:1] if cursor is None else self.channels[1:]
        return {"channels": half, "response_metadata": {"next_cursor": "n" if cursor is None else ""}}


def sink_with(client, token="xoxb-test-token-0000"):
    db = Database(":memory:")
    bot, _ = db.add_slack_bot(1, token, "Acme", "b")
    sink = SlackSink(None, db)
    sink._clients[token] = client
    sink.bot_id = bot.id
    return sink


CH = [{"id": "C0AAAAAAA", "name": "general"}, {"id": "C0BBBBBBB", "name": "svitlo"}]


async def test_resolve_by_id_name_and_paging():
    pytest.importorskip("slack_sdk")
    s = sink_with(FakeClient(CH))
    assert await s.resolve(s.bot_id, "C0BBBBBBB") == ("C0BBBBBBB", "#svitlo")
    assert await s.resolve(s.bot_id, "#Svitlo") == ("C0BBBBBBB", "#svitlo")     # друга сторінка, регістр неважливий
    with pytest.raises(ChannelError):
        await s.resolve(s.bot_id, "#missing")
    with pytest.raises(ChannelError):
        await s.resolve(s.bot_id, "C0ZZZZZZZ")


async def test_resolve_without_scopes():
    pytest.importorskip("slack_sdk")
    s = sink_with(FakeClient(CH, scopes=False))
    assert await s.resolve(s.bot_id, "C0BBBBBBB") == ("C0BBBBBBB", "C0BBBBBBB")  # ID достатньо
    with pytest.raises(ChannelError) as e:
        await s.resolve(s.bot_id, "#svitlo")
    assert "channels:read" in str(e.value)


class PostClient:
    def __init__(self, error=None):
        self.error, self.posts = error, []

    def chat_postMessage(self, **kw):
        if self.error:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("x", {"ok": False, "error": self.error})
        self.posts.append(kw)

    def auth_test(self):
        if self.error:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("x", {"ok": False, "error": self.error})
        return {"team": "Acme", "user": "hoebot"}


async def test_sink_routes_by_bot_and_legacy_global_token():
    pytest.importorskip("slack_sdk")
    client = PostClient()
    s = sink_with(client)
    await s.send_text(f"{s.bot_id}/C0123ABCD", "Тема", "Дім", "тіло", silent=True)
    assert client.posts[0]["channel"] == "C0123ABCD"
    legacy = SlackSink("xoxb-global-000000000", None)
    gclient = PostClient()
    legacy._clients["xoxb-global-000000000"] = gclient
    await legacy.send_text("C0777", "Тема", "", "тіло", silent=True)
    assert gclient.posts[0]["channel"] == "C0777"
    with pytest.raises(TargetGone):
        await SlackSink(None, None).send_text("C0777", "", "", "x", silent=True)   # немає токена
    with pytest.raises(TargetGone):
        await s.send_text("999/C1", "", "", "x", silent=True)                         # бота видалено


async def test_sink_revoked_token_and_inspect():
    pytest.importorskip("slack_sdk")
    s = sink_with(PostClient(error="token_revoked"))
    with pytest.raises(TargetGone):
        await s.send_text(f"{s.bot_id}/C1", "", "", "x", silent=True)
    with pytest.raises(ChannelError):
        await s.inspect("xoxb-test-token-0000")
    ok = sink_with(PostClient())
    assert await ok.inspect("xoxb-test-token-0000") == ("Acme", "hoebot")