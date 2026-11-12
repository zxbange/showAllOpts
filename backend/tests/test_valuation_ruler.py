"""Offline service regression. Every source value in this module is synthetic."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services import valuation_ruler as m
from app.services.valuation_ruler_model import (validate_day, ValuationError, ValuationConflict,
    PUBLIC_SOURCE, JQ_SOURCE, decimal_value)
from app.services.valuation_ruler_source import SourceError

MOMENT=datetime.fromisoformat('2026-11-10T20:01:00+08:00')
DAYS=[date(2010,1,4),date(2026,11,6),date(2026,11,9),date(2026,11,10),date(2026,11,11),date(2026,12,31)]


def members(value='10'):
    return {f'{i:06d}.XSHE':Decimal(value) for i in range(1,301)}


def daily(day,value='10',source=PUBLIC_SOURCE):
    signed=members(value)
    return validate_day(day,'4059.11',signed,source),signed,{'source':source}


@pytest.fixture
def monitor(tmp_path):
    service=m.ValuationRuler(Settings(_env_file=None,log_dir=tmp_path,valuation_ruler_worker_enabled=False),
        client_factory=lambda:nullcontext(None))
    yield service
    service.stop()


def provider(monitor,*,failures=None,on_day=None):
    calls=[]
    class Source:
        def __init__(self,*args):pass
        def calendar(self,cutoff):return DAYS
        def collect(self,day):
            calls.append(day)
            if on_day:on_day(day)
            if failures and day in failures:raise failures[day]
            return daily(day)
    monitor.source_factory=Source
    return calls


def seed(monitor,day,value='10',source=PUBLIC_SOURCE):
    with monitor.db() as db:monitor.publish_day(db,*daily(day,value,source))


def test_first_run_latest_only_gap_rows_and_complete_day_quantiles(monitor):
    calls=provider(monitor)
    assert monitor.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,10)]
    data=monitor.history(MOMENT)
    assert data['latest']['mean_pe_ttm']==10
    assert data['benchmarks']==dict(p20=10.,p80=10.,sample_days=1,start_date='2026-11-10',end_date='2026-11-10')
    assert data['coverage']['missing_count']==3
    assert data['points'][0]['mean_pe_ttm'] is None
    assert all(date.fromisoformat(p['date']).weekday()<5 for p in data['points'])
    calls.clear();monitor.refresh(background=False,moment=MOMENT)
    assert calls==[]


def test_empty_history_unknown_coverage_and_no_false_equivalence(monitor):
    data=monitor.history(MOMENT)
    assert data['points']==[] and data['latest'] is None
    assert data['benchmarks']['p20'] is None and data['benchmarks']['sample_days']==0
    assert data['coverage']['missing_count'] is None
    assert monitor.status()['comparison_status']=='not_compared'


def test_manual_15_cutoff_converts_server_utc_and_monday_preclose_targets_friday(monitor):
    calls=provider(monitor)
    monitor.refresh(background=False,moment=MOMENT.replace(day=9,hour=14))
    assert calls==[date(2026,11,6)]
    assert m.close_cutoff(datetime.fromisoformat('2026-11-10T06:59:00+00:00'))==date(2026,11,9)
    assert m.close_cutoff(datetime.fromisoformat('2026-11-10T07:00:00+00:00'))==date(2026,11,10)
    seed(monitor,date(2026,11,10),'20')
    assert monitor.history(MOMENT.replace(hour=14))['latest']['date']=='2026-11-06'


def test_all_post_last_dates_and_persisted_older_failure_recovered_after_restart(monitor):
    seed(monitor,date(2026,11,6))
    failures={date(2026,11,9):SourceError('incomplete',retryable=True)}
    calls=provider(monitor,failures=failures)
    monitor.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,9),date(2026,11,10)]+[date(2026,11,9)]*3
    assert monitor.status()['failed_days']==1
    assert monitor.history(MOMENT)['latest']['date']=='2026-11-10'
    replacement=m.ValuationRuler(monitor.settings,client_factory=lambda:nullcontext(None))
    calls=provider(replacement)
    replacement.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,9)] and replacement.status()['failed_days']==0
    replacement.stop()


def test_common_access_denial_stops_without_retry_and_plan_survives(monitor):
    seed(monitor,date(2026,11,6))
    calls=provider(monitor,failures={date(2026,11,9):SourceError('access_denied',common=True)})
    monitor.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,9)]
    assert monitor.status()['failed_days']==2 and monitor.status()['status']=='failed'
    assert monitor.history(MOMENT)['latest']['date']=='2026-11-06'


def test_old_date_without_snapshot_is_not_retried_with_current_members(monitor):
    seed(monitor,date(2026,11,6))
    calls=provider(monitor,failures={date(2026,11,9):SourceError('snapshot_unavailable')})
    monitor.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,9),date(2026,11,10)]
    assert monitor.status()['recent_failures'][0]['code']=='snapshot_unavailable'


def test_cancel_preserves_committed_day_and_resumes_remaining_plan(monitor):
    seed(monitor,date(2026,11,6))
    def hook(day):
        if day==date(2026,11,10):assert monitor.cancel()
    provider(monitor,on_day=hook)
    monitor.refresh(background=False,moment=MOMENT)
    assert monitor.status()['phase']=='cancelled'
    assert monitor.history(MOMENT)['latest']['date']=='2026-11-09'
    calls=provider(monitor);monitor.refresh(background=False,moment=MOMENT)
    assert calls==[date(2026,11,10)] and not monitor.cancel()


def test_os_lock_blocks_another_instance_and_stale_run_cannot_overwrite(monitor):
    replacement=m.ValuationRuler(monitor.settings,client_factory=lambda:nullcontext(None))
    lock=monitor.acquire()
    try:
        assert not replacement.refresh(background=False,moment=MOMENT)
        assert replacement.running()
    finally:lock.release()
    provider(monitor);monitor.refresh(background=False,moment=MOMENT)
    state=monitor.read_state();state['run_id']='0'*32
    with pytest.raises(ValuationError):monitor.write_state(state)
    replacement.stop()


def test_20_slots_persist_across_restart_and_new_day_runs_once(monitor):
    monitor.settings.valuation_ruler_worker_enabled = True
    calls=provider(monitor)
    monitor.tick(MOMENT.replace(hour=19),background=False)
    assert calls==[date(2026,11,9)]
    monitor.tick(MOMENT,background=False)
    assert calls[-1]==date(2026,11,10)
    replacement=m.ValuationRuler(monitor.settings,client_factory=lambda:nullcontext(None));later=provider(replacement)
    assert not replacement.tick(MOMENT+timedelta(minutes=2),background=False)
    assert later==[]
    replacement.tick(MOMENT.replace(day=11),background=False)
    assert later==[date(2026,11,11)]
    replacement.stop()


def test_interrupted_slot_resumes_but_failed_finished_slot_does_not_loop(monitor):
    monitor.settings.valuation_ruler_worker_enabled = True
    slot=m.slot_for(date(2026,11,10))
    with monitor.db() as db:db.execute('INSERT INTO slots VALUES (?,?)',(slot,'running'))
    calls=provider(monitor,failures={date(2026,11,10):SourceError('access_denied',common=True)})
    assert monitor.tick(MOMENT,background=False)
    assert not monitor.tick(MOMENT,background=False)
    assert len(calls)==1


def test_exception_diagnostics_do_not_echo_raw_urls_or_credentials(monitor,caplog):
    class Source:
        def __init__(self,*args):pass
        def calendar(self,*args):raise RuntimeError('https://secret.example?token=PASSWORD')
    monitor.source_factory=Source
    monitor.refresh(background=False,moment=MOMENT)
    assert 'PASSWORD' not in caplog.text+json.dumps(monitor.status())
    assert 'RuntimeError' in caplog.text and 'source=valuation_ruler' in caplog.text


def test_cross_source_overlap_preserves_original_and_full_candidate_audit(monitor):
    day=date(2026,11,10);seed(monitor,day,'10',JQ_SOURCE)
    calls=provider(monitor);monitor.refresh(background=False,moment=MOMENT)
    assert calls==[day] and monitor.status()['comparison']['match']
    signed=members('12');signed.pop('000001.XSHE');signed['000999.XSHE']=Decimal('12')
    candidate=validate_day(day,'4060',signed,PUBLIC_SOURCE)
    with monitor.db() as db:
        with pytest.raises(ValuationConflict):monitor.publish_day(db,candidate,signed,{'source':PUBLIC_SOURCE})
    data=monitor.history(MOMENT)
    assert data['latest']['mean_pe_ttm']==10 and data['latest']['source']==JQ_SOURCE
    audit=monitor.status()['comparison']
    assert audit['added_members']==['000999.XSHE'] and audit['removed_members']==['000001.XSHE']
    assert audit['changed_member_count']==301
    assert Decimal(audit['mean_pe_delta'])==2 and Decimal(audit['index_close_delta'])==Decimal('.89')
    assert 'candidate_members' not in audit
    with monitor.db() as db:
        saved=json.loads(db.execute('SELECT payload FROM comparisons ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert len(saved['candidate_members'])==300 and len(saved['changed_members'])==301


def test_complete_jq_day_unavailable_comparison_is_not_missing_data(monitor):
    day=date(2026,11,10);seed(monitor,day,'10',JQ_SOURCE)
    provider(monitor,failures={day:SourceError('snapshot_unavailable')})
    monitor.refresh(background=False,moment=MOMENT)
    assert monitor.status()['failed_days']==0
    assert monitor.status()['comparison_error'] and monitor.status()['comparison_status']=='not_compared'
    calls=provider(monitor);monitor.refresh(background=False,moment=MOMENT.replace(day=11))
    assert calls==[date(2026,11,11)]
    assert monitor.status()['failed_days']==0 and monitor.status()['comparison_error'] is None


def test_all_history_linear_quantiles_not_viewport_or_crosssection(monitor):
    for day,value in zip(DAYS[:4],['10','20','40','100']):seed(monitor,day,value)
    data=monitor.history(MOMENT)
    assert data['benchmarks']['p20']==16 and data['benchmarks']['p80']==64
    assert data['benchmarks']['sample_days']==4 and data['coverage']['missing_count'] is None
    assert m.quantile([],.2) is None and m.quantile(['1.000000000001'],.8)==1.000000000001
    assert m.exact_delta('999999999999999999.999999999999','999999999999999999.999999999998')==Decimal('1e-12')


@pytest.mark.parametrize('value',[None,0,True,'NaN','Infinity','-Infinity','0.0000000000001'])
def test_invalid_pe_is_not_a_loss_exclusion(value):
    signed=members();signed['000001.XSHE']=value
    with pytest.raises(ValuationError):validate_day(date(2026,11,10),'4059.11',signed,PUBLIC_SOURCE)


def test_decimal_precision_positive_only_and_boundary_no_implicit_rounding():
    signed=members('1');signed['000001.XSHE']=Decimal('-10');signed['000002.XSHE']=Decimal('1.000000000001')
    result=validate_day(date(2026,11,10),'4059.11000000',signed,PUBLIC_SOURCE)
    assert result['positive_count']==299 and result['loss_count']==1
    assert result['pe_sum']=='299.000000000001' and result['mean_pe_ttm']=='1.000000000000'
    assert decimal_value('999999999999999999.999999999999',30,12)==Decimal('999999999999999999.999999999999')
    with pytest.raises(ValuationError):decimal_value('1.000000000',24,8)


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(positive_count=299),lambda p:p.update(members_sha256='0'*64),
    lambda p:p.update(mean_pe_ttm='10.000000000001'),lambda p:p.update(pe_sum='1'),
    lambda p:p.update(method_version='wrong'),lambda p:p.update(index_code='000905.XSHG'),
])
def test_daily_aggregate_must_recompute_from_all_members(mutation):
    day=date(2026,11,10);point,signed,_=daily(day);mutation(point)
    with pytest.raises(ValuationError):validate_day(day,point['index_close'],signed,PUBLIC_SOURCE,expected=point)


def test_stopped_worker_and_start_failure_release_lock(monitor,monkeypatch):
    monitor.stop_event.set();assert not monitor.refresh()
    monitor.stop_event.clear()
    monkeypatch.setattr(threading.Thread,'start',lambda *args:(_ for _ in ()).throw(RuntimeError('thread failed')))
    with pytest.raises(RuntimeError):monitor.refresh()
    assert not monitor.running() and monitor.status()['status']=='failed'


def test_synthetic_snapshot_evidence_retained_across_restart_and_metadata_change(monitor,monkeypatch):
    # Deliberately synthetic bytes for the storage contract; not a claimed CSI workbook.
    raw=b'synthetic-storage-unit-test';day=date(2026,11,10);roster=sorted(members())
    proof=dict(source='CSI.OFFICIAL.CONS.V1',internal_date=str(day),workbook_sha256=hashlib.sha256(raw).hexdigest(),
        members_sha256=hashlib.sha256('\n'.join(roster).encode()).hexdigest())
    monkeypatch.setattr(m,'now',lambda:MOMENT)
    monitor.save_snapshot(day,roster,proof,raw=raw)
    replacement=m.ValuationRuler(monitor.settings)
    assert replacement.load_snapshot(day)==(roster,proof) and replacement.load_snapshot(day-timedelta(days=1)) is None
    changed_bytes=raw+b'metadata'
    replacement.save_snapshot(day,roster,dict(proof,workbook_sha256=hashlib.sha256(changed_bytes).hexdigest()),raw=changed_bytes)
    assert replacement.load_snapshot(day)==(roster,proof)
    changed=sorted(roster[1:]+['000999.XSHE'])
    with pytest.raises(ValuationConflict):replacement.save_snapshot(day,changed,dict(proof,
        members_sha256=hashlib.sha256('\n'.join(changed).encode()).hexdigest()),raw=raw)
    assert replacement.load_snapshot(day)==(roster,proof)
    replacement.stop()


def test_calendar_regression_preserves_history_and_visible_gaps(monitor):
    provider(monitor);monitor.refresh(background=False,moment=MOMENT);before=monitor.history(MOMENT)
    class Source:
        def __init__(self,*args):pass
        def calendar(self,cutoff):return DAYS[1:]
    monitor.source_factory=Source;monitor.refresh(background=False,moment=MOMENT)
    assert monitor.history(MOMENT)==before and monitor.status()['status']=='failed'


def test_production_scheduler_flag_start_stop_without_network(monitor,monkeypatch):
    assert monitor.tick(MOMENT, background=False) is None
    ticked=threading.Event()
    monkeypatch.setattr(monitor,'tick',lambda:ticked.set())
    monitor.start();assert ticked.wait(1)
    original=monitor.scheduler;monitor.start();assert monitor.scheduler is original
    monitor.stop();assert not monitor.scheduler.is_alive() and not monitor.refresh()


def test_manual15_vs_schedule1959_and20(monitor):
    monitor.settings.valuation_ruler_worker_enabled = True
    calls=provider(monitor)
    monitor.tick(MOMENT.replace(hour=19,minute=59),background=False)
    assert calls==[date(2026,11,9)]
    calls.clear();monitor.refresh(background=False,moment=MOMENT.replace(hour=15,minute=0))
    assert calls==[date(2026,11,10)]
    assert monitor.history(MOMENT.replace(hour=15,minute=0))['latest']['date']=='2026-11-10'
    with monitor.db() as db:assert db.execute('SELECT 1 FROM slots WHERE slot=?',(m.slot_for(date(2026,11,10)),)).fetchone() is None
    monitor.tick(MOMENT.replace(hour=20,minute=0),background=False)
    with monitor.db() as db:assert db.execute('SELECT outcome FROM slots WHERE slot=?',(m.slot_for(date(2026,11,10)),)).fetchone()[0]=='complete'


@pytest.mark.parametrize('candidate_value',['10','12'])
def test_complete_automatic_overlap_deduplicated_over_weekend_and_restart(monitor,candidate_value):
    monitor.settings.valuation_ruler_worker_enabled = True
    friday=date(2026,11,6);seed(monitor,friday,'10',JQ_SOURCE);calls=[]
    class Source:
        def __init__(self,*args):pass
        def calendar(self,cutoff):return DAYS
        def collect(self,day):calls.append(day);return daily(day,candidate_value)
    monitor.source_factory=Source
    monitor.tick(MOMENT.replace(day=6,hour=20),background=False)
    assert calls==[friday] and monitor.status()['failed_days']==0
    assert monitor.status()['comparison']['match']==(candidate_value=='10')
    replacement=m.ValuationRuler(monitor.settings,client_factory=lambda:nullcontext(None),source_factory=Source)
    try:
        for day in (7,8):replacement.tick(MOMENT.replace(day=day,hour=20),background=False)
        assert calls==[friday] and replacement.status()['failed_days']==0
        with replacement.db() as db:assert db.execute('SELECT count(*) FROM comparisons').fetchone()[0]==1
        replacement.refresh(background=False,moment=MOMENT.replace(day=8,hour=20))
        assert calls==[friday,friday]
        with replacement.db() as db:assert db.execute('SELECT count(*) FROM comparisons').fetchone()[0]==2
    finally:replacement.stop()


def test_unavailable_overlap_retry_next_slot_no_false_gap(monitor):
    monitor.settings.valuation_ruler_worker_enabled = True
    friday=date(2026,11,6);seed(monitor,friday,'10',JQ_SOURCE)
    failures={friday:SourceError('snapshot_unavailable')};calls=provider(monitor,failures=failures)
    monitor.tick(MOMENT.replace(day=6,hour=20),background=False)
    assert monitor.status()['failed_days']==0 and monitor.status()['comparison_status']=='not_compared'
    failures.clear();monitor.tick(MOMENT.replace(day=7,hour=20),background=False)
    assert calls==[friday,friday] and monitor.status()['comparison']['match']
    assert not monitor.tick(MOMENT.replace(day=7,hour=20,minute=30),background=False)


def test_admin_api_contract_and_no_store(engine,tmp_path,monkeypatch):
    app=create_app(Settings(_env_file=None,log_dir=tmp_path,valuation_ruler_worker_enabled=False),engine)
    with TestClient(app) as client:
        service=app.state.valuation_ruler
        monkeypatch.setattr(service,'refresh',lambda:True);monkeypatch.setattr(service,'cancel',lambda:False)
        for name in ('history','status'):
            response=client.get('/api/v1/valuation-ruler/'+name)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
        for name,key,value in [('refresh','started',True),('cancel','cancelled',False)]:
            response=client.post('/api/v1/valuation-ruler/'+name)
            assert response.status_code==202 and response.json()=={key:value}
        assert client.post('/api/v1/valuation-ruler/sync-joinquant').status_code == 404
        assert any(item['id']=='valuation-ruler' for item in client.get('/api/v1/modules').json()['modules'])


def test_polling_status_quiet_but_errors_logged():
    import logging
    from app.core.logging import PollingAccessFilter
    def record(status):return logging.LogRecord('uvicorn.access',logging.INFO,'',1,'%s - %s %s HTTP/%s %s',
        ('127.0.0.1','GET','/api/v1/valuation-ruler/status','1.1',status),None)
    assert not PollingAccessFilter().filter(record(200))
    assert PollingAccessFilter().filter(record(500))
