import pytest
from hoe_notifier.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_add_and_dedupe(db):
    sub, created = db.add_subscription("tg", "1", "10", "5", "A")
    assert created
    _, created2 = db.add_subscription("tg", "1", "10", "5", "A")
    assert not created2
    assert db.count_for_target("tg", "1") == 1


def test_unique_addresses_shared_between_chats(db):
    db.add_subscription("tg", "1", "10", "5", "A")
    db.add_subscription("tg", "2", "10", "5", "A")
    db.add_subscription("tg", "2", "11", "7", "B")
    assert sorted(db.event_addresses()) == [("10", "5"), ("11", "7")]
    assert len(db.event_subscribers("10", "5")) == 2


def test_events_off_excluded_from_polling(db):
    sub, _ = db.add_subscription("tg", "1", "10", "5", "A")
    db.toggle(sub.id, "events")
    assert db.event_addresses() == []


def test_toggle_rejects_bad_field(db):
    sub, _ = db.add_subscription("tg", "1", "10", "5", "A")
    with pytest.raises(ValueError):
        db.toggle(sub.id, "label; DROP TABLE subscriptions")


def test_state_roundtrip(db):
    assert db.get_state("1", "2") is None
    db.save_state("1", "2", "text")
    db.save_state("1", "2", "text2")
    assert db.get_state("1", "2").last_text == "text2"


def test_delete_target_keeps_config_entries(db):
    db.add_subscription("tg", "1", "10", "5", "bot", source="bot")
    db.sync_config_subscriptions([{"channel": "tg", "target": "1", "street_id": "11", "house": "1",
                                   "label": "cfg", "events": True, "graphs": False, "silent": True}])
    assert db.delete_target("tg", "1") == 1
    assert [s.label for s in db.list_subscriptions("tg", "1")] == ["cfg"]


def test_sync_config_removes_stale(db):
    entry = {"channel": "tg", "target": "1", "street_id": "11", "house": "1",
             "label": "cfg", "events": True, "graphs": True, "silent": False}
    db.sync_config_subscriptions([entry])
    assert db.list_subscriptions("tg", "1")[0].graphs is True
    db.sync_config_subscriptions([])
    assert db.list_subscriptions("tg", "1") == []


def test_graph_targets_distinct(db):
    db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    db.add_subscription("tg", "1", "11", "5", "B", graphs=True)
    assert len(db.graph_targets()) == 1


def test_move_target_merges_duplicates(db):
    db.add_subscription("tg", "old", "10", "5", "A")
    db.add_subscription("tg", "new", "10", "5", "A")       # така адреса на новому ID вже є
    db.add_subscription("tg", "old", "11", "6", "B")
    db.move_target("tg", "old", "new")
    assert db.list_subscriptions("tg", "old") == []
    assert sorted((s.street_id, s.house) for s in db.list_subscriptions("tg", "new")) == [("10", "5"), ("11", "6")]

def test_graph_images_follow_graphs_by_default_and_toggle_separately(db):
    both, _ = db.add_subscription("tg", "1", "10", "5", "A", graphs=True)
    assert both.graphs and both.graph_images
    text_only, _ = db.add_subscription("tg", "2", "10", "5", "A", graphs=True, graph_images=False)
    assert [t.target for t in db.graph_targets()] == ["1", "2"]
    assert [t.target for t in db.graph_targets(images=True)] == ["1"]
    assert db.toggle(text_only.id, "graph_images").graph_images is True
    assert db.toggle(both.id, "graphs").graphs is False and db.get_subscription(both.id).graph_images is True


def test_migration_copies_graphs_into_graph_images(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)                    # база старої версії: без graph_images
    conn.execute("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL DEFAULT 'tg', "
                 "target TEXT NOT NULL, street_id TEXT NOT NULL, house TEXT NOT NULL, label TEXT NOT NULL, "
                 "events INTEGER NOT NULL DEFAULT 1, graphs INTEGER NOT NULL DEFAULT 0, "
                 "silent INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'bot', owner_id INTEGER, "
                 "created_at INTEGER NOT NULL, UNIQUE (channel, target, street_id, house))")
    conn.execute("INSERT INTO subscriptions (target,street_id,house,label,graphs,created_at) VALUES ('1','10','5','A',1,0)")
    conn.execute("INSERT INTO subscriptions (target,street_id,house,label,graphs,created_at) VALUES ('2','10','5','A',0,0)")
    conn.commit()
    conn.close()
    d = Database(path)
    subs = {s.target: s for s in d.all_subscriptions()}
    assert subs["1"].graphs and subs["1"].graph_images            # хто мав «Графік», лишається з обома
    assert not subs["2"].graphs and not subs["2"].graph_images
    d.close()


def test_config_graph_images(db):
    base = {"channel": "tg", "target": "1", "street_id": "10", "house": "5", "label": "cfg",
            "events": True, "graphs": True, "silent": True}
    db.sync_config_subscriptions([{**base, "graph_images": False}])
    s = db.list_subscriptions("tg", "1")[0]
    assert s.graphs and not s.graph_images
    db.sync_config_subscriptions([base])                           # без ключа — як graphs
    assert db.list_subscriptions("tg", "1")[0].graph_images