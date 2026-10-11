"""Whole numbers written with thousands separators (casefile._int): the private dataset may hold quantities,
strokes or carton sizes of 1000 and more, written German- or English-style; the public one never did."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from casefile import _int  # noqa: E402


class WholeNumbers(unittest.TestCase):
    def test_a_separator_before_three_digits_groups_thousands(self):
        for written in ("1.250", "1,250", "1 250", "1'250", "12.500", "1.250.000"):
            self.assertEqual(_int(written), int(written.replace(".", "").replace(",", "").replace(" ", "").replace("'", "")), written)

    def test_plain_and_unit_suffixed_values_are_unchanged(self):
        self.assertEqual([_int(v) for v in (25, "25", "600 mm", "3 Wochen", None, "–")], [25, 25, 600, 3, None, None])


if __name__ == "__main__":
    unittest.main()
