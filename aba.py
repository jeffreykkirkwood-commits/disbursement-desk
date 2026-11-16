# START HERE — ABA routing-number check-digit validation (#112).
# The standard ABA 3-7-1 checksum is applied when a routing number is entered
# for a payee or payout. A number that fails the check is rejected with a
# plain-language message. A valid number, or no number, is accepted. This
# file does not look up a directory of banks and does not call the network.
"""Validate an ABA routing number's check digit. Stdlib only."""
import re
from desk_common import APIError

# FEATURE 01 — Name the 3-7-1 weights and the error sentences
# Positions 1, 4 and 7 are multiplied by 3; 2, 5 and 8 by 7; 3, 6 and 9 by 1.
# The nine products must sum to a multiple of 10. This is the public ABA
# checksum, not a bank directory check. Only ASCII digits 0-9 are accepted
# (Unicode digits such as superscripts are not). All-zero is refused even
# though its checksum passes; there is no Federal Reserve prefix check.
# WELL_KNOWN_TEST is Stripe's documented US test routing number; it is a
# synthetic test value, not a customer account.
WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)
ASCII_NINE = re.compile(r'[0-9]{9}', re.ASCII)
WELL_KNOWN_TEST = '110000000'
LENGTH_MESSAGE = 'Enter a 9-digit ABA routing number.'
CHECK_MESSAGE = 'This routing number fails the check-digit test. Check the number and try again.'
ZERO_MESSAGE = 'An all-zero routing number is not accepted.'


def _ascii_nine(number):
    return isinstance(number, str) and ASCII_NINE.fullmatch(number) is not None


# FEATURE 02 — Work out whether nine digits have a valid check digit
# Returns True only for a nine-character ASCII 0-9 string whose weighted sum
# is a multiple of 10. Anything else is False. Read-only; no I/O.
def check_digit_valid(number):
    if not _ascii_nine(number):
        return False
    total = sum(weight * int(digit) for weight, digit in zip(WEIGHTS, number))
    return total % 10 == 0


# FEATURE 03 — Accept a missing number, or reject a bad one
# None or blank means no routing number was entered, so the check is skipped.
# A present value must be exactly nine ASCII digits 0-9 and pass the checksum,
# and must not be all zeros, or this raises the same 422 error type the payout
# create path already uses.
def validate(number):
    if number is None:
        return None
    if not isinstance(number, str):
        raise APIError(422, 'Routing number must be text if given.')
    text = number.strip()
    if not text:
        return None
    if not _ascii_nine(text):
        raise APIError(422, LENGTH_MESSAGE)
    if not check_digit_valid(text):
        raise APIError(422, CHECK_MESSAGE)
    if text == '000000000':
        raise APIError(422, ZERO_MESSAGE)
    return text
