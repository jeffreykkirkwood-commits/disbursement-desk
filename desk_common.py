# START HERE — Small helpers the Disbursement Desk owns itself, so no Desk module imports
# the Payments Lab (payments.py or dashboard.py). This lets the Desk move to its own
# repo with its history (#122) without dragging Lab code along.
"""Desk-owned error class and HTML helpers. Same behavior as the Lab copies."""
from decimal import Decimal
from html import escape


# FEATURE 01 — Carry the expected request error
# Store the response number, readable message, and optional stable code for the
# web error handler (see api_contract.ERROR_CODES).
class APIError(Exception):
    def __init__(self, status, message, code=None):
        self.status = status
        self.message = message
        self.code = code


# FEATURE 02 — Make text safe for HTML
# Escape anything a user or fixture could have typed before it reaches the page.
def text(value):
    return escape(str(value), quote=True)


# FEATURE 03 — Format fictional money
# Show amounts as "$1,234.50" using exact decimal math (no rounding errors).
def money(value):
    return '$' + format(Decimal(value), ',.2f')
