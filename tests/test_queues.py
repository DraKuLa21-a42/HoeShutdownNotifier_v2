"""Фільтрація за чергою: модуль queues, БД, монітор, бот, addresses.toml."""
import sqlite3

import pytest

from hoe_notifier.addresses_file import load_addresses
from hoe_notifier.db import Database
from hoe_notifier.hoe import NO_OUTAGES
from hoe_notifier.monitor import SUBJECT_CHANGED, SUBJECT_NEW, Monitor, describe_state, subject_none
from hoe_notifier.queues import (
    QUEUES, filter_outages, filter_schedule, normalize_queue, outage_view, queue_matches,
)

from .test_bot import SCHEDULE, alerts, button_texts, last_send_message, make_env
from .test_monitor import FakeHoe, FakeSink

BLOCKS = (
    "Адреса: вул. А, 1\nЧерга (ГПВ): 1.1\nДата: 07.11\n\n"
    "Адреса: вул. А, 1\nЧерга (ГПВ): 1.2\nДата: 08.11\n\n"
    "Адреса: вул. А, 1\nЧерга (ГАВ): 7\nДата: 09.11\n\n"
    "Адреса: вул. А, 1\nЧерга (ГПВ): 2\nДата: 10.11"
)
FULL_SCHEDULE = (
    "Графік на 07 листопада:\n\n"
    "Підчерга 1.1 – 00:00-04:00; 09:00-13:00.\n"
    "Підчерга 1.2 – 04:00-08:00.\n"
    "Підчерга 6.2 – 20:00-24:00."
)


def test_normalize_and_matching():
    assert normalize_queue("1,1") == "1.1" and normalize_queue(" 6.2 ") == "6.2"
    assert normalize_queue("7.1") is None and normalize_queue("abc") is None and normalize_queue(None) is None
    assert len(QUEUES) == 12 and QUEUES[0] == "1.1" and QUEUES[-1] == "6.2"
    assert queue_matches("1.1", "1.1") and queue_matches("1", "1.2") and not queue_matches("1.2", "1.1")
    assert not queue_matches("2", "1.1")


def test_filter_outages_keeps_own_queue_and_queueless_blocks():
    out = filter_outages(BLOCKS, "1.1")
    assert "Дата: 07.11" in out and "Дата: 09.11" in out        # своя + без ГПВ-черги (ГАВ)
    assert "08.11" not in out and "10.11" not in out
    assert "Дата: 10.11" in filter_outages(BLOCKS, "2.2")        # черга «2» = обидві підчерги
    assert filter_outages(BLOCKS, None) == BLOCKS


def test_outage_view():
    assert outage_view(NO_OUTAGES, "1.1") == "" and outage_view("", "1.1") == ""
    assert outage_view(BLOCKS, "6.1") == filter_outages(BLOCKS, "6.1")
    assert "Дата: 09.11" in outage_view(BLOCKS, "6.1")           # лишилось лише ГАВ-блок


def test_filter_schedule():
    out = filter_schedule(FULL_SCHEDULE, frozenset({"1.1"}))
    assert "Підчерга 1.1" in out and "1.2" not in out and "6.2" not in out and "Графік на 07" in out
    both = filter_schedule(FULL_SCHEDULE, frozenset({"1.1", "6.2"}))
    assert "Підчерга 6.2" in both and "Підчерга 1.2" not in both
    assert filter_schedule(FULL_SCHEDULE, None) == FULL_SCHEDULE
    assert "Для черги 2.1 у цьому графіку відключень немає." in filter_schedule(FULL_SCHEDULE, frozenset({"2.1"}))
    plain = "Сьогодні змін немає"
    assert filter_schedule(plain, frozenset({"1.1"})) == plain


# --- БД -----------------------------------------------------------------------------

def test_db_queue_roundtrip_and_stats():
    db = Database(":memory:")
    sub, _ = db.add_subscription("tg", "1", "10", "5", "A", queue="1.1")
    assert db.get_subscription(sub.id).queue == "1.1"
    assert db.stats()["with_queue"] == 1
    assert db.set_queue(sub.id, None).queue is None
    assert db.stats()["with_queue"] == 0


