from datetime import date, datetime
from pathlib import Path

from hoe_notifier.schedule import (
    KYIV, QUEUES, changes, fmt_intervals, parse_days, slots_from_json, slots_hash, slots_to_json, status_now,
)

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 27)


def page(*sections: str) -> str:
    return "<div class='post'>" + "<hr>".join(sections) + "<hr><p>Перевірити чергу</p></div>"


def test_extra_hours_are_merged_into_main_schedule():
    days = parse_days((FIXTURES / "graph_extra_hours_27_11.html").read_text(), TODAY)
    assert [d.day for d in days] == [date(2026, 11, 27)]
    d = days[0]
    assert d.updated == "12:30"
    assert fmt_intervals(d.slots["6.1"]) == "13:00–17:00, 20:00–22:00"       # 13-14 додано до 14-17
    assert fmt_intervals(d.slots["1.1"]) == "06:00–08:00, 11:00–14:00, 17:00–20:00, 22:00–24:00"
    assert set(d.slots) == set(QUEUES)


def test_single_paragraph_with_br_and_word_date():
    days = parse_days((FIXTURES / "graph_text_page.html").read_text(), TODAY)
    assert days[0].day == date(2026, 11, 7) and days[0].updated is None
    assert fmt_intervals(days[0].slots["3.2"]) == "04:00–08:00, 13:00–18:00"


def test_several_dates_newest_version_wins():
    html = page(
        "<p>Графік ГПВ на 28 листопада:</p><p>Підчерга 1.1 – 10:00-12:00.</p>",
        "<p>Оновлено графік ГПВ на 27 листопада (оновлено, 23:40):</p><p>Підчерга 1.1 – 00:00-02:00.</p>",
        "<p>Графік ГПВ на 27.11.2026:</p><p>Підчерга 1.1 – 06:00-09:00.</p>",       # старіша версія
    )
    days = {d.day: d for d in parse_days(html, TODAY)}
    assert list(days) == [date(2026, 11, 28), date(2026, 11, 27)]
    assert fmt_intervals(days[date(2026, 11, 27)].slots["1.1"]) == "00:00–02:00"
    assert days[date(2026, 11, 27)].updated == "23:40"


def test_extra_hours_older_than_full_update_are_ignored():
    html = page(
        "<p>Оновлено графік ГПВ на 27 листопада (оновлено, 18:00):</p><p>Підчерга 2.1 – 10:00-11:00.</p>",
        "<p>Оновлено ГПВ на 27 листопада (оновлено, 12:30). Додаткові години:</p><p>Підчерга 2.1 - 20:00-21:00</p>",
        "<p>Графік ГПВ на 27.11.2026:</p><p>Підчерга 2.1 – 06:00-07:00.</p>",
    )
    d = parse_days(html, TODAY)[0]
    assert fmt_intervals(d.slots["2.1"]) == "10:00–11:00" and d.updated == "18:00"


def test_whole_queue_half_hours_and_midnight():
    html = page("<p>ГПВ на 28 листопада:</p><p>Черга 3 – 08:30-10:00; 22:00-00:00.</p>")
    d = parse_days(html, TODAY)[0]
    assert fmt_intervals(d.slots["3.1"]) == fmt_intervals(d.slots["3.2"]) == "08:30–10:00, 22:00–24:00"
    assert not any(d.slots["1.1"])


def test_year_is_inferred_across_new_year():
    html = page("<p>Графік ГПВ на 1 січня:</p><p>Підчерга 1.1 – 10:00-12:00.</p>")
    assert parse_days(html, date(2026, 12, 31))[0].day == date(2027, 1, 1)


def test_day_without_outages():
    html = page("<p>28 листопада графіки погодинних відключень не застосовуватимуться.</p>")
    d = parse_days(html, TODAY)[0]
    assert d.day == date(2026, 11, 28) and not any(any(s) for s in d.slots.values())


def test_page_without_schedule():
    assert parse_days(page("<p>Перевірити чергу</p>"), TODAY) == []


def test_hash_ignores_wording_and_json_roundtrip():
    a = parse_days(page("<p>ГПВ на 28 листопада:</p><p>Підчерга 1.1 – 10:00-12:00.</p>"), TODAY)[0]
    b = parse_days(page("<p>Графік на 28.11.2026 р.</p><p>Підчерга 1.1 — 10:00 - 12:00</p>"), TODAY)[0]
    assert a.hash == b.hash
    assert slots_hash(slots_from_json(slots_to_json(a.slots))) == a.hash


def test_status_and_changes():
    d = parse_days(page("<p>ГПВ на 27 листопада:</p><p>Підчерга 1.1 – 10:00-12:00; 17:00-20:00.</p>"), TODAY)[0]
    s = d.slots["1.1"]
    at = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=KYIV)  # noqa: E731
    assert status_now(s, at(9, 15)) == "🟢 світло є за графіком · планове відключення о 10:00 (через 45 хв)"
    assert status_now(s, at(11, 0), with_left=False) == "🔴 світла немає за графіком · планове увімкнення о 12:00"
    assert status_now(s, at(21, 0)) == "🟢 світло є за графіком · до кінця доби відключень не заплановано"
    new = parse_days(page("<p>ГПВ на 27 листопада:</p><p>Підчерга 1.1 – 09:00-12:00.</p>"), TODAY)[0].slots["1.1"]
    assert changes(s, new) == "+09:00–10:00, −17:00–20:00"
    assert changes(s, s) == ""