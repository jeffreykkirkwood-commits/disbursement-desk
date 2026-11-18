# START HERE — The web layer: every browser or API request enters here and gets matched to
# that visitor's own demo database. Each demo gets the payout engine's tables and
# fictional starting payouts (#122 step 3). The Desk page and the payout routes (#122
# step 4) are moved here unchanged from payments-api-simulator.
"""Hosted HTTP interface for the Disbursement Desk. Fictional data, never Dwolla credentials."""
import atexit
import logging
import os
import sqlite3
import storage
import time
import urllib.parse
from pathlib import Path
from uuid import uuid4
from flask import Flask, Response, abort, g, jsonify, redirect, request, stream_with_context
from werkzeug.exceptions import HTTPException
from desk_common import APIError
import api_contract
import execution_replay
import payouts
import payout_rules
import payout_events
import stripe_connect
import stripe_reconciliation
from desk import render_desk
from demo_sessions import DemoStore, DemoCapacityError
from event_log import event, request_context, make_logger
import metrics
import operators
import operator_oidc
import deploy_posture
import rate_limits
import render_events


# FEATURE 00 — Open one visitor demo with its Desk payouts
# Each visitor gets their own demo database file. The file keeps the name
# payments.sqlite3 that demo_sessions.py looks for, so that module moves unchanged.
# The first time it opens, the payout tables and the fictional starting payouts
# (payouts.py) are added to that file, and the payout event log is brought up to
# date. Opening the same demo again changes nothing.
class DeskDemo:
    def __init__(self, database, **options):
        self.database = database
        connection = storage.connect(database)
        try:
            connection.execute('SELECT 1')
            connection.commit()
        finally:
            connection.close()
        payouts.ensure_seeded(database)
        payout_events.sync(database)


# FEATURE 01a — Optional Sentry (errors + release)
# Idle when SENTRY_DSN is unset. Release is RENDER_GIT_COMMIT when present.
# Capture runs in unexpected_error: FlaskIntegration's got_request_exception hook
# never sees exceptions that an @app.errorhandler(Exception) already handles.
def _init_sentry():
    dsn = (os.environ.get('SENTRY_DSN') or '').strip()
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        release=os.environ.get('RENDER_GIT_COMMIT') or None,
    )
    return True


