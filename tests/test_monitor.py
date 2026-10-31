import pytest
from hoe_notifier.db import Database
from hoe_notifier.hoe import HoeUnavailable
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
        self.calls = 0

    async def fetch_events_html(self, street_id, house):
        self.calls += 1
        if self.fail:
            raise HoeUnavailable("down")
        return self.html

    async def fetch_graph_url(self):
        return self.graph_url

    async def download(self, url):
        return b"PNG"


class FakeSink:
    def __init__(self):
        self.texts, self.photos, self.gone, self.moved = [], [], set(), {}

    async def send_text(self, target, subject, label, body, *, silent):
        if target in self.gone:
            raise TargetGone("blocked")
        if target in self.moved:
            raise TargetMigrated(self.moved[target])
        self.texts.append((target, subject, label, body))

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