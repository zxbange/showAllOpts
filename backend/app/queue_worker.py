"""Opt-in standalone ETF/market executor; existing API owns all other work."""

import logging
import signal
import threading
import time
from typing import Any

from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.core.database import VersionedDatabase
from app.core.logging import configure_logging
from app.services.collection_notifications import stop_notifications
from app.services.etf_collection import ETFCollection
from app.services.market_collection import MarketCollection
from app.services.queue_runtime import GROUPS, HEARTBEAT_SECONDS, QueueRuntime, TickGate
from app.services.system_runtime import start_runtime, stop_runtime

LOG = logging.getLogger(__name__)
DRAIN_SECONDS = 20.0
STOP_SECONDS = 10.0


def alive_groups(services: dict[str, Any]) -> set[str]:
    return {
        group
        for group, worker in services.items()
        if worker.worker is not None and worker.worker.is_alive()
    }


def _best_effort_heartbeat(runtime: QueueRuntime, state: str, groups: set[str]) -> None:
    try:
        runtime.publish(state, groups)
    except OSError as error:
        LOG.warning(
            'Queue heartbeat unavailable state=%s error_type=%s', state, type(error).__name__
        )


def drain_workers(
    runtime: QueueRuntime,
    gate: TickGate,
    services: dict[str, Any],
    *,
    drain_seconds: float = DRAIN_SECONDS,
    stop_seconds: float = STOP_SECONDS,
) -> bool:
    # New ticks cannot acquire admission once this closes. Already admitted
    # work keeps its old semantics until it finishes or the deadline expires.
    gate.close()
    _best_effort_heartbeat(runtime, 'draining', alive_groups(services))
    deadline = time.monotonic() + max(0.0, drain_seconds)
    drained = gate.wait_drained(0)
    while not drained and time.monotonic() < deadline:
        drained = gate.wait_drained(min(HEARTBEAT_SECONDS, max(0.0, deadline - time.monotonic())))
        _best_effort_heartbeat(runtime, 'draining', alive_groups(services))
    if not drained:
        LOG.warning('Queue drain deadline elapsed; requesting cooperative interruption')
    for service in services.values():
        service.stop_event.set()
        service.wake_event.set()
    deadline = time.monotonic() + max(0.0, stop_seconds)
    for service in services.values():
        while (
            service.worker is not None and service.worker.is_alive() and time.monotonic() < deadline
        ):
            service.worker.join(
                timeout=min(HEARTBEAT_SECONDS, max(0.0, deadline - time.monotonic()))
            )
            _best_effort_heartbeat(runtime, 'draining', alive_groups(services))
    surviving = alive_groups(services)
    if surviving:
        LOG.warning(
            'Queue stop deadline elapsed groups=%s; recovery remains required',
            ','.join(sorted(surviving)),
        )
    _best_effort_heartbeat(runtime, 'degraded' if surviving else 'stopped', surviving)
    return not surviving


def serve(settings: Settings, stop_requested: threading.Event) -> int:
    if settings.collection_runtime_role != 'queue-worker':
        raise RuntimeError('The dedicated worker requires the explicit queue-worker runtime role.')
    runtime = QueueRuntime(settings.log_path)
    lock = runtime.acquire_authority(exclusive=True)
    gate = TickGate()
    database = None
    services: dict[str, Any] = {}
    exit_code = 0
    try:
        runtime.publish('starting', set())
        database = VersionedDatabase(settings, for_cache=settings.cache_enabled)
        cache = SnapshotCache(settings, database)
        etf = ETFCollection(
            settings,
            refresh=cache.refresh_etf_collection,
            tick_gate=gate,
            cache_publisher=cache.publish_etf_collection,
        )
        market = MarketCollection(
            settings, refresh=cache.refresh_market_collection, cache=cache, tick_gate=gate
        )
        market.shared_configuration = etf.configuration
        services = {'etf': etf, 'market': market}
        revision = runtime.wake_revision()
        start_runtime(settings)
        if not stop_requested.is_set():
            for service in services.values():
                if stop_requested.is_set():
                    break
                service.start()
            while not stop_requested.is_set():
                alive = alive_groups(services)
                if alive != GROUPS:
                    runtime.publish('degraded', alive)
                    LOG.error(
                        'Dedicated queue scheduler terminated; stopping for normal process supervision'
                    )
                    exit_code = 1
                    break
                runtime.publish('ready', alive)
                latest = runtime.wake_revision()
                if latest != revision:
                    revision = latest
                    for service in services.values():
                        service.wake_event.set()
                stop_requested.wait(HEARTBEAT_SECONDS)
    except Exception as error:
        LOG.error('Dedicated queue worker failed error_type=%s', type(error).__name__)
        exit_code = 1
    finally:
        try:
            stopped = drain_workers(runtime, gate, services)
            if not stopped:
                exit_code = 1
            # Never dispose a pool concurrently with a surviving business
            # thread. Process exit releases it; ledger recovery remains the
            # authority for any interrupted or ambiguous transaction.
            if stopped and database is not None:
                database.dispose()
        finally:
            # Existing runtime/notification watchers remain supplemental;
            # the API also retains its shared durable outbox consumer.
            try:
                for stop in (stop_runtime, stop_notifications):
                    try:
                        stop(settings)
                    except Exception as error:
                        exit_code = 1
                        LOG.warning(
                            'Queue supplemental watcher cleanup failed error_type=%s',
                            type(error).__name__,
                        )
            finally:
                runtime.release_after_workers(
                    lock, [service.worker for service in services.values()]
                )
    return exit_code


def main() -> None:
    settings = Settings()
    configure_logging(settings)
    stop_requested = threading.Event()

    def request_stop(_signum, _frame) -> None:
        # The main supervisor performs the actual deadline-bound shutdown.
        stop_requested.set()

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            result = serve(settings, stop_requested)
        except Exception as error:
            LOG.error('Dedicated queue process refused startup error_type=%s', type(error).__name__)
            result = 1
        raise SystemExit(result)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        logging.shutdown()


if __name__ == '__main__':
    main()
