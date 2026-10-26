# START HERE — Writes troubleshooting log lines (one JSON record per event) into each
# visitor's own log file. Logs help explain what happened; the database is the truth.
"""Local diagnostic events, correlated by request ID. Not a financial audit ledger."""
import json
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

# Remembers "which log file and request ID belong to the request being handled right now",
# separately for each simultaneous request, so their log lines don't get mixed up.
request_context = ContextVar('request_context', default=None)


# FEATURE 01 — Write readable event records
# Convert an event and its details into a JSON log line with a timestamp and severity.
class EventFormatter(logging.Formatter):
    # FEATURE 02 — Attach a time and severity
    # The log line describes an observation, not a second source of account balances.
    def format(self, record):
        return json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                           'level': record.levelname, **record.event}, ensure_ascii=True)


# FEATURE 03 — Keep bounded diagnostic history
# Write rotating files plus console output. Rotation keeps only a limited recent
# history, so missing older logs do not prove an action never happened.
def make_logger(directory):
    directory = Path(directory)
    directory.mkdir(exist_ok=True, parents=True)
    logger = logging.Logger('payments.' + str(directory))
    logger.setLevel(logging.INFO)
    # When the file reaches ~256 KB, start a new one; keep 3 old ones and discard the rest.
    file_handler = RotatingFileHandler(directory / 'api.log', maxBytes=262144,
                                       backupCount=3, encoding='utf-8')
    # Write each event both to the file and to the Terminal window.
    for handler in (file_handler, logging.StreamHandler()):
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return logger


# FEATURE 04 — Label activity for the current request
# Record one named event (e.g. 'payment_saved') in the current visitor's log.
def event(name, level=logging.INFO, **fields):
    context = request_context.get()
    if context is None:
        return  # Direct non-HTTP calls, such as setup, are not fabricated requests.
    # Logging is separate from saving to the database: if the server stops right after a
    # payment is saved, the payment can exist without its log line.
    logger, request_id = context
    logger.log(level, name, extra={'event': {'request_id': request_id, 'event': name, **fields}})
