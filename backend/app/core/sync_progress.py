"""Per-refresh progress callbacks, isolated between worker threads."""
from contextvars import ContextVar

reporter = ContextVar('sync_progress', default=None)

def report(stage, rows=0):
    callback = reporter.get()
    if callback:
        callback(stage, rows)
