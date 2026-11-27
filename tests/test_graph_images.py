from datetime import date, datetime

import pytest

from hoe_notifier import render
from hoe_notifier.db import Database, GraphTarget
from hoe_notifier.graph_images import REFRESH_CALLBACK, GraphImages
from hoe_notifier.hoe import Graphs
from hoe_notifier.monitor import Monitor
from hoe_notifier.schedule import KYIV, parse_days
from hoe_notifier.sinks import MessageGone

from .test_monitor import FakeHoe

pytestmark = pytest.mark.skipif(render.available() is not None, reason=f"рендер недоступний: {render.available()}")


def page(*sections):
    return "<div class='post'>" + "<hr>".join(sections) + "</div>"


def day(date_text, line, today=date(2026, 11, 27)):
    body = line.replace(". Підчерга", ".<br>Підчерга")
    return parse_days(page(f"<p>Графік ГПВ на {date_text}:</p><p>{body}</p>"), today)[0]


class Clock:
    def __init__(self, *args):
        self.now = datetime(*args, tzinfo=KYIV)

    def __call__(self):
        return self.now

    def set(self, *args):
        self.now = datetime(*args, tzinfo=KYIV)


class GraphSink:
    def __init__(self):
        self.sent, self.edits, self.gone = [], [], set()
        self.n = 100

    async def send_text(self, *a, **k):
        return None

    async def send_photo(self, target, data, *, silent):
        self.sent.append({"target": target, "silent": silent, "photo_only": True})

    async def send_graph(self, target, data, caption, *, silent, buttons=(), reply_to=None):
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        self.n += 1
        self.sent.append({"target": target, "id": str(self.n), "caption": caption, "silent": silent,
                          "buttons": list(buttons), "reply_to": reply_to})
        return str(self.n)

    async def edit_graph(self, target, message_id, data, caption, *, buttons=()):
        if message_id in self.gone:
            raise MessageGone("message to edit not found")
        self.edits.append({"target": target, "id": message_id, "caption": caption, "buttons": list(buttons)})


@pytest.fixture
def env():
    db, sink = Database(":memory:"), GraphSink()
    clock = Clock(2026, 11, 27, 21, 10)
    gi = GraphImages(db, {"tg": sink}, send_delay=0, clock=clock, live_minutes=30)
    db.add_subscription("tg", "1", "10", "5", "Дім", queue="4.2", graph_images=True, silent=False)   # чат T
    yield db, sink, gi, clock
    db.close()


T = GraphTarget("tg", "1", False, frozenset({"4.2"}))
TOMORROW = "28 листопада"


async def test_tomorrow_lifecycle_future_today_past(env):
    db, sink, gi, clock = env
    rep = await gi.publish([day(TOMORROW, "Підчерга 4.2 – 07:00-09:30; 16:00-19:00.")], [T])
    assert rep.days == ["2026-11-28"] and rep.sent == 1
    msg = sink.sent[0]
    assert msg["silent"] is False and "Завтра, сб 28.11" in msg["caption"]
    assert "07:00–09:30, 16:00–19:00 (5.5 год)" in msg["caption"]
    assert msg["buttons"] == [("🔄 Оновити", REFRESH_CALLBACK + "2026-11-28")]

    assert (await gi.publish([day(TOMORROW, "Підчерга 4.2 – 07:00-09:30; 16:00-19:00.")], [T])).sent == 0
    assert await gi.tick() == 0                                  # той самий стан — не редагуємо

    clock.set(2026, 11, 28, 0, 0)                                # перехід доби: «Завтра» → «Сьогодні»
    assert await gi.tick() == 1
    edit = sink.edits[-1]
    assert edit["id"] == msg["id"] and "Сьогодні, сб 28.11" in edit["caption"]
    assert "🟢 світло є · відключення о 07:00" in edit["caption"] and edit["buttons"]

    clock.set(2026, 11, 28, 0, 20)
    assert await gi.tick() == 0                                  # той самий 30-хвилинний проміжок
    clock.set(2026, 11, 28, 7, 5)
    assert await gi.tick() == 1 and "🔴 світла немає · увімкнення о 09:30" in sink.edits[-1]["caption"]

    clock.set(2026, 11, 29, 0, 1)                                # учорашній — сірий «Минув», без кнопок
    assert await gi.tick() == 1
    assert "Минув" in sink.edits[-1]["caption"] and sink.edits[-1]["buttons"] == []
    assert db.live_graph_messages() == []
    assert await gi.tick() == 0


