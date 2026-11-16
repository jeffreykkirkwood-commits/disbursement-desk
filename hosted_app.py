# START HERE — The web layer: every browser or API request enters here, gets matched to
# that visitor's own demo database, and is handed to the payment rules in payments.py.
"""Hosted HTTP interface. Reuses the simulator rules and HTML, never Dwolla credentials."""
import atexit
import logging
import os
import sqlite3
import storage
import time
from pathlib import Path
from uuid import uuid4
from flask import Flask, Response, g, jsonify, redirect, request, stream_with_context
from werkzeug.exceptions import HTTPException
from payments import APIError, Payments
import desk_common
import execution_replay
import payouts
import payout_rules
import payout_events
from dashboard import render_dashboard
from desk import render_desk
from demo_sessions import DemoStore, DemoCapacityError
from event_log import event, request_context, make_logger
from log_view import render_logs


# FEATURE 00 — Open one visitor demo with its Desk payouts
# Open the visitor's payments database, then add the payout tables and the fictional
# starting payouts (payouts.py) to that same file the first time it opens. Each visitor
# gets their own copy. The /desk page (FEATURE 19) reads them. The decision routes
# (FEATURE 21) can approve, hold or return a payout. Reset demo (FEATURE 22) restores
# this visitor's synthetic seed only. The payout stream (FEATURE 23) pushes created
# and status-changed events for this visitor; the Desk does not subscribe yet.
def payments_with_payouts(database, **options):
    ledger = Payments(database, **options)
    payouts.ensure_seeded(database)
    payout_events.sync(database)
    return ledger


