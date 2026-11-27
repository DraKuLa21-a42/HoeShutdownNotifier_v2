from pathlib import Path

from hoe_notifier.hoe import (
    Option, ParseStatus, house_key, match_house, normalize_blocks, parse_events, parse_houses, parse_image_url,
    parse_options, parse_text_schedule, suggest_houses,
)

FULL = """
<table><thead><tr><th>Вид робіт</th><th>Черга (ГПВ)</th><th>Початок</th></tr></thead>
<tbody>
<tr><td>Графік погодинних відключень</td><td>2.2</td><td>26.11.2025 14:00</td></tr>
<tr><td>Аварійне</td><td>-</td><td>26.11.2025 18:00</td></tr>
</tbody></table>
"""


def test_full_table():
    p = parse_events(FULL)
    assert p.status is ParseStatus.OUTAGES
    assert p.text.splitlines()[0] == "Вид робіт: Графік погодинних відключень"
    assert "Черга (ГПВ): 2.2" in p.text
    assert "\n\n" in p.text  # рядки розділені порожнім рядком


def test_no_thead_no_tbody_does_not_crash():
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES
    assert p.text == "A: 1\nB: 2"


def test_mismatched_cells_not_lost():
    html = "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>x</td></tr></tbody></table>"
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES and p.text == "x"


def test_no_outages_message():
    p = parse_events("<div>Відключень за вашою адресою не заплановано</div>")
    assert p.status is ParseStatus.NONE


def test_unknown_layout():
    assert parse_events("<html><body>Internal error</body></html>").status is ParseStatus.UNKNOWN


def test_prefers_table_shutdowns_class():
    html = ("<table class='other'><tr><td>сміття</td></tr></table>"
            "<table class='table table-shutdowns'><thead><tr><th>Вид</th></tr></thead>"
            "<tbody><tr><td>Планове</td></tr></tbody></table>")
    assert parse_events(html).text == "Вид: Планове"


def test_real_six_column_layout():
    html = """<table class="table table-shutdowns"><thead><tr>
      <th>Вид робіт</th><th>Тип відключення</th><th>Черга ГПВ</th><th>Черга ГАВ</th><th>Початок</th><th>Кінець</th>
    </tr></thead><tbody><tr><td>Планові</td><td>Відключення</td><td>2.1</td><td>-</td>
      <td>26.11.2025 14:00</td><td>26.11.2025 18:00</td></tr></tbody></table>"""
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES
    assert "Тип відключення: Відключення" in p.text and "Кінець: 26.11.2025 18:00" in p.text


def test_parse_options_select2():
    assert parse_options([{"id": 5, "text": "Давидківці"}]) == [Option("5", "Давидківці")]
    assert parse_options({"results": [{"id": "7", "text": " вул. Подільська "}]}) == [Option("7", "вул. Подільська")]
    assert parse_options({"unexpected": 1}) == []
    assert parse_options([{"id": 1}, {"text": "без id"}, "рядок", {"id": "", "text": "x"}]) == []


def test_parse_houses_variants():
    assert parse_houses([1, "2", " 3А ", "2", None, ""]) == ["1", "2", "3А"]
    assert parse_houses({"results": [{"text": "5"}, {"id": 6}]}) == ["5", "6"]
    assert parse_houses("помилка") == []


HOUSES = ["1", "12А", "12/1", "13", "120", "5Б"]


def test_house_match_is_case_and_alphabet_insensitive():
    assert match_house(HOUSES, "12A") == "12А"     # латинська A → кирилична А
    assert match_house(HOUSES, "12 а") == "12А"
    assert match_house(HOUSES, "5б") == "5Б"
    assert match_house(HOUSES, "13") == "13"
    assert match_house(HOUSES, "99") is None
    assert house_key("12B") == house_key("12В")


def test_house_suggestions():
    assert suggest_houses(HOUSES, "12") == ["12А", "12/1", "120"]       # за префіксом
    assert suggest_houses(HOUSES, "12В") == ["12А", "12/1"]               # те саме число, інша літера
    assert suggest_houses(HOUSES, "7") == []
    assert suggest_houses(HOUSES, "") == []
    assert len(suggest_houses([str(i) for i in range(100, 200)], "1", limit=8)) == 8


# ---------------------------------------------------------------- текстовий графік
FIXTURES = Path(__file__).parent / "fixtures"
REAL_PAGE = (FIXTURES / "graph_text_page.html").read_text(encoding="utf-8")


