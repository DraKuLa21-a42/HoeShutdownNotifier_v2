"""SQLite-сховище: підписки на адреси, останній стан, службові значення."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT    NOT NULL DEFAULT 'tg',      -- tg | slack
    target     TEXT    NOT NULL,                   -- chat_id (tg) або id каналу (slack)
    street_id  TEXT    NOT NULL,
    house      TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    events     INTEGER NOT NULL DEFAULT 1,         -- слати погодинні відключення
    graphs     INTEGER NOT NULL DEFAULT 0,         -- слати картинку з графіком
    silent     INTEGER NOT NULL DEFAULT 1,         -- без звуку (лише Telegram)
    queue      TEXT,                               -- підчерга ГПВ (1.1 … 6.2); NULL — усі черги
    source     TEXT    NOT NULL DEFAULT 'bot',     -- bot | config
    owner_id   INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE (channel, target, street_id, house)
);
CREATE TABLE IF NOT EXISTS address_state (
    street_id  TEXT NOT NULL,
    house      TEXT NOT NULL,
    last_text  TEXT NOT NULL,                      -- текст відключень або NO_OUTAGES
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (street_id, house)
);
CREATE TABLE IF NOT EXISTS slack_bots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,                   -- Telegram-користувач, який підключив бота
    token      TEXT    NOT NULL,                   -- токен бота Slack (xoxb-…)
    team       TEXT    NOT NULL DEFAULT '',        -- назва воркспейсу
    bot_name   TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (owner_id, token)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

TOGGLE_FIELDS = {"events", "graphs", "silent"}


@dataclass(frozen=True)
class Subscription:
    id: int
    channel: str
    target: str
    street_id: str
    house: str
    label: str
    events: bool
    graphs: bool
    silent: bool
    source: str
    owner_id: int | None
    queue: str | None = None


@dataclass(frozen=True)
class SlackBot:
    id: int
    owner_id: int
    token: str
    team: str
    bot_name: str

    @property
    def masked(self) -> str:
        return f"xoxb-…{self.token[-4:]}"


@dataclass(frozen=True)
class GraphTarget:
    """Чат/канал, якому надсилати графік. `queues` — об'єднання черг його адрес; None — без фільтра."""
    channel: str
    target: str
    silent: bool
    queues: frozenset[str] | None


@dataclass(frozen=True)
class AddressState:
    street_id: str
    house: str
    last_text: str
    updated_at: int


def _slack_bot(row: sqlite3.Row) -> SlackBot:
    return SlackBot(id=row["id"], owner_id=row["owner_id"], token=row["token"],
                    team=row["team"], bot_name=row["bot_name"])


