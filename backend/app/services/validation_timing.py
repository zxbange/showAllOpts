"""Persist actual worker time, never derive runtime from wall-clock task age."""
import logging
import threading
import time
import uuid
from app.services.validation_errors import report

LOG = logging.getLogger(__name__)
PULSE_SECONDS = 1


def interrupt_timing(job):
    if job.get('execution_token'):
        job['execution_approximate'] = True
    job.pop('execution_token', None)
    job.pop('execution_stop_mono', None)


def stop_timing(job):
    if job.get('execution_token') and job.get('execution_stop_mono') is None:
        job['execution_stop_mono'] = time.monotonic()


class ExecutionTimer:
    def __init__(self, jobs, job, stamp):
        self.jobs, self.ident = jobs, job['id']
        interrupt_timing(job)
        # Absence means pre-feature history, not a zero-duration historical job.
        if 'execution_seconds' not in job:
            job.update(execution_seconds=0.0, execution_partial=True)
        if not job.get('execution_partial') and not job.get('first_started_at'):
            job['first_started_at'] = stamp
        self.base = job['execution_seconds']
        self.started = time.monotonic()
        self.token = uuid.uuid4().hex
        job['execution_token'] = self.token
        self.done = threading.Event()
        self.stopped = None
        self.thread = None

    def request_stop(self):
        if self.stopped is None:
            self.stopped = time.monotonic()

    def flush(self, final=False):
        with self.jobs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                job = self.jobs.get(self.ident, db)
            except KeyError:
                return
            if job.get('execution_token') != self.token:
                return
            end = time.monotonic()
            for stopped in (self.stopped, job.get('execution_stop_mono')):
                if stopped is not None:
                    end = min(end, stopped)
            job['execution_seconds'] = max(job.get('execution_seconds', 0),
                                            self.base + max(0, end-self.started))
            if final:
                job.pop('execution_token', None)
                job.pop('execution_stop_mono', None)
            self.jobs.save(db, job)

    def pulse(self):
        while not self.done.wait(PULSE_SECONDS):
            try:
                self.flush()
            except Exception as exc:
                report(LOG, exc, {'id': self.ident}, 'local_store')

    def start(self):
        self.thread = threading.Thread(target=self.pulse, daemon=True, name='validation-runtime')
        self.thread.start()

    def finish(self):
        self.done.set()
        self.request_stop()
        # Token checks serialize any late pulse against a subsequent work unit.
        self.flush(final=True)
