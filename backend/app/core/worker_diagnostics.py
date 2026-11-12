"""Parent-side worker diagnostics; no request, environment or credential data.

Keep Uvicorn's restart/signal policy unchanged. Only instrument its Process
boundary, so an exit before probing is distinguishable from an unanswered ping.
"""
from contextlib import contextmanager
import logging
import time

from uvicorn.supervisors import multiprocess

LOG = logging.getLogger('app.server.workers')
# More tolerance for brief contention on small servers, not a fix for hangs.
WORKER_HEALTHCHECK_SECONDS = 30


class DiagnosticProcess(multiprocess.Process):
    def is_alive(self, timeout=WORKER_HEALTHCHECK_SECONDS):
        alive = self.process.is_alive()
        before = self.process.exitcode
        started = time.monotonic()
        answered = self.ping(timeout) if alive else False
        if not answered:
            # This runs BEFORE the supervisor calls kill(). A later -9 alone
            # cannot distinguish external SIGKILL from the supervisor's own kill.
            LOG.warning(
                'Worker health failure pid=%s alive_before=%s exitcode_before=%s '
                'heartbeat=%s elapsed_seconds=%.3f timeout_seconds=%s '
                'alive_after=%s exitcode_after=%s',
                self.pid, alive, before,
                'no_response' if alive else 'not_sent',
                time.monotonic() - started, timeout,
                self.process.is_alive(), self.process.exitcode)
        return answered

    def join(self):
        super().join()
        LOG.info('Worker reaped pid=%s exitcode=%s', self.pid, self.process.exitcode)


@contextmanager
def worker_diagnostics():
    """Instrument only the parent's Uvicorn run, restoring globals on exit."""
    original = multiprocess.Process
    multiprocess.Process = DiagnosticProcess
    try:
        yield
    finally:
        multiprocess.Process = original
