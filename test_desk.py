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
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from hosted_app import create_app
import desk
import ofac
import payout_rules
import payouts
import review_reasons

EXCEPTION_IDS = ['PO-1042', 'PO-1043', 'PO-1044', 'PO-1038']
ALL_IDS = EXCEPTION_IDS + ['PO-1037', 'PO-1036', 'PO-1039', 'PO-1040', 'PO-1041', 'PO-1045']
ROW = re.compile(r'<li data-ex="(\d)"( hidden)?><button type="button" class="row" data-id="PO-\d+" '
                 r'aria-current="false"><span class="payee">(.*?)</span>'
                 r'<span class="amt">(.*?)</span><span class="sub">(.*?)</span>'
                 r'<span class="pills"><span class="pill (s-\w+)">(.*?)</span>'
                 r'(?:<span class="marks">(?:<span class="mark m-\w+">[^<]*</span>)+</span>)?'
                 r'</span></button></li>')
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

    def review_narrative(self, payout_id):
        reply = self.client.get(f'/payouts/{payout_id}/review-reasons',
                                base_url='https://demo.example')
        self.assertEqual(reply.status_code, 200)
        return reply.json['narrative']


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

    # Scenario — the Desk header has only the example-data tag; no Payments Lab link (#122 step 5)
    # The root address still redirects to the Desk.
    def test_header_has_tag_and_no_lab_link(self):
        desk = self.get().get_data(as_text=True)
        self.assertIn('<span class="tag">Example data · fake money</span>', desk)
        self.assertIn('class="tag op"', desk)
        self.assertIn('Desk operator · admin', desk)
        self.assertNotIn('Payments Lab', desk)
        self.assertNotIn('lab-link', desk)
        self.assertEqual(self.get('/').headers['Location'], '/desk')

    # Scenario — the queue row keyboard focus ring is drawn inside the row so .list does not clip it (#102)
    def test_queue_row_focus_ring_is_inset(self):
        html = self.get().get_data(as_text=True)
        self.assertIn(':focus-visible{outline:3px solid #b38528;outline-offset:2px}', html)
        self.assertIn('.row:focus-visible{outline-offset:-3px}', html)

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
        # Create dialog owns the only form; GETs still write nothing (asserted above).
        self.assertEqual(html.count('<form'), 1)
        self.assertIn('<form id="create-form">', html)

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
                      '<dt>To account</dt><dd>DEMO-BLUE-002</dd>', '<dt>Rail</dt><dd>RTP · USD</dd>'):
            self.assertIn(words, panel)
        self.assertIn('Why this is in review', panel)
        self.assertIn('1 reason', panel)
        self.assertIn('Payee bank details changed 2 days ago.', panel)
        self.assertIn('Confirm the change with the payee by phone before sending.', panel)
        self.assertIn('BANK_CHANGED_7D', panel)
        self.assertIn('From fixed rules. Review before sending.', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('RULES NOTE', panel)
        self.assertNotIn('This payout is final', panel)

    # Scenario — Held shows its flag and no final message; no action buttons in slice 2
    def test_held_detail(self):
        panel = self.panels()['PO-1038']
        self.assertIn('<span class="pill s-held">Held</span>', panel)
        self.assertIn('Why this is in review', panel)
        self.assertIn('1 reason', panel)
        self.assertIn('Amount is 4× this payee’s usual payout ($520 avg).', panel)
        self.assertIn('AMOUNT_OVER_3X_AVG', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('This payout is final', panel)

    # Scenario — Sent and Returned payouts are final: message, no buttons
    def test_final_payouts(self):
        panels = self.panels()
        returned, sent = panels['PO-1037'], panels['PO-1040']
        self.assertIn('<span class="pill s-returned">Returned</span>', returned)
        self.assertIn('Why this was in review', returned)
        self.assertIn('Payee bank details changed 1 day ago.', returned)
        self.assertIn('Confirm the change with the payee by phone before sending.', returned)
        self.assertIn('BANK_CHANGED_7D', returned)
        self.assertNotIn('Why flagged', returned)
        self.assertNotIn('Returned by operator', returned)
        self.assertNotIn('Why held', returned)
        self.assertIn('<span class="pill s-sent">Sent</span>', sent)
        self.assertNotIn('Why flagged', sent)
        self.assertNotIn('Why this was in review', sent)
        self.assertNotIn('Why held', sent)
        self.assertIn('No rules triggered. Within normal range for this payee.', sent)
        self.assertIn('<dt>Rail</dt><dd>ACH · USD</dd>', sent)
        for panel in (returned, sent):
            self.assertIn('<p class="done">This payout is final. No actions available.</p>', panel)
            self.assertNotIn('class="btn', panel)

    # Scenario — seed panels show an empty Activity section (no audit rows yet)
    def test_activity_empty_on_seed(self):
        panel = self.panels()['PO-1042']
        self.assertIn('<section class="audit" aria-label="Activity">', panel)
        self.assertIn('<h3>Activity</h3>', panel)
        self.assertIn('No activity yet.', panel)
        self.assertNotIn('class="audit-list"', panel)

    # Scenario — after Hold, Activity shows who/what/when with Desk status words
    def test_activity_lists_hold_decision(self):
        self.get()
        payouts.decide(self.database(), 'PO-1042', payouts.HOLD, 'Ada Reviewer',
                       'desk-audit-hold-1', reason='Checking bank change')
        panel = self.panels()['PO-1042']
        self.assertIn('<ol class="audit-list">', panel)
        self.assertIn('Ada Reviewer', panel)
        self.assertIn('Held · Needs review → Held', panel)
        self.assertIn('Checking bank change', panel)
        self.assertNotIn('No activity yet.', panel)
        self.assertNotIn('Execution ·', panel)

    # Scenario — Approve + settle shows status and execution rows from stored events
    def test_activity_lists_approve_and_execution(self):
        self.get()
        payouts.decide(self.database(), 'PO-1042', payouts.APPROVE, 'Ada Reviewer',
                       'desk-audit-approve-1', reason='Verified with payee')
        panel = self.panels()['PO-1042']
        self.assertIn('Ada Reviewer', panel)
        self.assertIn('Approved · Needs review → Sent', panel)
        self.assertIn('Verified with payee', panel)
        self.assertIn('Demo settle', panel)
        self.assertIn('Execution · Settled', panel)
        self.assertNotIn('demo:PO-1042', panel)

    # Scenario — GET /payouts/<id>/events matches Activity for known stored events
    def test_events_api_matches_activity_panel(self):
        self.get()
        payouts.decide(self.database(), 'PO-1042', payouts.HOLD, 'Ada Reviewer',
                       'desk-audit-api-hold-1', reason='Call back number on file')
        reply = self.client.get('/payouts/PO-1042/events', base_url='https://demo.example')
        self.assertEqual(reply.status_code, 200)
        body = reply.get_json()
        self.assertEqual(body['payout_id'], 'PO-1042')
        self.assertEqual(len(body['events']), 1)
        event = body['events'][0]
        self.assertEqual(event['kind'], 'status')
        self.assertEqual(event['action'], 'hold')
        self.assertEqual(event['actor'], 'Ada Reviewer')
        self.assertEqual(event['from_status'], 'FLAGGED')
        self.assertEqual(event['to_status'], 'HELD')
        self.assertEqual(event['reason'], 'Call back number on file')
        self.assertNotIn('evidence', event)
        self.assertNotIn('idempotency_key', event)
        panel = self.panels()['PO-1042']
        self.assertIn('Ada Reviewer', panel)
        self.assertIn('Held · Needs review → Held', panel)
        self.assertIn('Call back number on file', panel)

    # Scenario — events API includes execution after Approve and 404s for missing ids
    def test_events_api_includes_execution_and_404(self):
        self.get()
        payouts.decide(self.database(), 'PO-1042', payouts.APPROVE, 'Ada Reviewer',
                       'desk-audit-api-approve-1')
        reply = self.client.get('/payouts/PO-1042/events', base_url='https://demo.example')
        self.assertEqual(reply.status_code, 200)
        kinds = [event['kind'] for event in reply.get_json()['events']]
        self.assertEqual(kinds, ['status', 'execution'])
        execution = reply.get_json()['events'][1]
        self.assertEqual(execution['source'], 'demo')
        self.assertEqual(execution['execution_status'], 'SETTLED')
        self.assertEqual(execution['execution_ref'], 'demo:PO-1042')
        panel = self.panels()['PO-1042']
        self.assertIn('Execution · Settled', panel)
        self.assertNotIn('demo:PO-1042', panel)
        missing = self.client.get('/payouts/PO-MISSING/events', base_url='https://demo.example')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()['code'], 'NOT_FOUND')
        empty = self.client.get('/payouts/PO-1043/events', base_url='https://demo.example')
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json()['events'], [])

    # Scenario — the review panel replaces the RULES NOTE card; stored note text is not shown
    def test_stored_note_is_not_rendered_in_the_detail(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET note_text = 'Stored note', note_source = 'rules', note_fallback = 0 "
                       "WHERE payout_id = 'PO-1043'")
        html = self.get().get_data(as_text=True)
        panel = self.panels(html)['PO-1043']
        self.assertNotIn('Stored note', panel)
        self.assertNotIn('RULES NOTE', panel)
        self.assertIn('Why this is in review', panel)
        self.assertIn('Payee bank details changed 2 days ago.', panel)
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

    # Scenario — the review panel lists a reason count, each reason line, and the footer
    def test_review_panel_lists_each_reason_and_count(self):
        panels = self.panels()
        acme = panels['PO-1042']
        self.assertIn('<span class="review-count">1 reason</span>', acme)
        self.assertIn('<p class="review-reason">Amount is 6× this payee’s usual payout ($790 avg).</p>', acme)
        self.assertIn('<code class="review-code">AMOUNT_OVER_3X_AVG</code>', acme)
        self.assertIn('<p class="review-foot">From fixed rules. Review before sending.</p>', acme)
        northwind = panels['PO-1044']
        self.assertIn('<span class="review-count">1 reason</span>', northwind)
        self.assertIn('Same payee and amount as yesterday (possible duplicate).', northwind)
        self.assertIn('<code class="review-code">DUPLICATE_24H</code>', northwind)

    # Scenario — a bank-details reason has the phone-check line on its own line
    def test_review_panel_phone_line_under_bank_reason(self):
        panel = self.panels()['PO-1043']
        self.assertIn('<p class="review-reason">Payee bank details changed 2 days ago.</p>', panel)
        self.assertIn('<p class="review-phone">Confirm the change with the payee by phone before sending.</p>', panel)
        # The phone line sits under the bank sentence and above the rule code.
        bank = panel.split('Payee bank details changed 2 days ago.', 1)[1]
        phone_at = bank.find('Confirm the change with the payee by phone before sending.')
        code_at = bank.find('BANK_CHANGED_7D')
        self.assertGreater(phone_at, 0)
        self.assertGreater(code_at, phone_at)

    # Scenario — a final payout keeps its original reasons under the past-tense heading
    def test_final_payout_uses_was_in_review_heading(self):
        panel = self.panels()['PO-1037']
        self.assertIn('<h3>Why this was in review <span class="review-count">1 reason</span></h3>', panel)
        self.assertNotIn('Why this is in review', panel)
        self.assertNotIn('Why held', panel)
        self.assertIn('Payee bank details changed 1 day ago.', panel)
        self.assertIn('Confirm the change with the payee by phone before sending.', panel)
        self.assertNotIn('Returned by operator', panel)

    # Scenario — two stored flags show two reasons, in stored order, with the phone line
    def test_review_panel_shows_combined_reasons_in_stored_order(self):
        self.get()
        from datetime import datetime, timezone
        now = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)
        payout_rules.create(
            self.database(), 'PO-2100', 'Cedar Lane Plumbing', 'DEMO-CEDL-012', 185000,
            'ACH', bank_details_changed_at=now.isoformat(), now=now)
        panel = self.panels()['PO-2100']
        self.assertIn('<span class="review-count">2 reasons</span>', panel)
        self.assertIn('Why this is in review', panel)
        first, second = [block for block in panel.split('<ul class="review-list">', 1)[1]
                         .split('</ul>', 1)[0].split('<li>') if block]
        self.assertIn('No paid payout to this payee yet; amount is over $1,000.', first)
        self.assertIn('NEW_PAYEE_OVER_1000', first)
        self.assertNotIn('Confirm the change', first)
        self.assertIn('Payee bank details changed today.', second)
        self.assertIn('Confirm the change with the payee by phone before sending.', second)
        self.assertIn('BANK_CHANGED_7D', second)

    # Scenario — a sanctions match uses the short sentence and label/value rows
    def test_review_panel_sanctions_rows(self):
        self.get()
        payout_rules.create(
            self.database(), 'PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH')
        panel = self.panels()['PO-4100']
        self.assertIn('<span class="review-count">1 reason</span>', panel)
        self.assertIn('<p class="review-reason">Possible sanctions match.</p>', panel)
        self.assertIn('<dt>Listed name</dt><dd>BANCO NACIONAL DE CUBA</dd>', panel)
        self.assertIn('<dt>Score</dt><dd>100 (100 is an exact match)</dd>', panel)
        self.assertIn('<dt>List published</dt><dd>2026-11-09</dd>', panel)
        self.assertIn('<dt>Snapshot taken</dt><dd>2026-11-12</dd>', panel)
        self.assertIn('<code class="review-code">POSSIBLE_SANCTIONS_MATCH</code>', panel)
        self.assertIn('data-act="clear"', panel)
        self.assertIn('data-act="block"', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('RULES NOTE', panel)
        # Facts come from the same match for_payout reports.
        api = review_reasons.for_payout(self.database(), 'PO-4100')
        sanctions = next(item for item in api if item['code'] == ofac.FLAG_CODE)
        self.assertEqual(sanctions['details']['entry_name'], 'BANCO NACIONAL DE CUBA')
        self.assertEqual(sanctions['details']['score'], 100)
        self.assertEqual(sanctions['details']['list_published'], '2026-11-09')
        self.assertEqual(sanctions['details']['snapshot_taken'], '2026-11-12')


class DeskWhyHeldTests(DeskBase):
    def panels(self, html=None):
        return dict(PANEL.findall(html or self.get().get_data(as_text=True)))

    def held_paragraph(self, narrative):
        return f'<p>{desk.text(narrative)}</p>'

    def why_held_block(self, panel):
        marker = '<section class="why-held" aria-label="Why held">'
        start = panel.index(marker)
        return panel[start:panel.index('</section>', start) + len('</section>')]

    # Scenario — Why held matches JSON narrative between the reasons and Activity
    def test_why_held_matches_json_narrative_between_reasons_and_activity(self):
        html = self.get().get_data(as_text=True)
        for payout_id in ('PO-1042', 'PO-1038'):
            panel = self.panels(html)[payout_id]
            narrative = self.review_narrative(payout_id)
            self.assertTrue(narrative)
            block = self.why_held_block(panel)
            self.assertIn('<h3>Why held</h3>', block)
            self.assertIn(self.held_paragraph(narrative), block)
            self.assertLess(panel.index('From fixed rules. Review before sending.'),
                            panel.index(block))
            self.assertLess(panel.index(block), panel.index('<h3>Activity</h3>'))
            self.assertEqual(panel.count('class="why-held"'), 1)

    # Scenario — every current-queue payout: Why held equals JSON narrative, else absent
    def test_why_held_matches_json_narrative_for_every_current_payout(self):
        html = self.get().get_data(as_text=True)
        panels = self.panels(html)
        self.assertEqual(list(panels), ALL_IDS)
        shown = 0
        for pid, panel in panels.items():
            narrative = self.review_narrative(pid)
            if narrative:
                self.assertIn('<h3>Why held</h3>', panel, pid)
                self.assertIn(self.held_paragraph(narrative), panel, pid)
                shown += 1
            else:
                self.assertNotIn('Why held', panel, pid)
                self.assertNotIn('class="why-held"', panel, pid)
        self.assertGreater(shown, 0)

    # Scenario — Why held keeps the stored sanctions sentence; the list stays short
    def test_why_held_ofac_uses_full_stored_sentence(self):
        self.get()
        payout_rules.create(
            self.database(), 'PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH')
        panel = self.panels()['PO-4100']
        narrative = self.review_narrative('PO-4100')
        reasons = panel.split('<section class="why-held"', 1)[0]
        self.assertIn('Possible sanctions match:', narrative)
        self.assertIn(self.held_paragraph(narrative), self.why_held_block(panel))
        self.assertIn('<p class="review-reason">Possible sanctions match.</p>', reasons)
        self.assertNotIn('Possible sanctions match:', reasons)

    # Scenario — two stored flags become one Why held paragraph, in stored order
    def test_why_held_joins_multi_flag_labels(self):
        self.get()
        from datetime import datetime, timezone
        now = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)
        payout_rules.create(
            self.database(), 'PO-2100', 'Cedar Lane Plumbing', 'DEMO-CEDL-012', 185000,
            'ACH', bank_details_changed_at=now.isoformat(), now=now)
        panel = self.panels()['PO-2100']
        narrative = self.review_narrative('PO-2100')
        self.assertIn(payouts.FLAG_TEXT_SEP, narrative)
        self.assertLess(narrative.index('No paid payout to this payee yet'),
                        narrative.index('Payee bank details changed today'))
        self.assertIn(self.held_paragraph(narrative), self.why_held_block(panel))
        review = panel.split('<ul class="review-list">', 1)[1].split('</ul>', 1)[0]
        self.assertEqual(review.count('<li>'), 2)

    # Scenario — approving removes Why held and keeps the past-tense reasons
    def test_why_held_empty_after_payout_leaves_review(self):
        self.get()
        self.assertTrue(self.review_narrative('PO-1042'))
        payouts.decide(self.database(), 'PO-1042', payouts.APPROVE, 'Ada Reviewer',
                       'desk-why-held-approve-1')
        panel = self.panels()['PO-1042']
        self.assertEqual(self.review_narrative('PO-1042'), '')
        self.assertNotIn('Why held', panel)
        self.assertIn('Why this was in review', panel)
        self.assertIn('Amount is 6× this payee’s usual payout ($790 avg).', panel)

    # Scenario — a clean Sent payout has no Why held
    def test_why_held_absent_on_clean_sent_payout(self):
        panel = self.panels()['PO-1040']
        self.assertEqual(self.review_narrative('PO-1040'), '')
        self.assertNotIn('Why held', panel)
        self.assertIn('No rules triggered. Within normal range for this payee.', panel)

    # Scenario — a stored label with markup is escaped inside Why held
    def test_why_held_escapes_the_narrative(self):
        self.get()
        with closing(sqlite3.connect(str(self.database()))) as db, db:
            db.execute("UPDATE payouts SET flag_text = '<img alt=\"x\">' WHERE payout_id = 'PO-1042'")
        panel = self.panels()['PO-1042']
        narrative = self.review_narrative('PO-1042')
        self.assertIn('<img', narrative)
        self.assertIn(self.held_paragraph(narrative), self.why_held_block(panel))
        self.assertNotIn('<img', panel)


