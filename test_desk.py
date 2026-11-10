# Automated checks for the Disbursement Desk (/desk, issue #32 slices 1 to 5): what a
# viewer sees first, the All filter without history rows, the empty and load-error
# states, the header links both ways, each payout's detail, the Approve / Hold / Return
# buttons and confirm dialog words (slice 3), Reset demo (slice 4), the stored
# rules notes and AI-unavailable fallback (slice 5, Q3 option A), that opening
# the page writes nothing, and that a payout flagged after the seed appears in
# the queue (#80).
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
import desk
import payout_rules
import payouts

EXCEPTION_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038']
ALL_IDS = EXCEPTION_IDS + ['PO-1037', 'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']
ROW = re.compile(r'<li data-ex="(\d)"( hidden)?><button type="button" class="row" data-id="PO-\d+" '
                 r'aria-current="false"><span class="payee">(.*?)</span>'
                 r'<span class="amt">(.*?)</span><span class="sub">(.*?)</span>'
                 r'<span class="pill (s-\w+)">(.*?)</span></button></li>')
PANEL = re.compile(r'<div class="panel" data-id="(PO-\d+)"[^>]* hidden>(.*?)</div>(?=<div class="panel"|</aside>)', re.S)


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
                      'Your changes stay in this browser only.', '>Reset demo</button>',
                      '<span id="r-error" class="err-inline" role="status" hidden>'
                      'That didn’t reset. Nothing has changed. Try again.</span>'):
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
        self.assertNotIn('<form', html)

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
    def test_held_detail(self):
        panel = self.panels()['PO-1038']
        self.assertIn('<span class="pill s-held">Held</span>', panel)
        self.assertIn('Why flagged:</strong> Amount is 4× this payee’s usual payout ($520 avg)', panel)
        self.assertNotIn('This payout is final', panel)

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

    # Scenario — the Desk shows the stored #37 note text under RULES NOTE / From fixed rules
    def test_stored_note_is_rendered(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET note_text = 'Stored note', note_source = 'rules', note_fallback = 0 "
                       "WHERE payout_id = 'PO-1043'")
        html = self.get().get_data(as_text=True)
        panel = self.panels(html)['PO-1043']
        self.assertIn('Stored note', panel)
        self.assertIn('<span class="badge b-rules">RULES NOTE</span>From fixed rules', panel)
        self.assertNotIn('Rule BANK_CHANGED_7D', panel)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI NOTE', panel)

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


APPROVE_BUTTON = ('<button type="button" class="btn primary" data-act="approve" data-heading="Approve and send?" '
                  'data-body="The payout is sent to the payee." data-ok="Approve" data-ok-class="btn primary">Approve</button>')
HOLD_BUTTON = ('<button type="button" class="btn" data-act="hold" data-heading="Hold this payout?" '
               'data-body="It stays in Exceptions and will not send until someone approves it." data-ok="Hold" '
               'data-ok-class="btn primary"{}>Hold</button>')
RETURN_BUTTON = ('<button type="button" class="btn danger" data-act="return" data-heading="Return this payout?" '
                 'data-body="It is cancelled and no money moves. This cannot be undone." data-ok="Return" '
                 'data-ok-class="btn danger fill">Return</button>')


