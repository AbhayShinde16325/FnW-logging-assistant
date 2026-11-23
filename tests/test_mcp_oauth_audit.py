from __future__ import annotations

import base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth.config as auth_config
import auth.mcp_oauth_routes as mcp_oauth_routes
import auth.routes as auth_routes
import auth.service as auth_service
import auth.mcp_oauth as mcp_oauth
import mcp_server.server as server
import database.task_repository as task_repository
import services.task_management as task_management
import services.conversation_memory as conversation_memory
import auth.employee_service as employee_service
from database.connection import Base
from database.models import Employee, Role, Task
from auth.permissions import ROLE_EMPLOYEE
from auth.service import sync_rbac
from mcp_server.security.execution_context import execution_context_from_token, MCPAuthenticationError
from types import SimpleNamespace


def _settings():
    base = auth_config.load_auth_settings()
    return type(base)(**{**base.__dict__, 'required':True,'secret_key':'x'*48,'google_client_id':'gid','google_client_secret':'gsecret','google_redirect_uri':'http://localhost:8000/auth/google/callback','frontend_origins':('http://localhost:5173',),'post_login_redirect':'http://localhost:5173','cookie_secure':False})


@pytest.fixture
def env(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for m in (auth_service, mcp_oauth, task_repository, task_management, conversation_memory, employee_service):
        monkeypatch.setattr(m, 'SessionLocal', factory)
    task_management.notification_service = SimpleNamespace(
        ReminderValidationError=Exception, TYPE_TASK_ASSIGNED='task_assigned', PRIORITY_HIGH='HIGH',
        utcnow=lambda: datetime.now(timezone.utc), validate_reminder_values=lambda **_: None,
        create_task_event_notification=lambda **_: None, cancel_pending_notifications=lambda **_: None,
    )
    settings = _settings()
    monkeypatch.setattr(auth_config, 'settings', settings)
    monkeypatch.setattr(auth_routes, 'auth_config', auth_config)
    monkeypatch.setattr(mcp_oauth_routes, 'auth_config', auth_config)
    import auth.google as ag
    monkeypatch.setattr(ag, 'authorization_url', lambda **kw: 'https://accounts.google.com/o/oauth2/v2/auth?state=' + kw['state'])
    monkeypatch.setattr(auth_routes, 'exchange_code', lambda **kw: {'id_token': 'id'})
    monkeypatch.setattr(auth_routes, 'verify_id_token', lambda **kw: {'sub': 'sub1', 'email': 'rahul@example.com', 'name': 'Rahul'})
    sync_rbac()
    s = factory(); role = s.query(Role).filter(Role.name == ROLE_EMPLOYEE).one()
    e1 = Employee(name='Rahul', email='rahul@example.com', role_id=role.id, is_active=True)
    e2 = Employee(name='Neha', email='neha@example.com', role_id=role.id, is_active=True)
    s.add_all([e1, e2]); s.commit(); s.refresh(e1); s.refresh(e2)
    t1 = Task(task_title='Own', assigned_to='Rahul', assigned_to_employee_id=e1.id, created_by_employee_id=e1.id, status='PENDING')
    t2 = Task(task_title='Other', assigned_to='Neha', assigned_to_employee_id=e2.id, created_by_employee_id=e2.id, status='PENDING')
    s.add_all([t1, t2]); s.commit(); t1id, t2id, e1id = t1.id, t2.id, e1.id; s.close()
    from contextlib import asynccontextmanager
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.types import Receive, Scope, Send

    sm = StreamableHTTPSessionManager(app=server.mcp_server, json_response=True, stateless=True)
    class ASGI:
        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            await sm.handle_request(scope, receive, send)
    mcp_app = server.MCPAuthenticationMiddleware(ASGI(), server.settings)

    @asynccontextmanager
    async def lifespan(_app):
        async with sm.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(auth_routes.router)
    app.include_router(mcp_oauth_routes.router)
    app.mount('/mcp', mcp_app)
    with TestClient(app, follow_redirects=False) as c:
        yield {'c': c, 'settings': settings, 'factory': factory, 't1': t1id, 't2': t2id, 'e1': e1id}


def _pkce():
    v = secrets.token_urlsafe(64)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b'=').decode()
    return v, ch


