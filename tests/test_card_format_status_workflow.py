"""#47: card-format and issue-link post overwriteable commit statuses (inventory pattern).

Each pull_request edited event used to leave a completed failing Actions check run on
the Checks card. These workflows must post a fixed-context status on the PR head SHA,
always exit 0 after posting, and never interpolate untrusted title/body into run:.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD_WORKFLOW = ROOT / '.github' / 'workflows' / 'card-format.yml'
ISSUE_WORKFLOW = ROOT / '.github' / 'workflows' / 'pr-issue-link.yml'

CARD = (
    '\n## Type\nFix\n## What changes\nA thing.\n## Who notices and how\nNobody.\n'
    '## Files touched\n- a.py\n## Proof\nTests pass.\nInventory: no change\n'
)
GOOD_TITLE = '[Bug fix] Fix a synthetic thing'

STUB_GH = r"""#!/usr/bin/env bash
# Synthetic stand-in for gh, used only inside this test's own PATH.
args="$*"
case "$args" in
  *"-X POST"*) printf '%s\n' "$@" > "$STUB_DIR/status_call" ;;
  *pulls/*) cat "$STUB_DIR/pr.json" ;;
  *) echo "unexpected gh call: $args" >&2; exit 2 ;;
esac
"""


def workflow_run_script(path: Path) -> str:
    text = path.read_text()
    m = re.search(r'^        run: \|\n((?:          .*\n|\s*\n)+)', text, re.M)
    assert m, f'no run: | block in {path}'
    return '\n'.join(l[10:] for l in m.group(1).splitlines())


@unittest.skipUnless(shutil.which('bash') and shutil.which('jq'), 'needs bash and jq')
class CardFormatStatusWorkflow(unittest.TestCase):
    def run_step(self, title, body, author='alice'):
        d = tempfile.mkdtemp()
        runner_temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.addCleanup(shutil.rmtree, runner_temp)
        Path(d, 'pr.json').write_text(json.dumps({
            'head': {'sha': 'abc1234def'},
            'title': title,
            'body': body,
            'user': {'login': author},
        }))
        gh = Path(d, 'bin', 'gh')
        gh.parent.mkdir()
        gh.write_text(STUB_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        env = {
            'PATH': f"{gh.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            'HOME': d,
            'GH_TOKEN': 'synthetic',
            'REPO': 'example/repo',
            'PR_NUMBER': '47',
            'RUN_URL': 'https://example.invalid/run',
            'RUNNER_TEMP': runner_temp,
            'STUB_DIR': d,
        }
        p = subprocess.run(
            ['bash', '-e', '-c', workflow_run_script(CARD_WORKFLOW)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        call = Path(d, 'status_call')
        return p, (call.read_text().splitlines() if call.exists() else None)

    def test_pass_posts_success_and_job_exits_0(self):
        p, call = self.run_step(GOOD_TITLE, CARD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNotNone(call)
        self.assertIn('repos/example/repo/statuses/abc1234def', call)
        self.assertIn('state=success', call)
        self.assertIn('context=check-card-format', call)

    def test_fail_still_exits_0_and_posts_failure_status(self):
        # Body edit that still fails must not leave the Actions job red (#47).
        p, call = self.run_step('No category title', CARD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('state=failure', call)
        self.assertIn('context=check-card-format', call)
        self.assertTrue(any('description=' in line for line in call))


@unittest.skipUnless(shutil.which('bash') and shutil.which('jq'), 'needs bash and jq')
class IssueLinkStatusWorkflow(unittest.TestCase):
    def run_step(self, body, author='alice'):
        d = tempfile.mkdtemp()
        runner_temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.addCleanup(shutil.rmtree, runner_temp)
        Path(d, 'pr.json').write_text(json.dumps({
            'head': {'sha': 'def5678abc'},
            'body': body,
            'user': {'login': author},
        }))
        gh = Path(d, 'bin', 'gh')
        gh.parent.mkdir()
        gh.write_text(STUB_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        env = {
            'PATH': f"{gh.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            'HOME': d,
            'GH_TOKEN': 'synthetic',
            'REPO': 'example/repo',
            'PR_NUMBER': '47',
            'RUN_URL': 'https://example.invalid/run',
            'RUNNER_TEMP': runner_temp,
            'STUB_DIR': d,
        }
        p = subprocess.run(
            ['bash', '-e', '-c', workflow_run_script(ISSUE_WORKFLOW)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        call = Path(d, 'status_call')
        return p, (call.read_text().splitlines() if call.exists() else None)

    def test_pass_posts_success(self):
        p, call = self.run_step('Refs #122\n' + CARD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('state=success', call)
        self.assertIn('context=check-issue-link', call)

    def test_fail_posts_failure_and_job_exits_0(self):
        p, call = self.run_step(CARD)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('state=failure', call)
        self.assertIn('context=check-issue-link', call)


class WorkflowShape(unittest.TestCase):
    def test_card_format_status_permissions_and_context(self):
        text = CARD_WORKFLOW.read_text()
        self.assertIn('statuses: write', text)
        self.assertIn('context=check-card-format', text)
        self.assertIn('set-card-format-status:', text)
        self.assertNotIn('jobs:\n  check-card-format:', text)
        self.assertIn("!= 'dependabot[bot]'", text)
        self.assertIn('#47', text)
        for bad in ('pull_request.body', 'pull_request.title', 'head_ref', 'pull_request_target'):
            self.assertNotIn(bad, text, bad)

    def test_issue_link_status_permissions_and_context(self):
        text = ISSUE_WORKFLOW.read_text()
        self.assertIn('statuses: write', text)
        self.assertIn('context=check-issue-link', text)
        self.assertIn('set-issue-link-status:', text)
        self.assertNotIn('jobs:\n  check-issue-link:', text)
        self.assertIn("!= 'dependabot[bot]'", text)
        self.assertIn('#47', text)
        for bad in ('pull_request.body', 'pull_request.title', 'head_ref', 'pull_request_target'):
            self.assertNotIn(bad, text, bad)

    def test_both_refetch_pr_via_api(self):
        for path in (CARD_WORKFLOW, ISSUE_WORKFLOW):
            text = path.read_text()
            self.assertIn('gh api "repos/$REPO/pulls/$PR_NUMBER"', text)
            self.assertIn('statuses/$HEAD_SHA', text)


if __name__ == '__main__':
    unittest.main()
