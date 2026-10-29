"""Shared, atomic schedule preferences, independent of snapshot publication state."""
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime

import portalocker


TIME_PATTERN = r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'


class CacheScheduleError(RuntimeError):
    pass


class CacheSchedule:
    def __init__(self, settings):
        self.root = settings.cache_path
        self.path = self.root / 'schedule.json'
        # Migrate the former shipped default, including old private config files.
        # A time saved through the UI always wins, including an explicit 20:00.
        self.fallback = '07:00' if settings.cache_refresh_time == '20:00' else settings.cache_refresh_time

    @contextmanager
    def locked(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with portalocker.Lock(str(self.root / 'schedule.lock'), timeout=2):
                yield
        except (OSError, portalocker.exceptions.LockException) as exc:
            raise CacheScheduleError('无法读写同步时间设置，请检查服务存储权限后重试。') from exc

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.get('version') != 1
                    or not isinstance(data.get('daily_time'), str)
                    or re.fullmatch(TIME_PATTERN, data['daily_time']) is None
                    or not isinstance(data.get('revision'), str) or not data['revision']):
                raise ValueError('Invalid schedule')
            changed = datetime.fromisoformat(data['changed_at'])
            if changed.utcoffset() is None:
                raise ValueError('Naive schedule timestamp')
            return data
        except FileNotFoundError:
            return dict(daily_time=self.fallback, revision='config:' + self.fallback, changed_at=None)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Never silently reset a saved preference when its file is damaged.
            raise CacheScheduleError('无法读取同步时间设置，请检查服务存储后重试。') from exc

    def save(self, daily_time, clock):
        if not isinstance(daily_time, str) or re.fullmatch(TIME_PATTERN, daily_time) is None:
            raise ValueError('同步时间须为 HH:mm（00:00–23:59）。')
        with self.locked():
            current = self.read()
            if current['daily_time'] == daily_time and current.get('version') == 1:
                return current
            data = dict(version=1, daily_time=daily_time, revision=uuid.uuid4().hex,
                        changed_at=clock().isoformat())
            temporary = self.root / ('schedule-' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return data