def test_db_migration_adds_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
        target TEXT NOT NULL, street_id TEXT NOT NULL, house TEXT NOT NULL, label TEXT NOT NULL,
        events INTEGER NOT NULL DEFAULT 1, graphs INTEGER NOT NULL DEFAULT 0, silent INTEGER NOT NULL DEFAULT 1,
        owner_id INTEGER, source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL DEFAULT '',
        UNIQUE(channel, target, street_id, house))""")
    con.execute("INSERT INTO subscriptions (channel,target,street_id,house,label) VALUES ('tg','1','10','5','A')")
    con.commit()
    con.close()
    db = Database(str(path))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue is None
    assert db.set_queue(sub.id, "2.1").queue == "2.1"


def test_graph_targets_and_chat_queues():
    db = Database(":memory:")
    a, _ = db.add_subscription("tg", "1", "10", "5", "A", graphs=True, queue="1.1")
    db.add_subscription("tg", "1", "11", "6", "B", graphs=True, queue="2.2")
    db.add_subscription("tg", "2", "10", "5", "A", graphs=True, queue="1.1")
    db.add_subscription("tg", "3", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "4", "10", "5", "A", graphs=False, queue="3.1")
    by = {t.target: t for t in db.graph_targets()}
    assert set(by) == {"1", "2", "3"}
    assert by["1"].queues == frozenset({"1.1", "2.2"}) and by["2"].queues == frozenset({"1.1"})
    assert by["3"].queues is None
    assert db.chat_queues("tg", "1") == frozenset({"1.1", "2.2"})
    assert db.chat_queues("tg", "3") is None and db.chat_queues("tg", "999") is None
    db.add_subscription("tg", "1", "12", "7", "C", graphs=True)         # адреса без черги → фільтра немає
    assert db.chat_queues("tg", "1") is None
    assert {t.target: t for t in db.graph_targets()}["1"].queues is None


def test_config_sync_stores_queue():
    db = Database(":memory:")
    entry = {"channel": "tg", "target": "1", "street_id": "1", "house": "1", "label": "cfg",
             "events": True, "graphs": False, "silent": True, "queue": "4.2"}
    db.sync_config_subscriptions([entry])
    assert db.list_subscriptions("tg", "1")[0].queue == "4.2"
    db.sync_config_subscriptions([{**entry, "queue": None}])
    assert db.list_subscriptions("tg", "1")[0].queue is None


def test_addresses_file_queue(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text('[[address]]\nstreet_id=1\nhouse="1"\ntarget=5\nqueue="1,2"\n'
                 '[[address]]\nstreet_id=2\nhouse="1"\ntarget=5\nqueue="9.9"\n'
                 '[[address]]\nstreet_id=3\nhouse="1"\ntarget=5\n', encoding="utf-8")
    e = load_addresses(f)
    assert [x["queue"] for x in e] == ["1.2", None]           # некоректний запис пропущено


# --- монітор ------------------------------------------------------------------------

@pytest.fixture
def menv():
    db, hoe, sink = Database(":memory:"), FakeHoe(), FakeSink()
    return db, hoe, sink, Monitor(db, hoe, {"tg": sink}, send_delay=0)


def tbl(*queues):
    rows = "".join(f"<tr><td>{q}</td></tr>" for q in queues)
    return f"<table><thead><tr><th>Черга (ГПВ)</th></tr></thead><tbody>{rows}</tbody></table>"


def test_describe_state_with_queue():
    assert subject_none("1.1") == "Погодинних відключень для черги 1.1 немає!"


async def test_other_queue_changes_are_silent_own_queue_notifies(menv):
    db, hoe, sink, mon = menv
    db.add_subscription("tg", "1", "10", "5", "Мій", queue="1.1")
    db.add_subscription("tg", "2", "10", "5", "Усі")
    hoe.html = tbl("1.1")
    await mon.poll_once()
    assert {t[0]: t[1] for t in sink.texts} == {"1": SUBJECT_NEW, "2": SUBJECT_NEW}
    assert sink.texts[0][2] == "Мій · черга 1.1" or sink.texts[1][2] == "Мій · черга 1.1"

    sink.texts.clear()
    hoe.html = tbl("1.1", "2.2")        # додалась чужа черга
    await mon.poll_once()
    assert [t[0] for t in sink.texts] == ["2"]

    sink.texts.clear()
    hoe.html = tbl("2.2")               # своя зникла
    await mon.poll_once()
    by = {t[0]: t for t in sink.texts}
    assert by["1"][1] == subject_none("1.1")


async def test_first_poll_other_queue_only_says_none_for_queue(menv):
    db, hoe, sink, mon = menv
    db.add_subscription("tg", "1", "10", "5", "Мій", queue="1.1")
    hoe.html = tbl("3.1")
    await mon.poll_once()
    assert len(sink.texts) == 1 and sink.texts[0][1] == subject_none("1.1")
    await mon.poll_once()
    assert len(sink.texts) == 1


async def test_text_graph_filtered_per_target_and_dedup_per_target(menv):
    db, hoe, sink, mon = menv
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True, queue="1.1")
    db.add_subscription("tg", "2", "10", "5", "A", graphs=True)
    hoe.graph_text = FULL_SCHEDULE
    await mon.send_graphs()
    by = {t[0]: t[3] for t in sink.texts}
    assert "Підчерга 1.1" in by["1"] and "6.2" not in by["1"]
    assert "Підчерга 6.2" in by["2"]
    n = len(sink.texts)
    await mon.send_graphs()
    assert len(sink.texts) == n


# --- бот ----------------------------------------------------------------------------

async def test_add_id_with_queue_option_and_keyboard():
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/add_id 10 5 Дім --queue=1.1"))
    sub = db.list_subscriptions("tg", "1")[0]
    assert sub.queue == "1.1" and sub.label == "Дім"
    assert any("Черга: 1.1" in t for t in button_texts(last_send_message(session)))


async def test_add_id_queue_comma_and_invalid():
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/add_id 10 5 --queue 2,2"))
    assert db.list_subscriptions("tg", "1")[0].queue == "2.2"
    await send(msg("/add_id 11 6 Інша --queue=7.7"))
    assert "Невідома черга" in session.texts()[-1]
    assert db.count_for_target("tg", "1") == 1


async def test_queue_picker_set_change_clear():
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/add_id 10 5 Дім"))
    sub = db.list_subscriptions("tg", "1")[0]
    await send(cb(f"sub:{sub.id}:queue"))
    edits = [m for m in session.methods if type(m).__name__ == "EditMessageReplyMarkup"]
    labels = [b.text for r in edits[-1].reply_markup.inline_keyboard for b in r]
    assert "1.1" in labels and "6.2" in labels
    await send(cb(f"q:{sub.id}:3.2"))
    assert db.get_subscription(sub.id).queue == "3.2"
    await send(cb(f"q:{sub.id}:5.1"))
    assert db.get_subscription(sub.id).queue == "5.1"
    await send(cb(f"q:{sub.id}:back"))
    assert db.get_subscription(sub.id).queue == "5.1"
    await send(cb(f"q:{sub.id}:all"))
    assert db.get_subscription(sub.id).queue is None


async def test_queue_picker_permissions():
    db, hoe, session, send, msg, cb = make_env()
    other, _ = db.add_subscription("tg", "555", "10", "5", "Чужа")
    await send(cb(f"q:{other.id}:1.1", chat_id=1))
    assert db.get_subscription(other.id).queue is None
    assert alerts(session)
    db.sync_config_subscriptions([{"channel": "tg", "target": "1", "street_id": "1", "house": "1",
                                   "label": "cfg", "events": True, "graphs": False, "silent": True}])
    cfg = db.list_subscriptions("tg", "1")[0]
    await send(cb(f"q:{cfg.id}:1.1"))
    assert db.get_subscription(cfg.id).queue is None


async def test_check_filtered_by_queue():
    db, hoe, session, send, msg, cb = make_env()
    hoe.html = tbl("2.2")
    await send(msg("/add_id 10 5 Дім --queue=1.1"))
    await send(msg("/check"))
    assert "для черги 1.1 немає" in session.texts()[-1]


async def test_graph_command_filters_by_chat_queue_and_all():
    db, hoe, session, send, msg, cb = make_env()
    hoe.graph_text = FULL_SCHEDULE
    await send(msg("/add_id 10 5 Дім --queue=1.1"))
    await send(msg("/graph"))
    out = last_send_message(session).text
    assert "Підчерга 1.1" in out and "6.2" not in out and "/graph all" in out
    await send(msg("/graph all"))
    assert "Підчерга 6.2" in last_send_message(session).text


async def test_graph_command_unfiltered_without_queue():
    db, hoe, session, send, msg, cb = make_env()
    hoe.graph_text = FULL_SCHEDULE
    await send(msg("/graph"))
    assert "Підчерга 6.2" in last_send_message(session).text
