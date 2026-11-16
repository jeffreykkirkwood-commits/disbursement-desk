# START HERE — Proves the Desk no longer imports the Payments Lab (#122 step 1) and that
# its own helpers behave exactly like the Lab copies they replace.
import ast
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import dashboard
import desk_common
import payments
from hosted_app import create_app

ROOT = Path(__file__).resolve().parent

# Every module that moves to the disbursement-desk repo in #122 steps 3 and 4.
DESK_MODULES = (
    'aba', 'check_dwolla', 'demo_scenario', 'desk', 'desk_common', 'execution_replay',
    'ofac', 'payout_events', 'payout_notes', 'payout_rules', 'payouts', 'rails',
    'reconciliation', 'review_reasons',
)
LAB_MODULES = {'payments', 'dashboard'}


def imported_modules(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split('.')[0])
    return names


class DeskImportsTests(unittest.TestCase):
    # FEATURE 01 — Scenario — no Desk module reaches into the Lab
    def test_no_desk_module_imports_lab_code(self):
        for name in DESK_MODULES:
            with self.subTest(module=name):
                path = ROOT / f'{name}.py'
                self.assertTrue(path.exists(), f'{name}.py is missing')
                self.assertEqual(imported_modules(path) & LAB_MODULES, set())


class DeskHelpersTests(unittest.TestCase):
    # FEATURE 02 — Scenario — helpers match the Lab copies byte for byte
    def test_money_matches_lab(self):
        for value in (0, 5, '12.5', '1234.50', '-3.10', Decimal('1000000'), '0.005'):
            with self.subTest(value=value):
                self.assertEqual(desk_common.money(value), dashboard.money(value))

    def test_text_matches_lab(self):
        for value in ('plain', '<b>"x" & \'y\'</b>', 42, None, 'Café ✓'):
            with self.subTest(value=value):
                self.assertEqual(desk_common.text(value), dashboard.text(value))

    def test_error_keeps_status_and_message(self):
        error = desk_common.APIError(409, 'Payout already decided.')
        self.assertEqual((error.status, error.message), (409, 'Payout already decided.'))
        self.assertNotIsInstance(error, payments.APIError)


class DeskErrorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    # FEATURE 03 — Scenario — a Desk error still becomes the same JSON reply
    def test_desk_error_is_json_with_its_status(self):
        self.client.get('/desk', base_url='https://demo.example')
        reply = self.client.get('/payouts/PO-9999/review-reasons',
                                base_url='https://demo.example')
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.json, {'error': 'Payout not found.'})


if __name__ == '__main__':
    unittest.main()
