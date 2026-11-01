"""Real account sessions for domain/API tests; no production auth bypass."""
from app.api.routes.accounts import router as account_router
from app.services.accounts import Accounts, COOKIE

PASSWORD = 'test-only-long-password-9287'


def install_accounts(app, settings):
    app.state.accounts = Accounts(settings)
    app.state.accounts.create('admin', PASSWORD, bootstrap=True)
    app.include_router(account_router)
    return app.state.accounts


def sign_in(client, username='admin', password=PASSWORD):
    client.headers.update({'Origin': str(client.base_url).rstrip('/'), 'X-App-Client': 'browser'})
    result = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']
    return result


def domain_session(client):
    """Domain regressions start with a genuine administrator session."""
    accounts = getattr(client.app.state, 'accounts', None)
    if accounts is None:
        return  # Isolated service routers already supply their own dependencies.
    accounts.settings.auth_allow_http = True  # TestClient transport only.
    if not accounts.configured():
        accounts.create('admin', PASSWORD, bootstrap=True)
    sign_in(client)
