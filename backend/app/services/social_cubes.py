"""Persistent portfolio events, sharing social login, scheduling and delivery."""
from datetime import datetime, timedelta, timezone
import json

from app.services.social_cube_sources import cube_symbol, cube_message_parts
from app.services.social_sources import MonitoringDeferred, plain_text

CHINA = timezone(timedelta(hours=8))


def cube_monitoring_time(moment):
    moment = moment.astimezone(CHINA)
    return moment.weekday() < 5 and 9 <= moment.hour < 15


def next_cube_check_time(moment, delay=0):
    candidate = moment.astimezone(CHINA) + timedelta(seconds=delay)
    if cube_monitoring_time(candidate):
        return candidate.timestamp()
    if candidate.weekday() < 5 and candidate.hour < 9:
        return candidate.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    candidate = (candidate + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def event_time(value):
    try:
        moment = datetime.fromisoformat(value)
        return moment.replace(tzinfo=CHINA) if moment.tzinfo is None else moment.astimezone(CHINA)
    except (ValueError, TypeError):
        return None


class CubeMonitorMixin:
    @staticmethod
    def init_cube_schema(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS cubes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                initialized INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL,
                last_check TEXT, last_error TEXT, last_success_at TEXT,
                scan_page INTEGER NOT NULL DEFAULT 1, scan_started_at TEXT,
                baseline_at TEXT, baseline_max_id INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cube_rebalances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cube_id INTEGER NOT NULL REFERENCES cubes(id) ON DELETE CASCADE,
                rebalance_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                occurred_at TEXT NOT NULL, discovered_at TEXT NOT NULL,
                notify_status TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT, last_error TEXT, UNIQUE(cube_id,rebalance_id));
            CREATE INDEX IF NOT EXISTS cube_rebalances_cube ON cube_rebalances(cube_id,id);
        ''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(cubes)')}
        for name, declaration in (
                ('cube_name', "TEXT NOT NULL DEFAULT ''"),
                ('owner_name', "TEXT NOT NULL DEFAULT ''"),
                ('owner_id', "TEXT NOT NULL DEFAULT ''"),
                ('metadata_checked_at', 'TEXT')):
            if name not in columns:
                db.execute(f'ALTER TABLE cubes ADD COLUMN {name} {declaration}')

    def add_cube(self, symbol, name=''):
        import sqlite3
        symbol = cube_symbol(symbol)
        try:
            with self.db() as db:
                db.execute('INSERT INTO cubes(symbol,name,added_at) VALUES(?,?,?)',
                           (symbol, name.strip() or symbol, self.current_moment().isoformat(timespec='seconds')))
                config = self.config(db)
                if config['enabled']:
                    config['next_check'] = self.current_moment().timestamp()
                    self.write_config(db, config)
        except sqlite3.IntegrityError:
            raise ValueError('这个组合已经在监控列表中') from None

    def change_cube(self, identifier, enabled=None, delete=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if delete:
                result = db.execute('DELETE FROM cubes WHERE id=?', (identifier,))
            else:
                result = db.execute('UPDATE cubes SET enabled=? WHERE id=?', (int(enabled), identifier))
            if result.rowcount:
                config = self.config(db)
                queue = config.get('platform_queues', {}).get('xueqiu')
                if queue and (delete or not enabled):
                    queue['excluded_ids'] = sorted(set(queue.get('excluded_ids', ())) | {-identifier})
                if config['enabled']:
                    config['next_check'] = self.current_moment().timestamp()
                self.write_config(db, config)
            return result.rowcount > 0

    def cube_rebalances(self, cube_id=None, page=1, size=20):
        condition, params = ('r.cube_id=?', (cube_id,)) if cube_id is not None else ('1=1', ())
        with self.db() as db:
            total = db.execute(f'SELECT count(*) FROM cube_rebalances r WHERE {condition}', params).fetchone()[0]
            rows = db.execute(
                f"SELECT r.*,COALESCE(NULLIF(c.cube_name,''),c.name) AS cube_name,c.name AS cube_alias,"
                f'c.owner_name,c.owner_id,c.symbol AS cube_symbol FROM cube_rebalances r '
                f'JOIN cubes c ON c.id=r.cube_id WHERE {condition} '
                'ORDER BY r.occurred_at DESC,r.id DESC LIMIT ? OFFSET ?',
                (*params, size, (page - 1) * size)).fetchall()
        return {'total': total, 'page': page,
                'items': [dict(row, payload=json.loads(row['payload'])) for row in rows]}

    def active_cube(self, db, cube):
        row = db.execute('SELECT * FROM cubes WHERE id=? AND symbol=? AND enabled=1',
                         (cube['id'], cube['symbol'])).fetchone()
        if row and cube.get('_queue_id'):
            config = self.config(db)
            queue = config.get('platform_queues', {}).get('xueqiu')
            if (not config['enabled'] or not queue or queue['id'] != cube['_queue_id']
                    or -cube['id'] in queue['excluded_ids']):
                return None
        return row

    def update_cube_metadata(self, cube, metadata):
        moment = self.current_moment()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = self.active_cube(db, cube)
            if not active or not cube_monitoring_time(moment):
                raise MonitoringDeferred('组合已暂停、删除或不在监控时段')
            name = plain_text(metadata.get('cube_name'))[:100] or active['cube_name']
            owner_id = str(metadata.get('owner_id') or '')
            owner_id = owner_id if owner_id.isascii() and owner_id.isdigit() and len(owner_id) <= 20 else ''
            owner_name = plain_text(metadata.get('owner_name'))[:100]
            if not owner_name and (not owner_id or owner_id == active['owner_id']):
                owner_name = active['owner_name']
            db.execute('UPDATE cubes SET cube_name=?,owner_name=?,owner_id=?,metadata_checked_at=? WHERE id=?',
                       (name, owner_name, owner_id or active['owner_id'],
                        moment.isoformat(timespec='seconds'), cube['id']))

    def collect_cube(self, cube, events, initial=False):
        moment = self.current_moment()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = self.active_cube(db, cube)
            if not active or not cube_monitoring_time(moment):
                raise MonitoringDeferred('组合已暂停、删除或不在监控时段')
            config = self.config(db)
            tracking = max(filter(None, (event_time(active['added_at']),
                                         event_time(config.get('enabled_since')))), default=moment)
            baseline = event_time(active['baseline_at']) or event_time(active['scan_started_at']) or moment
            max_id = active['baseline_max_id']
            for event in events:
                identifier = str(event['id'])
                previous = db.execute('SELECT * FROM cube_rebalances WHERE cube_id=? AND rebalance_id=?',
                                      (cube['id'], identifier)).fetchone()
                happened = event_time(event.get('updated_at')) or event_time(event.get('time'))
                eligible = (event.get('status') == 'success'
                            and event.get('category') == 'user_rebalancing' and bool(event.get('changes')))
                if previous and previous['notify_status'] in ('pending', 'sent'):
                    notification = previous['notify_status']
                elif eligible:
                    transitioned = previous is not None and previous['status'] != 'success'
                    fresh = ((tracking <= happened <= moment) if initial and happened else
                             (baseline < happened <= moment) if happened else
                             not initial and int(identifier) > active['baseline_max_id'])
                    notification = 'pending' if transitioned or (previous is None and fresh) else 'baseline'
                else:
                    notification = 'waiting' if event.get('status') in ('pending', 'waiting', 'processing') else 'baseline'
                payload = (previous['payload'] if previous and previous['notify_status'] in ('pending', 'sent')
                           and notification == previous['notify_status'] else json.dumps(event, ensure_ascii=False))
                db.execute('INSERT INTO cube_rebalances(cube_id,rebalance_id,status,payload,occurred_at,discovered_at,notify_status) '
                           'VALUES(?,?,?,?,?,?,?) ON CONFLICT(cube_id,rebalance_id) DO UPDATE SET '
                           'status=excluded.status,payload=excluded.payload,occurred_at=excluded.occurred_at,'
                           'notify_status=excluded.notify_status',
                           (cube['id'], identifier, event.get('status', 'unknown'), payload,
                            event.get('time') or event.get('updated_at') or '', moment.isoformat(timespec='seconds'), notification))
                max_id = max(max_id, int(identifier))
            if initial:
                db.execute('UPDATE cubes SET baseline_max_id=?,baseline_at=COALESCE(baseline_at,?) WHERE id=?',
                           (max_id, baseline.isoformat(timespec='seconds'), cube['id']))

    def check_cube(self, cube, config, fetcher):
        started = self.current_moment().isoformat(timespec='seconds')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.active_cube(db, cube):
                raise MonitoringDeferred('组合已暂停或删除')
            db.execute('UPDATE cubes SET scan_started_at=COALESCE(scan_started_at,?) WHERE id=?', (started, cube['id']))
            known = {row[0] for row in db.execute('SELECT rebalance_id FROM cube_rebalances WHERE cube_id=?', (cube['id'],))}
            pending = {row[0] for row in db.execute("SELECT rebalance_id FROM cube_rebalances WHERE cube_id=? "
                                                  "AND status IN ('pending','waiting','processing')", (cube['id'],))}

        def should_stop():
            if self.stop_event.is_set() or not cube_monitoring_time(self.current_moment()):
                return True
            if getattr(self, 'browser_login', None) and self.browser_login.active('xueqiu'):
                return True
            with self.db() as db:
                current = self.config(db)
                return not self.active_cube(db, cube) or current.get('platforms', {}).get('xueqiu', {}).get('paused', False)

        def save_page(events, next_page, complete):
            self.collect_cube(cube, events, initial=not cube['initialized'])
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if not self.active_cube(db, cube) or not cube_monitoring_time(self.current_moment()):
                    raise MonitoringDeferred('组合已暂停、删除或不在监控时段')
                db.execute('UPDATE cubes SET scan_page=? WHERE id=?', (next_page, cube['id']))

        tracking = max(filter(None, (event_time(cube['added_at']),
                                     event_time(config.get('enabled_since')))), default=event_time(started))
        checked = event_time(cube.get('metadata_checked_at'))
        metadata_ttl = timedelta(days=1) if cube.get('cube_name') and cube.get('owner_name') else timedelta(hours=1)
        source = dict(cube, cookie=self.decode(config['xueqiu_cookie']), _initial=not cube['initialized'],
                      _notify_since=tracking.isoformat(timespec='seconds') if not cube['initialized'] else None,
                      _known_ids=known, _pending_ids=pending, _scan_page=cube['scan_page'],
                      _should_stop=should_stop, _wait=self.stop_event.wait, _on_page=save_page,
                      _metadata_lookup_due=not checked or checked <= self.current_moment() - metadata_ttl,
                      _on_metadata=lambda metadata: self.update_cube_metadata(cube, metadata))
        result = fetcher(source, pages=20)
        self.collect_cube(cube, result, initial=not cube['initialized'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.active_cube(db, cube) or not cube_monitoring_time(self.current_moment()):
                raise MonitoringDeferred('组合已暂停、删除或不在监控时段')
            db.execute('UPDATE cubes SET last_check=?,last_error=NULL WHERE id=?',
                       (self.current_moment().isoformat(timespec='seconds'), cube['id']))
            if getattr(result, 'complete', True):
                db.execute('UPDATE cubes SET initialized=1,last_success_at=?,scan_page=1,scan_started_at=NULL WHERE id=?',
                           (started, cube['id']))

    def deliver_cubes(self, config, identifier=None):
        if not cube_monitoring_time(self.current_moment()):
            return
        with self.db() as db:
            rows = db.execute("SELECT r.*,c.symbol,c.name,c.cube_name,c.owner_name,c.owner_id "
                              "FROM cube_rebalances r JOIN cubes c ON c.id=r.cube_id "
                              "WHERE r.notify_status='pending' AND c.enabled=1 AND c.scan_started_at IS NULL "
                              "AND (? IS NULL OR r.id=?) ORDER BY "
                              "COALESCE(julianday(r.occurred_at),julianday(r.discovered_at),0),r.id LIMIT 100",
                              (identifier, identifier)).fetchall()
        for row in rows:
            event = json.loads(row['payload'])
            message_cube = event.get('_notification_cube')
            if not isinstance(message_cube, dict):
                # Metadata may refresh before a multipart retry. Keep its original
                # heading so saved part numbers still point to the same text.
                message_cube = {key: row[key] for key in ('symbol', 'name', 'cube_name', 'owner_name')}
                event['_notification_cube'] = message_cube
                with self.db() as db:
                    db.execute("UPDATE cube_rebalances SET payload=? WHERE id=? AND notify_status='pending'",
                               (json.dumps(event, ensure_ascii=False), row['id']))
            parts = cube_message_parts(message_cube, event)
            for index in range(row['next_part'], len(parts)):
                if self.stop_event.is_set() or not cube_monitoring_time(self.current_moment()):
                    return
                with self.db() as db:
                    current = self.config(db)
                    active = db.execute('SELECT 1 FROM cube_rebalances r JOIN cubes c ON c.id=r.cube_id '
                                        "WHERE r.id=? AND c.symbol=? AND c.enabled=1 AND r.notify_status='pending'",
                                        (row['id'], row['symbol'])).fetchone()
                if not current['enabled'] or current.get('platforms', {}).get('xueqiu', {}).get('paused'):
                    return
                if not active:
                    break
                try:
                    self.send_part(self.decode(current['token']), self.decode(current['chat_id']), parts[index])
                except RuntimeError as error:
                    with self.db() as db:
                        db.execute('UPDATE cube_rebalances SET last_error=? WHERE id=?', (str(error), row['id']))
                    raise
                with self.db() as db:
                    db.execute('UPDATE cube_rebalances SET next_part=?,notify_status=?,sent_at=?,last_error=NULL WHERE id=?',
                               (index + 1, 'sent' if index == len(parts) - 1 else 'pending',
                                self.current_moment().isoformat(timespec='seconds') if index == len(parts) - 1 else None, row['id']))
                self.stop_event.wait(1.1)
