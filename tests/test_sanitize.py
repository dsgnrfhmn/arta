"""core/sanitize.plain_text: markup, control characters, ANSI, invisible and bidi characters."""

import pytest

from core import sanitize

INVISIBLE = "​‌‍﻿‪‫‬‭‮⁦⁧⁨⁩"


@pytest.mark.parametrize("ch", list(INVISIBLE))
def test_each_invisible_or_bidi_character_is_removed(ch):
    assert sanitize.plain_text(f"a{ch}b", 10) == "ab"


def test_a_right_to_left_override_cannot_reorder_a_name():
    assert sanitize.plain_text("Grant ‮gnp.exe", 40) == "Grant gnp.exe"


def test_ansi_sequences_are_removed_whole_not_just_the_escape_byte():
    assert sanitize.plain_text("x\x1b[31mred\x1b[0m y\x1b[2K", 40) == "xred y"


def test_markup_control_characters_and_whitespace_are_still_handled():
    assert sanitize.plain_text("  <b>Hi</b><script>x()</script> a &amp; b\x00\x07\r\nnext\t", 40) == "Hi a & b\nnext"


def test_normal_text_including_non_latin_survives():
    text = "".join(chr(0x0430 + i) for i in range(5)) + " | XX, Berlin – 2024"   # Cyrillic letters, no words
    assert sanitize.plain_text(text, 40) == text


def test_too_long_is_refused_and_non_text_is_refused():
    with pytest.raises(ValueError):
        sanitize.plain_text("x" * 11, 10)
    with pytest.raises(ValueError):
        sanitize.plain_text(5, 10)
