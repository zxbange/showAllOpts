"""Runtime controls change real clients/handlers, never health claims or TLS policy."""
import json
import logging
from types import SimpleNamespace
import httpx
import pytest
from app.services import system_runtime as m

@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(log_path=tmp_path,log_level='INFO',log_retention_days=30,
                           cache_db_read_timeout=600,cache_sync_attempts=3)


def test_defaults_preserve_startup_sync_and_logging(settings):
    settings.log_level='WARNING';settings.cache_sync_attempts=4
    value=m.default_runtime(settings)
    assert value['logging']==dict(level='WARNING',retention_days=30)
    assert value['database_sync']==dict(read_timeout_seconds=600,attempts=4)
    assert value['sources']['tdx']['request_timeout_seconds']==600
    assert value['sources']['eastmoney']['request_timeout_seconds']==60

@pytest.mark.parametrize('body',[
 {'extra':{}},{'logging':{'level':'TRACE'}},{'logging':{'retention_days':0}},
 {'logging':{'retention_days':True}},{'database_sync':{'attempts':0}},
 {'database_sync':{'attempts':6}},{'database_sync':{'read_timeout_seconds':3601}},
 {'sources':{'unknown':{'request_timeout_seconds':60}}},
 {'sources':{'tdx':{'request_timeout_seconds':601}}},
 {'sources':{'tdx':{'request_timeout_seconds':True}}},
 {'sources':{'tdx':{'request_timeout_seconds':60,'verify':False}}},
])
def test_bounds_and_unsupported_controls_rejected(settings,body):
    from app.services.system_configuration import SystemConfigurationError
    with pytest.raises(SystemConfigurationError):m.validate_runtime(body,m.default_runtime(settings))


def test_partial_update_is_immutable_and_status_is_not_health(settings):
    previous=m.default_runtime(settings)
    updated=m.validate_runtime({'logging':{'level':'DEBUG'},'sources':{'tdx':{'request_timeout_seconds':500}}},previous)
    assert previous['logging']['level']=='INFO' and updated['logging']['level']=='DEBUG'
    rows=m.source_status(settings,updated,{'asharehub':{'token':'private-token'}})
    assert len(rows)==10 and all(r['health']=='not_checked' and r['read_only'] for r in rows)
    assert next(r for r in rows if r['id']=='asharehub')['status']=='configured'
    assert 'private-token' not in json.dumps(rows)
    missing=m.source_status(settings,updated,{})
    assert next(r for r in missing if r['id']=='asharehub')['status']=='credential_missing'


@pytest.mark.parametrize('url,expected',[
 ('https://82.push2delay.eastmoney.com/api/qt/clist/get','eastmoney'),
 ('https://www.tdx.com.cn/products/data/data/g4day/20261109.zip','tdx'),
 ('https://asharehub.com/v2/market/daily','asharehub'),
 ('https://query.sse.com.cn/commonQuery.do','sse'),
 ('https://www.szse.cn/api/report/ShowReport/data','szse'),
 ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get','tencent'),
 ('https://hq.sinajs.cn/','sina'),('https://m.ccmn.cn/','ccmn'),
 ('https://coalindex.com/market','coalindex'),('https://oss-ch.csindex.com.cn/a.xls','csi'),
 ('https://evil-eastmoney.com/','none'),('https://eastmoney.com.attacker.test/','none'),
 ('https://api.telegram.org/bot/example','none'),
])
def test_platform_exact_suffix_matching(url,expected):
    assert (m.platform_for(url) or 'none')==expected


class FakeClient:
    def __init__(self,**kwargs):self.kwargs=kwargs;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def get(self,*args,**kwargs):self.calls.append((args,kwargs));return kwargs
    post=request=stream=get


def test_configured_timeout_overrides_get_and_stream_without_altering_tls(settings,monkeypatch):
    config=m.default_runtime(settings)
    monkeypatch.setattr(m,'runtime_config',lambda _:config)
    client=m.source_client(settings,factory=FakeClient,timeout=15,verify=True,follow_redirects=False)
    with client:
        first=client.get('https://www.tdx.com.cn/a.zip',timeout=httpx.Timeout(120),headers={'x':'safe'})
        assert first['timeout'].read==600 and first['timeout'].connect==30
        assert first['headers']=={'x':'safe'}
        config['sources']['tdx']['request_timeout_seconds']=180
        second=client.stream('GET','https://www.tdx.com.cn/a.zip',timeout=12)
        assert second['timeout'].read==180
        unknown=client.post('https://api.telegram.org/send',timeout=21)
        assert unknown['timeout']==21
    assert client.client.kwargs==dict(timeout=15,verify=True,follow_redirects=False)
    assert client.client.closed


def test_logging_applies_levels_retention_and_keeps_secret_redaction(settings,monkeypatch):
    from app.core.logging import StandardFormatter
    from app.services.system_configuration import SystemConfiguration
    config=m.default_runtime(settings);config['logging']=dict(level='DEBUG',retention_days=7)
    value=dict(runtime=config,database={'password':'new-private-password'},integrations={'telegram':{'token':'new-private-token'}})
    monkeypatch.setattr(SystemConfiguration,'load',lambda _:value)
    root=logging.getLogger();old_handlers=root.handlers[:];old_level=root.level
    names=('uvicorn','uvicorn.error','uvicorn.access','app','launcher','frontend')
    levels={name:logging.getLogger(name).level for name in names}
    app=logging.StreamHandler();app.showallopts_log_dir=str(settings.log_path)
    app.setFormatter(StandardFormatter(['existing-private-token']))
    error=logging.StreamHandler();error.showallopts_log_dir=str(settings.log_path);error.baseFilename='error.log'
    error.setFormatter(StandardFormatter())
    root.handlers=[app,error]
    try:
        assert m.apply_logging(settings)
        assert root.level==logging.DEBUG and app.level==logging.DEBUG and error.level==logging.ERROR
        assert app.backupCount==error.backupCount==7
        record=logging.LogRecord('app',logging.INFO,'',1,'new-private-token new-private-password',(),None)
        assert 'new-private' not in app.format(record)
        count=len(app.formatter.secrets)
        m.apply_logging(settings)
        assert len(app.formatter.secrets)==count
        config['logging']=dict(level='ERROR',retention_days=15)
        m.apply_logging(settings)
        assert app.level==logging.ERROR and app.backupCount==15
    finally:
        root.handlers=old_handlers;root.setLevel(old_level)
        for name,level in levels.items():logging.getLogger(name).setLevel(level)


def test_saved_timeout_reaches_real_httpx_request_without_network(tmp_path):
    from app.core.config import Settings
    from app.services.system_configuration import SystemConfiguration
    settings=Settings(_env_file=None,log_dir=tmp_path)
    config=SystemConfiguration(settings)
    seen=[]
    def transport(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200,json={'ok':True})
    def save(seconds):
        body=config.public();body['database']['password']=None
        body['runtime']['sources']['eastmoney']['request_timeout_seconds']=seconds
        config.save(body)
    save(35)
    with m.source_client(settings,transport=httpx.MockTransport(transport),follow_redirects=False) as client:
        client.get('https://datacenter-web.eastmoney.com/api/data/v1/get',timeout=2)
        save(90)
        client.get('https://datacenter-web.eastmoney.com/api/data/v1/get',timeout=2)
    assert [r['read'] for r in seen]==[35,90]
    assert [r['connect'] for r in seen]==[30,30]
    assert config.public()['runtime']['sources']['eastmoney']['request_timeout_seconds']==90