def test_text_schedule_from_real_page():
    text = parse_text_schedule(REAL_PAGE)
    lines = text.splitlines()
    assert lines[0].startswith("За розпорядженням НЕК «Укренерго» 07 листопада")
    assert lines[0].endswith("(ГПВ):")
    assert "Підчерга 1.1 – 00:00-04:00; 09:00-13:00; 18:00-21:00." in lines
    assert lines[-1] == "Підчерга 6.2 – 08:00-13:00; 17:00-22:00."
    assert sum(l.startswith("Підчерга") for l in lines) == 12


def test_text_schedule_keeps_group_separation():
    text = parse_text_schedule(REAL_PAGE)
    groups = text.split("\n\n")
    assert len(groups) == 7                                   # заголовок + 6 черг
    assert groups[1].count("\n") == 1                         # у групі дві підчерги
    assert "\n\n\n" not in text and not text.endswith("\n")


def test_text_schedule_ignores_unrelated_paragraphs():
    html = ("<div class='post'><p>Перевірити чергу</p><p>Кол-центр 0 800 50 50 62</p>"
            "<p>Підчерга 1.1 – 00:00-04:00.</p></div>")
    assert parse_text_schedule(html) == "Підчерга 1.1 – 00:00-04:00."


def test_text_schedule_absent():
    assert parse_text_schedule("<div class='post'><p>Графіки не застосовуються.</p></div>") is None
    assert parse_text_schedule("<html></html>") is None
    # час без згадки черг — не графік
    assert parse_text_schedule("<div class='post'><p>Кол-центр працює 08:00-17:00</p></div>") is None


def test_text_schedule_without_post_wrapper():
    html = "<body><p>Черга 2: 06:00-10:00<br>Черга 3: 10:00-14:00</p></body>"
    assert parse_text_schedule(html) == "Черга 2: 06:00-10:00\nЧерга 3: 10:00-14:00"


def test_text_schedule_other_dash_and_nbsp():
    html = "<div class='post'><p>Підчерга\xa01.1 — 00:00–04:00;<br> 09:00–13:00</p></div>"
    assert parse_text_schedule(html) == "Підчерга 1.1 — 00:00–04:00;\n09:00–13:00"


def test_normalize_blocks():
    assert normalize_blocks("  a \n\n\n\n b\n\n") == "a\n\nb"
    assert normalize_blocks("") == ""


def test_image_url_not_found_on_text_page():
    assert parse_image_url(REAL_PAGE, "https://hoe.com.ua") is None


def test_image_url_resolves_relative_path():
    assert parse_image_url('<img alt="ГПВ" src="/Content/a.png">', "https://hoe.com.ua/") == \
        "https://hoe.com.ua/Content/a.png"


# ------------------------------------------- переноси рядків у вихідному HTML не мають ділити речення
def test_text_schedule_ignores_line_wraps_in_html_source():
    """Редактор/форматер може перенести довгий рядок у файлі — це не розрив рядка на сторінці."""
    expected = parse_text_schedule(REAL_PAGE)
    variants = {
        "wrap_mid_sentence": REAL_PAGE.replace("застосовані графіки погодинних", "застосовані графіки\nпогодинних"),
        "wrap_with_indent": REAL_PAGE.replace("на Хмельниччині будуть", "на Хмельниччині\n        \t будуть"),
        "crlf": REAL_PAGE.replace("\n", "\r\n"),
        "wrap_everywhere": REAL_PAGE.replace(" ", "\n"),
    }
    for name, html in variants.items():
        assert parse_text_schedule(html) == expected, name
    first = expected.splitlines()[0]
    assert first.endswith("(ГПВ):") and "графіки погодинних відключень" in first


def test_text_schedule_br_is_the_only_line_break():
    html = "<div class='post'><p>Підчерга 1.1 – 00:00-04:00;\n   09:00-13:00.<br>\nПідчерга 1.2 – 02:00-06:00.</p></div>"
    assert parse_text_schedule(html) == "Підчерга 1.1 – 00:00-04:00; 09:00-13:00.\nПідчерга 1.2 – 02:00-06:00."


def test_text_schedule_double_br_still_separates_groups_after_wrapping():
    html = "<div class='post'><p>Заголовок черг:<br>\n<br>\nПідчерга 1.1 – 00:00-04:00.<br>\n<br>\nПідчерга 2.1 – 04:00-08:00.</p></div>"
    assert parse_text_schedule(html) == "Заголовок черг:\n\nПідчерга 1.1 – 00:00-04:00.\n\nПідчерга 2.1 – 04:00-08:00."