APPROVE_BUTTON = ('<button type="button" class="btn primary" data-act="approve" data-heading="Approve and send?" '
                  'data-body="The payout is sent to the payee." data-ok="Approve" data-ok-class="btn primary">Approve</button>')
HOLD_BUTTON = ('<button type="button" class="btn" data-act="hold" data-heading="Hold this payout?" '
               'data-body="It stays in Exceptions and will not send until someone approves it." data-ok="Hold" '
               'data-ok-class="btn primary"{}>Hold</button>')
HELD_DISABLED = (' disabled title="Already held. It will not send until someone approves or returns it."')
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
        self.assertIn(APPROVE_BUTTON + HOLD_BUTTON.format(HELD_DISABLED) + RETURN_BUTTON, panel)

    # Scenario — Sent and Returned payouts have no action buttons
    def test_final_payouts_have_no_buttons(self):
        panels = self.panels()
        for pid in ('PO-1037', 'PO-1036', 'PO-1040'):
            self.assertNotIn('data-act=', panels[pid])
            self.assertIn('This payout is final. No actions available.', panels[pid])

    # Scenario — confirm dialog: Cancel, a confirm button, and the Q2 A failure line, hidden
    def test_confirm_dialog_markup(self):
        html = self.get().get_data(as_text=True)
        self.assertIn('<dialog id="confirm" aria-labelledby="ch">', html)
        self.assertIn('<dialog id="create" aria-labelledby="cr-h">', html)
        self.assertIn('<dialog id="recon-confirm" aria-labelledby="rq-h">', html)
        self.assertEqual(html.count('<dialog'), 3)
        self.assertIn('<p><strong id="c-payee"></strong> · <span id="c-amount"></span> · <span id="c-id"></span></p>', html)
        self.assertIn('<p id="c-error" class="err-inline" role="alert" hidden>'
                      'That didn’t save. Nothing has changed. Try again.</p>', html)
        self.assertIn('<button type="button" class="btn" id="cancel">Cancel</button>', html)
        confirm = html.split('<dialog id="confirm"', 1)[1].split('</dialog>', 1)[0]
        for word in ('<select', '<textarea', 'reason', 'Reason', 'note'):
            self.assertNotIn(word, confirm)

    # Scenario — create dialog: labeled fields, Idempotency-Key client path, failure line hidden
    def test_create_dialog_markup(self):
        html = self.get().get_data(as_text=True)
        self.assertIn('id="create-open"', html)
        self.assertIn('<dialog id="create" aria-labelledby="cr-h">', html)
        self.assertIn('id="cr-payee"', html)
        self.assertIn('id="cr-account"', html)
        self.assertIn('id="cr-amount"', html)
        self.assertIn('id="cr-rail"', html)
        self.assertIn('<p id="cr-error" class="err-inline" role="alert" hidden>'
                      'That didn’t create. Nothing has changed. Try again.</p>', html)
        script = self.client.get('/desk.js').get_data(as_text=True)
        self.assertIn("fetch('/v1/payouts'", script)
        self.assertIn("'Idempotency-Key': pendingCreate.key", script)
        self.assertIn('pendingCreate = {key: newKey(), payoutId: newPayoutId()}', script)
        self.assertIn('typedId || pendingCreate.payoutId', script)
        self.assertNotIn("fetch('/payouts/simulate'", script)
        self.assertIn("'#f=all'", script)
        self.assertIn('location.reload()', script)

    # Scenario — create failures name a known code, and a local miss does not send
    def test_create_errors_name_known_codes_only(self):
        script = self.client.get('/desk.js').get_data(as_text=True)
        self.assertIn('Enter a payee name, a DEMO- account, and an amount greater than zero. Nothing was sent.', script)
        self.assertIn("code === 'local'", script)
        for code, sentence in (
            ('VALIDATION_ERROR', 'Check the payee name, DEMO- account, amount, and rail. Nothing was created.'),
            ('CONFLICT', 'A payout with this id already exists. Nothing new was created.'),
            ('RAIL_OVER_LIMIT', 'That amount is over this rail’s limit. Lower the amount or pick another rail.'),
            ('RAIL_WINDOW_CLOSED', 'That rail’s send window is closed. Pick another rail or wait for the next window.'),
            ('IDEMPOTENCY_KEY_REUSED', 'This create key was already used for a different payout. Close the dialog and start again.'),
        ):
            self.assertIn(code, script)
            self.assertIn(sentence, script)
        self.assertIn("CREATE_CODE_TEXT[code] + ' (' + code + ')'", script)
        self.assertIn('showCreateError(code)', script)
        self.assertIn("ok.textContent = 'Creating…'", script)
        self.assertIn('if (!pendingCreate || !createForm || creating) return;', script)
        # Stable code only. Never the response text, and never reply.json / reply.text.
        self.assertNotIn('.error', script)
        self.assertNotIn('reply.json', script)
        self.assertNotIn('reply.text', script)
        self.assertNotIn('.json()', script)
        self.assertNotIn('internal_error', script)

    # Scenario — Review, On hold, and OFAC chips, plus one detail callout
    def test_queue_and_detail_show_review_hold_and_ofac(self):
        self.get()
        payout_rules.create(
            self.database(), 'PO-4100', ofac.DEMO_PAYEE_NAME, ofac.DEMO_PAYEE_ACCOUNT,
            ofac.DEMO_AMOUNT_CENTS, 'ACH')
        html = self.get().get_data(as_text=True)
        rows = {row['sub'].split(' · ')[0]: row for row in self.rows(html)}
        self.assertEqual(rows['PO-1042']['word'], 'Needs review')
        self.assertEqual(rows['PO-1038']['word'], 'Held')
        self.assertIn('PO-4100', rows)
        self.assertNotIn('class="mark m-review"', html)
        self.assertNotIn('class="mark m-held"', html)
        self.assertIn('<span class="mark m-ofac">OFAC</span>', html)
        panels = dict(PANEL.findall(html))
        self.assertIn(
            '<p class="callout s-review" role="status">Needs review. Read the reasons below before you approve, hold, or return.</p>',
            panels['PO-1042'])
        self.assertIn(
            '<p class="callout s-held" role="status">Held. This payout will not send until someone approves it.</p>',
            panels['PO-1038'])
        self.assertIn(
            '<p class="callout s-ofac" role="status">Possible sanctions match. Approve is unavailable. Use Clear or Block.</p>',
            panels['PO-4100'])
        self.assertNotIn('data-act="approve"', panels['PO-4100'])
        self.assertIn('data-act="clear"', panels['PO-4100'])
        self.assertIn('data-act="block"', panels['PO-4100'])
        self.assertNotIn('class="callout', panels['PO-1040'])
        for code in ('FLAGGED', 'PENDING', 'APPROVED', 'HELD<', 'PAID', 'RETURNED'):
            self.assertNotIn(code, html)

    # Scenario — a decision in flight disables confirm and cancel, and ignores a second click
    def test_decide_busy_state_blocks_a_second_submit(self):
        script = self.client.get('/desk.js').get_data(as_text=True)
        self.assertIn('if (!pending || deciding) return;', script)
        self.assertIn("ok.textContent = 'Saving…'", script)
        self.assertIn("ok.setAttribute('aria-busy', busy ? 'true' : 'false')", script)
        self.assertIn('if (deciding) event.preventDefault();', script)
        self.assertIn('if (creating) event.preventDefault();', script)
        self.assertIn('if (deciding) return;', script)

    # Scenario — after an operator Hold: Held, "Held by operator", still in Exceptions, Hold disabled
    def test_after_hold(self):
        self.decide('PO-1042', 'HELD')
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1042 · Held by operator', html)
        self.assertIn('Exceptions <b>4</b>', html)
        panel = dict(PANEL.findall(html))['PO-1042']
        self.assertIn('Why this is in review', panel)
        self.assertIn('Amount is 6× this payee’s usual payout ($790 avg).', panel)
        self.assertIn('AMOUNT_OVER_3X_AVG', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('Held by operator', panel)
        self.assertIn(HOLD_BUTTON.format(HELD_DISABLED), panel)
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
        self.assertIn('Why this was in review', panel)
        self.assertIn('Same payee and amount as yesterday (possible duplicate).', panel)
        self.assertIn('DUPLICATE_24H', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('Returned by operator', panel)
        self.assertIn('This payout is final. No actions available.', panel)
        self.assertNotIn('data-act=', panel)

    # Scenario — after Approve: Sent, original reasons stay under the past-tense heading
    def test_after_approve(self):
        self.decide('PO-1043', 'PAID')
        html = self.get().get_data(as_text=True)
        self.assertIn('PO-1043 · RTP', html)
        panel = dict(PANEL.findall(html))['PO-1043']
        self.assertIn('<span class="pill s-sent">Sent</span>', panel)
        self.assertIn('Why this was in review', panel)
        self.assertIn('Payee bank details changed 2 days ago.', panel)
        self.assertIn('Confirm the change with the payee by phone before sending.', panel)
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
        self.assertIn('JSON.stringify({})', script)
        self.assertNotIn("'reviewer'", script)
        self.assertNotIn('reason', script)

    # Scenario — a Desk POST after the demo folder is gone (instance replace) is session missing
    def test_desk_post_after_demo_folder_gone_is_session_missing(self):
        page = self.get()
        demo = page.headers['X-Demo-ID']
        folder = self.database().parent
        demos = self.app.extensions['demos']
        cached = demos.loaded.pop(folder.name, None)
        if cached:
            for handler in cached[1].handlers[:]:
                handler.close()
                cached[1].removeHandler(handler)
        shutil.rmtree(folder)
        reply = self.client.post('/payouts/PO-1043/approve', json={},
                                 base_url='https://demo.example',
                                 headers={'Idempotency-Key': 'k-restart',
                                          'Origin': 'https://demo.example',
                                          'X-Demo-ID': demo})
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(reply.json['code'], 'DEMO_SESSION_CHANGED')
        self.assertEqual(reply.json['error'],
                         'Demo session missing. Reload the dashboard before submitting.')

    # Scenario — the page script reloads once on DEMO_SESSION_CHANGED, then shows the same error
    def test_script_reloads_once_on_demo_session_changed(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("=== 'DEMO_SESSION_CHANGED'", script)
        self.assertIn("sessionStorage.getItem('desk-session-reloaded')", script)
        self.assertIn("sessionStorage.setItem('desk-session-reloaded', '1')", script)
        self.assertIn('recoverSession()', script)
        self.assertGreaterEqual(script.count('recoverSession()'), 2)
        self.assertIn('location.reload()', script)

    # Scenario — end to end through the #35 route, once PR #51 is merged (skipped before that)
    def test_decisions_through_api(self):
        rules = {str(rule) for rule in self.app.url_map.iter_rules()}
        if not any(r.startswith('/payouts/') for r in rules):
            self.skipTest('Needs the #35 decision API (PR #51).')
        page = self.get()
        demo = page.headers['X-Demo-ID']
        def post(pid, action, key):
            return self.client.post(f'/payouts/{pid}/{action}', json={},
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
        return client.post(f'/payouts/{payout_id}/{action}', json={},
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

    # Scenario — Escape closes the open detail panel the same way Back does (#73)
    def test_script_closes_detail_on_escape(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("addEventListener('keydown'", script)
        self.assertIn("e.key !== 'Escape'", script)
        self.assertIn('dialog.open', script)
        self.assertIn('close(true)', script)
        self.assertIn('writeHash()', script)
        # Leave Escape to confirm/create/recon dialogs while open.
        self.assertIn('if (dialog && dialog.open) return;', script)
        self.assertIn('if (createDialog && createDialog.open) return;', script)
        self.assertIn('if (reconConfirm && reconConfirm.open) return;', script)
        self.assertIn("fetch('/payouts/' + encodeURIComponent(pending.id)", script)
        self.assertIn("fetch('/payouts/reset'", script)
        self.assertIn("fetch('/v1/payouts'", script)
        self.assertIn("fetch('/stripe/reconciliation'", script)

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

    # Scenario — Harbor Hardware shows its amount reason in the review panel, not a RULES NOTE
    def test_harbor_shows_amount_reason_in_the_review_panel(self):
        panel = self.panels()['PO-1038']
        self.assertIn('<div class="review">', panel)
        self.assertIn('Why this is in review', panel)
        self.assertIn('<span class="review-count">1 reason</span>', panel)
        self.assertIn('Amount is 4× this payee’s usual payout ($520 avg).', panel)
        self.assertIn('<code class="review-code">AMOUNT_OVER_3X_AVG</code>', panel)
        self.assertIn('From fixed rules. Review before sending.', panel)
        self.assertNotIn('RULES NOTE', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI NOTE', panel)
        self.assertNotIn('AI-drafted, review required', panel)

    # Scenario — Cedar Cleaning Co. shows the clean line, with no review panel
    def test_cedar_shows_the_clean_review_line(self):
        panel = self.panels()['PO-1040']
        self.assertIn('No rules triggered. Within normal range for this payee.', panel)
        self.assertNotIn('<div class="review">', panel)
        self.assertNotIn('Why held', panel)
        self.assertNotIn('RULES NOTE', panel)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI NOTE', panel)

    # Scenario — Bluebird Bakery shows the bank reason and the phone-check line
    def test_bluebird_shows_bank_reason_and_phone_line(self):
        panel = self.panels()['PO-1043']
        self.assertIn('Why this is in review', panel)
        self.assertIn('<span class="review-count">1 reason</span>', panel)
        self.assertIn('Payee bank details changed 2 days ago.', panel)
        self.assertIn('Confirm the change with the payee by phone before sending.', panel)
        self.assertIn('<code class="review-code">BANK_CHANGED_7D</code>', panel)
        self.assertNotIn('RULES NOTE', panel)
        self.assertNotIn('Why flagged', panel)
        self.assertNotIn('AI NOTE', panel)
        self.assertNotIn('Written by AI', panel)
        self.assertNotIn('AI-drafted, review required', panel)

    # Scenario — every current-queue payout has either the review panel or the clean line
    def test_every_payout_has_review_panel_or_clean_line(self):
        panels = self.panels()
        self.assertEqual(list(panels), ALL_IDS)
        for pid, panel in panels.items():
            self.assertNotIn('RULES NOTE', panel, pid)
            self.assertNotIn('Why flagged', panel, pid)
            has_review = '<div class="review">' in panel
            has_clean = 'No rules triggered. Within normal range for this payee.' in panel
            self.assertTrue(has_review or has_clean, pid)
            self.assertFalse(has_review and has_clean, pid)
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
            self.assertNotIn('RULES NOTE', panel, pid)

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
        self.assertIn('Amount is 6× this payee’s usual payout ($790 avg).', panel)
        self.assertNotIn('Why flagged', panel)
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
        # The stored note card is no longer shown; escaped text must not appear raw.
        self.assertNotIn('<script>z</script>', panel)
        self.assertNotIn('RULES NOTE', panel)

    # Scenario — the page script honors #ai=off and keeps it in the hash, without calling a model
    def test_script_honors_ai_off(self):
        script = self.get('/desk.js').get_data(as_text=True)
        self.assertIn("q.get('ai') === 'off'", script)
        self.assertIn("q.set('ai', 'off')", script)
        self.assertIn('.note[data-note="fallback"]', script)
        self.assertIn('.note[data-note="rules"]', script)
        # Fetches are decide, reset, create, and Stripe recon — no model endpoint.
        self.assertIn("fetch('/payouts/' + encodeURIComponent(pending.id)", script)
        self.assertIn("fetch('/payouts/reset'", script)
        self.assertIn("fetch('/v1/payouts'", script)
        self.assertIn("fetch('/stripe/reconciliation'", script)
        self.assertEqual(script.count('fetch('), 4)
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

    def _flagged(self, db, payout_id):
        payout_rules.create(
            db, payout_id, f'Sort Payee {payout_id}', 'DEMO-SORT-001', 100001, 'ACH')

    def test_queue_sorts_ids_by_number_not_text(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'payments.sqlite3'
            payouts.ensure_seeded(db)
            # Starting seed ids are all four digits, so the visible order is unchanged.
            self.assertEqual([p['payout_id'] for p in payouts.current_queue(db)], ALL_IDS)
            self._flagged(db, 'PO-998')
            self._flagged(db, 'PO-10000')
            self._flagged(db, 'DEMO-7')
            self._flagged(db, 'MANUAL')
            flagged = [p['payout_id'] for p in payouts.current_queue(db)
                       if p['status'] == payouts.FLAGGED]
        # Text sort would put PO-10000 before PO-1042, and PO-1042 before PO-998.
        self.assertEqual(
            flagged, ['DEMO-7', 'PO-998', 'PO-1042', 'PO-1043', 'PO-1044', 'PO-10000', 'MANUAL'])
        self.assertEqual(payouts.payout_id_sort_key('PO-998'), (0, 998, 'PO-998'))
        self.assertEqual(payouts.payout_id_sort_key('PO-1046'), (0, 1046, 'PO-1046'))
        self.assertEqual(payouts.payout_id_sort_key('PO-10000'), (0, 10000, 'PO-10000'))
        self.assertEqual(payouts.payout_id_sort_key('MANUAL'), (1, 0, 'MANUAL'))
        self.assertLess(payouts.payout_id_sort_key('PO-998'),
                        payouts.payout_id_sort_key('PO-1046'))
        self.assertLess(payouts.payout_id_sort_key('PO-1046'),
                        payouts.payout_id_sort_key('PO-10000'))


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
