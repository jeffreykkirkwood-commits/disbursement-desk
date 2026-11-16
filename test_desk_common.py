# START HERE — Proves the Desk never imports the Payments Lab (#122) and that its own
# helpers keep the exact output the Lab copies had. The expected values below were
# checked against the Lab's dashboard.money and dashboard.text in
# payments-api-simulator PR #131 before the move.
import ast
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import desk_common
from hosted_app import create_app

ROOT = Path(__file__).resolve().parent
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
    # FEATURE 01 — Scenario — no module in this repo reaches into the Lab
    # Covers every Python file at the top of the repo, including files that arrive in
    # later move steps, so a stray Lab import fails here.
    def test_no_module_imports_lab_code(self):
        files = sorted(ROOT.glob('*.py'))
        self.assertIn(ROOT / 'hosted_app.py', files)
        for path in files:
            with self.subTest(module=path.name):
                self.assertEqual(imported_modules(path) & LAB_MODULES, set())
        for name in LAB_MODULES:
            self.assertFalse((ROOT / f'{name}.py').exists(), f'{name}.py belongs to the Lab')


class DeskHelpersTests(unittest.TestCase):
    # FEATURE 02 — Scenario — helpers keep the Lab's exact output
    def test_money_output(self):
        for value, expected in ((0, '$0.00'), (5, '$5.00'), ('12.5', '$12.50'),
                                ('1234.50', '$1,234.50'), ('-3.10', '$-3.10'),
                                (Decimal('1000000'), '$1,000,000.00'), ('0.005', '$0.00')):
            with self.subTest(value=value):
                self.assertEqual(desk_common.money(value), expected)

    def test_text_output(self):
        for value, expected in (('plain', 'plain'),
                                ('<b>"x" & \'y\'</b>', '&lt;b&gt;&quot;x&quot; &amp; &#x27;y&#x27;&lt;/b&gt;'),
                                (42, '42'), (None, 'None'), ('Café ✓', 'Café ✓')):
            with self.subTest(value=value):
                self.assertEqual(desk_common.text(value), expected)

    def test_error_keeps_status_and_message(self):
        error = desk_common.APIError(409, 'Payout already decided.')
        self.assertEqual((error.status, error.message), (409, 'Payout already decided.'))


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
