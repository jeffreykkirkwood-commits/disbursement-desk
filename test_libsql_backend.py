# Re-runs the unittest suite against the libsql client on local files, so
# a plain `python -m unittest` (the existing CI job) also exercises the
# Turso-client path. No network and no credentials are used. A dedicated
# CI job would be Platform Engineer's, because workflows are PE-owned.
"""Run the full suite once more under STORAGE_TEST_BACKEND=libsql-local."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import storage

ROOT = Path(__file__).resolve().parent


class LibsqlBackendSuiteTests(unittest.TestCase):
    # FEATURE 01 — Scenario — the existing unittest job covers libsql
    def test_suite_passes_on_local_libsql(self):
        if os.environ.get(storage.TEST_BACKEND) == storage.LIBSQL_LOCAL:
            self.skipTest('already running under the libsql backend')
        env = os.environ.copy()
        env[storage.TEST_BACKEND] = storage.LIBSQL_LOCAL
        env.pop(storage.DATABASE_URL, None)
        env.pop(storage.AUTH_TOKEN, None)
        result = subprocess.run(
            [sys.executable, '-m', 'unittest'],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        if result.returncode != 0:
            self.fail(result.stderr + '\n' + result.stdout)
        self.assertIn('OK', (result.stderr or '') + (result.stdout or ''))


if __name__ == '__main__':
    unittest.main()
