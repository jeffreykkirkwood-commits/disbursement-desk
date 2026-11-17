# P0 pre-send gates: mandatory fraud/exception rules and OFAC settle hygiene.
# Rule bypass or settling an OFAC match without Clear is a release blocker.
"""Prove mandatory pre-send rules cannot be skipped before send."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ofac
import payout_rules
import payouts
from desk_common import APIError

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent
SKILL_STRUCTURE = REPO / '.cursor/skills/verify-ofac-settle/scripts/check_structure.py'


class MandatoryRuleCatalogTests(unittest.TestCase):
    # FEATURE 01 — Scenario — the documented mandatory set matches FLAG_TEXT
    def test_mandatory_rules_are_exactly_flag_text_keys(self):
        self.assertEqual(payouts.MANDATORY_PRE_SEND_RULES, frozenset(payouts.FLAG_TEXT))
        self.assertEqual(
            payouts.MANDATORY_PRE_SEND_RULES,
            frozenset({
                payouts.AMOUNT_OVER_3X_AVG,
                payouts.BANK_CHANGED_7D,
                payouts.DUPLICATE_24H,
                payouts.NEW_PAYEE_OVER_1000,
                payouts.POSSIBLE_SANCTIONS_MATCH,
            }),
        )
        self.assertEqual(payouts.POSSIBLE_SANCTIONS_MATCH, ofac.FLAG_CODE)

    # FEATURE 02 — Scenario — explain_flag source still evaluates every mandatory rule
    def test_explain_flag_body_references_every_mandatory_rule(self):
        text = Path(payouts.__file__).read_text()
        start = text.index('def explain_flag(')
        end = text.index('\ndef ', start + 1)
        body = text[start:end]
        for code in payouts.MANDATORY_PRE_SEND_RULES:
            with self.subTest(code=code):
                self.assertIn(code, body)
        self.assertIn('ofac.screen', body)


class OfacStructureSyncTests(unittest.TestCase):
    # FEATURE 03 — Scenario — CI blocks if decide/_settle/route_one OFAC gates drift
    def test_ofac_structural_sync_passes(self):
        self.assertTrue(SKILL_STRUCTURE.is_file(), SKILL_STRUCTURE)
        completed = subprocess.run(
            [sys.executable, str(SKILL_STRUCTURE), str(REPO)],
            check=False, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, 0,
            f'structural OFAC sync failed:\n{completed.stdout}\n{completed.stderr}')
        self.assertIn('OK:', completed.stdout)


class ExceptionRuleBypassTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, payout_id, payee, account, cents, **kwargs):
        return payout_rules.create(
            self.db, payout_id, payee, account, cents, 'ACH', now=NOW, **kwargs)

    # FEATURE 04 — Scenario — each non-OFAC mandatory rule stops automation at FLAGGED
    def test_each_exception_rule_blocks_auto_settle(self):
        cases = (
            ('PO-GATE-AMT', 'Harbor Hardware', 'DEMO-HRBR-006', 207550,
             payouts.AMOUNT_OVER_3X_AVG, {}),
            ('PO-GATE-BANK', 'Oak Street Books', 'DEMO-OAKS-010', 50000,
             payouts.BANK_CHANGED_7D,
             {'bank_details_changed_at': (NOW - timedelta(days=1)).isoformat()}),
            ('PO-GATE-NEW', 'Brand New Payee LLC', 'DEMO-NEWP-001', 100001,
             payouts.NEW_PAYEE_OVER_1000, {}),
        )
        for payout_id, payee, account, cents, code, extra in cases:
            with self.subTest(code=code):
                result = self.create(payout_id, payee, account, cents, **extra)
                self.assertEqual(result['status'], payouts.FLAGGED)
                payout = payouts.get_payout(self.db, payout_id)
                self.assertEqual(payout['status'], payouts.FLAGGED)
                self.assertEqual(payout['execution_status'], payouts.NOT_STARTED)
                self.assertIn(code, payouts.split_flag_codes(payout['flag_code']))
                self.assertNotEqual(payout['status'], payouts.PAID)
                # A second route cannot approve or settle past the flag.
                again = payout_rules.route(self.db, payout_id=payout_id, now=NOW)
                self.assertFalse(again['changed'])
                self.assertEqual(payouts.get_payout(self.db, payout_id)['status'],
                                 payouts.FLAGGED)

    def test_duplicate_rule_blocks_auto_settle(self):
        first = self.create('PO-GATE-DUP-1', 'Oak Street Books', 'DEMO-OAKS-010', 42000)
        self.assertEqual(first['status'], payouts.PAID)
        later = NOW + timedelta(hours=2)
        second = payout_rules.create(
            self.db, 'PO-GATE-DUP-2', 'Oak Street Books', 'DEMO-OAKS-010', 42000, 'ACH',
            now=later)
        self.assertEqual(second['status'], payouts.FLAGGED)
        payout = payouts.get_payout(self.db, 'PO-GATE-DUP-2')
        self.assertIn(payouts.DUPLICATE_24H, payouts.split_flag_codes(payout['flag_code']))
        self.assertEqual(payout['execution_status'], payouts.NOT_STARTED)

    # FEATURE 05 — Scenario — OFAC match cannot settle without Clear (direct _settle too)
    def test_settle_without_clear_holds_ofac_match(self):
        payout_rules.create(
            self.db, 'PO-GATE-OFAC', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        payout = payouts.get_payout(self.db, 'PO-GATE-OFAC')
        self.assertEqual(payout['status'], payouts.FLAGGED)
        self.assertIn(payouts.POSSIBLE_SANCTIONS_MATCH,
                      payouts.split_flag_codes(payout['flag_code']))
        at = NOW.isoformat()
        with sqlite3.connect(self.db) as db:
            db.execute(
                'UPDATE payouts SET review_status = ? WHERE payout_id = ?',
                (payouts.APPROVED, 'PO-GATE-OFAC'))
            held = payouts._settle(db, 'PO-GATE-OFAC', at, allow_sanctions_clear=False,
                                   actor='tester', idempotency_key='settle-bypass-ofac')
            self.assertIsNotNone(held)
            self.assertEqual(held['action'], payouts.SCREEN)
            self.assertEqual(held['to_status'], payouts.FLAGGED)
            row = dict(zip(payouts.COLUMNS, db.execute(
                f'SELECT {", ".join(payouts.COLUMNS)} FROM payouts WHERE payout_id = ?',
                ('PO-GATE-OFAC',)).fetchone()))
            self.assertEqual(row['status'], payouts.FLAGGED)
            self.assertEqual(row['execution_status'], payouts.NOT_STARTED)
            self.assertNotEqual(row['status'], payouts.PAID)

    def test_clear_is_only_operator_path_that_settles_ofac_match(self):
        payout_rules.create(
            self.db, 'PO-GATE-CLR', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        with self.assertRaises(APIError) as caught:
            payouts.decide(self.db, 'PO-GATE-CLR', payouts.APPROVE, 'Reviewer',
                           'approve-ofac-blocked', now=NOW)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('Clear or Block', caught.exception.message)
        self.assertEqual(payouts.get_payout(self.db, 'PO-GATE-CLR')['status'],
                         payouts.FLAGGED)
        code, event = payouts.decide(
            self.db, 'PO-GATE-CLR', payouts.CLEAR, 'Reviewer', 'clear-ofac-ok', now=NOW)
        self.assertEqual(code, 201)
        self.assertEqual(event['action'], payouts.CLEAR)
        payout = payouts.get_payout(self.db, 'PO-GATE-CLR')
        self.assertEqual(payout['status'], payouts.PAID)
        self.assertEqual(payout['execution_status'], payouts.SETTLED)


class ConcurrentDecideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 06 — Scenario — concurrent decides on one payout serialize (one send)
    def test_concurrent_approve_same_payout_only_one_settles(self):
        payout_id = 'PO-1042'
        barrier = threading.Barrier(8)
        results = []

        def worker(index):
            barrier.wait(timeout=10)
            try:
                code, event = payouts.decide(
                    self.db, payout_id, payouts.APPROVE, 'Reviewer',
                    f'concurrent-approve-{index}', now=NOW)
                return ('ok', code, event['action'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(8)]
            for future in as_completed(futures):
                results.append(future.result())

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        losses = [row for row in results if row[0] == 'err']
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(wins[0][3], payouts.PAID)
        self.assertEqual(len(losses), 7, results)
        self.assertTrue(all(row[1] == 409 for row in losses), losses)
        payout = payouts.get_payout(self.db, payout_id)
        self.assertEqual(payout['status'], payouts.PAID)
        self.assertEqual(payout['execution_status'], payouts.SETTLED)
        events = [e for e in payouts.list_events(self.db, payout_id)
                  if e['action'] == payouts.APPROVE]
        self.assertEqual(len(events), 1)

    # FEATURE 07 — Scenario — concurrent same-key approve replays instead of double-write
    def test_concurrent_same_idempotency_key_replays_once(self):
        payout_id = 'PO-1043'
        key = 'same-key-concurrent-hold'
        barrier = threading.Barrier(6)
        results = []

        def worker():
            barrier.wait(timeout=10)
            try:
                code, event = payouts.decide(
                    self.db, payout_id, payouts.HOLD, 'Reviewer', key, now=NOW)
                return ('ok', code, event['event_id'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker) for _ in range(6)]
            for future in as_completed(futures):
                results.append(future.result())

        oks = [row for row in results if row[0] == 'ok']
        self.assertEqual(len(oks), 6, results)
        created = [row for row in oks if row[1] == 201]
        replayed = [row for row in oks if row[1] == 200]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(len(replayed), 5, results)
        event_ids = {row[2] for row in oks}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(payouts.get_payout(self.db, payout_id)['status'], payouts.HELD)
        self.assertEqual(len(payouts.list_events(self.db, payout_id)), 1)


if __name__ == '__main__':
    unittest.main()