class DeskActionTests(DeskBase):
    def panels(self):
        return dict(PANEL.findall(self.get().get_data(as_text=True)))

    def decide(self, payout_id, status, when='2026-11-10T16:00:00+00:00'):
        # Stand-in for the #35 decision API (PR #51): change status and updated_at only.
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute('UPDATE payouts SET status = ?, updated_at = ? WHERE payout_id = ?', (status, when, payout_id))

    # Scenario — Needs review payouts get Approve, Hold and Return with the #40 dialog words
    def test_needs_review_buttons(self):
        panel = self.panels()['PO-1043']
        self.assertIn('<div class="actions">' + APPROVE_BUTTON + HOLD_BUTTON.format('') + RETURN_BUTTON + '</div>', panel)
        self.assertIn('data-payee="Bluebird Bakery" data-amount="$312.40"', self.get().get_data(as_text=True))

    # Scenario — a Held payout can still be approved or returned; Hold is disabled
    def test_held_buttons(self):
        panel = self.panels()['PO-1038']
        self.assertIn(APPROVE_BUTTON + HOLD_BUTTON.format(' disabled') + RETURN_BUTTON, panel)

    # Scenario — Sent and Returned payouts have no action buttons
    def test_final_payouts_have_no_buttons(self):
        panels = self.panels()
        for pid in ('PO-1037', 'PO-1036', 'PO-1040'):
            self.assertNotIn('data-act=', panels[pid])
            self.assertIn('This payout is final. No actions available.', panels[pid])

    # Scenario — one confirm dialog: Cancel, a confirm button, and the Q2 A failure line, hidden
    def test_confirm_dialog_markup(self):
        html = self.get().get_data(as_text=True)
        self.assertEqual(html.count('<dialog'), 1)
        self.assertIn('<dialog id="confirm" aria-labelledby="ch">', html)
        self.assertIn('<p><strong id="c-payee"></strong> · <span id="c-amount"></span> · <span id="c-id"></span></p>', html)
        self.assertIn('<p id="c-error" class="err-inline" role="alert" hidden>'
                      'That didn’t save. Nothing has changed. Try again.</p>', html)
        self.assertIn('<button type="button" class="btn" id="cancel">Cancel</button>', html)
        for word in ('<select', '<textarea', 'reason', 'Reason', 'note'):
            self.assertNotIn(word, html.split('<dialog')[1])

    # Scenario — after an operator Hold: Held, "Held by operator", still in Exceptions, Hold disabled
    def test_after_hold(self):
        self.decide('PO-1042', 'HELD')
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', html)
        self.assertIn('Exceptions <b>4</b>', html)
        panel = dict(PANEL.findall(html))['PO-1042']
        self.assertIn('Why flagged:</strong> Held by operator', panel)
        self.assertIn(HOLD_BUTTON.format(' disabled'), panel)
        self.assertIn(APPROVE_BUTTON, panel)
        self.assertIn(RETURN_BUTTON, panel)

    # Scenario — after an operator Return: Returned, "Returned by operator", final, no buttons
    def test_after_return(self):
        self.decide('PO-1044', 'RETURNED')
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1044 · Returned by operator', html)
        self.assertIn('Exceptions <b>3</b>', html)
        panel = dict(PANEL.findall(html))['PO-1044']
        self.assertIn('<span class="pill s-returned">Returned</span>', panel)
        self.assertIn('Why flagged:</strong> Returned by operator', panel)
        self.assertIn('This payout is final. No actions available.', panel)
        self.assertNotIn('data-act=', panel)

    # Scenario — after Approve: Sent, the why-flagged line is gone, final, no buttons
    def test_after_approve(self):
        self.decide('PO-1043', 'PAID')
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1043 · RTP', html)
        panel = dict(PANEL.findall(html))['PO-1043']
        self.assertIn('<span class="pill s-sent">Sent</span>', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertIn('This payout is final. No actions available.', panel)
        self.assertNotIn('data-act=', panel)

    # Scenario — the seeded Held and Returned payouts keep their stored rule sentence
    def test_seeded_flags_unchanged(self):
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1038 · Amount is 4× this payee’s usual payout ($520 avg)', html)
        self.assertIn('PO-1037 · Payee bank details changed 1 day ago', html)
        self.assertNotIn('by operator', html)

    # Scenario — the page script posts to the #35 decision API with a key, never a reason
    def test_script_uses_decision_api(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("fetch('/payouts/' + encodeURIComponent(pending.id) + '/' + pending.action", script)
        self.assertIn("'Idempotency-Key': pending.key", script)
        self.assertIn("JSON.stringify({reviewer: 'Desk operator'})", script)
        self.assertNotIn('reason', script)

    # Scenario — end to end through the #35 route, once PR #51 is merged (skipped before that)
    def test_decisions_through_api(self):
        rules = {str(rule) for rule in self.app.url_map.iter_rules()}
        if not any(r.startswith('/payouts/') for r in rules):
            self.skipTest('Needs the #35 decision API (PR #51).')
        page = self.get()
        demo = page.headers['X-Demo-ID']
        def post(pid, action, key):
            return self.client.post(f'/payouts/{pid}/{action}', json={'reviewer': 'Desk operator'},
                                    base_url='https://demo.example',
                                    headers={'Idempotency-Key': key, 'Origin': 'https://demo.example',
                                             'X-Demo-ID': demo})
        self.assertEqual(post('PO-1042', 'hold', 'k1').status_code, 201)
        self.assertEqual(post('PO-1042', 'hold', 'k1').status_code, 200)  # same key: saved once
        self.assertEqual(post('PO-1043', 'return', 'k2').status_code, 201)
        self.assertEqual(post('PO-1044', 'approve', 'k3').status_code, 201)
        self.assertEqual(post('PO-1038', 'approve', 'k4').status_code, 201)  # Held can be approved
        html = self.get().get_data(as_text=True)
        panels = dict(PANEL.findall(html))
        self.assertIn('PO-1042 · Held by operator', html)
        self.assertIn('PO-1043 · Returned by operator', html)
        self.assertNotIn('Why flagged', panels['PO-1044'])
        self.assertIn('<span class="pill s-sent">Sent</span>', panels['PO-1038'])
        self.assertIn('Exceptions <b>1</b>', html)
        for code in ('APPROVED', 'PENDING'):
            self.assertNotIn(code, html)


class DeskResetTests(DeskBase):
    def decide(self, payout_id, action, key='k1', client=None):
        client = client or self.client
        page = client.get('/desk', base_url='https://demo.example')
        demo = page.headers['X-Demo-ID']
        return client.post(f'/payouts/{payout_id}/{action}', json={'reviewer': 'Desk operator'},
                           base_url='https://demo.example',
                           headers={'Idempotency-Key': key, 'Origin': 'https://demo.example',
                                    'X-Demo-ID': demo})

    def reset(self, client=None, **headers):
        client = client or self.client
        page = client.get('/desk', base_url='https://demo.example')
        demo = page.headers.get('X-Demo-ID')
        extra = {'Origin': 'https://demo.example', 'X-Demo-ID': demo}
        extra.update(headers)
        return client.post('/payouts/reset', base_url='https://demo.example', headers=extra)

    # Scenario — after a Hold, this same browser still shows Held on reload
    def test_hold_survives_reload(self):
        self.assertEqual(self.decide('PO-1042', 'hold').status_code, 201)
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', html)
        self.assertIn('Exceptions <b>4</b>', html)
        self.assertIn('<span class="pill s-held">Held</span>', dict(PANEL.findall(html))['PO-1042'])

    # Scenario — Reset demo restores the starting 10 current-queue payouts and 4 exceptions
    def test_reset_restores_starting_set(self):
        self.assertEqual(self.decide('PO-1042', 'hold').status_code, 201)
        self.assertEqual(self.decide('PO-1043', 'return', 'k2').status_code, 201)
        self.assertEqual(self.decide('PO-1044', 'approve', 'k3').status_code, 201)
        before = self.get().get_data(as_text=True)
        self.assertIn('Exceptions <b>2</b>', before)
        self.assertIn('Held by operator', before)
        reply = self.reset()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json['restored'], 40)
        html = self.get().get_data(as_text=True)
        self.assertIn('Exceptions <b>4</b>', html)
        self.assertIn('All <b>10</b>', html)
        rows = self.rows(html)
        visible = [r for r in rows if not r['hidden']]
        self.assertEqual([r['sub'].split(' · ')[0] for r in visible], EXCEPTION_IDS)
        self.assertEqual([r['word'] for r in visible], ['Needs review'] * 3 + ['Held'])
        self.assertIn('PO-1042 · Amount is 6× this payee’s usual payout ($790 avg)', html)
        self.assertIn('PO-1043 · Payee bank details changed 2 days ago', html)
        self.assertIn('PO-1044 · Same payee and amount as yesterday (possible duplicate)', html)
        self.assertIn('PO-1038 · Amount is 4× this payee’s usual payout ($520 avg)', html)
        self.assertNotIn('Held by operator', html)
        self.assertNotIn('Returned by operator', html)
        self.assertIn('<aside id="detail" class="detail" aria-label="Payout detail" hidden>', html)
        self.assertIn('Your changes stay in this browser only.', html)
        self.assertIn('>Reset demo</button>', html)
        self.assertIn('Example data · fake money', html)
        with closing(sqlite3.connect(str(self.database()))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payout_events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payouts').fetchone()[0], 40)

    # Scenario — a second browser still sees the original seed; Reset demo affects only this viewer
    def test_reset_only_this_viewer(self):
        first, second = self.app.test_client(), self.app.test_client()
        self.assertEqual(self.decide('PO-1042', 'hold', client=first).status_code, 201)
        first_html = first.get('/desk', base_url='https://demo.example').get_data(as_text=True)
        second_html = second.get('/desk', base_url='https://demo.example').get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', first_html)
        self.assertIn('PO-1042 · Amount is 6× this payee’s usual payout ($790 avg)', second_html)
        self.assertNotIn('Held by operator', second_html)
        self.assertIn('Exceptions <b>4</b>', second_html)
        self.assertEqual(self.reset(client=first).status_code, 200)
        first_after = first.get('/desk', base_url='https://demo.example').get_data(as_text=True)
        second_after = second.get('/desk', base_url='https://demo.example').get_data(as_text=True)
        self.assertIn('PO-1042 · Amount is 6× this payee’s usual payout ($790 avg)', first_after)
        self.assertNotIn('Held by operator', first_after)
        self.assertEqual(second_after, second_html)
        databases = sorted(Path(self.temp.name).rglob('payments.sqlite3'))
        self.assertEqual(len(databases), 2)
        for db in databases:
            self.assertEqual(payouts.get_payout(db, 'PO-1042')['status'], 'FLAGGED')
            self.assertEqual(len(payouts.list_payouts(db)), 40)

    # Scenario — Reset demo cannot run without this viewer's demo, or from another site
    def test_reset_needs_own_session(self):
        self.assertEqual(self.decide('PO-1042', 'hold').status_code, 201)
        stranger = self.app.test_client()
        refused = stranger.post('/payouts/reset', base_url='https://demo.example',
                                headers={'Origin': 'https://demo.example'})
        self.assertEqual(refused.status_code, 409)
        foreign = self.client.post('/payouts/reset', base_url='https://demo.example',
                                   headers={'Origin': 'https://evil.example',
                                            'X-Demo-ID': self.get().headers['X-Demo-ID']})
        self.assertEqual(foreign.status_code, 403)
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', html)

    # Scenario — a failed reset changes nothing and does not show internal details
    def test_failed_reset_changes_nothing(self):
        self.assertEqual(self.decide('PO-1042', 'hold').status_code, 201)
        before = json.dumps(payouts.list_payouts(self.database()), sort_keys=True)
        with mock.patch('payouts.reset', side_effect=sqlite3.OperationalError('disk I/O error')):
            reply = self.reset()
        self.assertEqual(reply.status_code, 500)
        body = reply.get_data(as_text=True)
        self.assertNotIn('disk I/O', body)
        self.assertNotIn('OperationalError', body)
        self.assertNotIn('That didn’t reset. Nothing has changed. Try again.', body)
        self.assertEqual(json.dumps(payouts.list_payouts(self.database()), sort_keys=True), before)
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', html)
        # The approved line is on the page, hidden, ready for the script to show. A
        # reload keeps it hidden (the line only appears after a failed POST in-page).
        self.assertIn('<span id="r-error" class="err-inline" role="status" hidden>'
                      'That didn’t reset. Nothing has changed. Try again.</span>', html)
        self.assertNotIn('disk I/O', html)

    # Scenario — the page script posts Reset demo and then drops the hash, so detail closes
    def test_script_wires_reset(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("fetch('/payouts/reset'", script)
        self.assertIn("'X-Demo-ID': demo ? demo.content : ''", script)
        self.assertIn('location.replace(location.pathname + location.search)', script)
        self.assertIn("closest('#reset')", script)
        self.assertIn("getElementById('r-error')", script)
        self.assertIn('resetError.hidden = false', script)
        self.assertIn('resetError.hidden = true', script)
        # No confirm dialog for Reset. Badge wording stays in the HTML. The
        # failed-reset line lives in the HTML (option A, c6774718893); the script
        # never copies the API body onto the page.
        self.assertNotIn('confirm=reset', script)
        self.assertNotIn('RULES NOTE', script)
        self.assertNotIn('AI NOTE', script)
        self.assertNotIn('reply.json', script)
        self.assertNotIn('reply.text', script)
        self.assertNotIn('.json()', script)
        self.assertNotIn('internal_error', script)
        # The approved sentence is in the page HTML, not built from the response.
        self.assertNotIn('That didn’t reset. Nothing has changed. Try again.', script)

    # Scenario — the failed-reset line is the exact approved sentence, next to Reset demo
    def test_failed_reset_wording_is_exact(self):
        html = self.get().get_data(as_text=True)
        foot = html[html.index('class="foot"'):html.index('id="detail"')]
        self.assertIn('>Reset demo</button>', foot)
        self.assertIn('<span id="r-error" class="err-inline" role="status" hidden>'
                      'That didn’t reset. Nothing has changed. Try again.</span>', foot)
        # Curly apostrophe (U+2019), never a straight quote or the slice-3 sentence.
        self.assertIn('didn’t reset', foot)
        self.assertNotIn("didn't reset", foot)
        self.assertNotIn('didn’t save', foot)
        self.assertEqual(foot.count('That didn’t reset. Nothing has changed. Try again.'), 1)



class DeskNoteTests(DeskBase):
    def panels(self, html=None):
        return dict(PANEL.findall(html or self.get().get_data(as_text=True)))

    def visible_note(self, panel):
        # The first note block in the panel is the default (not hidden).
        return [block for block in panel.split('<div class="note"')[1:]
                if not block.startswith(' data-note="fallback" hidden')
                and not block.startswith(' data-note="ai" hidden')
                and not block.startswith(' data-note="rules" hidden')][0]

    # Scenario — Harbor Hardware shows its stored amount-flag note as a rules note
    def test_harbor_shows_stored_rules_note(self):
        panel = self.panels()['PO-1038']
        note = self.visible_note(panel)
        self.assertIn('data-note="rules"', note)
        self.assertIn('<span class="badge b-rules">RULES NOTE</span>From fixed rules', note)
        self.assertIn('Rule AMOUNT_OVER_3X_AVG: Amount is 4× this payee’s usual payout ($520 avg). Review before sending.', note)
        self.assertNotIn('AI NOTE', note)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('operator', note.lower())
        self.assertNotIn('MANUAL_HOLD', note)
        self.assertIn('data-note="fallback" hidden', panel)
        self.assertIn('AI unavailable, rules fallback', panel)
        self.assertNotIn('AI-drafted, review required', panel)

    # Scenario — Cedar Cleaning Co. shows the stored clean-payout rules note
    def test_cedar_shows_stored_clean_note(self):
        panel = self.panels()['PO-1040']
        note = self.visible_note(panel)
        self.assertIn('data-note="rules"', note)
        self.assertIn('<span class="badge b-rules">RULES NOTE</span>From fixed rules', note)
        self.assertIn('No rules triggered. Within normal range for this payee.', note)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI NOTE', panel)

    # Scenario — Bluebird Bakery shows its stored bank-change note as a rules note
    def test_bluebird_rules_note(self):
        panel = self.panels()['PO-1043']
        note = self.visible_note(panel)
        self.assertIn('data-note="rules"', note)
        self.assertIn('<span class="badge b-rules">RULES NOTE</span>From fixed rules', note)
        self.assertIn('Rule BANK_CHANGED_7D: bank details changed within 7 days. Confirm the change with the payee by phone before sending.', note)
        self.assertNotIn('AI NOTE', panel)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI-drafted, review required', panel)
        self.assertIn('data-note="fallback" hidden', panel)

    # Scenario — every current-queue payout has one default stored rules note
    def test_every_payout_has_one_default_note(self):
        panels = self.panels()
        self.assertEqual(list(panels), ALL_IDS)
        for pid, panel in panels.items():
            visible = []
            hidden = []
            for raw in panel.split('<div class="note"')[1:]:
                tag, _, _rest = raw.partition('>')
                (hidden if ' hidden' in tag else visible).append(tag)
            self.assertEqual(len(visible), 1, pid)
            self.assertIn('data-note="rules"', visible[0], pid)
            self.assertEqual(len(hidden), 1, pid)
            self.assertIn('data-note="fallback"', hidden[0], pid)
        html = self.get().get_data(as_text=True)
        self.assertNotIn('AI-drafted, review required', html)
        self.assertNotIn('Written by AI', html)

    # Scenario — Written by AI never appears in the rendered Desk for any seeded payout
    def test_written_by_ai_never_appears_for_seeded_payouts(self):
        html = self.get().get_data(as_text=True)
        self.assertNotIn('Written by AI', html)
        self.assertNotIn('AI NOTE', html)
        for pid, panel in self.panels(html).items():
            self.assertNotIn('Written by AI', panel, pid)
            self.assertNotIn('AI NOTE', panel, pid)
            self.assertIn('RULES NOTE', panel, pid)

    # Scenario — off, slow (over 3s) or error shows the approved fallback helper
    # on the stored note, never Written by AI
    def test_ai_unavailable_uses_fallback(self):
        self.get()
        acme = next(p for p in payouts.current_queue(self.database()) if p['payout_id'] == 'PO-1042')
        bluebird = next(p for p in payouts.current_queue(self.database()) if p['payout_id'] == 'PO-1043')
        cedar = next(p for p in payouts.current_queue(self.database()) if p['payout_id'] == 'PO-1040')
        acme_body = acme['note_text']
        for kwargs in ({'ai_off': True}, {'error': True}, {'elapsed_seconds': 3.01}):
            note = desk.shown_note(acme, **kwargs)
            self.assertEqual(note['badge'], 'RULES NOTE')
            self.assertEqual(note['helper'], 'AI unavailable, rules fallback')
            self.assertEqual(note['body'], acme_body)
            self.assertTrue(note['fallback'])
            self.assertEqual(note['kind'], 'rules')
            self.assertNotIn('Written by AI', note['helper'])
        still_rules = desk.shown_note(acme, elapsed_seconds=3)
        self.assertEqual(still_rules['badge'], 'RULES NOTE')
        self.assertEqual(still_rules['helper'], 'From fixed rules')
        self.assertEqual(still_rules['body'], acme_body)
        self.assertFalse(still_rules['fallback'])
        for kwargs in ({'ai_off': True}, {'error': True}, {'elapsed_seconds': 5}):
            note = desk.shown_note(bluebird, **kwargs)
            self.assertEqual(note['badge'], 'RULES NOTE')
            self.assertEqual(note['helper'], 'AI unavailable, rules fallback')
            self.assertEqual(note['body'], bluebird['note_text'])
            self.assertTrue(note['fallback'])
        self.assertEqual(desk.shown_note(cedar, ai_off=True)['body'], cedar['note_text'])
        self.assertEqual(desk.shown_note(cedar)['helper'], 'From fixed rules')

    # Scenario — the AI never decides the flag; opening the Desk does not rewrite notes
    def test_ai_does_not_change_the_flag_or_store_notes(self):
        html = self.get().get_data(as_text=True)
        panel = self.panels(html)['PO-1042']
        self.assertIn('Why flagged:</strong> Amount is 6× this payee’s usual payout ($790 avg)', panel)
        before = payouts.get_payout(self.database(), 'PO-1042')
        self.assertEqual(before['flag_text'], 'Amount is 6× this payee’s usual payout ($790 avg)')
        self.assertTrue(before['note_text'])
        self.assertEqual(before['note_source'], 'rules')
        self.assertIs(before['note_fallback'], False)
        self.get()
        after = payouts.get_payout(self.database(), 'PO-1042')
        self.assertEqual(after['note_text'], before['note_text'])
        self.assertEqual(after['note_source'], before['note_source'])
        self.assertEqual(after['note_fallback'], before['note_fallback'])
        self.assertEqual(after['flag_text'], before['flag_text'])
        self.assertNotIn('cloudflare', html.lower())
        self.assertNotIn('CF_API', html)
        self.assertNotIn('WORKERS_AI', html)
        self.assertNotIn('Written by AI', html)

    # Scenario — stored note text is escaped
    def test_stored_note_text_is_escaped(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET note_text = '<script>z</script> Amount' WHERE payout_id = 'PO-1042'")
        panel = self.panels()['PO-1042']
        self.assertIn('&lt;script&gt;z&lt;/script&gt; Amount', panel)
        self.assertNotIn('<script>z</script>', panel)

    # Scenario — the page script honors #ai=off and keeps it in the hash, without calling a model
    def test_script_honors_ai_off(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("q.get('ai') === 'off'", script)
        self.assertIn("q.set('ai', 'off')", script)
        self.assertIn('.note[data-note="fallback"]', script)
        self.assertIn('.note[data-note="rules"]', script)
        # The only fetches are the existing decision and reset routes; no model endpoint.
        self.assertEqual(script.count('fetch('), 2)
        self.assertIn("fetch('/payouts/' + encodeURIComponent(pending.id)", script)
        self.assertIn("fetch('/payouts/reset'", script)
        self.assertNotIn('workers.ai', script.lower())
        self.assertNotIn('openai', script.lower())
        self.assertNotIn('AI-drafted, review required', script)
        self.assertNotIn('Written by AI', script)


class CurrentQueueTests(unittest.TestCase):
    def test_current_queue_order_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'payments.sqlite3'
            payouts.ensure_seeded(db)
            queue = payouts.current_queue(db)
        self.assertEqual([p['payout_id'] for p in queue], ALL_IDS)
        self.assertEqual({p['payout_id'] for p in queue}, set(payouts.CURRENT_IDS))
        self.assertTrue(payouts.HISTORY_IDS.isdisjoint(p['payout_id'] for p in queue))

    def test_newly_flagged_payout_appears_in_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'payments.sqlite3'
            payouts.ensure_seeded(db)
            result = payout_rules.create(
                db, 'PO-1046', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 320000, 'ACH')
            self.assertEqual(result['status'], payouts.FLAGGED)
            queue = payouts.current_queue(db)
        ids = [p['payout_id'] for p in queue]
        self.assertIn('PO-1046', ids)
        self.assertEqual(
            [p['payout_id'] for p in queue if p['status'] == payouts.FLAGGED],
            ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1046'])
        self.assertEqual(ids[:5], ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1046', 'PO-1038'])
        self.assertTrue(payouts.HISTORY_IDS.isdisjoint(ids))
        self.assertEqual(len(queue), 11)

    def test_reset_drops_newly_flagged_payout(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'payments.sqlite3'
            payouts.ensure_seeded(db)
            payout_rules.create(
                db, 'PO-1046', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 320000, 'ACH')
            payouts.reset(db)
            queue = payouts.current_queue(db)
            listed = payouts.list_payouts(db)
        self.assertEqual([p['payout_id'] for p in queue], ALL_IDS)
        self.assertNotIn('PO-1046', [p['payout_id'] for p in queue])
        self.assertNotIn('PO-1046', [p['payout_id'] for p in listed])
        self.assertEqual(len(listed), 40)
        self.assertEqual({p['payout_id'] for p in queue}, set(payouts.CURRENT_IDS))


class DeskNewFlaggedTests(DeskBase):
    # Scenario — a payout created and flagged after the seed shows on the Desk
    def test_desk_lists_newly_flagged_payout(self):
        self.get()
        result = payout_rules.create(
            self.database(), 'PO-1046', 'Cedar Cleaning Co.', 'DEMO-CEDR-004', 320000, 'ACH')
        self.assertEqual(result['status'], payouts.FLAGGED)
        html = self.get().get_data(as_text=True)
        self.assertIn('Exceptions <b>5</b>', html)
        self.assertIn('All <b>11</b>', html)
        rows = self.rows(html)
        visible = [r for r in rows if not r['hidden']]
        self.assertEqual([r['sub'].split(' · ')[0] for r in visible],
                         ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1046', 'PO-1038'])
        added = next(r for r in visible if r['sub'].startswith('PO-1046'))
        self.assertEqual(
            (added['payee'], added['amount'], added['word']),
            ('Cedar Cleaning Co.', '$3,200.00', 'Needs review'))
        self.assertIn('PO-1046 · Amount is 5× this payee’s usual payout ($640 avg)', html)


if __name__ == '__main__':
    unittest.main()
