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
REAL_PAGE = (Path(__file__).parent / "fixtures" / "graph_text_page.html").read_text(encoding="utf-8")


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