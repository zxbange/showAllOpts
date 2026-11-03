import pytest
from app.core.config import Settings
from app.services.commodities import Commodities
from app.services.commodity_source import parse,QuoteError,safe_url
URL='https://www.ccmn.cn/news/ZX003/202610/47eabce357dd492885c36c56f1ba8f14.html'
def body(q='10月9日长江现货A00铝均价23660元/吨，跌100元/吨，价格区间23640-23680元/吨'):
 return f'<p>2026-10-12</p><h2>五、长江市场行情</h2>{q}<h2>六、CCMN今日价格预测</h2>预计今日24000-25000元/吨'
def test_parse():
 r=parse(body(),URL)
 assert (r['quoteDate'],r['low'],r['high'],r['mean'],r['change'])==('2026-10-09',23640,23680,23660,-100)
 assert not {'open','close','volume'}&r.keys()
@pytest.mark.parametrize('old,new',[('长江现货','长江综合'),('10月9日',''),('10月9日','10月12日'),('23640-23680','24640-24680')])
def test_reject(old,new):
 with pytest.raises(QuoteError):parse(body().replace(old,new),URL)
def test_known_date():
 known={'quoteDate':'2026-10-09','publishedDate':'2026-10-12'}
 assert parse(body().replace('10月9日',''),URL,known)['quoteDate']=='2026-10-09'
 with pytest.raises(QuoteError):parse(body(),URL,dict(known,publishedDate='2026-10-13'))
@pytest.mark.parametrize('url',['http://www.ccmn.cn/','https://www.ccmn.cn@169.254.169.254/','https://evil.ccmn.cn/x','https://www.ccmn.cn:123/x','https://www.ccmn.cn/news/x?next=http://127.0.0.1'])
def test_scope(url):
 with pytest.raises(ValueError):safe_url(url)
def test_seed_and_lock(tmp_path):
 settings=Settings(_env_file=None,log_dir=tmp_path);service=Commodities(settings);data=service.history()
 assert len(data['rows'])==18 and data['status']['automatic']
 weak=[r for r in data['rows'] if r['low'] is None]
 assert len(weak)==1 and weak[0]['quoteDate']=='2026-10-16' and weak[0]['mean']==23460
 assert len(Commodities(settings).history()['rows'])==18
 lock=service.lock()
 try:assert not service.refresh()
 finally:lock.release()
def test_failed_fetch(tmp_path,monkeypatch):
 from app.services import commodities
 service=Commodities(Settings(_env_file=None,log_dir=tmp_path));old=service.history()['rows']
 def fail(*a,**k):raise RuntimeError('secret')
 monkeypatch.setattr(commodities,'fetch',fail);lock=service.lock();service.collect(lock)
 assert service.history()['rows']==old and 'secret' not in str(service.status())
 assert not service.status()['running'] and service.status()['failures']>0

def test_api_authentication_and_csrf(tmp_path):
 from fastapi import FastAPI,Depends
 from fastapi.testclient import TestClient
 from app.api.routes.commodities import router
 from app.api.routes.accounts import require_user
 from account_helpers import install_accounts,sign_in,PASSWORD
 app=FastAPI();settings=Settings(_env_file=None,log_dir=tmp_path,auth_allow_http=True)
 install_accounts(app,settings);app.state.commodities=Commodities(settings)
 app.include_router(router,prefix='/api/v1',dependencies=[Depends(require_user)])
 with TestClient(app) as client:
  assert client.get('/api/v1/commodities/a00').status_code==401
  app.state.accounts.create('viewer',PASSWORD,'viewer',app.state.accounts.list()[0]['id'])
  sign_in(client,'viewer')
  assert client.get('/api/v1/commodities/a00').status_code==200
  assert client.post('/api/v1/commodities/a00/refresh').status_code==403
  sign_in(client);client.headers['X-CSRF-Token']='bad'
  assert client.post('/api/v1/commodities/a00/refresh').status_code==403

def test_collector_upsert_preserves_other_dates(tmp_path,monkeypatch):
 from app.services import commodities
 service=Commodities(Settings(_env_file=None,log_dir=tmp_path));old=service.history()['rows']
 monkeypatch.setattr(commodities,'fetch',lambda *a,**k:body())
 monkeypatch.setattr(commodities,'discover',lambda *_:[])
 monkeypatch.setattr(service.stopping,'wait',lambda *_:False)
 service.collect(service.lock())
 assert len(service.history()['rows'])==len(old)
 assert next(r for r in service.history()['rows'] if r['quoteDate']=='2026-10-09')['low']==23640
 assert next(r for r in service.history()['rows'] if r['quoteDate']=='2026-10-16')['low'] is None

from datetime import datetime, timezone
from app.services.commodities import latest_slot

