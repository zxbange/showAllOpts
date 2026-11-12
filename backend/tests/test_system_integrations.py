"""Integration settings are write-only and clears cannot revive legacy credentials."""
import json
import sqlite3
from types import SimpleNamespace
from cryptography.fernet import Fernet
import pytest
from pydantic import SecretStr
from app.services import system_integrations as m

@pytest.fixture
def settings(tmp_path,monkeypatch):
    for fields in m.FIELDS.values():
        for key in fields.values():monkeypatch.delenv(key,raising=False)
    return SimpleNamespace(log_path=tmp_path,asharehub_api_key=SecretStr(''),telegram_bot_token=SecretStr(''),telegram_chat_id=SecretStr(''))

def legacy(settings):
    root=settings.log_path/'social';root.mkdir()
    key=Fernet.generate_key();cipher=Fernet(key)
    (root/'secrets.key').write_bytes(key)
    with sqlite3.connect(root/'monitor.sqlite3') as db:
        db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO config VALUES (1,?)',(json.dumps(dict(token=cipher.encrypt(b'legacy-token').decode(),chat_id=cipher.encrypt(b'legacy-chat').decode())),))
    return root

def test_defaults_never_create_files_and_keep_secrets_out_of_public(settings):
    result=m.integration_defaults(settings,{'ASHAREHUB_API_KEY':'private-api'})
    assert not list(settings.log_path.iterdir())
    public=m.sanitize_integrations(result)
    assert public==dict(asharehub=dict(token_set=True),telegram=dict(token_set=False,chat_id_set=False))
    assert 'private-api' not in json.dumps(public)

def test_legacy_fallback_is_read_only_and_empty_file_value_prevents_resurrection(settings):
    root=legacy(settings);before={p.name:p.read_bytes() for p in root.iterdir()}
    result=m.integration_defaults(settings,{})
    assert result['telegram']==dict(token='legacy-token',chat_id='legacy-chat')
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}
    assert m.integration_defaults(settings,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''})['telegram']==dict(token='',chat_id='')

def test_process_override_and_explicit_empty(settings,monkeypatch):
    legacy(settings)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','');monkeypatch.setenv('ASHAREHUB_API_KEY','process-api')
    result=m.integration_defaults(settings,{'TELEGRAM_BOT_TOKEN':'file-token','ASHAREHUB_API_KEY':'file-api'})
    assert result['telegram']['token']=='' and result['asharehub']['token']=='process-api'

def test_null_retains_clear_is_explicit_and_env_updates_only_differences(settings):
    previous=dict(asharehub=dict(token='api'),telegram=dict(token='bot',chat_id='chat'))
    assert m.validate_integrations(None,previous)==previous
    candidate=m.validate_integrations(dict(asharehub=dict(token=None),telegram=dict(token='',chat_id=None)),previous)
    assert candidate['telegram']==dict(token='',chat_id='chat')
    assert m.integration_env_updates(candidate,previous)=={'TELEGRAM_BOT_TOKEN':''}
    assert previous['telegram']['token']=='bot'

@pytest.mark.parametrize('body',[{'unknown':{}},{'telegram':{'token':1}},{'asharehub':{'token':'line\nbreak'}},{'telegram':{'chat_id':'x'*257}},{'telegram':{'unexpected':True}}])
def test_invalid_integrations_fail_without_echoing_values(settings,body):
    from app.services.system_configuration import SystemConfigurationError
    with pytest.raises(SystemConfigurationError):m.validate_integrations(body,m.integration_defaults(settings,{}))

def test_removed_file_key_does_not_resurrect_startup_secret(settings):
    settings.asharehub_api_key=SecretStr('stale-startup')
    assert m.integration_defaults(settings,{})['asharehub']['token']==''
    assert m.integration_defaults(settings,None)['asharehub']['token']=='stale-startup'

def test_central_credentials_drive_social_status_and_sender_contract(tmp_path,monkeypatch):
    from app.core.config import Settings
    from app.services.social import SocialMonitor
    from app.services.system_configuration import SystemConfiguration
    for fields in m.FIELDS.values():
        for key in fields.values():monkeypatch.delenv(key,raising=False)
    settings=Settings(_env_file=None,log_dir=tmp_path);social=SocialMonitor(settings)
    social.update_config(dict(token='old-private-token',chat_id='old-private-chat'))
    central=SystemConfiguration(settings);body=central.public()
    assert body['integrations']['telegram']['token_set']
    body['database']['password']=None
    body['integrations']={'telegram':dict(token='new-private-token',chat_id='new-private-chat')}
    result=central.save(body)
    assert 'new-private-token' not in json.dumps(result) and b'new-private-token' not in central.path.read_bytes()
    with social.db() as db:config=social.config(db)
    assert social.decode(config['token'])=='new-private-token' and social.decode(config['chat_id'])=='new-private-chat'
    with social.db() as db: unchanged=social.config(db)
    assert unchanged['token']==config['token'] and unchanged['chat_id']==config['chat_id']
    with pytest.raises(ValueError,match='系统配置'):social.update_config(dict(token='ignored'))
    body=central.public();body['database']['password']=None
    body['integrations']={'telegram':dict(token='',chat_id='')};central.save(body)
    with social.db() as db:config=social.config(db)
    assert config['token']==config['chat_id']==''

def test_file_deleted_asharehub_key_is_not_restored_from_settings(tmp_path,monkeypatch):
    from app.core.config import Settings
    from app.services.system_configuration import SystemConfiguration
    for fields in m.FIELDS.values():
        for key in fields.values():monkeypatch.delenv(key,raising=False)
    path=tmp_path/'app.config'
    path.write_text('ASHAREHUB_API_KEY=first-private-api\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n')
    settings=Settings(_env_file=path,log_dir=tmp_path/'logs')
    assert m.integrations_config(settings)['asharehub']['token']=='first-private-api'
    path.write_text('TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n')
    assert m.integrations_config(settings)['asharehub']['token']==''
    assert SystemConfiguration(settings).public()['integrations']['asharehub']['token_set'] is False
