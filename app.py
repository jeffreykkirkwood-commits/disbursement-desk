"""Local learning simulator. Uses fictional accounts; never moves money."""
import json
import logging
import time
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from dashboard import render_dashboard
from log_view import render_logs
from urllib.parse import urlsplit, parse_qs
from event_log import event, make_logger, request_context


# Expected request errors carry an HTTP status and a message for the caller.
class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


# Payment features: store instructions, process fictional transfers, and read saved data.
class Payments:
    # DATABASE SETUP: create missing tables without deleting existing payments or balances.
    def __init__(self, database):
        self.database = str(database)
        with sqlite3.connect(self.database) as db:
            # Keep the original request for duplicate checks and the latest outcome for retrieval.
            db.execute('''CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                request TEXT NOT NULL,
                response TEXT NOT NULL
            )''')
            # Store whole cents for exact arithmetic; the database forbids negative balances.
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, balance_cents INTEGER NOT NULL CHECK(balance_cents >= 0)
            )''')
            # Seed checking with $100 and merchant with $0 only if missing. Either can later send.
            # Restarting preserves their existing balances.
            db.executemany('INSERT OR IGNORE INTO accounts VALUES (?, ?)',
                           [('DEMO-CHECKING-001', 10000), ('DEMO-MERCHANT-002', 0)])

    # PROCESSING: find a saved instruction, evaluate it, and save its final outcome.
    def process(self, payment_id):
        # Balance changes and the final status are committed together, once.
        with sqlite3.connect(self.database, timeout=10) as db:
            # Reserve the database write transaction so simultaneous requests cannot race these checks.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT response FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
            # No record matches this ID: report 404 instead of inventing a payment.
            if not row:
                raise APIError(404, 'Payment not found.')
            payment = json.loads(row[0])
            event('payment_loaded', payment_id=payment_id, status=payment['status'])
            # Already final: return the saved result. Never debit again or restart a rejected instruction.
            if payment['status'] in ('COMPLETED', 'REJECTED'):
                event('processing_skipped', payment_id=payment_id, status=payment['status'], reason='Already final; balances unchanged')
                return payment
            # Only a pending instruction can enter this simplified processing step.
            if payment['status'] != 'RECEIVED':
                raise APIError(409, 'Payment cannot be processed from its current status.')
            # Look up both accounts. The first column of each returned row is its balance in cents.
            sender = db.execute('SELECT balance_cents FROM accounts WHERE account_id = ?',
                                (payment['debtor_account'],)).fetchone()
            receiver = db.execute('SELECT balance_cents FROM accounts WHERE account_id = ?',
                                  (payment['creditor_account'],)).fetchone()
            # Convert the validated dollar amount to whole cents without floating-point rounding.
            cents = int(Decimal(payment['amount']) * 100)
            # An unknown account cannot participate: preserve the instruction as a business rejection.
            if sender is None or receiver is None:
                payment.update(status='REJECTED', reason_code='ACCOUNT_NOT_FOUND')
            # Your rule: insufficient funds means REJECTED; leave both balances unchanged.
            elif sender[0] < cents:
                payment.update(status='REJECTED', reason_code='INSUFFICIENT_FUNDS')
            else:
                # The else branch means both accounts exist and funds suffice: subtract from sender.
                db.execute('UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id = ?',
                           (cents, payment['debtor_account']))
                # Add the same cents to the receiver. This only changes fictional database balances.
                db.execute('UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?',
                           (cents, payment['creditor_account']))
                payment.update(status='COMPLETED')
            # Save when the final decision happened, whether completed or rejected.
            payment['processed_at'] = datetime.now(timezone.utc).isoformat()
            # Save the outcome in the same transaction as balance changes: commit together or roll back.
            db.execute('UPDATE payments SET response = ? WHERE payment_id = ?',
                       (json.dumps(payment), payment_id))
        # These events describe committed results, never merely attempted SQL updates.
        details = dict(payment_id=payment_id, status=payment['status'], amount=payment['amount'])
        if payment['status'] == 'COMPLETED':
            details.update(sender=payment['debtor_account'], receiver=payment['creditor_account'],
                           sender_before=format(Decimal(sender[0]) / 100, '.2f'),
                           sender_after=format(Decimal(sender[0] - cents) / 100, '.2f'),
                           receiver_before=format(Decimal(receiver[0]) / 100, '.2f'),
                           receiver_after=format(Decimal(receiver[0] + cents) / 100, '.2f'), currency='USD')
        else:
            details.update(reason=payment['reason_code'], balances_changed=False)
        event('payment_processed', **details)
        return payment

    # CREATION: this function saves or retrieves an instruction; it does not move balances.
    # The POST handler calls process() next, before returning the final outcome.
    def create(self, data, key):
        # Require a usable retry label. The 200-character cap is an existing technical limit.
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise APIError(422, 'Provide an Idempotency-Key header (1–200 characters).')
        fields = {'debtor_account', 'creditor_account', 'amount', 'currency', 'rail'}
        # Require exactly these fields; callers cannot submit their own payment ID or status.
        if not isinstance(data, dict) or set(data) != fields:
            raise APIError(422, 'Provide exactly: debtor_account, creditor_account, amount, currency, rail.')
        # Existing demo constraint: both account identifiers use the DEMO- naming convention.
        for field in ('debtor_account', 'creditor_account'):
            value = data[field]
            if not isinstance(value, str) or not value.startswith('DEMO-') or not 6 <= len(value) <= 80:
                raise APIError(422, field + ' must be a fictional DEMO- account (6–80 characters).')
        # Your rule: sender and receiver must differ. Reject before saving an instruction.
        if data['debtor_account'] == data['creditor_account']:
            raise APIError(422, 'Sender and receiver must be different accounts.')
        # Existing demo scope: USD only, with ACH/RTP labels; no payment networks are contacted.
        if data['currency'] != 'USD' or data['rail'] not in ('ACH', 'RTP'):
            raise APIError(422, 'This simulator accepts USD and the ACH or RTP rail labels.')
        # Read money as text, then Decimal, to preserve its exact decimal value.
        raw = data['amount']
        if not isinstance(raw, str) or len(raw) > 32:
            raise APIError(422, 'Send amount as a decimal string, such as "125.50".')
        try:
            amount = Decimal(raw)
        # The supplied text is not a valid decimal amount.
        except InvalidOperation:
            raise APIError(422, 'Amount must be a valid decimal.')
        # Your positive-amount rule also excludes infinity and NaN, which are not usable money amounts.
        if not amount.is_finite() or amount <= 0:
            raise APIError(422, 'Payment amount must be greater than zero.')
        # Demo constraint: reject amounts above $1 million before creating a record.
        if amount > Decimal('1000000'):
            raise APIError(422, 'Demo payments are limited to $1,000,000.')
        # Reject fractional cents (5.101), but allow trailing zeros (5.100 equals 5.10).
        if amount != amount.quantize(Decimal('0.01')):
            raise APIError(422, 'USD amounts cannot have fractions of a cent.')
        # Normalize equivalent values such as 25 and 25.00 before comparing retry details.
        normalized = dict(data, amount=format(amount.quantize(Decimal('0.01')), 'f'))
        request = json.dumps(normalized, sort_keys=True)
        with sqlite3.connect(self.database, timeout=10) as db:
            # Check the key and insert under one write transaction: concurrent retries create one record.
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT request, response FROM payments WHERE idempotency_key = ?', (key,)).fetchone()
            # This retry key is already stored. Compare its original instruction with this request.
            if existing:
                # Same key with different details is a conflict, not a new payment or an overwrite.
                if existing[0] != request:
                    event('duplicate_conflict', level=logging.WARNING, payment_id=json.loads(existing[1])['payment_id'])
                    raise APIError(409, 'This Idempotency-Key was already used for different payment details.')
                # Same key and details: reuse the saved record, even if it is still RECEIVED.
                # The POST handler then processes it or returns its already-final outcome.
                event('duplicate_returned', payment_id=json.loads(existing[1])['payment_id'])
                return 200, json.loads(existing[1])
            # New key: generate an ID and timestamp, and save a pending instruction.
            payment = dict(normalized, payment_id=str(uuid4()), status='RECEIVED',
                           created_at=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO payments VALUES (?, ?, ?, ?)',
                       (payment['payment_id'], key, request, json.dumps(payment)))
        # HTTP 201 means a new record was created; the handler processes it before replying.
        event('payment_saved', payment_id=payment['payment_id'], status='RECEIVED', amount=payment['amount'], currency=payment['currency'])
        return 201, payment

    # DASHBOARD: read both tables in one snapshot so balances and outcomes agree.
    def dashboard_data(self):
        with sqlite3.connect(self.database) as db:
            # A read transaction keeps both SELECTs on the same database snapshot.
            db.execute('BEGIN')
            accounts = db.execute('SELECT account_id, balance_cents FROM accounts ORDER BY account_id').fetchall()
            rows = db.execute('SELECT response FROM payments ORDER BY rowid').fetchall()
        return {
            'accounts': [{'account_id': account_id,
                          'balance': format(Decimal(cents) / Decimal(100), '.2f'),
                          'currency': 'USD'} for account_id, cents in accounts],
            'payments': [json.loads(row[0]) for row in rows],
            'read_at': datetime.now(timezone.utc).isoformat()
        }

    # ACCOUNT VIEW: read balances without changing them; show dollars and a USD label.
    def list_accounts(self):
        with sqlite3.connect(self.database) as db:
            rows = db.execute('SELECT account_id, balance_cents FROM accounts ORDER BY account_id').fetchall()
        # Keep cents in storage; format exact dollars as a string with two decimal places.
        return [{'account_id': account_id,
                 'balance': format(Decimal(balance_cents) / Decimal(100), '.2f'),
                 'currency': 'USD'} for account_id, balance_cents in rows]

    # PAYMENT LIST: return all saved outcomes, in insertion order, including rejections.
    def list_payments(self):
        with sqlite3.connect(self.database) as db:
            rows = db.execute('SELECT response FROM payments ORDER BY rowid').fetchall()
        return [json.loads(row[0]) for row in rows]

    # PAYMENT LOOKUP: return the latest saved outcome for one ID.
    def get(self, payment_id):
        with sqlite3.connect(self.database) as db:
            row = db.execute('SELECT response FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        # No record matches this ID: report 404 instead of inventing a payment.
        if not row:
            raise APIError(404, 'Payment not found.')
        return json.loads(row[0])


# HTTP INTERFACE: map request methods and addresses to the payment features above.
def handler_for(payments):
    logger = make_logger(Path(payments.database).parent / 'logs')

    class Handler(BaseHTTPRequestHandler):
        # Give each API call a new request ID; retries get new request IDs but reuse payment IDs.
        # A context variable keeps simultaneous worker threads from mixing request IDs.
        def handle_one_request(self):
            self.request_id = str(uuid4())
            self.response_status = None
            self.request_seen = False
            started = time.monotonic()
            token = request_context.set((logger, self.request_id))
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                event('response_disconnected', level=logging.WARNING,
                      reason='Client disconnected; inspect payment outcome before retrying')
            except Exception as error:
                # Log the error type, not arbitrary exception text that may contain submitted data.
                event('internal_error', level=logging.ERROR, error_type=type(error).__name__)
                if self.response_status is None:
                    self.reply(500, {'error': 'Internal server error. Retry with the same idempotency key.'})
            finally:
                # This records the server's attempted response, not proof the client received it.
                if self.request_seen or self.response_status is not None:
                    event('request_finished', http_status=self.response_status,
                          duration_ms=round((time.monotonic() - started) * 1000, 2))
                request_context.reset(token)

        def parse_request(self):
            valid = super().parse_request()
            if valid:
                self.request_seen = True
                # This arrival event omits raw headers, bodies, query strings, and idempotency keys.
                # Later events include selected payment fields and fictional balance changes.
                path = self.path.split('?', 1)[0]
                if path.startswith('/payments/'):
                    path = '/payments/{payment_id}/process' if path.endswith('/process') else '/payments/{payment_id}'
                elif path not in ('/', '/payments', '/accounts', '/dashboard', '/dashboard/', '/health', '/payment-form.js', '/logs'):
                    path = '(unknown endpoint)'
                event('request_received', method=self.command, endpoint=path)
            return valid

        # Return the correlation ID in a header so callers can find this request in the logs.
        def send_response(self, code, message=None):
            self.response_status = code
            super().send_response(code, message)
            self.send_header('X-Request-ID', self.request_id)

        def log_message(self, format, *args):
            # Replace the default access line with our correlated structured events.
            pass

        def log_error(self, format, *args):
            event('http_protocol_error', level=logging.WARNING)

        # Send HTTP metadata plus a readable JSON response body.
        def reply(self, status, payload):
            if status >= 400:
                event('request_rejected', level=logging.WARNING, http_status=status, reason=payload.get('error', 'Request failed'))
            body = json.dumps(payload, indent=2).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # GET requests read app data; they do not move balances or create payment instructions.
        # They still generate diagnostic log events. Browser navigation uses GET.
        def do_GET(self):
            # Troubleshooting page reads diagnostic files, not payment records or balances.
            if urlsplit(self.path).path == '/logs':
                body = render_logs(Path(payments.database).parent / 'logs',
                                   parse_qs(urlsplit(self.path).query), self.request_id).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Serve the dashboard form's browser code from this one known file.
            if self.path == '/payment-form.js':
                body = Path(__file__).with_name('payment-form.js').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/javascript; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Combined browser UI: read one snapshot, then render HTML instead of JSON.
            if self.path in ('/dashboard', '/dashboard/'):
                body = render_dashboard(payments.dashboard_data()).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                # A refresh must read fresh data rather than reuse an older cached snapshot.
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Account list: display current dollar balances.
            if self.path == '/accounts':
                return self.reply(200, payments.list_accounts())
            # Payment list: display the current saved payment history.
            if self.path == '/payments':
                return self.reply(200, payments.list_payments())
            # Health check: confirm the server can answer a request.
            if self.path == '/health':
                return self.reply(200, {'status': 'ok', 'mode': 'fictional payments only'})
            # Home address: describe the supported methods and addresses.
            if self.path == '/':
                return self.reply(200, {'application': 'Payments API Simulator',
                                       'endpoints': ['GET /logs', 'GET /dashboard', 'GET /accounts', 'POST /payments', 'GET /payments', 'GET /payments/{payment_id}', 'POST /payments/{payment_id}/process', 'GET /health'],
                                       'note': 'RECEIVED means recorded, not processed. No money moves.'})
            # Single-payment address: extract the ID from the path and look up that record.
            if self.path.startswith('/payments/'):
                try:
                    return self.reply(200, payments.get(self.path[len('/payments/'):]))
                # Convert an expected application error into an HTTP error response.
                except APIError as error:
                    return self.reply(error.status, {'error': error.message})
            # No GET route matched this address.
            self.reply(404, {'error': 'Endpoint not found.'})

        # POST requests create instructions or explicitly ask to process an existing one.
        def do_POST(self):
            parts = self.path.strip('/').split('/')
            # Retain the manual processing endpoint; final outcomes are safe to request again.
            if len(parts) == 3 and parts[0] == 'payments' and parts[2] == 'process':
                try:
                    return self.reply(200, payments.process(parts[1]))
                # Convert an expected application error into an HTTP error response.
                except APIError as error:
                    return self.reply(error.status, {'error': error.message})
            # Other POST addresses are not implemented.
            if self.path != '/payments':
                return self.reply(404, {'error': 'Endpoint not found.'})
            try:
                # This small teaching server does not implement streamed/chunked request bodies.
                if self.headers.get('Transfer-Encoding'):
                    raise APIError(400, 'Transfer-Encoding is not supported.')
                # Require JSON input; return 415 for a different content type.
                if self.headers.get_content_type() != 'application/json':
                    raise APIError(415, 'Use Content-Type: application/json.')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                # The body length header must be a valid integer.
                except ValueError:
                    raise APIError(400, 'Invalid Content-Length.')
                # Existing technical limit: require a nonempty body no larger than 10,000 bytes.
                if length <= 0 or length > 10000:
                    raise APIError(400, 'Provide a JSON body of 1–10000 bytes.')
                try:
                    data = json.loads(self.rfile.read(length))
                # Malformed JSON or invalid text encoding: return 400 before creating anything.
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, 'Invalid JSON body.')
                # Agreed flow: save or retrieve the instruction, then process it before replying.
                status, result = payments.create(data, self.headers.get('Idempotency-Key'))
                # On a normal response, processing is synchronous: return COMPLETED or REJECTED.
                # Final retries do not move funds again. Unexpected failures may instead return 500.
                # Save and process are separate transactions: a crash between them can leave RECEIVED.
                # No background recovery worker exists; retry the same key or call /process.
                result = payments.process(result['payment_id'])
                # 201 for a new record, 200 for a retry, including business rejections.
                # Invalid input instead returns an HTTP error before any payment is saved.
                self.reply(status, result)
            # Convert an expected application error into an HTTP error response.
            except APIError as error:
                self.reply(error.status, {'error': error.message})

    return Handler


# STARTUP: runs when you type python3 app.py, not when tests import this file.
if __name__ == '__main__':
    # Use the database next to this script, regardless of the Terminal working folder.
    database = Path(__file__).with_name('payments.sqlite3')
    port = int(os.environ.get('PORT', '8000'))
    # Listen only on this Mac. This teaching server has no authentication; it is not public hosting.
    server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(Payments(database)))
    print('Payments simulator running at http://127.0.0.1:%s — Ctrl+C to stop' % port, flush=True)
    try:
        server.serve_forever()
    # Control+C stops the request loop.
    except KeyboardInterrupt:
        pass
    # Always close the listening socket when the server exits.
    finally:
        server.server_close()