# ------------------------------------- графік кількома абзацами <p> (формат сайту з 21.11.2026)
MULTI_P_PAGE = (
    "<div class='post'>"
    "<p>За розпорядженням НЕК «Укренерго» оновлено графік погодинних відключень (ГПВ) на 21 листопада (оновлено 23:00).</p>\n"
    "<p>Години відсутності електроенергії:</p>\n"
    "<p>Підчерга 1.1 – 00:00-02:00; 06:00-10:00.<br>\nПідчерга 1.2 – 06:00-10:00; 16:00-20:00.</p>\n"
    "<p>&nbsp;</p>\n"
    "<p>Підчерга 2.1 – 02:00-06:00.<br>Підчерга 2.2 – 06:00-10:00.</p>\n"
    "<p>Підчерга 3.1 – 00:00-02:00.<br>Підчерга 3.2 – 02:00-06:00.</p>\n"
    "<p>Підчерга 4.1 – 02:00-06:00.<br>Підчерга 4.2 – 02:00-06:00.</p>\n"
    "<p>Підчерга 5.1 – 00:00-02:00.<br>Підчерга 5.2 – 00:00-02:00.</p>\n"
    "<p>Підчерга 6.1 – 02:00-06:00.<br>Підчерга 6.2 – 00:00-02:00; 06:00-10:00; 13:00-16:00.</p>\n"
    "<p>21.11.2026 р.</p>\n"
    "<p>За розпорядженням НЕК «Укренерго» графік погодинних відключень (ГПВ) на 21 листопада.</p>\n"
    "<p>Години відсутності електроенергії:</p>\n"
    "<p>Підчерга 1.1 – 00:00-04:00.<br>Підчерга 1.2 – 04:00-08:00.</p>\n"
    "<hr><p>Перевірити чергу</p></div>"
)


def test_text_schedule_split_into_paragraphs_keeps_all_queues():
    text = parse_text_schedule(MULTI_P_PAGE)
    lines = text.splitlines()
    assert lines[0].startswith("За розпорядженням НЕК «Укренерго» оновлено графік") and lines[0].endswith("(оновлено 23:00).")
    assert "Години відсутності електроенергії:" in lines
    assert sum(l.startswith("Підчерга") for l in lines) == 12
    assert lines[-1] == "Підчерга 6.2 – 00:00-02:00; 06:00-10:00; 13:00-16:00."
    groups = text.split("\n\n")
    assert len(groups) == 8                                   # 2 заголовки + 6 груп черг
    assert "\n\n\n" not in text


def test_text_schedule_split_into_paragraphs_takes_only_first_schedule():
    text = parse_text_schedule(MULTI_P_PAGE)
    assert "21.11.2026 р." not in text and "00:00-04:00" not in text and "Перевірити чергу" not in text


def test_text_schedule_split_into_paragraphs_filters_by_queue():
    from hoe_notifier.queues import filter_schedule
    out = filter_schedule(parse_text_schedule(MULTI_P_PAGE), ["3.2"])
    assert out.startswith("За розпорядженням") and "Години відсутності" in out
    assert "Підчерга 3.2 – 02:00-06:00." in out and "Підчерга 3.1" not in out and "Підчерга 1.1" not in out


def test_full_text_schedule_does_not_cross_hr():
    rows = "<br>".join(f"Підчерга {q} – 00:00-04:00." for q in
                       (f"{n}.{m}" for n in range(1, 7) for m in (1, 2)))
    html = f"<div class='post'><p>{rows}</p><hr><p>Підчерга 2.1 – 04:00-08:00.</p></div>"
    assert "04:00-08:00" not in parse_text_schedule(html)


def test_partial_schedule_crosses_only_one_hr():
    html = ("<div class='post'><p>Підчерга 1.1 – 00:00-04:00.</p><hr><p>Перевірити чергу</p><hr>"
            "<p>Підчерга 2.1 – 04:00-08:00.</p></div>")
    assert parse_text_schedule(html) == "Підчерга 1.1 – 00:00-04:00."


def test_real_page_27_11_extra_hours_separated_by_hr():
    from hoe_notifier.queues import filter_schedule
    html = (FIXTURES / "graph_extra_hours_27_11.html").read_text(encoding="utf-8")
    text = parse_text_schedule(html)
    lines = [l for l in text.splitlines() if l]
    assert lines[:3] == ["За розпорядженням НЕК «Укренерго» оновлено графік погодинних відключень (ГПВ) на 27 "
                         "листопада (оновлено, 12:30).", "Додаткові години відсутності електроенергії:",
                         "Підчерга 6.1 - 13:00-14:00"]
    assert sum(l.startswith("Підчерга") for l in lines) == 13
    assert lines[-1] == "Підчерга 6.2 – 08:00-11:00; 14:00-17:00; 20:00-22:00."
    assert "Перевірити чергу" not in text and "Файли" not in text
    out = filter_schedule(text, ["4.1"])
    assert "Додаткові" not in out and out.splitlines()[-1].startswith("Підчерга 4.1 – 06:00-08:00")

