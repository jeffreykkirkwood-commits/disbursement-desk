"""Hosted HTTP interface. Reuses the simulator rules and HTML, never Dwolla credentials."""
import atexit
import logging
import os
import time
from pathlib import Path
from uuid import uuid4
from flask import Flask, Response, g, jsonify, redirect, request
from werkzeug.exceptions import HTTPException
from payments import APIError, Payments
from dashboard import render_dashboard
from demo_sessions import DemoStore, DemoCapacityError
from event_log import event, request_context, make_logger
from log_view import render_logs


def create_app(directory=None, public_origin=None, payments=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 10000
    # Render supplies its public HTTPS URL. An explicit origin supports custom domains.
    # Do not trust forwarded headers or a caller's Host header to authorize submissions.
    origin = (public_origin or os.environ.get('PUBLIC_ORIGIN') or
              os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8001').rstrip('/')
    if os.environ.get('RENDER') and not origin.startswith('https://'):
        raise RuntimeError('Hosted demos require an HTTPS public origin.')
    directory = Path(directory or os.environ.get('DEMO_DIRECTORY', '/tmp/payments-demos'))
    # Server diagnostics cover requests without a visitor session. Never expose these via /logs.
    server_logger = make_logger(directory / '_server_logs')
    demos = None if payments else DemoStore(directory, Payments, secure_cookie=origin.startswith('https://'))
    fixed_logger = make_logger(Path(payments.database).parent / 'logs') if payments else None
    app.extensions['demos'] = demos
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

    # Establish request tracing and the browser workspace before routing. Business
    # handlers never choose a database from a submitted payment/account identifier.
    @app.before_request
    def select_demo():
        g.request_id, g.started = str(uuid4()), time.monotonic()
        g.context_token = request_context.set((server_logger, g.request_id))
        g.leased = False
        event('http_request_received', method=request.method,
              endpoint=str(request.url_rule) if request.url_rule else '(unknown endpoint)')
        g.cookie = g.demo_id = None
        # Health probes, assets and unknown routes must not allocate databases.
        if request.endpoint in (None, 'health', 'script', 'home'):
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
        # Mirror only response metadata centrally; visitor details stay in that visitor's log.
        token = request_context.set((server_logger, g.request_id))
        try:
            event('http_response', http_status=response.status_code,
                  endpoint=str(request.url_rule) if request.url_rule else '(unknown endpoint)')
        finally:
            request_context.reset(token)
        return response

    @app.teardown_request
    def release_context(error):
        if g.get('leased'):
            demos.release(g.demo_id)
            g.leased = False
        token = g.pop('context_token', None)
        if token is not None:
            request_context.reset(token)

    @app.errorhandler(APIError)
    def expected_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        event('internal_error', level=logging.ERROR, error_type=type(error).__name__)
        return jsonify(error='Internal server error. Retry with the same idempotency key.'), 500

    @app.get('/')
    def home():
        return redirect('/dashboard')

    @app.get('/health')
    def health():
        return jsonify(status='ok', mode='fictional payments only')

    @app.get('/payment-form.js')
    def script():
        return Response(Path(__file__).with_name('payment-form.js').read_text(), mimetype='text/javascript')

    @app.get('/dashboard')
    @app.get('/dashboard/')
    def dashboard():
        data = g.payments.dashboard_data()
        data['demo_id'] = g.demo_id
        return render_dashboard(data)

    @app.get('/logs')
    def logs():
        return render_logs(Path(g.payments.database).parent / 'logs',
                           request.args.to_dict(flat=False), g.request_id)

    @app.get('/accounts')
    def accounts():
        return jsonify(g.payments.list_accounts())

    @app.get('/payments')
    def list_payments():
        return jsonify(g.payments.list_payments())

    @app.get('/payments/<payment_id>')
    def payment(payment_id):
        return jsonify(g.payments.get(payment_id))

    @app.post('/payments')
    def create_payment():
        if request.mimetype != 'application/json':
            raise APIError(415, 'Use Content-Type: application/json.')
        # Same save-then-process flow as the local server; no copied payment rules.
        status, result = g.payments.create(request.get_json(), request.headers.get('Idempotency-Key'))
        return jsonify(g.payments.process(result['payment_id'])), status

    @app.post('/payments/<payment_id>/process')
    def process_payment(payment_id):
        return jsonify(g.payments.process(payment_id))

    return app
