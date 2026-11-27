import logging

import pytest

from hoe_notifier import logview
from hoe_notifier.logview import LogBuffer, page, redact

from .test_bot import last_send_message, make_env  # noqa: F401  (фікстури/хелпери бота)


def fill(buf: LogBuffer, n: int, level=logging.INFO, name="hoe_notifier.monitor", text="запис {}"):
    logger = logging.getLogger(name)
    for i in range(n):
        rec = logger.makeRecord(name, level, __file__, 1, text.format(i), (), None)
        buf.handle(rec)


def test_redact_tokens_and_secrets():
    s = ("token 123456789:AAExampleTokenFromBotFather_abcdefghij bot987654321:"
         "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA xoxb-1234-5678-abcdEFGH Bearer abcdefghijklmnopqrstuv key=MYSECRETKEY123")
    out = redact(s, ("MYSECRETKEY123",))
    assert "AAExample" not in out and "AAAAAAAA" not in out and "xoxb-1234" not in out
    assert "abcdefghijklmnop" not in out and "MYSECRETKEY123" not in out
    assert "Bearer ***" in out


def test_buffer_redacts_and_keeps_traceback():
    buf = LogBuffer(secrets=("supersecretvalue",))
    log = logging.getLogger("t")
    try:
        raise ValueError("boom supersecretvalue")
    except ValueError:
        import sys
        rec = log.makeRecord("t", logging.ERROR, __file__, 1, "fail %s", ("x",), sys.exc_info())
    buf.handle(rec)
    r = buf.snapshot()[0]
    assert r.text.startswith("fail x") and "Traceback" in r.text and "supersecretvalue" not in r.text


def test_buffer_capacity_and_seq():
    buf = LogBuffer(capacity=5)
    fill(buf, 8)
    snap = buf.snapshot()
    assert len(snap) == 5 and [r.seq for r in snap] == [4, 5, 6, 7, 8]


def test_page_navigation_and_filter():
    buf = LogBuffer()
    fill(buf, 100)
    fill(buf, 3, level=logging.WARNING, text="увага {}")
    recs = buf.snapshot()

    latest = page(recs, "a", max_records=10)
    assert [r.seq for r in latest.records] == list(range(94, 104)) and latest.has_older and not latest.has_newer

    older = page(recs, "a", anchor=94, direction="o", max_records=10)
    assert [r.seq for r in older.records] == list(range(84, 94)) and older.has_newer

    newer = page(recs, "a", anchor=older.records[-1].seq, direction="n", max_records=10)
    assert newer.records[0].seq == 94

    warn = page(recs, "w")
    assert [r.text for r in warn.records] == ["увага 0", "увага 1", "увага 2"] and warn.total == 3
    assert page(recs, "e").records == []


def test_page_respects_budget_with_long_records():
    buf = LogBuffer()
    fill(buf, 20, text="x" * 2000 + "{}")
    pg = page(buf.snapshot(), "a")
    shown = "\n".join(logview.format_record(r) for r in pg.records)
    assert len(shown) < 3800 and "обрізано" in shown


def test_file_roundtrip_and_preload(tmp_path):
    path = tmp_path / "logs" / "hoe.log"
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logview._buffer)
    try:
        buf = logview.setup_logging("INFO", log_file=path, secrets=("hidden-secret-123",))
        lg = logging.getLogger("hoe_notifier.test")
        lg.info("перший hidden-secret-123")
        try:
            1 / 0
        except ZeroDivisionError:
            lg.exception("ой")
        for h in root.handlers:
            h.flush()
        content = path.read_text(encoding="utf-8")
        assert "перший ***" in content and "ZeroDivisionError" in content
        assert buf.file_tail() is not None and b"ZeroDivisionError" in buf.file_tail()

        # «перезапуск»: новий буфер підтягує записи з файлу, разом із traceback
        fresh = LogBuffer()
        assert fresh.preload_file(path) >= 2
        texts = [r.text for r in fresh.snapshot()]
        assert any(t.startswith("перший ***") for t in texts)
        err = [r for r in fresh.snapshot() if r.levelno == logging.ERROR][-1]
        assert "ZeroDivisionError" in err.text and err.name == "hoe_notifier.test"
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            if h not in saved[0]:
                h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
        logview._buffer = saved[2]


# ------------------------------------------------------------------ бот
@pytest.fixture
def buffer(monkeypatch):
    buf = LogBuffer()
    monkeypatch.setattr(logview, "_buffer", buf)
    return buf


def buttons(m):
    return [(b.text, b.callback_data) for row in m.reply_markup.inline_keyboard for b in row]


async def test_admin_panel_has_log_button(buffer):
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/admin"))
    assert ("📜 Журнал", "log:open") in buttons(last_send_message(session))
    db.close()


async def test_logs_command_shows_records_and_filters(buffer):
    db, hoe, session, send, msg, cb = make_env()
    fill(buffer, 3, text="звичайний <тег> {}")
    fill(buffer, 1, level=logging.ERROR, text="зламалось")
    await send(msg("/logs"))
    m = last_send_message(session)
    assert "звичайний &lt;тег&gt; 2" in m.text and "зламалось" in m.text and "<pre>" in m.text
    assert ("📄 Файлом", "log:file:a") in buttons(m)

    await send(msg("/logs error"))
    m = last_send_message(session)
    assert "зламалось" in m.text and "звичайний" not in m.text

    await send(msg("/logs nonsense"))
    assert "Формат" in last_send_message(session).text
    db.close()


async def test_logs_paging_edits_message(buffer):
    db, hoe, session, send, msg, cb = make_env()
    fill(buffer, 200)
    await send(cb("log:open"))
    first = last_send_message(session)
    older = [d for t, d in buttons(first) if t == "◀ Старіші"][0]
    await send(cb(older))
    edit = [m for m in session.methods if type(m).__name__ == "EditMessageText"][-1]
    assert "запис 199" not in edit.text and "Новіші ▶" in [t for t, _ in buttons(edit)]
    db.close()


async def test_logs_file_download(buffer):
    db, hoe, session, send, msg, cb = make_env()
    fill(buffer, 2, level=logging.WARNING, text="увага {}")
    await send(cb("log:file:w"))
    doc = [m for m in session.methods if type(m).__name__ == "SendDocument"][-1]
    assert doc.document.filename.endswith("-w.log") and b"WARNING" in doc.document.data
    db.close()


async def test_logs_hidden_from_non_admins_and_groups(buffer):
    db, hoe, session, send, msg, cb = make_env(admin_ids=frozenset())
    fill(buffer, 1)
    await send(msg("/logs"))
    assert session.methods == []
    await send(cb("log:open"))
    assert all(type(m).__name__ != "SendMessage" for m in session.methods)
    db.close()
    db, hoe, session, send, msg, cb = make_env()
    await send(msg("/logs", chat_type="supergroup", chat_id=-5))
    assert session.methods == []
    db.close()