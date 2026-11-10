# Automated checks for the Disbursement Desk (/desk, issue #32 slices 1 and 2): what a
# viewer sees first, the All filter without history rows, the empty and load-error
# states, the header links both ways, each payout's read-only detail, and that opening
# the page writes nothing.
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
ROW = re.compile(r'<li data-ex="(\d)"( hidden)?><button type="button" class="row" data-id="PO-\d+" '
                 r'aria-current="false"><span class="payee">(.*?)</span>'
                 r'<span class="amt">(.*?)</span><span class="sub">(.*?)</span>'
                 r'<span class="pill (s-\w+)">(.*?)</span></button></li>')
PANEL = re.compile(r'<div class="panel" data-id="(PO-\d+)" hidden>(.*?)</div>(?=<div class="panel"|</aside>)', re.S)


class DeskBase(unittest.TestCase):
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


class DeskTests(DeskBase):
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


class DeskDetailTests(DeskBase):
    def panels(self, html=None):
        return dict(PANEL.findall(html or self.get().get_data(as_text=True)))

    # Scenario — every current-queue payout has exactly one hidden detail panel, none for history
    def test_one_panel_per_current_payout(self):
        html = self.get().get_data(as_text=True)
        self.assertEqual(list(self.panels(html)), ALL_IDS)
        self.assertIn('<aside id="detail" class="detail" aria-label="Payout detail" hidden>', html)
        self.assertNotIn('data-id="PO-1001"', html)

    # Scenario — a Needs review payout shows its fields and why it was flagged
    def test_needs_review_detail(self):
        panel = self.panels()['PO-1043']
        for words in ('<button type="button" class="back" aria-label="Back to list">← Back</button>',
                      '<span class="pill s-review">Needs review</span>', '<h2 tabindex="-1">Bluebird Bakery</h2>',
                      '<p class="big">$312.40</p>', '<dt>Payout</dt><dd>PO-1043</dd>',
                      '<dt>To account</dt><dd>DEMO-BLUE-002</dd>', '<dt>Rail</dt><dd>RTP · USD</dd>',
                      '<div class="flag"><strong>Why flagged:</strong> Payee bank details changed 2 days ago</div>'):
            self.assertIn(words, panel)
        self.assertNotIn('This payout is final', panel)

    # Scenario — Held shows its flag and no final message; no action buttons in slice 2
    def test_held_detail_has_no_actions(self):
        panel = self.panels()['PO-1038']
        self.assertIn('<span class="pill s-held">Held</span>', panel)
        self.assertIn('Why flagged:</strong> Amount is 4× this payee’s usual payout ($520 avg)', panel)
        self.assertNotIn('This payout is final', panel)
        for word in ('Approve', 'Hold<', '>Return<', 'class="actions"', 'class="btn'):
            self.assertNotIn(word, panel)

    # Scenario — Sent and Returned payouts are final: message, no buttons
    def test_final_payouts(self):
        panels = self.panels()
        returned, sent = panels['PO-1037'], panels['PO-1040']
        self.assertIn('<span class="pill s-returned">Returned</span>', returned)
        self.assertIn('Why flagged:</strong> Payee bank details changed 1 day ago', returned)
        self.assertIn('<span class="pill s-sent">Sent</span>', sent)
        self.assertNotIn('Why flagged', sent)
        self.assertIn('<dt>Rail</dt><dd>ACH · USD</dd>', sent)
        for panel in (returned, sent):
            self.assertIn('<p class="done">This payout is final. No actions available.</p>', panel)
            self.assertNotIn('class="btn', panel)

    # Scenario — no note block on any payout (notes come after #37)
    def test_no_note_until_stored(self):
        for panel in self.panels().values():
            for word in ('class="note"', 'RULES NOTE', 'AI NOTE', 'Written by AI', 'From fixed rules'):
                self.assertNotIn(word, panel)

    # Scenario — no note block even when a note is stored: notes come after #37 in their own PR
    def test_stored_note_is_not_rendered(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET note_text = 'Stored note', note_source = 'rules', note_fallback = 0 "
                       "WHERE payout_id = 'PO-1043'")
        html = self.get().get_data(as_text=True)
        self.assertNotIn('Stored note', html)
        self.assertNotIn('class="note"', html)

    # Scenario — the load-error page has no detail panels
    def test_load_error_has_no_panels(self):
        with mock.patch('payouts.current_queue', side_effect=sqlite3.OperationalError('locked')):
            html = self.get().get_data(as_text=True)
        self.assertEqual(self.panels(html), {})

    # Scenario — detail text is escaped
    def test_detail_text_is_escaped(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET payee_name = '<i>X</i>', flag_text = '<script>y</script>' WHERE payout_id = 'PO-1042'")
        panel = self.panels()['PO-1042']
        self.assertIn('&lt;i&gt;X&lt;/i&gt;', panel)
        self.assertIn('&lt;script&gt;y&lt;/script&gt;', panel)
        self.assertNotIn('<script>', panel)


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
