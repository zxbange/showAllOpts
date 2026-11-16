"""Durable handoff: validated public data -> configured MySQL -> local SQLite.

Callers hold their collection lock. A failed database/local commit leaves one
pending payload, replayed before fetching again. No credentials enter SQLite.
"""
from contextlib import nullcontext
import json

from app.core.database import build_engine
from app.core.history_sync import source_identity
from app.services.system_configuration import database_config


class PublicPublication:
    def __init__(self, service):
        self.service = service
        self.config = database_config(service.settings)
        self.engine = build_engine(service.settings, configuration=self.config)
        self.target = source_identity(self.engine)
        try:
            with service.db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS public_publication '
                           '(id INTEGER PRIMARY KEY CHECK(id=1), target TEXT NOT NULL, payload TEXT NOT NULL)')
        except BaseException:
            self.engine.dispose()
            raise

    def close(self):
        self.engine.dispose()

    def stage(self, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.service.db() as db:
            # Never discard a previous database commit awaiting local publication.
            db.execute('INSERT INTO public_publication VALUES (1,?,?)', (self.target, encoded))

    def resume(self, writer, apply, check, prepare=None):
        with self.service.db() as db:
            row = db.execute('SELECT target,payload FROM public_publication WHERE id=1').fetchone()
        if row is None:
            return False
        if row[0] != self.target:
            raise RuntimeError('待写入结果属于另一个数据库，请恢复原数据库配置后重试。')
        check()
        canonical = writer(self.engine, json.loads(row[1]), check)
        if prepare is not None:
            canonical = prepare(canonical)
        check()
        if database_config(self.service.settings) != self.config:
            raise RuntimeError('数据库配置已变化，已保留待发布结果。')
        lock = getattr(self.service, 'publication_lock', nullcontext)
        with lock(), self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            apply(canonical, db)
            check()
            db.execute('DELETE FROM public_publication WHERE id=1')
        return True
