"""Per-refresh stage progress, isolated between worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar

reporter = ContextVar('sync_progress', default=None)
span = ContextVar('sync_progress_span', default=(0, 100))

@contextmanager
def stage_range(start, end):
    low, high = span.get()
    token = span.set((low + (high-low)*start/100, low + (high-low)*end/100))
    try:
        yield
    finally:
        span.reset(token)

def report(stage, rows=0, percent=None):
    callback = reporter.get()
    if callback:
        if percent is not None:
            low, high = span.get()
            percent = round(low + (high-low)*percent/100)
        callback(stage, rows, percent)
