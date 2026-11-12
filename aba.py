# START HERE — ABA routing-number check-digit validation (#112).
# The standard ABA 3-7-1 checksum is applied when a routing number is entered
# for a payee or payout. A number that fails the check is rejected with a
# plain-language message. A valid number, or no number, is accepted. This
# file does not look up a directory of banks and does not call the network.
"""Validate an ABA routing number's check digit. Stdlib only."""
from payments import APIError

# FEATURE 01 — Name the 3-7-1 weights and the error sentences
# Positions 1, 4 and 7 are multiplied by 3; 2, 5 and 8 by 7; 3, 6 and 9 by 1.
# The nine products must sum to a multiple of 10. This is the public ABA
# checksum, not a bank directory check. WELL_KNOWN_TEST is Stripe's documented
# US test routing number; it is a synthetic test value, not a customer account.
WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)
WELL_KNOWN_TEST = '110000000'
LENGTH_MESSAGE = 'Enter a 9-digit ABA routing number.'
CHECK_MESSAGE = 'This routing number fails the check-digit test. Check the number and try again.'


# FEATURE 02 — Work out whether nine digits have a valid check digit
# Returns True only for a 9-digit string whose weighted sum is a multiple of 10.
# Anything else is False. Read-only; no I/O.
def check_digit_valid(number):
    if not isinstance(number, str) or not number.isdigit() or len(number) != 9:
        return False
    total = sum(weight * int(digit) for weight, digit in zip(WEIGHTS, number))
    return total % 10 == 0


# FEATURE 03 — Accept a missing number, or reject a bad one
# None or blank means no routing number was entered, so the check is skipped.
# A present value must be exactly nine digits and pass the checksum, or this
# raises the same 422 error type the payout create path already uses.
def validate(number):
    if number is None:
        return None
    if not isinstance(number, str):
        raise APIError(422, 'Routing number must be text if given.')
    text = number.strip()
    if not text:
        return None
    if not text.isdigit() or len(text) != 9:
        raise APIError(422, LENGTH_MESSAGE)
    if not check_digit_valid(text):
        raise APIError(422, CHECK_MESSAGE)
    return text
