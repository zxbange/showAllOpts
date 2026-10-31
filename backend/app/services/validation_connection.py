"""One source engine and checked-out connection per uninterrupted task execution."""
from contextlib import nullcontext
from pymysql.err import OperationalError


class TaskSource:
    def __init__(self, jobs, lock):
        self.jobs, self.lock = jobs, lock
        self.key = self.context = self.engine = self.connection = None

    def get(self, job, needs_source):
        key = (job['id'], job['generation'])
        if self.key != key:
            self.close()
        if not needs_source:
            return self.engine if self.engine is not None else self.jobs.engine
        if self.engine is None:
            self.context = (self.jobs.sources.engine(job['source']) if self.jobs.sources is not None
                            else nullcontext(self.jobs.engine))
            try:
                self.engine = self.context.__enter__()
            except BaseException:
                self.context = None
                raise
            self.key = key
        return self.engine

    def checkout(self):
        if self.connection is not None and (self.connection.invalidated or self.connection.closed):
            # Do not let SQLAlchemy implicitly revalidate this checked-out socket.
            # Reconnection belongs to the durable task retry state machine.
            raise OperationalError(2006, 'validation connection unavailable')
        if self.connection is None:
            self.connection = self.engine.connect()
        return self.connection

    def close(self):
        context, connection = self.context, self.connection
        self.key = self.context = self.engine = self.connection = None
        try:
            if connection is not None:
                connection.close()
        finally:
            if context is not None:
                context.__exit__(None, None, None)
