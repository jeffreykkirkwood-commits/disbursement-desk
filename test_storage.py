# Automated checks for the #30 connection factory: local SQLite when Turso is
# unset, and the libSQL client when both TURSO_DATABASE_URL and
# TURSO_AUTH_TOKEN are set. The Turso client is mocked; these tests make no
# network calls and do not use real credentials.
"""Exercise storage.connect() backend selection."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storage


def _env(**extra):
    env = {key: value for key, value in os.environ.items()
           if key not in (storage.DATABASE_URL, storage.AUTH_TOKEN)}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class StorageFactoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    # FEATURE 01 — Scenario — with no Turso settings, the local file is used
    def test_falls_back_to_sqlite_when_unset(self):
        with _env():
            self.assertFalse(storage.turso_configured())
            db = storage.connect(self.path)
            try:
                self.assertIsInstance(db, sqlite3.Connection)
                db.execute('CREATE TABLE probe (n INTEGER)')
                db.execute('INSERT INTO probe VALUES (1)')
                db.commit()
                self.assertEqual(db.execute('SELECT n FROM probe').fetchone(), (1,))
            finally:
                db.close()
            with sqlite3.connect(self.path) as check:
                self.assertEqual(check.execute('SELECT n FROM probe').fetchone(), (1,))

    # FEATURE 02 — Scenario — one variable alone still uses local SQLite
    def test_falls_back_when_only_one_variable_is_set(self):
        cases = (
            {storage.DATABASE_URL: 'libsql://example.turso.io'},
            {storage.AUTH_TOKEN: 'test-token'},
        )
        libsql = mock.Mock()
        for extra in cases:
            with self.subTest(env=list(extra)):
                with _env(**extra), mock.patch.dict(sys.modules, {'libsql': libsql}):
                    self.assertFalse(storage.turso_configured())
                    db = storage.connect(self.path)
                    try:
                        self.assertIsInstance(db, sqlite3.Connection)
                    finally:
                        db.close()
                    libsql.connect.assert_not_called()

    # FEATURE 03 — Scenario — both variables select the mocked libSQL client
    def test_uses_libsql_when_both_variables_are_set(self):
        libsql = mock.Mock()
        remote = object()
        libsql.connect.return_value = remote
        url = 'libsql://example.turso.io'
        with _env(**{storage.DATABASE_URL: url, storage.AUTH_TOKEN: 'test-token'}):
            self.assertTrue(storage.turso_configured())
            with mock.patch.dict(sys.modules, {'libsql': libsql}):
                db = storage.connect(self.path, timeout=10)
        self.assertIs(db, remote)
        libsql.connect.assert_called_once_with(database=url, auth_token='test-token')
        self.assertTrue(self.path.is_file())


if __name__ == '__main__':
    unittest.main()