# FEATURE 01 — Assemble the web application
# Connect HTTP requests (messages from browsers or API clients) to the shared payment
# rules. Limit request size and choose a trusted public website address. Normal visitors
# get separate temporary demo databases; tests may supply fixed storage.
def create_app(directory=None, public_origin=None, payments=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 10000  # Refuse request bodies over ~10 KB.
    # Render supplies its public HTTPS URL. An explicit origin supports custom domains.
    # Do not trust forwarded headers or a caller's Host header to authorize submissions.
    origin = (public_origin or os.environ.get('PUBLIC_ORIGIN') or
              os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8001').rstrip('/')
    if os.environ.get('RENDER') and not origin.startswith('https://'):
        raise RuntimeError('Hosted demos require an HTTPS public origin.')
    directory = Path(directory or os.environ.get('DEMO_DIRECTORY', '/tmp/payments-demos'))
    # Server diagnostics cover requests without a visitor session. Never expose these via /logs.
    server_logger = make_logger(directory / '_server_logs')
    demos = None if payments else DemoStore(directory, payments_with_payouts, secure_cookie=origin.startswith('https://'))
    fixed_logger = make_logger(Path(payments.database).parent / 'logs') if payments else None
    app.extensions['demos'] = demos
    # FEATURE 02 — Release resources on shutdown
    # Close log files and cached demo log handlers so restarting does not leave their
    # handles open. This does not reset saved balances.
    def close():
        if demos:
            demos.close()
        for logger in (server_logger, fixed_logger):
            if logger:
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)
    app.extensions['close'] = close
    atexit.register(close)

    # FEATURE 03 — Choose the visitor workspace before any action
    # Runs before every request. A browser cookie (a small ID the browser sends back each
    # time) picks which demo database this visitor uses. Viewing a page may start a new
    # demo, but submitting a payment never silently creates one. Requests from other
    # websites, or from an old browser tab, are rejected before any money logic runs.
    # The "lease" marks the database as in use so cleanup cannot delete it mid-request.
    @app.before_request
    def select_demo():
        g.request_id, g.started = str(uuid4()), time.monotonic()
        g.context_token = request_context.set((server_logger, g.request_id))
        g.leased = False
        event('http_request_received', method=request.method,
              endpoint=str(request.url_rule) if request.url_rule else '(unknown endpoint)')
        g.cookie = g.demo_id = None
        # Health probes, assets and unknown routes must not allocate databases.
        if request.endpoint in (None, 'health', 'script', 'desk_script', 'home'):
            return
        # Fixed storage is a test fixture only; normal local and hosted users use cookies.
        if payments:
            g.payments = payments
            request_context.set((fixed_logger, g.request_id))
            event('request_received', method=request.method, endpoint=str(request.url_rule))
            return
        # Reject foreign websites before looking up a browser session. The server log retains the rejection.
        if request.method == 'POST':
            if (request.headers.get('Sec-Fetch-Site') == 'cross-site' or
                    (request.headers.get('Origin') and request.headers['Origin'] != origin)):
                return jsonify(error='Cross-site submissions are not allowed.'), 403
        # GET/HEAD may start a demo; a POST can only use an existing session.
        try:
            session = demos.resolve(request.headers.get('Cookie'), create=request.method in ('GET', 'HEAD'))
        except DemoCapacityError as error:
            return jsonify(error=str(error)), 503
        if not session:
            return jsonify(error='Demo session missing. Reload the dashboard before submitting.',
                           code='DEMO_SESSION_CHANGED'), 409
        g.demo_id, g.payments, logger, g.cookie = session
        g.leased = True
        request_context.set((logger, g.request_id))
        event('request_received', method=request.method, endpoint=str(request.url_rule))
        if request.method == 'POST':
            expected = request.headers.get('X-Demo-ID')
            if expected and expected != g.demo_id:
                return jsonify(error='This tab belongs to a different demo. Reload the dashboard.',
                               code='DEMO_SESSION_CHANGED'), 409

    # FEATURE 04 — Return the result with a trace label
    # Runs after every request. Adds a request ID (for matching log lines), the demo
    # cookie, and "don't cache this" and basic browser-safety headers. The server-wide log
    # gets the request ID, method, endpoint pattern and status (plus the rejection reason for requests refused before a demo is found); visitor details stay in that visitor's own log.
    @app.after_request
    def finish(response):
        response.headers.update({'X-Request-ID': g.request_id, 'Cache-Control': 'no-store',
                                 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin'})
        if g.demo_id:
            response.headers['X-Demo-ID'] = g.demo_id
        if g.cookie:
            response.headers['Set-Cookie'] = g.cookie
        if response.status_code >= 400:
            payload = response.get_json(silent=True) or {}
            event('request_rejected', level=logging.WARNING, http_status=response.status_code,
                  reason=payload.get('error', 'Request failed'))
        event('request_finished', http_status=response.status_code,
              duration_ms=round((time.monotonic() - g.started) * 1000, 2))
        token = request_context.set((server_logger, g.request_id))
        try:
            event('http_response', http_status=response.status_code,
                  endpoint=str(request.url_rule) if request.url_rule else '(unknown endpoint)')
        finally:
            request_context.reset(token)
        return response

    # FEATURE 05 — Finish even when a request fails
    # Always runs last, even after a crash. Releases the "in use" lease; otherwise a failed
    # request could keep a demo from ever expiring, or mix up log labels between requests.
    @app.teardown_request
    def release_context(error):
        if g.get('leased'):
            demos.release(g.demo_id)
            g.leased = False
        token = g.pop('context_token', None)
        if token is not None:
            request_context.reset(token)

    # FEATURE 06 — Explain payment input problems
    # Turn an expected rule violation from payments.py, or from the Desk's own modules
    # (desk_common.py), into a JSON error with its status code (for example 422 = "your
    # input is invalid").
    @app.errorhandler(APIError)
    @app.errorhandler(desk_common.APIError)
    def expected_error(error):
        return jsonify(error=error.message), error.status

    # FEATURE 07 — Explain web request problems
    # Preserve errors such as malformed JSON and oversized input as their proper client
    # response instead of a generic crash.
    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    # FEATURE 08 — Hide internal details after a failure
    # Record only the exception type and ask the caller to reuse the retry key. A
    # failure response alone does not prove whether a payment was already saved.
    @app.errorhandler(Exception)
    def unexpected_error(error):
        event('internal_error', level=logging.ERROR, error_type=type(error).__name__)
        return jsonify(error='Internal server error. Retry with the same idempotency key.'), 500

    # FEATURE 09 — Open the visual demo
    # Visiting the root address redirects to /dashboard; it is not an API information
    # page.
    @app.get('/')
    def home():
        return redirect('/dashboard')

    # FEATURE 10 — Check whether the server responds
    # Return a small status message without creating a visitor database or submitting a
    # payment.
    @app.get('/health')
    def health():
        return jsonify(status='ok', mode='fictional payments only',
                       commit=os.environ.get('RENDER_GIT_COMMIT') or None)

    # FEATURE 11 — Deliver the browser form logic
    # Serve this one named JavaScript file instead of exposing an entire directory of
    # server files.
    @app.get('/payment-form.js')
    def script():
        return Response(Path(__file__).with_name('payment-form.js').read_text(), mimetype='text/javascript')

    # FEATURE 12 — Build the current visitor screen
    # Read a consistent balance/history snapshot, attach the demo identifier and turn it
    # into HTML for the browser.
    @app.get('/dashboard')
    @app.get('/dashboard/')
    def dashboard():
        data = g.payments.dashboard_data()
        data['demo_id'] = g.demo_id
        return render_dashboard(data)

    # FEATURE 13 — Inspect this demo activity
    # Read only the selected database companion logs, apply the requested filters, and
    # exclude the log viewer request itself.
    @app.get('/logs')
    def logs():
        return render_logs(Path(g.payments.database).parent / 'logs',
                           request.args.to_dict(flat=False), g.request_id)

    # FEATURE 14 — Read balances through the API
    # Return structured account data from this visitor database without changing it.
    @app.get('/accounts')
    def accounts():
        return jsonify(g.payments.list_accounts())

    # FEATURE 15 — Read payment history through the API
    # Return saved instructions and their current outcomes for this demo only.
    @app.get('/payments')
    def list_payments():
        return jsonify(g.payments.list_payments())

    # FEATURE 16 — Read one saved payment
    # Resolve the URL identifier inside the already selected visitor database.
    @app.get('/payments/<payment_id>')
    def payment(payment_id):
        return jsonify(g.payments.get(payment_id))

    # FEATURE 17 — Submit or safely retry
    # Require JSON, validate and save or reuse the instruction, then process it before
    # replying. HTTP 201 only means a new record was saved (200 means this retry key was
    # seen before). It does not mean money moved: a 201 can carry status REJECTED, for
    # example for insufficient funds. Check the result's status field.
    @app.post('/payments')
    def create_payment():
        if request.mimetype != 'application/json':
            raise APIError(415, 'Use Content-Type: application/json.')
        status, result = g.payments.create(request.get_json(), request.headers.get('Idempotency-Key'))
        return jsonify(g.payments.process(result['payment_id'])), status

    # FEATURE 18 — Recover a saved instruction
    # Process an existing payment identifier. Already final outcomes are returned
    # unchanged, so recovery does not debit again.
    @app.post('/payments/<payment_id>/process')
    def process_payment(payment_id):
        return jsonify(g.payments.process(payment_id))

    # FEATURE 19 — Open the Disbursement Desk (read-only queue, #32 slice 1)
    # Read this visitor's current payouts and render them with the page, so the list is
    # the first paint (no loading message). If they cannot be read, the page still opens
    # and shows the approved load-error sentence (503). Nothing is written.
    @app.get('/desk')
    @app.get('/desk/')
    def desk():
        try:
            queue = payouts.current_queue(g.payments.database)
        except (sqlite3.Error, OSError, KeyError) as error:  # unreadable or unexpected data
            event('desk_load_failed', level=logging.ERROR, error_type=type(error).__name__)
            return render_desk(None, g.demo_id or 'local-test'), 503
        return render_desk(queue, g.demo_id or 'local-test')

    # FEATURE 20 — Deliver the Desk filter script
    # Serve this one named JavaScript file (Exceptions / All switch) without a demo database.
    @app.get('/desk.js')
    def desk_script():
        return Response(Path(__file__).with_name('desk.js').read_text(), mimetype='text/javascript')

    # FEATURE 28 — Run the fixed demo scenario (#61)
    # Behind the scenes only. POST uses this visitor's existing demo session
    # cookie (a POST cannot start a new session). Resets this visitor's demo,
    # applies the scripted Approve, Hold and Return, and runs the #56 replay
    # once. Results are JSON only. No page, link or button is added. The Desk
    # words and layout are unchanged. Reset demo still restores the starting
    # set, including execution_events and the reconciliation tables.
    # Reconciliation (#57, PR #107) is not on main yet and is not run here.
    @app.post('/demo-scenario')
    def run_demo_scenario():
        import demo_scenario
        database = g.payments.database
        report = demo_scenario.run(database)
        event('demo_scenario_run',
              decisions=len(report['decisions']),
              replay_applied=report['replay']['applied'])
        return jsonify(report)

    # FEATURE 21 — Approve, hold, return, clear or block one payout (Desk back end, issues #35 and #111)
    # JSON body: reviewer (required) and reason (optional; Hold and Return are
    # confirm-only). Needs an Idempotency-Key header. Works only on this visitor's own
    # demo database. 201 = decision recorded; 200 = same key seen before, nothing changed;
    # 409 = move not allowed from the current status. No screen calls this yet.
    @app.post('/payouts/<payout_id>/<any(approve, hold, return, clear, block):action>')
    def decide_payout(payout_id, action):
        if request.mimetype != 'application/json':
            raise APIError(415, 'Use Content-Type: application/json.')
        body = request.get_json()
        if not isinstance(body, dict):
            raise APIError(422, 'Send a JSON object with a reviewer name.')
        database = g.payments.database
        payout_events.sync(database)
        status, decision = payouts.decide(database, payout_id, action, body.get('reviewer'),
                                          request.headers.get('Idempotency-Key'), body.get('reason'))
        event('payout_decision_replayed' if status == 200 else 'payout_decision_saved',
              payout_id=payout_id, action=action, from_status=decision['from_status'],
              to_status=decision['to_status'])
        payout_events.sync(database)
        return jsonify(payout=payouts.get_payout(database, payout_id), decision=decision), status

    # FEATURE 29 — List every reason a payout is in review (#32)
    # Behind the scenes JSON for the later "Why this is in review" panel.
    # Read-only. No Desk HTML or screen change. 200 returns the payout id and a
    # list of {code, label, details} items (empty when the payout is not in
    # review). 404 if the payout does not exist.
    @app.get('/payouts/<payout_id>/review-reasons')
    def payout_review_reasons(payout_id):
        import review_reasons
        items = review_reasons.for_payout(g.payments.database, payout_id)
        return jsonify(payout_id=payout_id, reasons=items)

    # FEATURE 22 — Reset this visitor's Desk demo to the starting set (#32 slice 4)
    # Calls payouts.reset() on this visitor's own database only. That restores the
    # synthetic seed and clears this demo's action log; it never touches anyone else.
    # 200 means the starting set was written back. No request body is required.
    @app.post('/payouts/reset')
    def reset_payouts():
        database = g.payments.database
        restored = payouts.reset(database)
        event('payouts_reset', restored=restored)
        payout_events.after_reset(database)
        return jsonify(restored=restored)

    # FEATURE 26 — Replay the bundled synthetic payment-outcome events (#56)
    # Behind the scenes only: a POST that uses this visitor's existing demo
    # session cookie (a POST cannot start a new session). Plays fixtures/
    # execution_events_demo.json once, in order, into this visitor's database.
    # Results are JSON only. No page, link or button is added. Replay does not
    # emit stream events or change Desk words, layout or queue order. Reset
    # demo still restores the starting set.
    @app.post('/payouts/execution-replay')
    def replay_execution_events():
        database = g.payments.database
        report = execution_replay.replay(database)
        event('execution_events_replayed', applied=report['applied'],
              skipped=report['skipped'], refused=report['refused'])
        return jsonify(report)

    # FEATURE 25 — Create one synthetic payee payout in this visitor's demo (#91)
    # Behind the scenes only: POST JSON, requires this visitor's existing demo
    # session cookie (a POST cannot start a new session). Creates only fictional
    # DEMO- payouts in that visitor's database and returns JSON. No page, link
    # or button is added. Reset demo still restores the starting set.
    def _next_demo_payout_id(database):
        with storage.connect(str(database)) as db:
            ids = [row[0] for row in db.execute('SELECT payout_id FROM payouts')]
        numbers = [int(payout_id[3:]) for payout_id in ids
                   if payout_id.startswith('PO-') and payout_id[3:].isdigit()]
        return f'PO-{max(numbers, default=0) + 1}'

    @app.post('/payouts/simulate')
    def simulate_payout():
        if request.mimetype != 'application/json':
            raise APIError(415, 'Use Content-Type: application/json.')
        body = request.get_json()
        if not isinstance(body, dict):
            raise APIError(422, 'Send a JSON object with a payee name, DEMO- account and amount.')
        database = g.payments.database
        payout_id = body.get('payout_id') or _next_demo_payout_id(database)
        payout_events.sync(database)
        result = payout_rules.create(
            database, payout_id, body.get('payee_name'), body.get('payee_account'),
            body.get('amount_cents'), body.get('rail', 'ACH'),
            bank_details_changed_at=body.get('bank_details_changed_at'),
            routing_number=body.get('routing_number'))
        event('payout_simulated', payout_id=payout_id, status=result['status'])
        payout_events.sync(database)
        return jsonify(payout=payouts.get_payout(database, payout_id),
                       status=result['status'], changed=result['changed'],
                       event=result['event']), 201

    # FEATURE 23 — Stream payout created and status-changed events (#43)
    # Server-sent events for this visitor's demo only. A reconnect sends the
    # Last-Event-ID header (or lastEventId query) and resumes after that id.
    # The connection closes after a short hold so the one Gunicorn worker's
    # four threads stay available; EventSource reconnects. The Desk page does
    # not subscribe yet, so nothing on screen changes.
    # FEATURE 24 — Cap concurrent streams so they cannot block /health (#83)
    # At most two streams may be open in this process. A third request gets
    # 503 with Retry-After and a short plain body, so two of the four
    # Gunicorn threads stay free for /health and other work. The slot is
    # acquired before the response starts and released in a finally block
    # when the generator ends, errors, or the client disconnects. This does
    # not change the Render start command or thread count.
    @app.get('/payouts/stream')
    def payout_stream():
        last_id = payout_events.parse_last_id(
            request.headers.get('Last-Event-ID') or request.args.get('lastEventId'))
        if not payout_events.acquire_stream():
            return Response(
                'Too many live update streams are open. Try again shortly.\n',
                status=503,
                mimetype='text/plain',
                headers={'Retry-After': str(payout_events.RETRY_AFTER_SECONDS)})
        released = False

        def release_slot():
            nonlocal released
            if not released:
                released = True
                payout_events.release_stream()

        def body():
            try:
                yield from payout_events.stream(g.payments.database, last_id)
            finally:
                release_slot()

        try:
            return Response(stream_with_context(body()),
                            mimetype='text/event-stream',
                            headers={'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})
        except Exception:
            release_slot()
            raise

    # FEATURE 27 — Reconcile payment outcomes with a separate settlement file (#57)
    # Behind the scenes only. POST uses this visitor's existing demo session
    # cookie (a POST cannot start a new session). Compares this visitor's
    # payouts with fixtures/settlement_demo.csv, which was written by hand
    # and not generated from the replay event file. Results are JSON only.
    # No page, link or button is added. review_status and status are not
    # rewritten, so the Desk does not change. Reset demo still restores the
    # starting set.
    @app.post('/reconciliation')
    def run_reconciliation():
        import reconciliation
        database = g.payments.database
        report = reconciliation.reconcile(database)
        event('reconciliation_run', run_id=report['run']['run_id'],
              matched=report['run']['matched_count'],
              mismatched=report['run']['mismatched_count'],
              unknown=report['run']['unknown_count'])
        return jsonify(report)

    return app
