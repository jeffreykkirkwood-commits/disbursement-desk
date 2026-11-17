# JSP-17 — Concurrent decide exclusivity under BEGIN IMMEDIATE + UNIQUE keys.
"""Stress and structural sync for at-most-one mutating decide per payout."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import payouts
from desk_common import APIError

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent
SKILL_STRUCTURE = REPO / '.cursor/skills/verify-decide-exclusivity/scripts/check_structure.py'


class DecideExclusivityStructureTests(unittest.TestCase):
    # FEATURE 01 — Scenario — CI blocks if decide drops BEGIN IMMEDIATE / key exclusivity
    def test_decide_structural_sync_passes(self):
        self.assertTrue(SKILL_STRUCTURE.is_file(), SKILL_STRUCTURE)
        completed = subprocess.run(
            [sys.executable, str(SKILL_STRUCTURE), str(REPO)],
            check=False, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, 0,
            f'structural decide exclusivity sync failed:\n{completed.stdout}\n{completed.stderr}')
        self.assertIn('OK:', completed.stdout)


class DecideExclusivityStressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 02 — Scenario — approve vs return race: exactly one terminal mutation
    # Hold-then-approve is allowed sequentially (#41); return/approve both leave
    # a non-decidable status, so concurrent peers must lose with 409.
    def test_concurrent_approve_vs_return_one_terminal(self):
        payout_id = 'PO-1042'
        barrier = threading.Barrier(10)
        results = []

        def worker(index):
            barrier.wait(timeout=10)
            action = payouts.APPROVE if index % 2 == 0 else payouts.RETURN
            try:
                code, event = payouts.decide(
                    self.db, payout_id, action, 'Reviewer',
                    f'race-{action}-{index}', now=NOW)
                return ('ok', code, event['action'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker, i) for i in range(10)]
            for future in as_completed(futures):
                results.append(future.result())

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        losses = [row for row in results if row[0] == 'err']
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(len(losses), 9, results)
        self.assertTrue(all(row[1] == 409 for row in losses), losses)
        payout = payouts.get_payout(self.db, payout_id)
        if wins[0][2] == payouts.APPROVE:
            self.assertEqual(payout['status'], payouts.PAID)
            self.assertEqual(payout['execution_status'], payouts.SETTLED)
        else:
            self.assertEqual(payout['status'], payouts.RETURNED)
        mutating = [e for e in payouts.list_events(self.db, payout_id)
                    if e['action'] in (payouts.APPROVE, payouts.RETURN)]
        self.assertEqual(len(mutating), 1)

    # FEATURE 03 — Scenario — high fan-in approve: one settle, rest 409
    def test_high_fan_in_approve_only_one_settles(self):
        payout_id = 'PO-1043'
        workers = 16
        barrier = threading.Barrier(workers)
        results = []

        def worker(index):
            barrier.wait(timeout=15)
            try:
                code, event = payouts.decide(
                    self.db, payout_id, payouts.APPROVE, 'Reviewer',
                    f'fan-in-approve-{index}', now=NOW)
                return ('ok', code, event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, i) for i in range(workers)]
            for future in as_completed(futures):
                results.append(future.result())

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(wins[0][2], payouts.PAID)
        self.assertEqual(
            len([row for row in results if row[0] == 'err' and row[1] == 409]),
            workers - 1, results)
        payout = payouts.get_payout(self.db, payout_id)
        self.assertEqual(payout['status'], payouts.PAID)
        self.assertEqual(payout['execution_status'], payouts.SETTLED)
        self.assertEqual(
            len([e for e in payouts.list_events(self.db, payout_id)
                 if e['action'] == payouts.APPROVE]),
            1)

    # FEATURE 04 — Scenario — same key under fan-in: one create, rest replay
    def test_high_fan_in_same_key_replays(self):
        payout_id = 'PO-1044'
        key = 'fan-in-same-key-hold'
        workers = 12
        barrier = threading.Barrier(workers)
        results = []

        def worker():
            barrier.wait(timeout=15)
            try:
                code, event = payouts.decide(
                    self.db, payout_id, payouts.HOLD, 'Reviewer', key, now=NOW)
                return ('ok', code, event['event_id'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
            for future in as_completed(futures):
                results.append(future.result())

        oks = [row for row in results if row[0] == 'ok']
        self.assertEqual(len(oks), workers, results)
        self.assertEqual(len([row for row in oks if row[1] == 201]), 1, results)
        self.assertEqual(len([row for row in oks if row[1] == 200]), workers - 1, results)
        self.assertEqual({row[2] for row in oks}, {oks[0][2]})
        self.assertEqual(payouts.get_payout(self.db, payout_id)['status'], payouts.HELD)


if __name__ == '__main__':
    unittest.main()