async def test_update_sends_new_and_marks_old_superseded(env):
    db, sink, gi, clock = env
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00. Підчерга 1.1 – 01:00-02:00.")], [T])
    first = sink.sent[-1]["id"]

    # змінилась лише 1.1 — для чату з чергою 4.2 нове повідомлення приходить без звуку
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00. Підчерга 1.1 – 03:00-04:00.")], [T])
    new = sink.sent[-1]
    assert new["silent"] is True and new["reply_to"] == first
    assert "Графік змінено" in new["caption"] and "для цієї черги без змін" in new["caption"]
    old = sink.edits[-1]
    assert old["id"] == first and "Застарів" in old["caption"] and old["buttons"] == []

    # змінилась 4.2 — зі звуком і переліком змін
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-13:00. Підчерга 1.1 – 03:00-04:00.")], [T])
    assert sink.sent[-1]["silent"] is False and "зміни: +12:00–13:00" in sink.sent[-1]["caption"]
    live = db.live_graph_messages()
    assert [m.message_id for m in live] == [sink.sent[-1]["id"]]


async def test_live_minutes_zero_only_transitions(env):
    db, sink, gi, clock = env
    gi.live_minutes = 0
    clock.set(2026, 11, 27, 9, 0)
    await gi.publish([day("27 листопада", "Підчерга 4.2 – 10:00-12:00.")], [T])
    clock.set(2026, 11, 27, 15, 0)
    assert await gi.tick() == 0
    clock.set(2026, 11, 28, 0, 0)
    assert await gi.tick() == 1


async def test_deleted_message_stops_being_tracked(env):
    db, sink, gi, clock = env
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00.")], [T])
    sink.gone.add(sink.sent[-1]["id"])
    clock.set(2026, 11, 28, 0, 0)
    assert await gi.tick() == 0
    assert db.live_graph_messages() == []


async def test_past_dates_on_site_are_ignored_and_force_resends(env):
    db, sink, gi, clock = env
    old = day("26 листопада", "Підчерга 4.2 – 10:00-12:00.")
    tom = day(TOMORROW, "Підчерга 4.2 – 10:00-12:00.")
    assert (await gi.publish([old, tom], [T])).days == ["2026-11-28"]
    first = sink.sent[-1]["id"]
    rep = await gi.publish([old, tom], [T], force=True)
    assert rep.days == ["2026-11-28"] and rep.sent == 1
    assert sink.edits[-1]["id"] == first and "Застарів" in sink.edits[-1]["caption"]


async def test_ensure_chat_and_refresh(env):
    db, sink, gi, clock = env
    db.add_subscription("tg", "5", "10", "1", "A", queue="2.1", graph_images=True)
    await gi.publish([day(TOMORROW, "Підчерга 2.1 – 10:00-12:00.")], [])
    assert await gi.ensure_chat("tg", "5") == 1
    assert await gi.ensure_chat("tg", "5") == 0                  # уже є живе повідомлення
    sent = sink.sent[-1]
    assert sent["target"] == "5" and "<b>2.1</b>: 10:00–12:00" in sent["caption"]

    assert await gi.refresh("tg", "5", sent["id"], "2026-11-28") is None
    assert sink.edits[-1]["id"] == sent["id"]
    assert "Щойно" in await gi.refresh("tg", "5", sent["id"], "2026-11-28")    # захист від частих натискань

    # знімок із /graph (не відстежується) оновлюється до найновішої версії
    assert await gi.refresh("tg", "5", "777", "2026-11-28") is None
    assert sink.edits[-1]["id"] == "777" and "Знімок" not in sink.edits[-1]["caption"]  # для завтра без часу


