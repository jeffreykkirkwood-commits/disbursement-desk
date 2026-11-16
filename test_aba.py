# Automated checks for ABA routing-number check-digit validation (#112).
# Numbers used here are synthetic or well-known published test values
# (Stripe's US test routing number), not real customer accounts.
"""Exercise the ABA 3-7-1 checksum helper and its 422 messages."""
import unittest

import aba
from desk_common import APIError

# 110000000 is Stripe's documented US test routing number. The check digit is
# valid: 3+7+0+0+0+0+0+0+0 = 10. 110000001 changes only the last digit and fails.
VALID = aba.WELL_KNOWN_TEST
INVALID_CHECK = '110000001'


class AbaCheckDigitTests(unittest.TestCase):
    # FEATURE 01 — Scenario — a well-known test number passes; a bad check digit fails
    def test_valid_and_invalid_check_digits(self):
        self.assertEqual(VALID, '110000000')
        self.assertTrue(aba.check_digit_valid(VALID))
        self.assertEqual(aba.validate(VALID), VALID)
        self.assertFalse(aba.check_digit_valid(INVALID_CHECK))
        with self.assertRaises(APIError) as failed:
            aba.validate(INVALID_CHECK)
        self.assertEqual(failed.exception.status, 422)
        self.assertEqual(failed.exception.message, aba.CHECK_MESSAGE)
        self.assertIn('check-digit', failed.exception.message)

    # FEATURE 02 — Scenario — a missing or blank number is skipped
    def test_missing_number_is_skipped(self):
        self.assertIsNone(aba.validate(None))
        self.assertIsNone(aba.validate(''))
        self.assertIsNone(aba.validate('   '))
        self.assertFalse(aba.check_digit_valid(None))
        self.assertFalse(aba.check_digit_valid(''))

    # FEATURE 03 — Scenario — a number that is not nine digits is refused
    def test_non_nine_digit_values_are_refused(self):
        for bad in ('11000000', '1100000000', '11000000a', '11-0000-000', 110000000, True):
            with self.subTest(value=bad):
                self.assertFalse(aba.check_digit_valid(bad))
                with self.assertRaises(APIError) as failed:
                    aba.validate(bad)
                self.assertEqual(failed.exception.status, 422)
                if isinstance(bad, str):
                    self.assertEqual(failed.exception.message, aba.LENGTH_MESSAGE)
                else:
                    self.assertIn('text', failed.exception.message)

    # FEATURE 04 — Scenario — the 3-7-1 weights themselves are correct
    def test_checksum_uses_three_seven_one_weights(self):
        self.assertEqual(aba.WEIGHTS, (3, 7, 1, 3, 7, 1, 3, 7, 1))
        # 111111118: 3+7+1+3+7+1+3+7+8 = 40, a multiple of 10.
        self.assertTrue(aba.check_digit_valid('111111118'))
        self.assertFalse(aba.check_digit_valid('111111111'))
        self.assertEqual(aba.validate(' 110000000 '), VALID)

    # FEATURE 05 — Scenario — Unicode digits are refused, not accepted or crashed
    # str.isdigit() is true for these, but they are not ASCII 0-9. Superscript
    # twos used to crash int() (500 on simulate); Arabic-Indic 110000000 used
    # to be accepted.
    def test_non_ascii_digits_are_refused(self):
        for bad in ('²²²²²²²²²', '١١٠٠٠٠٠٠٠'):
            with self.subTest(value=bad):
                self.assertTrue(bad.isdigit())
                self.assertFalse(bad.isascii())
                self.assertFalse(aba.check_digit_valid(bad))
                with self.assertRaises(APIError) as failed:
                    aba.validate(bad)
                self.assertEqual(failed.exception.status, 422)
                self.assertEqual(failed.exception.message, aba.LENGTH_MESSAGE)

    # FEATURE 06 — Scenario — all-zero is refused even though the checksum passes
    def test_all_zero_is_refused(self):
        self.assertTrue(aba.check_digit_valid('000000000'))
        with self.assertRaises(APIError) as failed:
            aba.validate('000000000')
        self.assertEqual(failed.exception.status, 422)
        self.assertEqual(failed.exception.message, aba.ZERO_MESSAGE)


if __name__ == '__main__':
    unittest.main()