# FEATURE 01 — Assemble the web application
# Connect HTTP requests (messages from browsers or API clients) to the Desk. Limit
# request size and choose a trusted public website address. Every visitor gets a
# separate temporary demo database.
def create_app(directory=None, public_origin=None):
    app = Flask(__name__, static_folder=None)
    app.extensions['sentry_enabled'] = _init_sentry()
    app.config['MAX_CONTENT_LENGTH'] = 10000  # Refuse request bodies over ~10 KB.
    WEBHOOK_MAX_CONTENT_LENGTH = 65536
    ENDPOINTS_WITHOUT_DEMO_DB = frozenset({
        None, 'health', 'ready', 'metrics_snapshot', 'openapi_document',
        'desk_script', 'home', 'stripe_webhook', 'render_deploy_events',
        'oidc_login', 'oidc_callback', 'oidc_mock_authorize',
    })
    # Render supplies its public HTTPS URL. An explicit origin supports custom domains.
    # Do not trust forwarded headers or a caller's Host header to authorize submissions.
    origin = (public_origin or os.environ.get('PUBLIC_ORIGIN') or
              os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8001').rstrip('/')
    if os.environ.get('RENDER') and not origin.startswith('https://'):
        raise RuntimeError('Hosted demos require an HTTPS public origin.')
    directory = Path(directory or os.environ.get('DEMO_DIRECTORY', '/tmp/payments-demos'))
    try:
        oidc_config = operator_oidc.load_config(os.environ)
    except operator_oidc.OidcConfigError as error:
        oidc_config = error
    if (isinstance(oidc_config, operator_oidc.OidcConfig) and oidc_config.mock
            and os.environ.get('RENDER')):
        raise RuntimeError('DESK_OIDC_MOCK must not be set on Render.')
    # On Render, /tmp (and unset DEMO_DIRECTORY) is wiped on every deploy/restart.
    # Prefer Turso tenancy or a paid disk — see docs/deploy-posture.md.
    deploy_posture.warn_ephemeral_demo_directory(directory)
    # Server diagnostics cover requests without a visitor session. Never expose these to visitors.
    server_logger = make_logger(directory / '_server_logs')
    demos = DemoStore(directory, DeskDemo, secure_cookie=origin.startswith('https://'))
    app.extensions['demos'] = demos
    oidc_sessions = operator_oidc.SessionStore(directory)
    app.extensions['oidc'] = oidc_config
    app.extensions['oidc_sessions'] = oidc_sessions
    # Shared stream-slot cap across Gunicorn workers on this DEMO_DIRECTORY
    # (STREAM_SLOT_BACKEND=file by default; memory for single-process tests).
    payout_events.configure_stream_slots(directory)

    # FEATURE 02 — Release resources on shutdown
    # Close log files and cached demo log handlers so restarting does not leave their
    # handles open. This does not reset saved data.
    def close():
        demos.close()
        payout_events.configure_stream_slots(None)
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
        g.operator = None
        g.operator_cookie = None
        if request.endpoint in ('stripe_webhook', 'render_deploy_events'):
            request.max_content_length = WEBHOOK_MAX_CONTENT_LENGTH
        if request.endpoint in ENDPOINTS_WITHOUT_DEMO_DB:
            return
        # Reject foreign websites before looking up a browser session. Cookie browser
        # POSTs need a matching Origin or same-origin/same-site Sec-Fetch-Site.
        # Bearer API clients may omit Origin. The server log retains the rejection.
        if request.method == 'POST':
            site = request.headers.get('Sec-Fetch-Site')
            req_origin = request.headers.get('Origin')
            if site == 'cross-site' or (req_origin and req_origin != origin):
                return jsonify(error='Cross-site submissions are not allowed.'), 403
            bearer = (request.headers.get('Authorization') or '').startswith('Bearer ')
            if not bearer and not (
                    req_origin == origin or site in ('same-origin', 'same-site')):
                return jsonify(error='Cross-site submissions are not allowed.'), 403
        # GET/HEAD may start a demo; a POST can only use an existing session.
        # Issuer set (OidcConfig or OidcConfigError) never mints operator.json.
        oidc_broken = isinstance(oidc_config, operator_oidc.OidcConfigError)
        oidc_ready = isinstance(oidc_config, operator_oidc.OidcConfig)
        try:
            session = demos.resolve(
                request.headers.get('Cookie'),
                create=request.method in ('GET', 'HEAD'),
                mint_operator=oidc_config is None,
            )
        except DemoCapacityError as error:
            g.metric_503_reason = 'demo_capacity'
            return jsonify(error=str(error)), 503
        if not session:
            return jsonify(error='Demo session missing. Reload the dashboard before submitting.',
                           code='DEMO_SESSION_CHANGED'), 409
        g.demo_id, g.payments, logger, g.cookie, session_operator = session
        g.leased = True
        # Bearer replaces any browser principal first. An unknown token is 401
        # and does not fall through. A half-configured issuer still serves bearers.
        try:
            bearer_operator = operators.resolve_bearer(request.headers.get('Authorization'))
        except (PermissionError, ValueError) as error:
            return jsonify(error=str(error), code='OPERATOR_UNAUTHORIZED'), 401
        if bearer_operator is not None:
            g.operator = bearer_operator
        elif oidc_broken:
            if request.method in ('GET', 'HEAD'):
                return jsonify(error=oidc_config.message,
                               code='OPERATOR_UNAUTHORIZED'), 503
            return jsonify(error='Operator sign-in required.',
                           code='OPERATOR_UNAUTHORIZED'), 401
        elif oidc_ready:
            g.operator = oidc_sessions.operator_from_cookie(
                request.headers.get('Cookie'), time.time())
            if g.operator is None:
                if request.method in ('GET', 'HEAD'):
                    nxt = urllib.parse.quote(operator_oidc.safe_next(request.path), safe='/')
                    return redirect(f'/auth/oidc/login?next={nxt}')
                return jsonify(error='Operator sign-in required.',
                               code='OPERATOR_UNAUTHORIZED'), 401
        else:
            g.operator = session_operator
        request_context.set((logger, g.request_id))
        event('request_received', method=request.method, endpoint=str(request.url_rule),
              operator_id=g.operator.operator_id, operator_role=g.operator.role)
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
        response.headers.update({
            'X-Request-ID': g.request_id,
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'same-origin',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'"
            ),
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
        })
        if origin.startswith('https://') or os.environ.get('RENDER'):
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains')
        if g.demo_id:
            response.headers['X-Demo-ID'] = g.demo_id
        cookies = [value for value in (g.cookie, g.get('operator_cookie')) if value]
        if cookies:
            while 'Set-Cookie' in response.headers:
                response.headers.pop('Set-Cookie')
            for value in cookies:
                response.headers.add('Set-Cookie', value)
        endpoint = str(request.url_rule) if request.url_rule else '(unknown endpoint)'
        if response.status_code >= 400:
            payload = response.get_json(silent=True) or {}
            event('request_rejected', level=logging.WARNING, http_status=response.status_code,
                  reason=payload.get('error', 'Request failed'))
        if response.status_code == 503:
            # Catch-all for 503s not already labeled (desk load, stream cap, etc.).
            reason = getattr(g, 'metric_503_reason', None) or 'http_503'
            metrics.inc(metrics.HTTP_503_TOTAL, reason=reason, endpoint=endpoint)
        event('request_finished', http_status=response.status_code,
              duration_ms=round((time.monotonic() - g.started) * 1000, 2))
        token = request_context.set((server_logger, g.request_id))
        try:
            event('http_response', http_status=response.status_code, endpoint=endpoint)
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
    # Include stable `code` when the raiser set one (api_contract.ERROR_CODES).
    @app.errorhandler(APIError)
    def expected_error(error):
        payload = {'error': error.message}
        if error.code:
            payload['code'] = error.code
        return jsonify(payload), error.status

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
        if app.extensions.get('sentry_enabled'):
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(error)
            except Exception:
                pass
        return jsonify(error='Internal server error. Retry with the same idempotency key.',
                       code=api_contract.INTERNAL_ERROR), 500

    # FEATURE 10 — Check whether the server responds (liveness)
    # Return a small status message without creating a visitor database or submitting a
    # payment. Orchestrators use this to know the process is up; prefer /ready for
    # traffic admission.
    @app.get('/health')
    def health():
        return jsonify(status='ok', mode='fictional payments only',
                       commit=os.environ.get('RENDER_GIT_COMMIT') or None)

    # FEATURE 10c — Publish the versioned API contract (OpenAPI 3)
    # Static document for /v1 create + decide. Does not mint a demo session.
    @app.get('/openapi.json')
    def openapi_document():
        return jsonify(api_contract.openapi_document(origin))

    # FEATURE 10b — Check whether the process can accept traffic (readiness)
    # Probes the demo directory (writable) without minting a visitor session. A 503
    # means the load balancer should stop sending traffic until storage recovers.
    @app.get('/ready')
    def ready():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f'.ready-{uuid4().hex}'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink(missing_ok=True)
        except OSError as error:
            event('readiness_failed', level=logging.ERROR, error_type=type(error).__name__)
            return jsonify(status='not_ready', reason='demo storage is not writable'), 503
        return jsonify(status='ready', mode='fictional payments only',
                       commit=os.environ.get('RENDER_GIT_COMMIT') or None)

    # FEATURE 10c — Process-local metrics snapshot (decide/create/503/stream)
    # No demo allocation. Values are per Gunicorn worker; scrapers should poll each
    # instance. Structured metric_recorded lines are also written to the request log.
    # Wave C: require X-Metrics-Secret when DESK_METRICS_SECRET is set; on Render
    # with the secret unset, answer 404 like an unknown path (tests stay open).
    @app.get('/metrics')
    def metrics_snapshot():
        if not metrics.snapshot_allowed(request.headers.get(metrics.HEADER)):
            abort(404)
        return jsonify(status='ok', metrics=metrics.snapshot(),
                       commit=os.environ.get('RENDER_GIT_COMMIT') or None)

    def _rate_limited(bucket):
        """Refuse with 429 when this IP+demo session exceeds the in-process cap."""
        if rate_limits.allow(bucket, request.remote_addr, g.demo_id):
            return None
        return jsonify(error='Too many requests. Try again shortly.',
                       code=api_contract.RATE_LIMITED), 429

    # FEATURE 09 — Open the Desk
    # This repo holds only the Desk, so visiting the root address redirects to /desk.
    # It is not an API information page and does not start a demo by itself.
    @app.get('/')
    def home():
        return redirect('/desk')

    # FEATURE 19 — Open the Disbursement Desk (read-only queue, #32 slice 1)
    # Read this visitor's current payouts and render them with the page, so the list is
    # the first paint (no loading message). If they cannot be read, the page still opens
    # and shows the approved load-error sentence (503). Nothing is written.
    @app.get('/desk')
    @app.get('/desk/')
    def desk():
        try:
            queue = payouts.current_queue(g.payments.database)
            # One read of the audit trail (status + execution), grouped for Activity.
            events_by_id = {}
            for row in payouts.audit_entries(g.payments.database):
                events_by_id.setdefault(row['payout_id'], []).append(row)
        except (sqlite3.Error, OSError, KeyError) as error:  # unreadable or unexpected data
            event('desk_load_failed', level=logging.ERROR, error_type=type(error).__name__)
            g.metric_503_reason = 'desk_load'
            return render_desk(None, g.demo_id or 'local-test', operator=g.operator), 503
        last_recon = stripe_reconciliation.load_last_summary(g.payments.database)
        return render_desk(queue, g.demo_id or 'local-test', operator=g.operator,
                           events_by_id=events_by_id, stripe_recon_summary=last_recon)

    # FEATURE 19b — Report the signed-in operator for this demo session
    # Read-only JSON. Actor identity for decide/reset comes from this principal (or a
    # Bearer token), never from a free-text reviewer field.
    @app.get('/operator')
    def current_operator():
        return jsonify(operator=g.operator.public())

    # FEATURE 19c — OIDC browser login (authorization code + PKCE)
    # Idle unless DESK_OIDC_ISSUER is set. The desk_operator cookie is a random
    # session id. The ID token and the client secret are not stored.
    def _oidc_ready():
        if isinstance(oidc_config, operator_oidc.OidcConfigError):
            return None, (jsonify(error=oidc_config.message), 503)
        if not isinstance(oidc_config, operator_oidc.OidcConfig):
            return None, (jsonify(error='OIDC is not configured.'), 404)
        return oidc_config, None

    @app.get('/auth/oidc/login')
    def oidc_login():
        config, failure = _oidc_ready()
        if failure is not None:
            return failure
        try:
            _pending, url = operator_oidc.start_login(
                config, oidc_sessions, request.args.get('next'))
        except operator_oidc.OidcAuthError as error:
            return jsonify(error=error.message, code=error.code), error.status
        return redirect(url)

    @app.get('/auth/oidc/callback')
    def oidc_callback():
        config, failure = _oidc_ready()
        if failure is not None:
            return failure
        if request.args.get('error'):
            oidc_sessions.take('pending', request.args.get('state') or '', time.time())
            return jsonify(error='The identity provider refused sign-in.',
                           code='OPERATOR_UNAUTHORIZED'), 401
        try:
            _operator, session, nxt = operator_oidc.finish_login(
                config, oidc_sessions, request.args.get('code'), request.args.get('state'))
        except operator_oidc.OidcAuthError as error:
            return jsonify(error=error.message, code=error.code), error.status
        g.operator_cookie = operator_oidc.cookie_header(
            session, secure=origin.startswith('https://'), now=time.time())
        return redirect(nxt)

    if isinstance(oidc_config, operator_oidc.OidcConfig) and oidc_config.mock:
        @app.get('/auth/oidc/mock/authorize')
        def oidc_mock_authorize():
            try:
                location = operator_oidc.mock_authorize(
                    oidc_config, oidc_sessions, request.args)
            except operator_oidc.OidcAuthError as error:
                return jsonify(error=error.message, code=error.code), error.status
            return redirect(location)

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
    def _require_demo_tools():
        """Gate demo mutators: admin, or DESK_DEMO_TOOLS=1 for any role."""
        if not g.operator.can_use_demo_tools():
            return jsonify(
                error='Demo tools require an admin operator or DESK_DEMO_TOOLS=1.',
                code=api_contract.OPERATOR_FORBIDDEN), 403
        return None

    @app.post('/demo-scenario')
    def run_demo_scenario():
        refused = _require_demo_tools()
        if refused is not None:
            return refused
        limited = _rate_limited(rate_limits.BUCKET_DEMO_SCENARIO)
        if limited is not None:
            return limited
        import demo_scenario
        database = g.payments.database
        report = demo_scenario.run(database)
        event('demo_scenario_run',
              decisions=len(report['decisions']),
              replay_applied=report['replay']['applied'])
        return jsonify(report)

    # FEATURE 21 — Approve, hold, return, clear or block one payout (Desk back end, issues #35 and #111)
    # JSON body: reason optional (Hold and Return are confirm-only). Do not send
    # reviewer — the authenticated operator is the audit actor. Needs an
    # Idempotency-Key header. Works only on this visitor's own demo database.
    # 201 = decision recorded; 200 = same key seen before, nothing changed;
    # 409 = move not allowed from the current status.
    def _require_json_object(hint):
        if request.mimetype != 'application/json':
            raise APIError(415, 'Use Content-Type: application/json.',
                           code=api_contract.CONTENT_TYPE_REQUIRED)
        body = request.get_json(silent=True)
        if body is None or not isinstance(body, dict):
            raise APIError(422, hint, code=api_contract.INVALID_JSON_BODY)
        return body

    def _record_ofac_hold(replayed, action, held_flag_code=None):
        """Count one new OFAC hold. A replay does not count again."""
        if replayed:
            return
        if action == payouts.SCREEN:
            metrics.inc(metrics.OFAC_HOLD_TOTAL, source='screen')
        elif payouts.POSSIBLE_SANCTIONS_MATCH in payouts.split_flag_codes(held_flag_code):
            metrics.inc(metrics.OFAC_HOLD_TOTAL, source='create')

    def _run_decide(payout_id, action, body):
        if 'reviewer' in body:
            return jsonify(
                error='Do not send reviewer; decisions bind to the signed-in operator.',
                code=api_contract.REVIEWER_NOT_ACCEPTED), 422
        if not g.operator.can_decide():
            return jsonify(error='This operator cannot decide payouts.',
                           code=api_contract.OPERATOR_FORBIDDEN), 403
        database = g.payments.database
        payout_events.sync(database)
        status, decision = payouts.decide(database, payout_id, action, g.operator.actor,
                                          request.headers.get('Idempotency-Key'), body.get('reason'))
        event('payout_decision_replayed' if status == 200 else 'payout_decision_saved',
              payout_id=payout_id, action=action, from_status=decision['from_status'],
              to_status=decision['to_status'], operator_id=g.operator.operator_id)
        metrics.inc(metrics.DECIDE_TOTAL, action=action, http_status=status,
                    replayed=(status == 200))
        _record_ofac_hold(status == 200, decision.get('action'))
        payout_events.sync(database)
        return jsonify(payout=payouts.get_payout(database, payout_id), decision=decision,
                       operator=g.operator.public()), status

    @app.post('/payouts/<payout_id>/<any(approve, hold, return, clear, block):action>')
    def decide_payout(payout_id, action):
        limited = _rate_limited(rate_limits.BUCKET_DECIDE)
        if limited is not None:
            return limited
        body = _require_json_object('Send a JSON object (reason optional).')
        return _run_decide(payout_id, action, body)

    # FEATURE 21b — Versioned decide (action in JSON body)
    @app.post('/v1/payouts/<payout_id>/decisions')
    def v1_decide_payout(payout_id):
        limited = _rate_limited(rate_limits.BUCKET_DECIDE)
        if limited is not None:
            return limited
        body = _require_json_object('Send a JSON object with action (reason optional).')
        action = body.get('action')
        if action not in payouts.DECISIONS:
            raise APIError(422, 'Decision must be approve, hold, return, clear or block.',
                           code=api_contract.VALIDATION_ERROR)
        return _run_decide(payout_id, action, body)

    # FEATURE 29 — List every reason a payout is in review (#32)
    # Behind the scenes JSON for the later "Why this is in review" panel.
    # Read-only. No Desk HTML or screen change. 200 returns the payout id, a
    # list of {code, label, details} items, and `narrative` (one paragraph
    # joined from those labels). reasons and narrative are empty when the
    # payout is not in review. 404 if the payout does not exist.
    @app.get('/payouts/<payout_id>/review-reasons')
    def payout_review_reasons(payout_id):
        import review_reasons
        items = review_reasons.for_payout(g.payments.database, payout_id)
        narrative = review_reasons.narrative_from_stored_reasons(items)
        return jsonify(payout_id=payout_id, reasons=items, narrative=narrative)

    # FEATURE 29b — Read-only audit trail for one payout (status + execution)
    # Same session/operator gate as /desk and /review-reasons. Rows come only
    # from stored payout_events and execution_events (oldest first). 404 when
    # the payout is missing. No secrets; evidence stays off this payload.
    @app.get('/payouts/<payout_id>/events')
    def payout_audit_events(payout_id):
        events = payouts.audit_trail(g.payments.database, payout_id)
        return jsonify(payout_id=payout_id, events=events)

    # FEATURE 22 — Reset this visitor's Desk demo to the starting set (#32 slice 4)
    # Calls payouts.reset() on this visitor's own database only. That restores the
    # synthetic seed and clears this demo's action log; it never touches anyone else.
    # Requires the admin role. 200 means the starting set was written back.
    @app.post('/payouts/reset')
    def reset_payouts():
        if not g.operator.can_reset():
            return jsonify(error='Only an admin operator can reset the demo.',
                           code='OPERATOR_FORBIDDEN'), 403
        limited = _rate_limited(rate_limits.BUCKET_RESET)
        if limited is not None:
            return limited
        database = g.payments.database
        restored = payouts.reset(database)
        event('payouts_reset', restored=restored, operator_id=g.operator.operator_id)
        payout_events.after_reset(database)
        return jsonify(restored=restored, operator=g.operator.public())

    # FEATURE 26 — Replay the bundled synthetic payment-outcome events (#56)
    # Behind the scenes only: a POST that uses this visitor's existing demo
    # session cookie (a POST cannot start a new session). Plays fixtures/
    # execution_events_demo.json once, in order, into this visitor's database.
    # Results are JSON only. No page, link or button is added. Replay does not
    # emit stream events or change Desk words, layout or queue order. Reset
    # demo still restores the starting set.
    @app.post('/payouts/execution-replay')
    def replay_execution_events():
        refused = _require_demo_tools()
        if refused is not None:
            return refused
        database = g.payments.database
        report = execution_replay.replay(database)
        event('execution_events_replayed', applied=report['applied'],
              skipped=report['skipped'], refused=report['refused'])
        return jsonify(report)

    # FEATURE 25 — Create one synthetic payee payout in this visitor's demo (#91)
    # Behind the scenes only: POST JSON, requires this visitor's existing demo
    # session cookie (a POST cannot start a new session). Creates only fictional
    # DEMO- payouts in that visitor's database and returns JSON. Needs an
    # Idempotency-Key header so a double-submit replays the first create (200)
    # instead of inserting a second disbursement. No page, link or button is
    # added. Reset demo still restores the starting set.
    def _next_demo_payout_id(database):
        with storage.connect(str(database)) as db:
            ids = [row[0] for row in db.execute('SELECT payout_id FROM payouts')]
        numbers = [int(payout_id[3:]) for payout_id in ids
                   if payout_id.startswith('PO-') and payout_id[3:].isdigit()]
        return f'PO-{max(numbers, default=0) + 1}'

    def _run_create(body):
        database = g.payments.database
        # Require the header here so a missing key is 422 (not a silent create:{id} default).
        idempotency_key = payouts.clean_idempotency_key(request.headers.get('Idempotency-Key'))
        payout_id = body.get('payout_id') or _next_demo_payout_id(database)
        payout_events.sync(database)
        result = payout_rules.create(
            database, payout_id, body.get('payee_name'), body.get('payee_account'),
            body.get('amount_cents'), body.get('rail', 'ACH'),
            bank_details_changed_at=body.get('bank_details_changed_at'),
            routing_number=body.get('routing_number'),
            idempotency_key=idempotency_key)
        payout_id = result['payout_id']
        status = 200 if result.get('replayed') else 201
        event('payout_simulated_replayed' if status == 200 else 'payout_simulated',
              payout_id=payout_id, status=result['status'])
        payout = payouts.get_payout(database, payout_id)
        route_event = result.get('event') or {}
        held_flag = payout.get('flag_code') if result.get('status') == payouts.FLAGGED else None
        metrics.inc(metrics.CREATE_TOTAL, http_status=status,
                    payout_status=result['status'],
                    replayed=bool(result.get('replayed')))
        _record_ofac_hold(bool(result.get('replayed')), route_event.get('action'), held_flag)
        payout_events.sync(database)
        return jsonify(payout=payout,
                       status=result['status'], changed=result['changed'],
                       replayed=bool(result.get('replayed')),
                       create_event=result.get('create_event'),
                       event=result['event']), status

    @app.post('/payouts/simulate')
    def simulate_payout():
        refused = _require_demo_tools()
        if refused is not None:
            return refused
        limited = _rate_limited(rate_limits.BUCKET_SIMULATE)
        if limited is not None:
            return limited
        body = _require_json_object(
            'Send a JSON object with a payee name, DEMO- account and amount.')
        return _run_create(body)

    # FEATURE 25b — Versioned create (same body as simulate)
    @app.post('/v1/payouts')
    def v1_create_payout():
        limited = _rate_limited(rate_limits.BUCKET_CREATE)
        if limited is not None:
            return limited
        body = _require_json_object(
            'Send a JSON object with a payee name, DEMO- account and amount.')
        return _run_create(body)

    # FEATURE 23 — Stream payout created and status-changed events (#43)
    # Server-sent events for this visitor's demo only. A reconnect sends the
    # Last-Event-ID header (or lastEventId query) and resumes after that id.
    # The connection closes after a short hold so the one Gunicorn worker's
    # four threads stay available; EventSource reconnects. The Desk page does
    # not subscribe yet, so nothing on screen changes.
    # FEATURE 24 — Cap concurrent streams so they cannot block /health (#83)
    # At most two streams may be open across workers sharing DEMO_DIRECTORY.
    # A third request gets 503 with Retry-After and a short plain body so
    # /health keeps capacity. The slot token is acquired before the response
    # starts and released in a finally block when the generator ends, errors,
    # or the client disconnects.
    @app.get('/payouts/stream')
    def payout_stream():
        last_id = payout_events.parse_last_id(
            request.headers.get('Last-Event-ID') or request.args.get('lastEventId'))
        slot_token = payout_events.acquire_stream()
        if not slot_token:
            metrics.inc(metrics.STREAM_REFUSED_TOTAL, reason='slot_cap')
            g.metric_503_reason = 'stream_cap'
            return Response(
                'Too many live update streams are open. Try again shortly.\n',
                status=503,
                mimetype='text/plain',
                headers={'Retry-After': str(payout_events.RETRY_AFTER_SECONDS)})
        metrics.inc(metrics.STREAM_OPEN_TOTAL)
        released = False

        def release_slot():
            nonlocal released
            if not released:
                released = True
                payout_events.release_stream(slot_token)

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
        refused = _require_demo_tools()
        if refused is not None:
            return refused
        limited = _rate_limited(rate_limits.BUCKET_RECON)
        if limited is not None:
            return limited
        import reconciliation
        database = g.payments.database
        report = reconciliation.reconcile(database)
        event('reconciliation_run', run_id=report['run']['run_id'],
              matched=report['run']['matched_count'],
              mismatched=report['run']['mismatched_count'],
              unknown=report['run']['unknown_count'])
        return jsonify(report)

    # FEATURE 27b — Stripe recon operator surface (dry-run default; admin only)
    # GET returns the last mismatch summary (or null). POST runs
    # stripe_reconciliation; omit apply or send apply:false for dry-run.
    # apply:true is required to write. Never auto-applies.
    def _require_stripe_recon_admin():
        if not g.operator.can_reconcile_stripe():
            return jsonify(error='Only an admin operator can run Stripe reconciliation.',
                           code=api_contract.OPERATOR_FORBIDDEN), 403
        return None

    @app.route('/stripe/reconciliation', methods=['GET', 'POST'])
    def stripe_reconciliation_surface():
        refused = _require_stripe_recon_admin()
        if refused is not None:
            return refused
        database = g.payments.database
        if request.method == 'GET':
            return jsonify(summary=stripe_reconciliation.load_last_summary(database))
        body = request.get_json(silent=True)
        if body is None:
            if request.data:
                return jsonify(error='Send a JSON object, or an empty body for dry-run.',
                               code=api_contract.INVALID_JSON_BODY), 422
            body = {}
        if not isinstance(body, dict):
            return jsonify(error='Send a JSON object with optional apply.',
                           code=api_contract.INVALID_JSON_BODY), 422
        apply = bool(body.get('apply'))
        result = stripe_reconciliation.run_and_remember(
            database, dry_run=not apply, operator_id=g.operator.operator_id)
        event('stripe_reconciliation_run', dry_run=not apply,
              operator_id=g.operator.operator_id,
              mismatch=result['report']['run'].get('mismatch', 0),
              would_update=result['report']['run'].get('would_update', 0),
              updated=result['report']['run'].get('updated', 0))
        return jsonify(report=result['report'], summary=result['summary'],
                       operator=g.operator.public())

    @app.post('/stripe/webhook')
    def stripe_webhook():
        secret = (os.environ.get('STRIPE_WEBHOOK_SECRET') or '').strip()
        if not secret:
            return jsonify(error='Stripe webhook is not configured.'), 503
        try:
            command = stripe_connect.parse_webhook(
                request.get_data(cache=False), request.headers.get('Stripe-Signature'), secret)
        except stripe_connect.WebhookSignatureError:
            return jsonify(error='Invalid Stripe signature.'), 400
        if command is None:
            return jsonify(received=True), 200
        if command.get('livemode'):
            return jsonify(error='Stripe send is blocked.',
                           code=api_contract.STRIPE_SEND_BLOCKED), 422
        path = stripe_connect.demo_database(directory, command.get('demo_id') or '')
        if path is None:
            return jsonify(received=True), 200
        result = payouts.apply_stripe_event(path, command)
        if result == 'pending':
            return jsonify(error='Payout is not submitted yet.'), 503
        return jsonify(received=True), 200

    @app.route('/internal/render-events',
               methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
               provide_automatic_options=False)
    def render_deploy_events():
        secret = render_events.configured_secret()
        if not render_events.header_matches(
                request.headers.get(render_events.HEADER), secret):
            abort(404)
        if request.method != 'POST':
            abort(405)
        try:
            notice = render_events.notice_from_payload(request.get_data(cache=False))
        except ValueError:
            return jsonify(error='Invalid render event.'), 400
        event('render_deploy_event', **notice)
        metrics.inc(metrics.RENDER_DEPLOY_EVENT_TOTAL,
                    event_type=notice['event_type'], status=notice.get('status'))
        return Response(status=204)

    return app
