"""RTF voucher labels must not emit raw UTF-8 under an \\ansi header.

The document declares \\ansi, so a raw UTF-8 byte is read as one Windows-1252
character: an accented collection prefix rendered as mojibake. Non-ASCII is
escaped as \\uN?, using UTF-16 code units so characters outside the BMP survive
as a surrogate pair instead of being replaced.
"""

import re
import unittest

from app.main.routes import (
    VOUCHER_LABEL_PRESETS, VOUCHER_RTF_FONT_CHOICES,
    _build_voucher_rtf, _rtf_escape,
)


def _layout():
    preset = next(iter(VOUCHER_LABEL_PRESETS.values()))
    font = VOUCHER_RTF_FONT_CHOICES["ibm_plex_sans"]
    return {"preset": preset, "font": font, "font_size": 10}


class RtfEscapeTests(unittest.TestCase):
    def test_ascii_is_untouched(self):
        self.assertEqual(_rtf_escape("ARK-1234 Amanita muscaria"),
                         "ARK-1234 Amanita muscaria")

    def test_rtf_syntax_characters_are_escaped(self):
        self.assertEqual(_rtf_escape("a{b}c\\d"), "a\\{b\\}c\\\\d")

    def test_newlines_become_line_breaks(self):
        self.assertEqual(_rtf_escape("one\ntwo"), "one\\line two")
        self.assertEqual(_rtf_escape("one\r\ntwo"), "one\\line two")

    def test_tabs_become_tab_control_words(self):
        self.assertEqual(_rtf_escape("a\tb"), "a\\tab b")

    def test_accented_latin_uses_unicode_escapes(self):
        self.assertEqual(_rtf_escape("Boleté"), "Bolet\\u233?")
        self.assertEqual(_rtf_escape("Åland"), "\\u197?land")
        self.assertEqual(_rtf_escape("Ñ"), "\\u209?")

    def test_high_bmp_code_points_use_signed_16_bit_values(self):
        # U+FB01 (ﬁ ligature) is above 0x7FFF, so it must go out negative.
        self.assertEqual(_rtf_escape("\ufb01"), "\\u-1279?")

    def test_non_bmp_characters_become_a_surrogate_pair(self):
        # U+1F344 MUSHROOM -> D83C DF44 -> -10180, -8380
        self.assertEqual(_rtf_escape("\U0001F344"), "\\u-10180?\\u-8380?")

    def test_control_characters_are_dropped(self):
        self.assertEqual(_rtf_escape("a\x00\x07b"), "ab")

    def test_escape_output_is_pure_ascii(self):
        escaped = _rtf_escape("Boleté \U0001F344 Åland – ﬁ")
        escaped.encode("ascii", "strict")  # raises if anything slipped through


class RtfDocumentTests(unittest.TestCase):
    def test_document_body_contains_no_raw_utf8_bytes(self):
        data = _build_voucher_rtf(["Boleté edulis", "Åland 2026"], _layout())
        self.assertIsInstance(data, bytes)
        # Every byte must be 7-bit: that is the whole point of \uN escaping.
        self.assertTrue(all(b < 0x80 for b in data))
        self.assertIn(b"Bolet\\u233?", data)
        self.assertIn(b"\\u197?land", data)

    def test_header_declares_a_single_character_unicode_fallback(self):
        data = _build_voucher_rtf(["x"], _layout())
        self.assertTrue(data.startswith(b"{\\rtf1\\ansi\\ansicpg1252\\uc1\\deff0"))

    def test_non_bmp_label_survives_as_a_surrogate_pair(self):
        data = _build_voucher_rtf(["\U0001F344 find"], _layout())
        self.assertIn(b"\\u-10180?\\u-8380? find", data)

    def test_every_unicode_escape_is_followed_by_one_fallback_char(self):
        data = _build_voucher_rtf(["Boleté \U0001F344"], _layout()).decode("ascii")
        for match in re.finditer(r"\\u(-?\d+)(.)", data):
            self.assertEqual(match.group(2), "?")


if __name__ == "__main__":
    unittest.main()
