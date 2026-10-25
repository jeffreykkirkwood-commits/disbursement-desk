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
    # Keep the current file plus three backups, each roughly 1 MB.
    file_handler = RotatingFileHandler(directory / 'api.log', maxBytes=1000000,
                                       backupCount=3, encoding='utf-8')
    for handler in (file_handler, logging.StreamHandler()):
        handler.setFormatter(EventFormatter())
        logger.addHandler(handler)
    return logger


def event(name, level=logging.INFO, **fields):
    context = request_context.get()
    if context is None:
        return  # Direct non-HTTP calls, such as setup, are not fabricated requests.
    logger, request_id = context
    logger.log(level, name, extra={'event': {'request_id': request_id, 'event': name, **fields}})
