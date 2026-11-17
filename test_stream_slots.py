# START HERE — Shared stream-slot store tests (P1 multi-worker).
"""Memory + file backends, including a cross-process file cap."""
from __future__ import annotations

import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import payout_events
import stream_slots
from hosted_app import create_app


def _worker_acquire(directory, results, index):
    store = stream_slots.build_store(
        directory, payout_events.MAX_STREAMS, payout_events.HOLD_SECONDS,
        env={stream_slots.BACKEND_ENV: stream_slots.BACKEND_FILE})
    results[index] = store.acquire()


class MemoryStreamSlotsTests(unittest.TestCase):
    def setUp(self):
        payout_events.configure_stream_slots(None)
        payout_events.reset_stream_slots()

    def tearDown(self):
        payout_events.configure_stream_slots(None)
        payout_events.reset_stream_slots()

    def test_in_process_fallback_caps_at_max(self):
        tokens = [payout_events.acquire_stream() for _ in range(payout_events.MAX_STREAMS)]
        self.assertTrue(all(tokens))
        self.assertIsNone(payout_events.acquire_stream())
        payout_events.release_stream(tokens[0])
        self.assertIsNotNone(payout_events.acquire_stream())

    def test_configured_memory_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            payout_events.configure_stream_slots(temp, backend=stream_slots.BACKEND_MEMORY)
            first = payout_events.acquire_stream()
            second = payout_events.acquire_stream()
            self.assertTrue(first and second)
            self.assertIsNone(payout_events.acquire_stream())
            payout_events.release_stream(first)
            self.assertIsNotNone(payout_events.acquire_stream())


class FileStreamSlotsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = stream_slots.build_store(
            self.temp.name, 2, 20,
            env={stream_slots.BACKEND_ENV: stream_slots.BACKEND_FILE})
        self.assertIsInstance(self.store, stream_slots.FileStreamSlots)

    def tearDown(self):
        self.temp.cleanup()

    def test_file_cap_and_token_release(self):
        a = self.store.acquire()
        b = self.store.acquire()
        self.assertTrue(a and b)
        self.assertIsNone(self.store.acquire())
        self.store.release(a)
        c = self.store.acquire()
        self.assertTrue(c)
        self.assertNotEqual(c, a)
        self.store.release('missing-token')
        self.assertEqual(self.store.active_count(), 2)

    def test_expired_leases_free_slots(self):
        store = stream_slots.FileStreamSlots(
            Path(self.temp.name) / '_stream_slots_short', 2, lease_seconds=1)
        first = store.acquire()
        second = store.acquire()
        self.assertTrue(first and second)
        self.assertIsNone(store.acquire())
        time.sleep(1.1)
        self.assertEqual(store.active_count(), 0)
        self.assertIsNotNone(store.acquire())

    def test_cross_process_cap(self):
        directory = self.temp.name
        with multiprocessing.Manager() as manager:
            results = manager.list([None, None, None])
            procs = [
                multiprocessing.Process(target=_worker_acquire, args=(directory, results, i))
                for i in range(3)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=10)
                self.assertEqual(proc.exitcode, 0)
            tokens = [token for token in results if token]
            self.assertEqual(len(tokens), 2)
            self.assertEqual(len(set(tokens)), 2)


class HostedFileSlotsTests(unittest.TestCase):
    def setUp(self):
        payout_events.configure_stream_slots(None)
        payout_events.reset_stream_slots()
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'http://127.0.0.1')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()
        payout_events.configure_stream_slots(None)
        payout_events.reset_stream_slots()

    def test_create_app_installs_file_backend(self):
        self.assertTrue((Path(self.temp.name) / '_stream_slots').is_dir())
        with mock.patch.object(payout_events, 'stream', return_value=iter([': keepalive\n\n'])):
            first = self.client.get('/payouts/stream', base_url='http://127.0.0.1')
            second = self.client.get('/payouts/stream', base_url='http://127.0.0.1')
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            b''.join(first.response)
            b''.join(second.response)


if __name__ == '__main__':
    unittest.main()
