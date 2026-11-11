"""Durable source-version upgrades preserve published metrics, offline only."""
import json
import pytest
from app.services.market_diagnostics import source_failure_context, source_failure_details
from app.services.market_sina import SinaDataError
from test_market_collection import DAY, Provider, historical_provider, worker


def public_provider(provider, version):
    provider.source_id='public_market'
    historical_provider(provider)
    original=provider.fetch_universe
    provider.fetch_universe=lambda day: original(day)|{'source':'public_market','evidence_version':version}


@pytest.mark.parametrize('compatibility',[False,None,1])
def test_same_source_obsolete_evidence_refetch_archives_without_rewriting_published_crowding(tmp_path,compatibility):
    collection,old,writer,clock=worker(tmp_path)
    public_provider(old,0)
    old.failures={'sh600003'}
    assert not collection.tick()
    oldrow=collection._day(DAY)
    oldsnapshot=oldrow['snapshot_json']; oldhash=oldrow['crowding_hash']
    assert oldrow['crowding_status']=='written' and oldhash
    writer.calls.clear()
    new=Provider(clock);public_provider(new,1)
    new.snapshot_compatible=lambda snapshot: True if snapshot.get('evidence_version')==1 else compatibility
    collection.provider_factory=lambda client:new
    collection.request_run('refresh-evidence')
    assert collection.tick()
    assert new.universe_calls==[DAY] and len(new.quote_calls)==120
    assert collection._day(DAY)['crowding_hash']==oldhash
    assert all(call[2] is None for call in writer.calls)
    assert json.loads(collection._day(DAY)['snapshot_json'])['evidence_version']==1
    with collection.connect() as db:
        snapshots=[row[0] for row in db.execute("SELECT payload FROM methodology_archive WHERE kind='snapshot'")]
        assert oldsnapshot in snapshots
        assert db.execute("SELECT COUNT(*) FROM methodology_archive WHERE kind='quote'").fetchone()[0]>0


@pytest.mark.parametrize('code',['dated_membership_window_unverified','dated_membership_gap','dated_report_invalid','dated_report_cache_invalid'])
def test_dated_heat_error_remains_visible_and_crowding_published(tmp_path,code):
    collection,provider,writer,clock=worker(tmp_path);public_provider(provider,1)
    provider.quote_override=lambda quote:quote|{'status':'transient_failure','error':code}
    assert not collection.tick()
    row=collection._day(DAY)
    assert row['error']==code and row['crowding_status']=='written'
    assert len([call for call in writer.calls if call[2] is not None])==1
    assert not any(call[1] for call in writer.calls)


@pytest.mark.parametrize('source',['eastmoney_dated_universe','eastmoney_listing_metadata'])
@pytest.mark.parametrize('stage',['cache_read','cache_write','window_validation'])
def test_new_diagnostics_only_emit_allowlisted_facts(source,stage):
    with pytest.raises(SinaDataError) as caught:
        with source_failure_context(source_id=source,stage=stage):
            raise SinaDataError('https://private.invalid/?secret=never-log',code='dated_report_cache_invalid')
    details=source_failure_details(caught.value)
    assert source in details and stage in details and 'never-log' not in details and 'http' not in details.split('http_status')[0]
