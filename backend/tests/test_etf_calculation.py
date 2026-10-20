from datetime import date, timedelta, datetime
from types import SimpleNamespace
import numpy as np
import pytest
from app.services.etf_calculation import metrics, calculate_rows, rebuild_snapshot
from app.models.etf_heat import Bar, Heat, Pool

@pytest.mark.parametrize('direction',[1,-1,0])
def test_formula_matches_original_weighted_numpy(direction):
    p=np.exp(np.linspace(0,.1*direction,26))*(1+.003*np.sin(np.arange(26)))
    y=np.log(p);x=np.arange(26);w=np.linspace(1,2,26)
    slope,intercept=np.polyfit(x,y,1,w=w)
    expected=np.expm1(slope*250)*(1-np.sum(w*(y-slope*x-intercept)**2)/np.sum(w*(y-y.mean())**2))
    assert float(metrics(list(p))['score'])==pytest.approx(expected,abs=1e-9)
    assert metrics(list(p))['score']==metrics(list(p*100))['score']


def test_invalid_prices_paused_flat_and_no_filtering():
    assert metrics([1]*26)['score']==0
    assert metrics([1]*25)['score'] is None
    assert metrics([1]*25+[None])['score'] is None
    assert metrics([1]*26,True)['score'] is None
    assert metrics([1]*25+[.9])['score'] < 0


def bars(code='A',start=date(2026,1,1)):
    return [SimpleNamespace(code=code,name=code,trade_date=start+timedelta(days=i),close=1+i*.01,
        adjusted_close=1+i*.01,paused=0,observation_start=start+timedelta(days=25),updated_at=datetime(2026,2,1)) for i in range(28)]


def test_warmup_future_and_missing_calendar():
    data=bars();rows=calculate_rows(data)
    assert len(rows)==3 and rows[0]['score'] is not None
    data[-1].adjusted_close=999
    assert calculate_rows(data)[0]==rows[0]
    result=calculate_rows(data[:5]+data[6:]+bars('B'))
    assert all(r['score'] is None for r in result if r['code']=='A')


def test_snapshot_calculates_and_rejects_legacy_pool(engine,session):
    session.add_all(Bar(**vars(row)) for row in bars())
    head=Pool(id=1,as_of_date=date(2026,1,28),rule_version='etf_raw_market_v1',members=[{'code':'A'}],updated_at=datetime(2026,2,1))
    session.add(head);session.commit()
    with engine.begin() as c: assert rebuild_snapshot(c)==3
    assert session.query(Heat).count()==3
    head.rule_version='etf_close_momentum_v1';session.commit()
    with engine.begin() as c:
        with pytest.raises(ValueError,match='新版'): rebuild_snapshot(c)
    assert session.query(Heat).count()==3


def test_direct_mode_uses_raw_bars(client,session):
    session.add_all(Bar(**vars(row)) for row in bars())
    member=dict(code='A',name='A',category='国内LOF',fund_type='LOF',target_name='A',size_yi='40',size_date='2026-01-28',size_source='report')
    session.add(Pool(id=1,as_of_date=date(2026,1,28),rule_version='etf_raw_market_v1',members=[member],updated_at=datetime(2026,2,1)))
    session.commit()
    result=client.get('/api/v1/etf-momentum/latest')
    assert result.status_code==200
    assert result.json()['items'][0]['score']>0
    assert session.query(Heat).count()==0