@pytest.mark.parametrize('instant,expected', [
 ('2026-11-07T02:59:59+00:00','2026-11-06T18:00:00+08:00'),
 ('2026-11-07T03:00:00+00:00','2026-11-07T11:00:00+08:00'),
 ('2026-11-07T09:59:59+00:00','2026-11-07T11:00:00+08:00'),
 ('2026-11-07T10:00:00+00:00','2026-11-07T18:00:00+08:00'),
 ('2027-01-01T00:00:00+00:00','2026-12-31T18:00:00+08:00'),
])
def test_schedule_shanghai_including_weekend(instant,expected):
 assert latest_slot(datetime.fromisoformat(instant)).isoformat()==expected

def scheduled_service(tmp_path,monkeypatch):
 from app.services import commodities
 monkeypatch.setattr(commodities,'now',lambda:datetime(2026,11,7,2,tzinfo=timezone.utc))
 return Commodities(Settings(_env_file=None,log_dir=tmp_path))

def test_slots_persist_after_failure_and_restart(tmp_path,monkeypatch):
 from app.services import commodities
 service=scheduled_service(tmp_path,monkeypatch);old=service.history()['rows']
 def fail(*a,**k):raise TimeoutError('private')
 monkeypatch.setattr(commodities,'fetch',fail)
 monkeypatch.setattr(service.stopping,'wait',lambda *_:False)
 slot=latest_slot(datetime(2026,11,7,3,tzinfo=timezone.utc))
 assert service.refresh(slot=slot)
 service.thread.join(5)
 assert not service.thread.is_alive()
 assert not service.refresh(slot=slot)
 restored=Commodities(Settings(_env_file=None,log_dir=tmp_path))
 assert not restored.refresh(slot=slot)
 assert restored.history()['rows']==old
 with restored.db() as db:assert db.execute('SELECT status FROM schedule_runs').fetchone()[0]=='failed'
 assert 'private' not in str(restored.status())
 monkeypatch.setattr(restored.stopping,'wait',lambda *_:False)
 assert restored.refresh(slot=slot.replace(hour=18))
 restored.thread.join(5)
 with restored.db() as db:assert db.execute('SELECT count(*) FROM schedule_runs').fetchone()[0]==2

def test_restart_marks_interrupted(tmp_path,monkeypatch):
 service=scheduled_service(tmp_path,monkeypatch)
 slot=latest_slot(datetime(2026,11,7,3,tzinfo=timezone.utc))
 with service.db() as db:db.execute('INSERT INTO schedule_runs(slot,status) VALUES(?,?)',(slot.isoformat(),'running'))
 restored=Commodities(Settings(_env_file=None,log_dir=tmp_path))
 assert not restored.refresh(slot=slot)
 with restored.db() as db:assert db.execute('SELECT status FROM schedule_runs').fetchone()[0]=='interrupted'
 assert '中断' in restored.status()['stage']

def test_manual_and_two_schedulers_share_lock(tmp_path,monkeypatch):
 import threading
 service=scheduled_service(tmp_path,monkeypatch)
 second=Commodities(Settings(_env_file=None,log_dir=tmp_path));gate=threading.Event()
 def hold(lock,slot=None):
  try:gate.wait(5)
  finally:lock.release()
 monkeypatch.setattr(service,'collect',hold)
 slot=latest_slot(datetime(2026,11,7,3,tzinfo=timezone.utc))
 assert service.refresh()
 try:
  assert not second.refresh(slot=slot)
  with second.db() as db:assert db.execute('SELECT count(*) FROM schedule_runs').fetchone()[0]==0
 finally:gate.set();service.thread.join(5)
 monkeypatch.setattr(second,'collect',hold)
 assert second.refresh(slot=slot)
 second.thread.join(5)
 assert not service.refresh(slot=slot)

def test_initialization_does_not_backfill_previous_slots(tmp_path,monkeypatch):
 service=scheduled_service(tmp_path,monkeypatch)
 assert not service.refresh(slot=latest_slot(datetime(2026,11,7,2,tzinfo=timezone.utc)))

def test_no_new_quote_is_idempotent_across_runs(tmp_path,monkeypatch):
 from app.services import commodities
 service=scheduled_service(tmp_path,monkeypatch)
 monkeypatch.setattr(commodities,'fetch',lambda *a,**k:body())
 monkeypatch.setattr(commodities,'discover',lambda *_:[])
 monkeypatch.setattr(service.stopping,'wait',lambda *_:False)
 service.collect(service.lock());first=service.history()['rows']
 service.collect(service.lock())
 assert service.history()['rows']==first and len(first)==18

def test_cross_process_claim_and_lock(tmp_path,monkeypatch):
 import subprocess,sys
 service=scheduled_service(tmp_path,monkeypatch)
 script='''
import sys
from datetime import datetime,timezone
from app.services.commodities import Commodities,latest_slot
from app.core.config import Settings
service=Commodities(Settings(_env_file=None,log_dir=sys.argv[1]))
service.collect=lambda lock,slot=None:lock.release()
print(service.refresh(slot=latest_slot(datetime(2026,11,7,3,tzinfo=timezone.utc))))
if service.thread:service.thread.join(5)
'''
 def run():
  return subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
 lock=service.lock()
 try:assert run()=='False'
 finally:lock.release()
 assert run()=='True'
 assert run()=='False'