# ------------------------------- «Додаткові години» над основним графіком (сайт, 27.11.2026)
_FULL_27 = [
    "Підчерга 1.1 – 06:00-08:00; 11:00-14:00.", "Підчерга 1.2 – 06:00-08:00; 10:00-14:00.",
    "Підчерга 2.1 – 08:00-10:00.", "Підчерга 2.2 – 11:00-14:00.",
    "Підчерга 3.1 – 08:00-11:00.", "Підчерга 3.2 – 08:00-11:00; 17:00-20:00.",
    "Підчерга 4.1 – 06:00-08:00.", "Підчерга 4.2 – 11:00-14:00.",
    "Підчерга 5.1 – 08:00-11:00.", "Підчерга 5.2 – 06:00-08:00.",
    "Підчерга 6.1 – 14:00-17:00; 20:00-22:00.", "Підчерга 6.2 – 08:00-11:00.",
]
EXTRA_HOURS_PAGE = (
    "<div class='post'>"
    "<p>За розпорядженням НЕК «Укренерго» оновлено графік погодинних відключень (ГПВ) на 27 листопада (оновлено, 12:30).</p>"
    "<p>Додаткові години відсутності електроенергії:</p>"
    "<p>Підчерга 6.1 - 13:00-14:00</p>"
    "<p>&nbsp;</p>"
    "<p>За розпорядженням НЕК «Укренерго» 27.11.2026 р. на Хмельниччині будуть застосовані графіки погодинних "
    "відключень (ГПВ).</p>"
    "<p>Години відсутності електроенергії:</p>"
    + "".join(f"<p>{a}<br>{b}</p>" for a, b in zip(_FULL_27[::2], _FULL_27[1::2])) +
    "<p>26.11.2026 р.</p>"
    "<p>За розпорядженням НЕК «Укренерго» графік погодинних відключень (ГПВ) на 26 листопада.</p>"
    "<p>Години відсутності електроенергії:</p>"
    "<p>Підчерга 1.1 – 00:00-04:00.<br>Підчерга 1.2 – 04:00-08:00.</p>"
    "<hr><p>Перевірити чергу</p></div>"
)


def test_extra_hours_block_is_followed_by_main_schedule():
    text = parse_text_schedule(EXTRA_HOURS_PAGE)
    lines = [l for l in text.splitlines() if l]
    assert lines[0].endswith("(оновлено, 12:30).")
    assert lines[1:3] == ["Додаткові години відсутності електроенергії:", "Підчерга 6.1 - 13:00-14:00"]
    assert "будуть застосовані графіки" in lines[3] and lines[4] == "Години відсутності електроенергії:"
    assert lines[5:] == _FULL_27                               # основний графік повністю
    assert "26 листопада" not in text and "00:00-04:00" not in text   # старіший графік не береться


def test_extra_hours_filtered_for_other_queue_drops_extra_block():
    from hoe_notifier.queues import filter_schedule
    out = filter_schedule(parse_text_schedule(EXTRA_HOURS_PAGE), ["3.2"])
    assert "Додаткові години" not in out and "оновлено, 12:30" not in out
    assert out.splitlines()[-1] == "Підчерга 3.2 – 08:00-11:00; 17:00-20:00."
    assert "будуть застосовані графіки" in out and "Підчерга 3.1" not in out


def test_extra_hours_filtered_for_affected_queue_shows_both_blocks():
    from hoe_notifier.queues import filter_schedule
    out = filter_schedule(parse_text_schedule(EXTRA_HOURS_PAGE), ["6.1"])
    lines = [l for l in out.splitlines() if l]
    assert lines.count("Підчерга 6.1 - 13:00-14:00") == 1 and "Підчерга 6.1 – 14:00-17:00; 20:00-22:00." in lines
    assert "Додаткові години відсутності електроенергії:" in lines and "Підчерга 6.2" not in out


def test_full_schedule_first_does_not_pull_older_one():
    text = parse_text_schedule(MULTI_P_PAGE)        # перший блок має всі 12 підчерг
    assert "00:00-04:00" not in text