async def test_snapshots_for_graph_command(env):
    db, sink, gi, clock = env
    clock.set(2026, 11, 27, 13, 40)
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00."),
                      day("27 листопада", "Підчерга 4.2 – 13:00-15:00.")], [])
    shots = gi.snapshots(frozenset({"4.2"}))
    assert len(shots) == 2
    assert "Сьогодні" in shots[0][1] and "Знімок · 13:40" in shots[0][1] and "(через 1 год 20 хв)" in shots[0][1]
    assert "Завтра" in shots[1][1]
    assert all(png[:4] == b"\x89PNG" for png, _, _ in shots)


async def test_disabled_does_nothing(env):
    db, sink, gi, clock = env
    gi.enabled = False
    rep = await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00.")], [T])
    assert rep.sent == 0 and "вимкнено" in rep.error and sink.sent == []


async def test_monitor_uses_rendered_image_instead_of_site_image():
    db, sink, hoe = Database(":memory:"), GraphSink(), FakeHoe()
    clock = Clock(2026, 11, 27, 21, 10)
    gi = GraphImages(db, {"tg": sink}, send_delay=0, clock=clock)
    mon = Monitor(db, hoe, {"tg": sink}, send_delay=0, graph_images=gi)
    db.add_subscription("tg", "1", "10", "5", "A", graphs=False, graph_images=True, queue="4.2")
    hoe.graph_url = "https://x/1.png"
    days = (day(TOMORROW, "Підчерга 4.2 – 10:00-12:00."),)

    async def fetch():
        return Graphs(image_url=hoe.graph_url, text="Підчерга 4.2 – 10:00-12:00.", days=days)
    hoe.fetch_graphs = fetch
    report = await mon.send_graphs(text=False)
    assert report.rendered and report.image_sent == 1
    assert [s.get("photo_only") for s in sink.sent] == [None]       # картинку з сайту не дублюємо
    gi.enabled = False
    await mon.send_graphs(text=False)
    assert sink.sent[-1].get("photo_only") is True                  # вимкнено — як раніше, картинка з сайту
    db.close()


def test_render_all_states():
    d = day(TOMORROW, "Підчерга 4.2 – 07:00-09:30; 16:00-19:00.")
    for state in ("future", "today", "past", "superseded"):
        png = render.render_day(d.day, d.slots, state=state, highlight={"4.2", "1.1"},
                                previous=d.slots if state == "today" else None,
                                now=datetime(2026, 11, 28, 13, 0, tzinfo=KYIV), site_updated="12:30")
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


async def test_slack_messages_are_tracked_and_updated_without_buttons(env):
    db, sink, gi, clock = env

    class SlackLike(GraphSink):
        async def send_graph(self, target, data, caption, *, silent, buttons=(), reply_to=None):
            assert buttons == () and reply_to is None
            await super().send_graph(target, data, caption, silent=silent)
            return f"ts{self.n}|F{self.n}"

        async def edit_graph(self, target, message_id, data, caption, *, buttons=()):
            assert buttons == ()
            await super().edit_graph(target, message_id, data, caption)
            self.n += 1
            return f"{message_id.split('|')[0]}|F{self.n}"

    slack = SlackLike()
    gi.sinks["slack"] = slack
    db.add_subscription("slack", "1/C1", "10", "5", "Дім", queue="4.2", graph_images=True)
    S = GraphTarget("slack", "1/C1", True, frozenset({"4.2"}))
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-12:00.")], [S])
    await gi.publish([day(TOMORROW, "Підчерга 4.2 – 10:00-13:00.")], [S])        # нова версія
    assert slack.edits[-1]["id"] == "ts101|F101" and "Застарів" in slack.edits[-1]["caption"]
    clock.set(2026, 11, 28, 0, 0)
    assert await gi.tick() == 1
    live = db.live_graph_messages("slack")
    assert len(live) == 1 and live[0].message_id == "ts102|F104"                 # ідентифікатор файлу оновлено
    assert await gi.ensure_chat("slack", "1/C1") == 0                            # без повторів

    db.set_queue(db.list_subscriptions("slack", "1/C1")[0].id, None)               # без черги: підказки про /list немає
    caption = gi.compose("2026-11-28", live[0].hash, "today", None, channel="slack")[1]
    assert "Усі черги." in caption and "/list" not in caption
    assert "/list" in gi.compose("2026-11-28", live[0].hash, "today", None)[1]