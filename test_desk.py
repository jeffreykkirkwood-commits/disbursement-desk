# Automated checks for the Disbursement Desk queue (/desk, issue #32 slice 1): what a
# viewer sees first, the All filter without history rows, the empty and load-error
# states, the header links both ways, and that opening the page writes nothing.
"""Exercise /desk through the hosted app with temporary fictional demos."""
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from hosted_app import create_app
import payouts

EXCEPTION_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038']
ALL_IDS = EXCEPTION_IDS + ['PO-1037', 'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']
ROW = re.compile(r'<li data-ex="(\d)"( hidden)?><div class="row"><span class="payee">(.*?)</span>'
                 r'<span class="amt">(.*?)</span><span class="sub">(.*?)</span>'
                 r'<span class="pill (s-\w+)">(.*?)</span></div></li>')


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, 'https://demo.example')
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()

    def get(self, path='/desk'):
        return self.client.get(path, base_url='https://demo.example')

    def rows(self, html):
        return [dict(zip(('ex', 'hidden', 'payee', 'amount', 'sub', 'css', 'word'), m.groups()))
                for m in ROW.finditer(html)]

    def database(self):
        return next(Path(self.temp.name).rglob('payments.sqlite3'))

    # Scenario — the Desk opens on the 4 starting exceptions with the approved chrome
    def test_opens_on_exceptions(self):
        reply = self.get()
        self.assertEqual(reply.status_code, 200)
        html = reply.get_data(as_text=True)
        for words in ('<title>Disbursement Desk · Example data</title>', '>Disbursement Desk</div>',
                      'Example data · fake money', '<h1 id="qh">Outgoing payouts</h1>',
                      'aria-pressed="true">Exceptions <b>4</b></button>', 'aria-pressed="false">All <b>10</b></button>',
                      'Your changes stay in this browser only.', '>Reset demo</button>'):
            self.assertIn(words, html)
        rows = self.rows(html)
        visible = [r for r in rows if not r['hidden']]
        self.assertEqual([r['sub'].split(' · ')[0] for r in visible], EXCEPTION_IDS)
        self.assertEqual([r['word'] for r in visible], ['Needs review'] * 3 + ['Held'])
        first = visible[0]
        self.assertEqual((first['payee'], first['amount'], first['sub']),
                         ('Acme Supplies', '$4,850.00', 'PO-1042 · Amount is 6× this payee’s usual payout ($790 avg)'))
        self.assertIn('PO-1043 · Payee bank details changed 2 days ago', html)
        self.assertIn('PO-1044 · Same payee and amount as yesterday (possible duplicate)', html)
        self.assertIn('PO-1038 · Amount is 4× this payee’s usual payout ($520 avg)', html)
        # The list is the first paint: no loading sentence, no error, empty message hidden.
        self.assertNotIn('oading', html)
        self.assertNotIn('couldn’t be loaded', html)
        self.assertIn('<li class="empty" data-empty hidden>No exceptions left. Nice.</li>', html)

    # Scenario — All lists the 10 current-queue payouts only, in plain status words
    def test_all_is_current_queue_without_history(self):
        html = self.get().get_data(as_text=True)
        rows = self.rows(html)
        self.assertEqual([r['sub'].split(' · ')[0] for r in rows], ALL_IDS)
        self.assertEqual(len(payouts.list_payouts(self.database())), 40)  # history exists, not listed
        words = {r['sub'].split(' · ')[0]: r['word'] for r in rows}
        self.assertEqual(words['PO-1037'], 'Returned')
        self.assertEqual(words['PO-1036'], 'Sent')
        self.assertEqual({r['word'] for r in rows}, {'Needs review', 'Held', 'Returned', 'Sent'})
        for code in ('FLAGGED', 'PENDING', 'APPROVED', 'HELD<', 'PAID', 'RETURNED'):
            self.assertNotIn(code, html)
        # Sent rows without a flag show the rail.
        self.assertIn('PO-1040 · ACH', html)
        self.assertIn('PO-1041 · RTP', html)
        # Only non-exceptions start hidden (the page opens on Exceptions).
        self.assertTrue(all((r['ex'] == '1') == (r['hidden'] is None) for r in rows))

    # Scenario — no exceptions left shows the approved empty sentence
    def test_empty_exceptions(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET status = 'PAID', flag_text = NULL WHERE status IN ('FLAGGED', 'HELD')")
        html = self.get().get_data(as_text=True)
        self.assertIn('Exceptions <b>0</b>', html)
        self.assertIn('All <b>10</b>', html)
        self.assertIn('<li class="empty" data-empty>No exceptions left. Nice.</li>', html)
        self.assertTrue(all(r['hidden'] for r in self.rows(html)))

    # Scenario — the queue cannot be read: the page still opens with the approved sentence
    def test_load_error(self):
        with mock.patch('payouts.current_queue', side_effect=sqlite3.OperationalError('disk I/O error')):
            reply = self.get()
        self.assertEqual(reply.status_code, 503)
        html = reply.get_data(as_text=True)
        self.assertIn('<li class="err" role="alert">The payout list couldn’t be loaded. Refresh the page to try again.</li>', html)
        self.assertIn('Example data · fake money', html)
        self.assertNotIn('disk I/O', html)
        self.assertEqual(self.rows(html), [])

    # Scenario — one text link each way between Payments Lab and the Desk (Q1 option B)
    def test_header_links_both_ways(self):
        desk = self.get().get_data(as_text=True)
        self.assertIn('<a class="lab-link" href="/">Payments Lab</a><span class="tag">Example data · fake money</span>', desk)
        lab = self.get('/dashboard').get_data(as_text=True)
        self.assertIn('<a class="desk-link" href="/desk">Disbursement Desk</a>', lab)
        self.assertEqual(self.get('/').headers['Location'], '/dashboard')

    # Scenario — read-only: opening the Desk changes no payout and logs no action
    def test_opening_desk_writes_nothing(self):
        self.get()
        before = json.dumps(payouts.list_payouts(self.database()), sort_keys=True)
        for _ in range(2):
            self.get()
        self.assertEqual(json.dumps(payouts.list_payouts(self.database()), sort_keys=True), before)
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0], 0)
        html = self.get().get_data(as_text=True)
        for word in ('Approve', 'Hold', 'Return<', 'AI NOTE', 'RULES NOTE', '<form'):
            self.assertNotIn(word, html)

    # Scenario — the filter script is served without starting a demo
    def test_script_does_not_create_demo(self):
        reply = self.get('/desk.js')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.mimetype, 'text/javascript')
        self.assertNotIn('Set-Cookie', reply.headers)
        self.assertEqual(list(Path(self.temp.name).rglob('payments.sqlite3')), [])

    # Scenario — names are escaped as text
    def test_payee_text_is_escaped(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET payee_name = '<b>Evil</b>' WHERE payout_id = 'PO-1042'")
        html = self.get().get_data(as_text=True)
        self.assertIn('&lt;b&gt;Evil&lt;/b&gt;', html)
        self.assertNotIn('<b>Evil</b>', html)


class CurrentQueueTests(unittest.TestCase):
    def test_current_queue_order_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'payments.sqlite3'
            payouts.ensure_seeded(db)
            queue = payouts.current_queue(db)
        self.assertEqual([p['payout_id'] for p in queue], ALL_IDS)
        self.assertEqual({p['payout_id'] for p in queue}, set(payouts.CURRENT_IDS))


if __name__ == '__main__':
    unittest.main()
