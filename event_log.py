# START HERE — How requests leave bounded, visitor-specific diagnostic traces.
"""Local diagnostic events, correlated by request ID. Not a financial audit ledger."""
import json
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

# Each HTTP worker gets its own context; simultaneous requests do not mix IDs.
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
    # Rotate near 256 KB: retain the active log and three backups per demo.
    # Older events are discarded; this is not a permanent audit history.
    file_handler = RotatingFileHandler(directory / 'api.log', maxBytes=262144,
                                       backupCount=3, encoding='utf-8')
    # Write the same JSON event to disk and Terminal (stderr). No retroactive event reconstruction.
    for handler in (file_handler, logging.StreamHandler()):
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return logger


# FEATURE 04 — Label activity for the current request
# Use the current request context to associate events with the right visitor log.
# Logging happens separately from the financial database transaction and is not a
# complete accounting ledger.
def event(name, level=logging.INFO, **fields):
    context = request_context.get()
    if context is None:
        return  # Direct non-HTTP calls, such as setup, are not fabricated requests.
    # Diagnostic logging is separate from the SQLite transaction. A committed payment can
    # exist without its final log entry if the process stops or logging fails afterward.
    logger, request_id = context
    logger.log(level, name, extra={'event': {'request_id': request_id, 'event': name, **fields}})
