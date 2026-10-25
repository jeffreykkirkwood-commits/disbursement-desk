"""Local diagnostic events, correlated by request ID. Not a financial audit ledger."""
import json
import logging
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

# Each HTTP worker gets its own context; simultaneous requests do not mix IDs.
request_context = ContextVar('request_context', default=None)


class EventFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                           'level': record.levelname, **record.event}, ensure_ascii=True)


def make_logger(directory):
    directory = Path(directory)
    directory.mkdir(exist_ok=True, parents=True)
    logger = logging.Logger('payments.' + str(directory))
    logger.setLevel(logging.INFO)
    # Rotate near 1 MB: keep api.log plus .1 (newest backup), .2, and .3.
    # Older events are discarded; this is not a permanent audit history.
    file_handler = RotatingFileHandler(directory / 'api.log', maxBytes=1000000,
                                       backupCount=3, encoding='utf-8')
    # Write the same JSON event to disk and Terminal (stderr). No retroactive event reconstruction.
    for handler in (file_handler, logging.StreamHandler()):
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return logger


def event(name, level=logging.INFO, **fields):
    context = request_context.get()
    if context is None:
        return  # Direct non-HTTP calls, such as setup, are not fabricated requests.
    # Diagnostic logging is separate from the SQLite transaction. A committed payment can
    # exist without its final log entry if the process stops or logging fails afterward.
    logger, request_id = context
    logger.log(level, name, extra={'event': {'request_id': request_id, 'event': name, **fields}})