def _sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"], channel=row["channel"], target=row["target"],
        street_id=row["street_id"], house=row["house"], label=row["label"],
        events=bool(row["events"]), graphs=bool(row["graphs"]), silent=bool(row["silent"]),
        source=row["source"], owner_id=row["owner_id"], queue=row["queue"],
    )


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Додає колонки, яких немає в базах, створених старішими версіями."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(subscriptions)")}
        if "queue" not in cols:
            self._conn.execute("ALTER TABLE subscriptions ADD COLUMN queue TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- підписки -------------------------------------------------------
    def add_subscription(
        self, channel: str, target: str, street_id: str, house: str, label: str,
        *, events: bool = True, graphs: bool = False, silent: bool = True,
        source: str = "bot", owner_id: int | None = None, queue: str | None = None,
    ) -> tuple[Subscription, bool]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO subscriptions "
                "(channel,target,street_id,house,label,events,graphs,silent,source,owner_id,created_at,queue) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (channel, str(target), street_id, house, label, int(events), int(graphs),
                 int(silent), source, owner_id, int(time.time()), queue),
            )
            self._conn.commit()
            created = cur.rowcount > 0
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE channel=? AND target=? AND street_id=? AND house=?",
                (channel, str(target), street_id, house),
            ).fetchone()
        return _sub(row), created

    def get_subscription(self, sub_id: int) -> Subscription | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        return _sub(row) if row else None

    def list_subscriptions(self, channel: str, target: str) -> list[Subscription]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE channel=? AND target=? ORDER BY id",
                (channel, str(target)),
            ).fetchall()
        return [_sub(r) for r in rows]

    # --- Slack-боти користувачів ------------------------------------------
    def add_slack_bot(self, owner_id: int, token: str, team: str, bot_name: str) -> tuple[SlackBot, bool]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO slack_bots (owner_id,token,team,bot_name,created_at) VALUES (?,?,?,?,?)",
                (owner_id, token, team, bot_name, int(time.time())))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM slack_bots WHERE owner_id=? AND token=?", (owner_id, token)).fetchone()
        return _slack_bot(row), cur.rowcount > 0

    def get_slack_bot(self, bot_id: int) -> SlackBot | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM slack_bots WHERE id=?", (bot_id,)).fetchone()
        return _slack_bot(row) if row else None

    def list_slack_bots(self, owner_id: int) -> list[SlackBot]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM slack_bots WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
        return [_slack_bot(r) for r in rows]

    def stats_for_slack_bot(self, bot_id: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE channel='slack' AND target LIKE ?",
                (f"{bot_id}/%",)).fetchone()[0]

    def delete_slack_bot(self, bot_id: int) -> int:
        """Видаляє бота разом з усіма підписками на його канали. Повертає кількість підписок."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE channel='slack' AND target LIKE ?", (f"{bot_id}/%",))
            self._conn.execute("DELETE FROM slack_bots WHERE id=?", (bot_id,))
            self._conn.commit()
        return cur.rowcount

    def all_subscriptions(self) -> list[Subscription]:
        """Усі підписки, згруповані за чатом (для адміністратора)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM subscriptions ORDER BY channel, target, id").fetchall()
        return [_sub(r) for r in rows]

    def find_subscription(self, channel: str, target: str, street_id: str, house: str) -> Subscription | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE channel=? AND target=? AND street_id=? AND house=?",
                (channel, str(target), street_id, house),
            ).fetchone()
        return _sub(row) if row else None

    def list_by_owner(self, owner_id: int, channel: str, exclude_target: str) -> list[Subscription]:
        """Підписки, створені користувачем в інших чатах/каналах (щоб керувати ними з приватного чату).

        Це всі канали доставки (Telegram і Slack), крім самого чату `channel`/`exclude_target`.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE owner_id=? AND NOT (channel=? AND target=?) AND source='bot' "
                "ORDER BY channel, target, id",
                (owner_id, channel, str(exclude_target)),
            ).fetchall()
        return [_sub(r) for r in rows]

    def count_for_target(self, channel: str, target: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE channel=? AND target=?",
                (channel, str(target)),
            ).fetchone()[0]

    def delete_subscription(self, sub_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def set_queue(self, sub_id: int, queue: str | None) -> Subscription | None:
        """Задає підчергу адреси (None — усі черги)."""
        with self._lock:
            self._conn.execute("UPDATE subscriptions SET queue=? WHERE id=?", (queue, sub_id))
            self._conn.commit()
        return self.get_subscription(sub_id)

    def toggle(self, sub_id: int, field: str) -> Subscription | None:
        if field not in TOGGLE_FIELDS:
            raise ValueError(field)
        with self._lock:
            self._conn.execute(f"UPDATE subscriptions SET {field} = 1 - {field} WHERE id=?", (sub_id,))
            self._conn.commit()
        return self.get_subscription(sub_id)

    def delete_target(self, channel: str, target: str) -> int:
        """Прибирає підписки, створені через бота, для чату, який недоступний (заблокував бота тощо)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE channel=? AND target=? AND source='bot'",
                (channel, str(target)),
            )
            self._conn.commit()
        return cur.rowcount

    def move_target(self, channel: str, old: str, new: str) -> None:
        """Група стала супергрупою → змінився chat_id. Переносимо підписки на новий ID."""
        with self._lock:
            self._conn.execute(
                "UPDATE OR IGNORE subscriptions SET target=? WHERE channel=? AND target=?",
                (str(new), channel, str(old)))
            # якщо на новому ID така адреса вже була — залишок на старому ID зайвий
            self._conn.execute("DELETE FROM subscriptions WHERE channel=? AND target=?", (channel, str(old)))
            self._conn.commit()

    def event_addresses(self) -> list[tuple[str, str]]:
        """Унікальні адреси, на які є хоча б одна підписка з events=1 (один запит на адресу)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT street_id, house FROM subscriptions WHERE events=1"
            ).fetchall()
        return [(r["street_id"], r["house"]) for r in rows]

    def event_subscribers(self, street_id: str, house: str) -> list[Subscription]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE events=1 AND street_id=? AND house=?",
                (street_id, house),
            ).fetchall()
        return [_sub(r) for r in rows]

    def graph_targets(self) -> list[GraphTarget]:
        """Чати, які просять графік: по одному запису на (channel, target).

        Черги — об'єднання черг усіх їхніх адрес із ввімкненим графіком; якщо хоч одна адреса без черги,
        фільтра немає (чат отримує графік повністю).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, target, silent, queue FROM subscriptions WHERE graphs=1 ORDER BY id"
            ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            grouped.setdefault((r["channel"], r["target"]), []).append(r)
        return [
            GraphTarget(
                channel=channel, target=target,
                silent=all(bool(r["silent"]) for r in group),
                queues=None if any(r["queue"] is None for r in group) else frozenset(r["queue"] for r in group),
            )
            for (channel, target), group in grouped.items()
        ]

    def chat_queues(self, channel: str, target: str) -> frozenset[str] | None:
        """Черги всіх адрес чату; None — фільтра немає (немає адрес або хоч в одній черга не задана)."""
        subs = self.list_subscriptions(channel, target)
        if not subs or any(s.queue is None for s in subs):
            return None
        return frozenset(s.queue for s in subs)

    def sync_config_subscriptions(self, entries: list[dict]) -> None:
        """Приводить підписки з source='config' у відповідність до addresses.toml."""
        keep: set[tuple[str, str, str, str]] = set()
        for e in entries:
            key = (e["channel"], str(e["target"]), e["street_id"], e["house"])
            keep.add(key)
            with self._lock:
                self._conn.execute(
                    "INSERT INTO subscriptions "
                    "(channel,target,street_id,house,label,events,graphs,silent,source,created_at,queue) "
                    "VALUES (?,?,?,?,?,?,?,?, 'config', ?, ?) "
                    "ON CONFLICT(channel,target,street_id,house) DO UPDATE SET "
                    "label=excluded.label, events=excluded.events, graphs=excluded.graphs, "
                    "silent=excluded.silent, queue=excluded.queue, source='config'",
                    (*key[:2], key[2], key[3], e["label"], int(e["events"]), int(e["graphs"]),
                     int(e["silent"]), int(time.time()), e.get("queue")),
                )
                self._conn.commit()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, channel, target, street_id, house FROM subscriptions WHERE source='config'"
            ).fetchall()
            for r in rows:
                if (r["channel"], r["target"], r["street_id"], r["house"]) not in keep:
                    self._conn.execute("DELETE FROM subscriptions WHERE id=?", (r["id"],))
            self._conn.commit()

    # --- стан адрес -----------------------------------------------------
    def get_state(self, street_id: str, house: str) -> AddressState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM address_state WHERE street_id=? AND house=?", (street_id, house)
            ).fetchone()
        return AddressState(**dict(row)) if row else None

    def save_state(self, street_id: str, house: str, last_text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO address_state (street_id,house,last_text,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(street_id,house) DO UPDATE SET last_text=excluded.last_text, "
                "updated_at=excluded.updated_at",
                (street_id, house, last_text, int(time.time())),
            )
            self._conn.commit()

    # --- службове -------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            c = self._conn
            return {
                "subscriptions": c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
                "with_queue": c.execute("SELECT COUNT(*) FROM subscriptions WHERE queue IS NOT NULL").fetchone()[0],
                "targets": c.execute(
                    "SELECT COUNT(*) FROM (SELECT DISTINCT channel,target FROM subscriptions)").fetchone()[0],
                "addresses": c.execute(
                    "SELECT COUNT(*) FROM (SELECT DISTINCT street_id,house FROM subscriptions)").fetchone()[0],
            }