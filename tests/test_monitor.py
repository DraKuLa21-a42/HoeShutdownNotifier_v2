import pytest
from hoe_notifier.db import Database
from hoe_notifier.hoe import Graphs, HoeUnavailable
from hoe_notifier.monitor import Monitor, SUBJECT_CHANGED, SUBJECT_NEW, SUBJECT_NONE
from hoe_notifier.sinks import TargetGone, TargetMigrated

TABLE_A = "<table><thead><tr><th>Черга</th></tr></thead><tbody><tr><td>1.1</td></tr></tbody></table>"
TABLE_B = "<table><thead><tr><th>Черга</th></tr></thead><tbody><tr><td>2.2</td></tr></tbody></table>"
EMPTY = "<p>Відключень немає</p>"


class FakeHoe:
    def __init__(self):
        self.html = TABLE_A
        self.fail = False
        self.graph_url = None
        self.graph_text = None
        self.page_down = False
        self.calls = 0

    async def fetch_events_html(self, street_id, house):
        self.calls += 1
        if self.fail:
            raise HoeUnavailable("down")
        return self.html

    async def fetch_graphs(self):
        if self.page_down:
            return None
        return Graphs(image_url=self.graph_url, text=self.graph_text)

    async def download(self, url):
        return b"PNG"


class FakeSink:
    def __init__(self):
        self.texts, self.photos, self.gone, self.moved = [], [], set(), {}
        self.pre_flags = []

    async def send_text(self, target, subject, label, body, *, silent, pre=True):
        if target in self.gone:
            raise TargetGone("blocked")
        if target in self.moved:
            raise TargetMigrated(self.moved[target])
        self.texts.append((target, subject, label, body))
        self.pre_flags.append(pre)

    async def send_photo(self, target, data, *, silent):
        if target in self.moved:
            raise TargetMigrated(self.moved[target])
        self.photos.append(target)


@pytest.fixture
def env():
    db, hoe, sink = Database(":memory:"), FakeHoe(), FakeSink()
    alerts = []

    async def notify(t):
        alerts.append(t)

    mon = Monitor(db, hoe, {"tg": sink}, admin_notify=notify, send_delay=0)
    return db, hoe, sink, mon, alerts


async def test_first_then_same_then_changed_then_none(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "Дім")

    await mon.poll_once()
    assert [t[1] for t in sink.texts] == [SUBJECT_NEW]
    assert sink.texts[0][2] == "Дім" and "Черга: 1.1" in sink.texts[0][3]

    await mon.poll_once()  # без змін — тиша
    assert len(sink.texts) == 1

    hoe.html = TABLE_B
    await mon.poll_once()
    assert sink.texts[-1][1] == SUBJECT_CHANGED

    hoe.html = EMPTY
    await mon.poll_once()
    assert sink.texts[-1][1] == SUBJECT_NONE

    await mon.poll_once()
    assert len(sink.texts) == 3

    hoe.html = TABLE_A  # після "немає" знову з'являється
    await mon.poll_once()
    assert sink.texts[-1][1] == SUBJECT_NEW


async def test_one_request_per_address_for_many_subscribers(env):
    db, hoe, sink, mon, _ = env
    for chat in range(1, 21):
        db.add_subscription("tg", str(chat), "10", "5", "X")
    await mon.poll_once()
    assert hoe.calls == 1
    assert len(sink.texts) == 20


async def test_site_down_keeps_state_and_alerts_admin_once(env):
    db, hoe, sink, mon, alerts = env
    db.add_subscription("tg", "1", "10", "5", "A")
    await mon.poll_once()
    hoe.fail = True
    for _ in range(5):
        await mon.poll_once()
    assert len(sink.texts) == 1           # підписників не спамимо
    assert len(alerts) == 1               # адмін отримав одне попередження
    hoe.fail = False
    await mon.poll_once()
    assert len(alerts) == 2 and "знову працює" in alerts[1]
    assert len(sink.texts) == 1           # той самий стан — без повторного повідомлення


