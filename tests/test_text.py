from datetime import date

import pytest

from jobservice.text import html_to_text, parse_czech_date, slugify

TODAY = date(2026, 11, 16)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10. listopadu", "2026-11-10"),
        ("2. listopadu", "2026-11-02"),
        ("20. října", "2026-10-20"),
        ("20. prosince", "2025-12-20"),  # a future date means last year
        ("5. března 2024", "2024-03-05"),
        ("Aktualizováno dnes", "2026-11-16"),
        ("Aktualizováno před hodinou", "2026-11-16"),
        ("Přidáno před 2 hodinami", "2026-11-16"),
        ("Aktualizováno včera", "2026-11-15"),
        ("Přidáno před 3 dny", "2026-11-13"),
        ("Končí za 3 dny", None),
        ("Končí zítra", None),
        ("31. února", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_czech_date(raw, expected):
    assert parse_czech_date(raw, TODAY) == expected


def test_html_to_text_keeps_structure():
    html = (
        "<p>Ahoj&nbsp;<b>světe</b></p><ul><li>jedna</li><li>dvě</li></ul>"
        "<div>a<br>b</div><script>x()</script>"
    )
    assert html_to_text(html) == "Ahoj světe\n\n- jedna\n- dvě\n\na\nb"


def test_html_to_text_empty():
    assert html_to_text(None) is None
    assert html_to_text("<p> </p>") is None


def test_slugify():
    assert slugify("Hradec Králové") == "hradec-kralove"
    assert slugify("Praha") == "praha"
