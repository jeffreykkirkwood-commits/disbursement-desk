# Automated checks for the #30 connection factory: local SQLite when Turso is
# unset, and the libSQL client when both TURSO_DATABASE_URL and
# TURSO_AUTH_TOKEN are set with a safe tenancy mode. Adapter tests use
# libsql on a local file. The Turso-url path is mocked; these tests make no
# network calls and do not use real credentials.
"""Exercise storage.connect() backend selection, tenancy, and the libsql adapter."""
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
           if key not in (storage.DATABASE_URL, storage.AUTH_TOKEN,
                          storage.TEST_BACKEND, storage.TENANT_MODE,
                          storage.ALLOW_SHARED)}
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

    # FEATURE 02b — Scenario — blank or whitespace-only values count as unset
    def test_falls_back_when_variables_are_whitespace(self):
        cases = (
            {storage.DATABASE_URL: '   ', storage.AUTH_TOKEN: '   '},
            {storage.DATABASE_URL: '   ', storage.AUTH_TOKEN: 'test-token'},
            {storage.DATABASE_URL: 'libsql://example.turso.io', storage.AUTH_TOKEN: '\t\n'},
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

    # FEATURE 03 — Scenario — shared Turso requires an explicit single-tenant allow
    def test_uses_libsql_when_shared_turso_is_explicitly_allowed(self):
        libsql = mock.Mock()
        remote = mock.Mock()
        libsql.connect.return_value = remote
        url = 'libsql://example.turso.io'
        with _env(**{storage.DATABASE_URL: url, storage.AUTH_TOKEN: 'test-token',
                     storage.TENANT_MODE: storage.MODE_SHARED,
                     storage.ALLOW_SHARED: '1'}):
            self.assertTrue(storage.turso_configured())
            with mock.patch.dict(sys.modules, {'libsql': libsql}):
                db = storage.connect(self.path, timeout=10)
        try:
            self.assertIsInstance(db, storage.LibsqlConnection)
            self.assertIs(db._conn, remote)
            libsql.connect.assert_called_once_with(database=url, timeout=10,
                                                   auth_token='test-token')
            self.assertTrue(self.path.is_file())
        finally:
            db.close()

    # FEATURE 03b — Scenario — bare Turso URL is refused (no silent shared DB)
    def test_refuses_shared_turso_without_tenancy_mode(self):
        libsql = mock.Mock()
        with _env(**{storage.DATABASE_URL: 'libsql://example.turso.io',
                     storage.AUTH_TOKEN: 'test-token'}):
            with mock.patch.dict(sys.modules, {'libsql': libsql}):
                with self.assertRaises(storage.StorageConfigError) as raised:
                    storage.connect(self.path)
        self.assertIn('tenancy is not configured', str(raised.exception))
        libsql.connect.assert_not_called()

    # FEATURE 03c — Scenario — template mode isolates visitors by tenant URL
    def test_per_tenant_turso_urls_differ_by_demo_folder(self):
        libsql = mock.Mock()
        libsql.connect.return_value = mock.Mock()
        root = Path(self.temp.name)
        first = root / ('a' * 64) / 'payments.sqlite3'
        second = root / ('b' * 64) / 'payments.sqlite3'
        template = 'libsql://desk-{tenant}.turso.io'
        with _env(**{storage.DATABASE_URL: template, storage.AUTH_TOKEN: 'test-token',
                     storage.TENANT_MODE: storage.MODE_TEMPLATE}):
            with mock.patch.dict(sys.modules, {'libsql': libsql}):
                one = storage.connect(first)
                two = storage.connect(second)
                try:
                    self.assertEqual(
                        [call.kwargs['database'] for call in libsql.connect.call_args_list],
                        [f'libsql://desk-{"a" * 64}.turso.io',
                         f'libsql://desk-{"b" * 64}.turso.io'])
                finally:
                    one.close()
                    two.close()
        self.assertNotEqual(storage.tenant_from_path(first), storage.tenant_from_path(second))

    # FEATURE 04 — Scenario — the test backend opens libsql on the local file
    def test_libsql_local_backend_uses_local_file(self):
        with _env(**{storage.TEST_BACKEND: storage.LIBSQL_LOCAL}):
            db = storage.connect(self.path)
            try:
                self.assertIsInstance(db, storage.LibsqlConnection)
                db.execute('CREATE TABLE probe (n INTEGER)')
                db.execute('INSERT INTO probe VALUES (1)')
                db.commit()
                self.assertEqual(db.execute('SELECT n FROM probe').fetchone(), (1,))
            finally:
                db.close()
            with sqlite3.connect(self.path) as check:
                self.assertEqual(check.execute('SELECT n FROM probe').fetchone(), (1,))


class LibsqlAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'payments.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def _connect(self):
        with _env(**{storage.TEST_BACKEND: storage.LIBSQL_LOCAL}):
            return storage.connect(self.path)

    # FEATURE 05 — Scenario — a libsql cursor can be iterated like sqlite3
    def test_execute_is_iterable_and_fetch_methods_agree(self):
        db = self._connect()
        try:
            db.execute('CREATE TABLE items (n INTEGER)')
            db.executemany('INSERT INTO items VALUES (?)', [(1,), (2,), (3,)])
            db.commit()
            rows = [row for row in db.execute('SELECT n FROM items ORDER BY n')]
            self.assertEqual(rows, [(1,), (2,), (3,)])
            self.assertEqual(
                db.execute('SELECT n FROM items ORDER BY n').fetchall(),
                [(1,), (2,), (3,)])
            cursor = db.execute('SELECT n FROM items ORDER BY n')
            self.assertEqual(cursor.fetchone(), (1,))
            self.assertEqual(cursor.fetchall(), [(2,), (3,)])
            self.assertIsNone(cursor.fetchone())
            pragma = list(db.execute('PRAGMA table_info(items)'))
            self.assertEqual(pragma[0][1], 'n')
        finally:
            db.close()

    # FEATURE 06 — Scenario — libsql errors become sqlite3 exceptions
    def test_libsql_errors_become_sqlite3_exceptions(self):
        db = self._connect()
        try:
            db.execute(
                'CREATE TABLE items (n INTEGER PRIMARY KEY, name TEXT UNIQUE)')
            db.execute("INSERT INTO items VALUES (1, 'a')")
            db.commit()
            with self.assertRaises(sqlite3.IntegrityError) as unique:
                db.execute("INSERT INTO items VALUES (2, 'a')")
            self.assertIn('UNIQUE', str(unique.exception))
            with self.assertRaises(sqlite3.OperationalError) as missing:
                db.execute('SELECT * FROM missing')
            self.assertIn('no such table', str(missing.exception))
            self.assertIsInstance(missing.exception, sqlite3.Error)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