async def test_unknown_layout_does_not_overwrite_state(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A")
    await mon.poll_once()
    hoe.html = "<html>Error 500</html>"
    await mon.poll_once()
    assert db.get_state("10", "5").last_text.startswith("Черга")
    assert len(sink.texts) == 1


async def test_blocked_chat_gets_unsubscribed(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A")
    db.add_subscription("tg", "2", "10", "5", "A")
    sink.gone.add("1")
    await mon.poll_once()
    assert db.list_subscriptions("tg", "1") == []
    assert len(db.list_subscriptions("tg", "2")) == 1
    assert [t[0] for t in sink.texts] == ["2"]


async def test_state_saved_before_send_so_no_retry_storm(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A")
    sink.gone.add("1")
    await mon.poll_once()
    assert db.get_state("10", "5") is not None


async def test_graph_sent_once_per_new_url(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "1", "11", "6", "B", graphs=True)
    hoe.graph_url = "https://x/1.png"
    await mon.poll_once()
    await mon.poll_once()
    assert sink.photos == ["1"]           # один раз на чат, не двічі і не повторно
    hoe.graph_url = "https://x/2.png"
    await mon.poll_once()
    assert sink.photos == ["1", "1"]


async def test_refresh_before_subscribe_avoids_duplicate_for_new_user(env):
    db, hoe, sink, mon, _ = env
    await mon.refresh_address("10", "5")   # як у /add: спочатку оновлення
    assert sink.texts == []                 # підписників ще нема
    db.add_subscription("tg", "1", "10", "5", "A")
    await mon.poll_once()
    assert sink.texts == []                 # стан уже відомий — дубля не буде


async def test_manual_refresh_uses_cache_but_poll_does_not(env):
    db, hoe, sink, mon, _ = env
    await mon.refresh_address("10", "5", max_age=60)
    await mon.refresh_address("10", "5", max_age=60)
    assert hoe.calls == 1
    await mon.refresh_address("10", "5")            # без max_age — завжди реальний запит
    assert hoe.calls == 2


async def test_concurrent_manual_refreshes_make_one_request(env):
    import asyncio
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A")
    await asyncio.gather(*(mon.refresh_address("10", "5", max_age=60) for _ in range(10)))
    assert hoe.calls == 1
    assert len(sink.texts) == 1                       # і розсилка — одна


async def test_cache_expires(env, monkeypatch):
    import hoe_notifier.monitor as m
    db, hoe, sink, mon, _ = env
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"])
    await mon.refresh_address("10", "5", max_age=60)
    t["now"] += 61
    await mon.refresh_address("10", "5", max_age=60)
    assert hoe.calls == 2


async def test_fail_fast_stops_cycle_when_site_down(env):
    db, hoe, sink, mon, _ = env
    for i in range(10):
        db.add_subscription("tg", str(i), str(i), "1", "A")   # 10 різних адрес
    hoe.fail = True
    await mon.poll_once()
    assert hoe.calls == 3                                       # не 10 адрес × ретраї
    hoe.fail, hoe.calls = False, 0
    await mon.poll_once()
    assert hoe.calls == 10                                      # наступний цикл — повноцінний


async def test_group_migrated_to_supergroup(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "-100", "10", "5", "A")
    sink.moved["-100"] = "-1009"
    await mon.poll_once()
    assert db.list_subscriptions("tg", "-100") == []
    assert len(db.list_subscriptions("tg", "-1009")) == 1
    assert [t[0] for t in sink.texts] == ["-1009"]             # повідомлення дійшло на новий ID


async def test_graph_to_migrated_chat(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "-100", "10", "5", "A", graphs=True)
    sink.moved["-100"] = "-1009"
    hoe.graph_url = "https://x/1.png"
    await mon.poll_once()
    assert sink.photos == ["-1009"]


# ---------------------------------------------------------------- текстовий графік
SCHEDULE = "За розпорядженням 07 листопада:\n\nПідчерга 1.1 – 00:00-04:00; 09:00-13:00."
SCHEDULE_2 = "За розпорядженням 08 листопада:\n\nПідчерга 1.1 – 02:00-06:00."


def graph_texts(sink):
    return [t for t in sink.texts if t[1] == "Графік погодинних відключень"]


async def test_text_graph_sent_once_then_on_change(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await mon.poll_once()
    await mon.poll_once()
    assert [t[3] for t in graph_texts(sink)] == [SCHEDULE]          # без повторів
    hoe.graph_text = SCHEDULE_2
    await mon.poll_once()
    assert [t[3] for t in graph_texts(sink)] == [SCHEDULE, SCHEDULE_2]


async def test_text_graph_is_plain_not_preformatted_and_has_no_label(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await mon.poll_once()
    idx = [i for i, t in enumerate(sink.texts) if t[1] == "Графік погодинних відключень"][0]
    assert sink.pre_flags[idx] is False and sink.texts[idx][2] == ""


async def test_text_graph_only_for_graph_subscribers_once_per_chat(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "1", "11", "5", "B", graphs=True)     # той самий чат, інша адреса
    db.add_subscription("tg", "2", "10", "5", "A", graphs=False)    # графік вимкнено
    hoe.graph_text = SCHEDULE
    await mon.poll_once()
    assert [t[0] for t in graph_texts(sink)] == ["1"]


async def test_image_and_text_both_published(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_url, hoe.graph_text = "https://x/1.png", SCHEDULE
    await mon.poll_once()
    assert sink.photos == ["1"] and len(graph_texts(sink)) == 1


async def test_no_text_on_page_sends_nothing_and_keeps_marker(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await mon.poll_once()
    hoe.graph_text = None                      # сторінка перейшла на картинку
    await mon.poll_once()
    hoe.graph_text = SCHEDULE                  # і повернулась до того самого тексту
    await mon.poll_once()
    assert len(graph_texts(sink)) == 1


async def test_graph_page_down_is_harmless(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.page_down = True
    await mon.poll_once()
    assert graph_texts(sink) == [] and sink.photos == []


async def test_force_resends_even_if_unchanged(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text, hoe.graph_url = SCHEDULE, "https://x/1.png"
    await mon.poll_once()
    report = await mon.send_graphs(image=False, text=True, force=True)
    assert report.text_sent == 1 and report.image_sent == 0 and report.targets == 1
    assert len(graph_texts(sink)) == 2 and sink.photos == ["1"]       # картинку не чіпали


async def test_force_image_only(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text, hoe.graph_url = SCHEDULE, "https://x/1.png"
    report = await mon.send_graphs(image=True, text=False, force=True)
    assert report.image_sent == 1 and report.text_sent == 0
    assert graph_texts(sink) == [] and sink.photos == ["1"]


async def test_manual_send_marks_as_sent_so_auto_does_not_duplicate(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    hoe.graph_text = SCHEDULE
    await mon.send_graphs(image=False, text=True, force=True)
    await mon.poll_once()
    assert len(graph_texts(sink)) == 1


async def test_report_when_nothing_to_send(env):
    db, hoe, sink, mon, _ = env
    report = await mon.send_graphs(force=True)
    assert report.page_ok and not report.text_found and not report.image_found and report.targets == 0
    hoe.page_down = True
    assert (await mon.send_graphs(force=True)).page_ok is False


async def test_text_graph_blocked_chat_is_unsubscribed(env):
    db, hoe, sink, mon, _ = env
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    sink.gone.add("1")
    hoe.graph_text = SCHEDULE
    report = await mon.send_graphs(text=True, image=False, force=True)
    assert report.text_sent == 0 and db.list_subscriptions("tg", "1") == []