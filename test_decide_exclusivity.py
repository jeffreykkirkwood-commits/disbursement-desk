# JSP-17 — Concurrent decide exclusivity under BEGIN IMMEDIATE + UNIQUE keys.
"""Stress and structural sync for at-most-one mutating decide per payout."""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import ofac
import payout_rules
import payouts
from desk_common import APIError

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent
SKILL_STRUCTURE = REPO / '.cursor/skills/verify-decide-exclusivity/scripts/check_structure.py'


def _process_decide(db_path, payout_id, action, key, ready, go, result_queue):
    """One OS process: wait for the go signal, then call decide once."""
    ready.set()
    go.wait(timeout=30)
    try:
        code, event = payouts.decide(
            Path(db_path), payout_id, action, 'Reviewer', key, now=NOW)
        result_queue.put(('ok', code, event['action'], event['to_status'], event.get('event_id')))
    except APIError as exc:
        result_queue.put(('err', exc.status, exc.message, None, None))
    except sqlite3.Error as exc:
        result_queue.put(('sql', type(exc).__name__, str(exc), None, None))
    except Exception as exc:  # noqa: BLE001 — surface unexpected worker failures
        result_queue.put(('boom', type(exc).__name__, str(exc), None, None))


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

    # FEATURE 05 — Scenario — multi-process approve fan-in (separate SQLite connections)
    # Threads share a process; Gunicorn workers do not. Spawned processes prove the
    # file lock + BEGIN IMMEDIATE path still yields one settle / one approve event.
    def test_multiprocess_approve_only_one_settles(self):
        payout_id = 'PO-1042'
        workers = 8
        ctx = mp.get_context('spawn')
        ready_events = [ctx.Event() for _ in range(workers)]
        go = ctx.Event()
        result_queue = ctx.Queue()
        processes = []
        for index in range(workers):
            process = ctx.Process(
                target=_process_decide,
                args=(str(self.db), payout_id, payouts.APPROVE,
                      f'mp-approve-{index}', ready_events[index], go, result_queue))
            process.start()
            processes.append(process)
        for event in ready_events:
            self.assertTrue(event.wait(timeout=30), 'worker failed to become ready')
        go.set()
        results = [result_queue.get(timeout=60) for _ in range(workers)]
        for process in processes:
            process.join(timeout=60)
            self.assertEqual(process.exitcode, 0, process.exitcode)

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        losses = [row for row in results if row[0] == 'err']
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(wins[0][2], payouts.APPROVE)
        self.assertEqual(wins[0][3], payouts.PAID)
        self.assertEqual(len(losses), workers - 1, results)
        self.assertTrue(all(row[1] == 409 for row in losses), losses)
        self.assertFalse(any(row[0] in ('sql', 'boom') for row in results), results)
        payout = payouts.get_payout(self.db, payout_id)
        self.assertEqual(payout['status'], payouts.PAID)
        self.assertEqual(payout['execution_status'], payouts.SETTLED)
        self.assertEqual(
            len([e for e in payouts.list_events(self.db, payout_id)
                 if e['action'] == payouts.APPROVE]),
            1)

    # FEATURE 06 — Scenario — multi-process same Idempotency-Key replays once
    def test_multiprocess_same_key_replays(self):
        payout_id = 'PO-1043'
        key = 'mp-same-key-hold'
        workers = 6
        ctx = mp.get_context('spawn')
        ready_events = [ctx.Event() for _ in range(workers)]
        go = ctx.Event()
        result_queue = ctx.Queue()
        processes = []
        for index in range(workers):
            process = ctx.Process(
                target=_process_decide,
                args=(str(self.db), payout_id, payouts.HOLD, key,
                      ready_events[index], go, result_queue))
            process.start()
            processes.append(process)
        for event in ready_events:
            self.assertTrue(event.wait(timeout=30), 'worker failed to become ready')
        go.set()
        results = [result_queue.get(timeout=60) for _ in range(workers)]
        for process in processes:
            process.join(timeout=60)
            self.assertEqual(process.exitcode, 0, process.exitcode)

        oks = [row for row in results if row[0] == 'ok']
        self.assertEqual(len(oks), workers, results)
        self.assertEqual(len([row for row in oks if row[1] == 201]), 1, results)
        self.assertEqual(len([row for row in oks if row[1] == 200]), workers - 1, results)
        self.assertEqual({row[4] for row in oks}, {oks[0][4]})
        self.assertEqual(payouts.get_payout(self.db, payout_id)['status'], payouts.HELD)
        self.assertEqual(len(payouts.list_events(self.db, payout_id)), 1)

    # FEATURE 07 — Scenario — concurrent Clear on OFAC match: one settle; Approve still blocked
    # Does not weaken the Clear gate — Approve on a sanctions match remains 409.
    def test_concurrent_clear_ofac_only_one_settles(self):
        payout_id = 'PO-EXCL-OFAC-CLR'
        created = payout_rules.create(
            self.db, payout_id, ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        self.assertEqual(created['status'], payouts.FLAGGED)
        flagged = payouts.get_payout(self.db, payout_id)
        self.assertIn(ofac.FLAG_CODE, payouts.split_flag_codes(flagged['flag_code']))

        with self.assertRaises(APIError) as blocked:
            payouts.decide(self.db, payout_id, payouts.APPROVE, 'Reviewer',
                           'approve-still-blocked', now=NOW)
        self.assertEqual(blocked.exception.status, 409)
        self.assertIn('Clear or Block', blocked.exception.message)

        workers = 10
        barrier = threading.Barrier(workers)
        results = []

        def worker(index):
            barrier.wait(timeout=15)
            try:
                code, event = payouts.decide(
                    self.db, payout_id, payouts.CLEAR, 'Reviewer',
                    f'clear-race-{index}', now=NOW)
                return ('ok', code, event['action'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, i) for i in range(workers)]
            for future in as_completed(futures):
                results.append(future.result())

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        losses = [row for row in results if row[0] == 'err']
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(wins[0][2], payouts.CLEAR)
        self.assertEqual(wins[0][3], payouts.PAID)
        self.assertEqual(len(losses), workers - 1, results)
        self.assertTrue(all(row[1] == 409 for row in losses), losses)
        payout = payouts.get_payout(self.db, payout_id)
        self.assertEqual(payout['status'], payouts.PAID)
        self.assertEqual(payout['execution_status'], payouts.SETTLED)
        self.assertEqual(
            len([e for e in payouts.list_events(self.db, payout_id)
                 if e['action'] == payouts.CLEAR]),
            1)

    # FEATURE 08 — Scenario — Clear vs Block race: exactly one terminal mutation
    def test_concurrent_clear_vs_block_one_terminal(self):
        payout_id = 'PO-EXCL-OFAC-CB'
        payout_rules.create(
            self.db, payout_id, ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH', now=NOW)
        workers = 8
        barrier = threading.Barrier(workers)
        results = []

        def worker(index):
            barrier.wait(timeout=15)
            action = payouts.CLEAR if index % 2 == 0 else payouts.BLOCK
            try:
                code, event = payouts.decide(
                    self.db, payout_id, action, 'Reviewer',
                    f'clear-block-{action}-{index}', now=NOW)
                return ('ok', code, event['action'], event['to_status'])
            except APIError as exc:
                return ('err', exc.status, exc.message)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, i) for i in range(workers)]
            for future in as_completed(futures):
                results.append(future.result())

        wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
        losses = [row for row in results if row[0] == 'err']
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(len(losses), workers - 1, results)
        self.assertTrue(all(row[1] == 409 for row in losses), losses)
        payout = payouts.get_payout(self.db, payout_id)
        if wins[0][2] == payouts.CLEAR:
            self.assertEqual(payout['status'], payouts.PAID)
            self.assertEqual(payout['execution_status'], payouts.SETTLED)
        else:
            self.assertEqual(wins[0][2], payouts.BLOCK)
            self.assertEqual(payout['status'], payouts.RETURNED)
        mutating = [e for e in payouts.list_events(self.db, payout_id)
                    if e['action'] in (payouts.CLEAR, payouts.BLOCK)]
        self.assertEqual(len(mutating), 1)

    # FEATURE 09 — Scenario — repeated fan-in rounds: never double-settle
    def test_repeated_fan_in_rounds_never_double_settle(self):
        for round_index in range(5):
            payout_id = f'PO-EXCL-ROUND-{round_index}'
            # New payee over $1,000 flags without OFAC, so Approve is a legal settle path.
            created = payout_rules.create(
                self.db, payout_id, f'Excl Round Shop {round_index}',
                f'DEMO-EXCL-{round_index:03d}', 100001, 'ACH', now=NOW)
            self.assertEqual(created['status'], payouts.FLAGGED)
            payout = payouts.get_payout(self.db, payout_id)
            self.assertNotIn(ofac.FLAG_CODE, payouts.split_flag_codes(payout['flag_code'] or ''))

            workers = 12
            barrier = threading.Barrier(workers)
            results = []

            def worker(index, pid=payout_id):
                barrier.wait(timeout=15)
                try:
                    code, event = payouts.decide(
                        self.db, pid, payouts.APPROVE, 'Reviewer',
                        f'round-{round_index}-approve-{index}', now=NOW)
                    return ('ok', code, event['to_status'])
                except APIError as exc:
                    return ('err', exc.status, exc.message)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(worker, i) for i in range(workers)]
                for future in as_completed(futures):
                    results.append(future.result())

            wins = [row for row in results if row[0] == 'ok' and row[1] == 201]
            self.assertEqual(len(wins), 1, (round_index, results))
            self.assertEqual(wins[0][2], payouts.PAID)
            settled = payouts.get_payout(self.db, payout_id)
            self.assertEqual(settled['status'], payouts.PAID)
            self.assertEqual(settled['execution_status'], payouts.SETTLED)
            self.assertEqual(
                len([e for e in payouts.list_events(self.db, payout_id)
                     if e['action'] == payouts.APPROVE]),
                1, round_index)


if __name__ == '__main__':
    unittest.main()
