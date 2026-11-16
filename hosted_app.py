# START HERE — The web layer: every browser or API request enters here and gets matched to
# that visitor's own demo database. In this app shell (#122 step 2) the only route is
# /health; the payout routes and the Desk page arrive in steps 3 and 4.
"""Hosted HTTP interface for the Disbursement Desk. Fictional data, never Dwolla credentials."""
import atexit
import logging
import os
import storage
import time
from pathlib import Path
from uuid import uuid4
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from desk_common import APIError
from demo_sessions import DemoStore, DemoCapacityError
from event_log import event, request_context, make_logger


# FEATURE 00 — Open one visitor demo
# Each visitor gets their own demo database file. The file keeps the name
# payments.sqlite3 that demo_sessions.py looks for, so that module moves unchanged.
# The shell only creates the file; the payout tables and the fictional starting
# payouts are added here by the payout engine (#122 step 3).
class DeskDemo:
    def __init__(self, database, **options):
        self.database = database
        connection = storage.connect(database)
        try:
            connection.execute('SELECT 1')
            connection.commit()
        finally:
            connection.close()


# FEATURE 01 — Assemble the web application
# Connect HTTP requests (messages from browsers or API clients) to the Desk. Limit
# request size and choose a trusted public website address. Every visitor gets a
# separate temporary demo database.
def create_app(directory=None, public_origin=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 10000  # Refuse request bodies over ~10 KB.
    # Render supplies its public HTTPS URL. An explicit origin supports custom domains.
    # Do not trust forwarded headers or a caller's Host header to authorize submissions.
    origin = (public_origin or os.environ.get('PUBLIC_ORIGIN') or
              os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8001').rstrip('/')
    if os.environ.get('RENDER') and not origin.startswith('https://'):
        raise RuntimeError('Hosted demos require an HTTPS public origin.')
    directory = Path(directory or os.environ.get('DEMO_DIRECTORY', '/tmp/payments-demos'))
    # Server diagnostics cover requests without a visitor session. Never expose these to visitors.
    server_logger = make_logger(directory / '_server_logs')
    demos = DemoStore(directory, DeskDemo, secure_cookie=origin.startswith('https://'))
    app.extensions['demos'] = demos

    # FEATURE 02 — Release resources on shutdown
    # Close log files and cached demo log handlers so restarting does not leave their
    # handles open. This does not reset saved data.
    def close():
        demos.close()
        for handler in server_logger.handlers[:]:
            handler.close()
            server_logger.removeHandler(handler)
    app.extensions['close'] = close
    atexit.register(close)

    # FEATURE 03 — Choose the visitor workspace before any action
    # Runs before every request. A browser cookie (a small ID the browser sends back each
    # time) picks which demo database this visitor uses. Viewing a page may start a new
    # demo, but a submission never silently creates one. Requests from other websites,
    # or from an old browser tab, are rejected before any payout logic runs.
    # The "lease" marks the database as in use so cleanup cannot delete it mid-request.
    @app.before_request
    def select_demo():
        g.request_id, g.started = str(uuid4()), time.monotonic()
        g.context_token = request_context.set((server_logger, g.request_id))
        g.leased = False
        event('http_request_received', method=request.method,
              endpoint=str(request.url_rule) if request.url_rule else '(unknown endpoint)')
        g.cookie = g.demo_id = None
        # Health probes and unknown routes must not allocate databases.
        if request.endpoint in (None, 'health'):
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

    # FEATURE 06 — Explain expected request problems
    # Turn an expected rule violation from the Desk's modules (desk_common.py) into a
    # JSON error with its status code (for example 422 = "your input is invalid").
    @app.errorhandler(APIError)
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
    # failure response alone does not prove whether a decision was already saved.
    @app.errorhandler(Exception)
    def unexpected_error(error):
        event('internal_error', level=logging.ERROR, error_type=type(error).__name__)
        return jsonify(error='Internal server error. Retry with the same idempotency key.'), 500

    # FEATURE 10 — Check whether the server responds
    # Return a small status message without creating a visitor database or submitting a
    # payment.
    @app.get('/health')
    def health():
        return jsonify(status='ok', mode='fictional payments only',
                       commit=os.environ.get('RENDER_GIT_COMMIT') or None)

    return app