def _register(c, redirect='https://chatgpt.com/a'):
    r = c.post('/oauth/register', json={'redirect_uris':[redirect],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','client_name':'ChatGPT'})
    assert r.status_code == 201
    return r.json()['client_id']


def _issue_token(env, redirect='https://chatgpt.com/a', resource=None):
    c, settings = env['c'], env['settings']
    resource = resource or mcp_oauth.canonical_mcp_resource(settings)
    client_id = _register(c, redirect)
    v, ch = _pkce()
    auth = c.get('/oauth/authorize', params={'response_type':'code','client_id':client_id,'redirect_uri':redirect,'state':'st','code_challenge':ch,'code_challenge_method':'S256','resource':resource})
    assert auth.status_code == 302
    gs = parse_qs(urlparse(auth.headers['location']).query)['state'][0]
    cb = c.get('/auth/google/callback', params={'code':'gc','state':gs})
    assert cb.status_code == 302
    q = parse_qs(urlparse(cb.headers['location']).query)
    tok = c.post('/oauth/token', data={'grant_type':'authorization_code','code':q['code'][0],'redirect_uri':redirect,'client_id':client_id,'code_verifier':v,'resource':resource})
    assert tok.status_code == 200
    return client_id, tok.json(), resource


def test_dcr_valid_invalid_and_persistence(env):
    c = env['c']
    ok = c.post('/oauth/register', json={'redirect_uris':['https://chatgpt.com/a'],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'})
    assert ok.status_code == 201
    cid = ok.json()['client_id']
    # persistence: new session factory lookup
    s = env['factory']()
    from database.models import MCPOAuthClient
    row = s.query(MCPOAuthClient).filter_by(client_id=cid).first(); s.close()
    assert row is not None and 'https://chatgpt.com/a' in (row.redirect_uris or [])
    bad_redirect = c.post('/oauth/register', json={'redirect_uris':['javascript:alert(1)']})
    assert bad_redirect.status_code == 400
    bad_auth = c.post('/oauth/register', json={'redirect_uris':['https://chatgpt.com/a'],'token_endpoint_auth_method':'client_secret_post'})
    assert bad_auth.status_code == 400


def test_unregistered_client_and_exact_redirect_match(env):
    c, settings = env['c'], env['settings']
    resource = mcp_oauth.canonical_mcp_resource(settings)
    _, ch = _pkce()
    unreg = c.get('/oauth/authorize', params={'response_type':'code','client_id':'nope','redirect_uri':'https://chatgpt.com/a','state':'s','code_challenge':ch,'code_challenge_method':'S256','resource':resource})
    assert unreg.status_code == 401
    client_id = _register(c, 'https://chatgpt.com/a')
    mismatch = c.get('/oauth/authorize', params={'response_type':'code','client_id':client_id,'redirect_uri':'https://chatgpt.com/a/evil','state':'s','code_challenge':ch,'code_challenge_method':'S256','resource':resource})
    assert mismatch.status_code == 400


def test_pkce_resource_iss_and_wrong_resource(env):
    c, settings = env['c'], env['settings']
    resource = mcp_oauth.canonical_mcp_resource(settings)
    client_id = _register(c)
    v, ch = _pkce()
    auth = c.get('/oauth/authorize', params={'response_type':'code','client_id':client_id,'redirect_uri':'https://chatgpt.com/a','state':'st','code_challenge':ch,'code_challenge_method':'S256','resource':resource})
    gs = parse_qs(urlparse(auth.headers['location']).query)['state'][0]
    cb = c.get('/auth/google/callback', params={'code':'gc','state':gs})
    q = parse_qs(urlparse(cb.headers['location']).query)
    assert q.get('iss', [None])[0] == mcp_oauth.canonical_issuer(settings)
    bad = c.post('/oauth/token', data={'grant_type':'authorization_code','code':q['code'][0],'redirect_uri':'https://chatgpt.com/a','client_id':client_id,'code_verifier':'wrong','resource':resource})
    assert bad.status_code == 400
    # new code for wrong resource
    v2, ch2 = _pkce()
    auth2 = c.get('/oauth/authorize', params={'response_type':'code','client_id':client_id,'redirect_uri':'https://chatgpt.com/a','state':'st2','code_challenge':ch2,'code_challenge_method':'S256','resource':resource})
    gs2 = parse_qs(urlparse(auth2.headers['location']).query)['state'][0]
    cb2 = c.get('/auth/google/callback', params={'code':'gc','state':gs2})
    code2 = parse_qs(urlparse(cb2.headers['location']).query)['code'][0]
    wr = c.post('/oauth/token', data={'grant_type':'authorization_code','code':code2,'redirect_uri':'https://chatgpt.com/a','client_id':client_id,'code_verifier':v2,'resource':'https://evil.example/mcp'})
    assert wr.status_code == 400


def test_refresh_replay_revocation_and_audience(env):
    c, settings = env['c'], env['settings']
    client_id, tok, resource = _issue_token(env)
    r1 = c.post('/oauth/token', data={'grant_type':'refresh_token','refresh_token':tok['refresh_token'],'client_id':client_id,'resource':resource})
    assert r1.status_code == 200
    replay = c.post('/oauth/token', data={'grant_type':'refresh_token','refresh_token':tok['refresh_token'],'client_id':client_id,'resource':resource})
    assert replay.status_code == 400
    r2 = c.post('/oauth/token', data={'grant_type':'refresh_token','refresh_token':r1.json()['refresh_token'],'client_id':client_id,'resource':resource})
    assert r2.status_code == 400  # chain revoked
    with pytest.raises(Exception):
        mcp_oauth.validate_mcp_oauth_access_token(tok['access_token'], settings=settings, expected_resource='https://evil.example/mcp')


def test_mcp_tool_invocation_isolation_and_revocation(env):
    c, settings = env['c'], env['settings']
    client_id, tok, resource = _issue_token(env)
    h = {'Authorization': f'Bearer {tok["access_token"]}', 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    init = c.post('/mcp/', headers=h, json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'t','version':'1'}}})
    assert init.status_code == 200
    own = c.post('/mcp/', headers=h, json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_task','arguments':{'task_id':env['t1']}}})
    other = c.post('/mcp/', headers=h, json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_task','arguments':{'task_id':env['t2']}}})
    assert 'Own' in own.text
    assert 'not_found' in other.text
    c.post('/oauth/revoke', data={'token': tok['refresh_token']})
    with pytest.raises(Exception):
        execution_context_from_token(tok['access_token'])
