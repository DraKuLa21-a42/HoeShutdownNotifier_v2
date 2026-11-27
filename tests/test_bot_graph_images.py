"""Бот і картинки графіка: /graph, кнопка «Оновити», увімкнення «Графік: картинка», адмін-панель."""
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update

from hoe_notifier import render
from hoe_notifier.bot import build_dispatcher
from hoe_notifier.db import Database
from hoe_notifier.graph_images import GraphImages
from hoe_notifier.monitor import Monitor
from hoe_notifier.schedule import KYIV, parse_days
from hoe_notifier.sinks import TelegramSink

from .test_bot import USER, FakeSession, SearchHoe, settings

pytestmark = pytest.mark.skipif(render.available() is not None, reason=f"рендер недоступний: {render.available()}")

PAGE = ("<div class='post'><p>Графік ГПВ на 28 листопада:</p><p>Підчерга 4.2 – 10:00-12:00.</p><hr>"
        "<p>Графік ГПВ на 27.11.2026:</p><p>Підчерга 4.2 – 13:00-15:00.<br>Підчерга 1.1 – 01:00-02:00.</p></div>")


@pytest.fixture
async def env():
    db, hoe, session = Database(":memory:"), SearchHoe(), FakeSession()
    bot = Bot("1:x", session=session)
    sinks = {"tg": TelegramSink(bot)}
    clock = lambda: datetime(2026, 11, 27, 13, 40, tzinfo=KYIV)  # noqa: E731
    gi = GraphImages(db, sinks, send_delay=0, clock=clock)
    mon = Monitor(db, hoe, sinks, send_delay=0, graph_images=gi)
    dp = build_dispatcher(settings(), db, hoe, mon)
    hoe.graph_days = tuple(parse_days(PAGE, datetime(2026, 11, 27).date()))
    hoe.graph_text = "Підчерга 4.2 – 13:00-15:00."
    n = {"id": 0}

    def msg(text, chat_id=1):
        n["id"] += 1
        ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        return Update(update_id=n["id"], message=Message(
            message_id=n["id"], date=datetime.now(), chat=Chat(id=chat_id, type="private"), from_user=USER,
            text=text, entities=ents))

    def cb(data, chat_id=1, message_id=999):
        n["id"] += 1
        m = Message(message_id=message_id, date=datetime.now(), chat=Chat(id=chat_id, type="private"))
        return Update(update_id=n["id"], callback_query=CallbackQuery(
            id=str(n["id"]), from_user=USER, chat_instance="c", message=m, data=data))

    async def send(update):
        await dp.feed_update(bot, update)

    yield db, session, send, msg, cb, gi
    db.close()


def of(session, name):
    return [m for m in session.methods if type(m).__name__ == name]


async def test_graph_command_sends_images_with_highlight_and_refresh_button(env):
    db, session, send, msg, cb, gi = env
    db.add_subscription("tg", "1", "10", "5", "Дім", queue="4.2")
    await send(msg("/graph"))
    photos = of(session, "SendPhoto")
    assert len(photos) == 2                                   # сьогодні й завтра
    assert "Сьогодні" in photos[0].caption and "<b>4.2</b>: 13:00–15:00" in photos[0].caption
    assert photos[0].reply_markup.inline_keyboard[0][0].callback_data == "gimg:r:2026-11-27"
    assert "Завтра" in photos[1].caption
    await send(msg("/graph text"))
    assert of(session, "SendMessage") and len(of(session, "SendPhoto")) == 2   # текстом, без нових картинок


async def test_refresh_button_edits_message(env):
    db, session, send, msg, cb, gi = env
    await send(msg("/graph"))
    await send(cb("gimg:r:2026-11-27", message_id=555))
    edits = of(session, "EditMessageMedia")
    assert edits and edits[-1].message_id == 555 and "Сьогодні" in edits[-1].media.caption


async def test_enabling_image_sends_current_graphs_once(env):
    db, session, send, msg, cb, gi = env
    sub, _ = db.add_subscription("tg", "1", "10", "5", "Дім", queue="4.2", graph_images=False)
    await send(cb(f"sub:{sub.id}:gimg"))
    photos = of(session, "SendPhoto")
    assert len(photos) == 2 and db.count_live_graph_messages() == 2
    await send(cb(f"sub:{sub.id}:gimg"))                      # вимкнули
    await send(cb(f"sub:{sub.id}:gimg"))                      # увімкнули знову — дублікатів немає
    assert len(of(session, "SendPhoto")) == 2


async def test_admin_show_and_send_rendered_images(env):
    db, session, send, msg, cb, gi = env
    db.add_subscription("tg", "7", "10", "5", "A", graph_images=True)
    await send(cb("adm:poll"))                                # звичайний цикл: нові графіки → чату 7
    assert [p.chat_id for p in of(session, "SendPhoto")] == [7, 7]
    await send(cb("adm:show_img"))
    assert [p.chat_id for p in of(session, "SendPhoto")[2:]] == [1, 1]     # лише адміну, без відстеження
    assert db.count_live_graph_messages() == 2
    await send(cb("adm:send_img"))
    assert "свіжі картинки" in of(session, "SendMessage")[-1].text
    await send(cb("adm:send_img:yes"))
    assert [p.chat_id for p in of(session, "SendPhoto")[4:]] == [7, 7]
    assert len(of(session, "EditMessageMedia")) == 2          # попередні копії стали «Застарілими»
    await send(cb("adm:stats"))
    assert "живих повідомлень: 2" in of(session, "SendMessage")[-1].text