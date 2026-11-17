# OIDC operator sign-in. The mock issuer stays in process so these tests use no network.
"""Browser OIDC sessions, fail-closed groups, and the unchanged bearer map."""
import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import operators
import operator_oidc
from demo_sessions import DemoStore
from hosted_app import DeskDemo, create_app

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


SITE = 'https://demo.example'
ISSUER = 'https://issuer.example'
SECRET = 'dev-mock-secret'
REDIRECT = SITE + '/auth/oidc/callback'


def oidc_env(**extra):
    env = {
        operator_oidc.ENV_ISSUER: ISSUER,
        operator_oidc.ENV_CLIENT_ID: 'desk-web',
        operator_oidc.ENV_CLIENT_SECRET: SECRET,
        operator_oidc.ENV_REDIRECT: REDIRECT,
        operator_oidc.ENV_MOCK: '1',
    }
    env.update(extra)
    return env


class OidcConfigTests(unittest.TestCase):
    def test_unset_issuer_leaves_oidc_off(self):
        self.assertIsNone(operator_oidc.load_config({}))

    def test_issuer_without_client_fields_fails_closed(self):
        for missing in (operator_oidc.ENV_CLIENT_ID, operator_oidc.ENV_CLIENT_SECRET,
                        operator_oidc.ENV_REDIRECT):
            env = oidc_env()
            env.pop(missing)
            with self.assertRaises(operator_oidc.OidcConfigError):
                operator_oidc.load_config(env)

    def test_group_defaults_and_overrides(self):
        config = operator_oidc.load_config(oidc_env())
        self.assertEqual(config.groups_claim, 'groups')
        self.assertEqual(config.admin_group, 'desk-admin')
        self.assertEqual(config.reviewer_group, 'desk-reviewer')
        self.assertTrue(config.mock)
        custom = operator_oidc.load_config(oidc_env(**{
            operator_oidc.ENV_GROUPS_CLAIM: 'teams',
            operator_oidc.ENV_ADMIN_GROUP: 'admins',
            operator_oidc.ENV_REVIEWER_GROUP: 'reviewers',
            operator_oidc.ENV_MOCK: '0',
        }))
        self.assertEqual(custom.groups_claim, 'teams')
        self.assertEqual(custom.admin_group, 'admins')
        self.assertFalse(custom.mock)

    def test_discovery_url_inserts_well_known_before_a_path(self):
        self.assertEqual(
            operator_oidc.discovery_url('https://issuer.example'),
            'https://issuer.example/.well-known/openid-configuration')
        self.assertEqual(
            operator_oidc.discovery_url('https://issuer.example/realms/desk'),
            'https://issuer.example/.well-known/openid-configuration/realms/desk')

    def test_pkce_s256_matches_the_rfc_vector(self):
        verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
        self.assertEqual(operator_oidc.code_challenge(verifier),
                         'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')

    def test_safe_next_rejects_off_site_targets(self):
        self.assertEqual(operator_oidc.safe_next('/desk'), '/desk')
        for bad in ('https://evil.example', '//evil.example', '/\\evil',
                    '/auth/oidc/login', '/desk\r\nX', None, 'desk'):
            self.assertEqual(operator_oidc.safe_next(bad), '/desk')


class OidcClaimTests(unittest.TestCase):
    def setUp(self):
        self.config = operator_oidc.load_config(oidc_env())

    def _operator(self, **claims):
        body = {'sub': 'user-1', 'groups': ['desk-reviewer']}
        body.update(claims)
        return operator_oidc.operator_from_claims(body, self.config)

    def test_admin_group_wins_when_both_are_present(self):
        admin = self._operator(groups=['desk-reviewer', 'desk-admin'])
        self.assertEqual(admin.role, 'admin')
        self.assertTrue(admin.can_reset())
        reviewer = self._operator(groups=['desk-reviewer'])
        self.assertEqual(reviewer.role, 'reviewer')
        self.assertFalse(reviewer.can_reset())
        self.assertTrue(reviewer.can_decide())

    def test_missing_group_is_forbidden_and_not_admin(self):
        for groups in ([], ['other'], None, {'desk-admin': True}):
            with self.assertRaises(operator_oidc.OidcAuthError) as caught:
                self._operator(groups=groups)
            self.assertEqual(caught.exception.status, 403)
            self.assertEqual(caught.exception.code, 'OPERATOR_FORBIDDEN')

    def test_subject_keeps_case_and_rejects_the_demo_id_rules_only_when_unsafe(self):
        upper = self._operator(sub='User@Example')
        self.assertEqual(upper.operator_id, 'User@Example')
        long_sub = 'U' + ('a' * 69)
        self.assertGreater(len(long_sub), 64)
        self.assertEqual(self._operator(sub=long_sub).operator_id, long_sub)
        padded = self._operator(sub='  User@Example  ')
        self.assertEqual(padded.operator_id, 'User@Example')
        for bad in ('', '   ', 'line\nbreak', 'tag<admin>', 'a>' + 'b', 'x' * 257, 12):
            with self.assertRaises(operator_oidc.OidcAuthError) as caught:
                self._operator(sub=bad)
            self.assertEqual(caught.exception.status, 401)

    def test_display_name_uses_the_first_claim_and_caps_at_80(self):
        self.assertEqual(self._operator(name=' Ada ', email='ada@example').display_name, 'Ada')
        email_only = self._operator(email='ada@example', preferred_username='ada')
        self.assertEqual(email_only.display_name, 'ada@example')
        username = self._operator(preferred_username='ada')
        self.assertEqual(username.display_name, 'ada')
        fallback = self._operator(sub='only-sub')
        self.assertEqual(fallback.display_name, 'only-sub')
        long_name = self._operator(name='N' * 90)
        self.assertEqual(long_name.display_name, 'N' * 80)
        folded = self._operator(name='Ada\nOps')
        self.assertEqual(folded.display_name, 'Ada Ops')


class OidcTokenTests(unittest.TestCase):
    def setUp(self):
        self.config = operator_oidc.load_config(oidc_env())
        self.now = 1_700_000_000
        self.claims = {
            'iss': ISSUER,
            'aud': 'desk-web',
            'exp': self.now + 60,
            'nonce': 'nonce-1',
            'sub': 'user-1',
        }

    def _token(self, claims=None, secret=SECRET, header=None):
        body = dict(self.claims if claims is None else claims)
        if header is None:
            return operator_oidc.sign_hs256(body, secret)
        header_b64 = _b64url(json.dumps(header, separators=(',', ':')).encode())
        payload_b64 = _b64url(json.dumps(body, separators=(',', ':')).encode())
        return f'{header_b64}.{payload_b64}.not-a-signature'

    def _verify(self, token, nonce='nonce-1'):
        return operator_oidc.verify_id_token(
            token, self.config, nonce=nonce, now=self.now)

    def test_hs256_round_trip_and_rejections(self):
        claims = self._verify(self._token())
        self.assertEqual(claims['sub'], 'user-1')
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(secret='other-secret'))
        expired = dict(self.claims, exp=self.now)
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(expired))
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(dict(self.claims, iss='https://other.example')))
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(dict(self.claims, aud='other-client')))
        listed = dict(self.claims, aud=['desk-web', 'other'], azp='desk-web')
        self.assertEqual(self._verify(self._token(listed))['aud'][0], 'desk-web')
        missing_azp = dict(self.claims, aud=['desk-web', 'other'])
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(missing_azp))
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(), nonce='other-nonce')
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(header={'alg': 'none', 'typ': 'JWT'}))
        with self.assertRaises(operator_oidc.OidcAuthError):
            self._verify(self._token(header={'alg': 'RS256', 'typ': 'JWT'}))


class _Body:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, limit=-1):
        if limit < 0 or limit >= len(self.payload):
            data, self.payload = self.payload, b''
            return data
        data, self.payload = self.payload[:limit], self.payload[limit:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, request, timeout=5):
        self.urls.append(request.full_url)
        page = self.pages.get(request.full_url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise urllib.error.URLError(request.full_url)
        return _Body(page)


class OidcSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = operator_oidc.load_config(oidc_env())
        self.store = operator_oidc.SessionStore(self.temp.name)
        self.now = 1_700_000_000

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_is_single_use_and_sessions_round_trip(self):
        pending, url = operator_oidc.start_login(
            self.config, self.store, '/desk', now=self.now)
        self.assertNotIn(SECRET, url)
        self.assertIn('code_challenge_method=S256', url)
        location = operator_oidc.mock_authorize(
            self.config, self.store,
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query),
                 mock_sub='User@Example', mock_name='Ada', mock_groups='desk-admin'),
            now=self.now, env={})
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        operator, session, nxt = operator_oidc.finish_login(
            self.config, self.store, query['code'], query['state'], now=self.now)
        self.assertEqual(nxt, '/desk')
        self.assertEqual(operator.operator_id, 'User@Example')
        self.assertEqual(operator.role, 'admin')
        loaded = self.store.operator_from_cookie(
            f'{operator_oidc.COOKIE_NAME}={session.session_id}', self.now)
        self.assertEqual(loaded, operator)
        with self.assertRaises(operator_oidc.OidcAuthError):
            operator_oidc.finish_login(
                self.config, self.store, query['code'], query['state'], now=self.now)
        texts = [path.read_text(encoding='utf-8')
                 for path in self.store.root.rglob('*.json')]
        self.assertTrue(any(json.loads(text)['operator_id'] == 'User@Example' for text in texts))
        for text in texts:
            self.assertNotIn(SECRET, text)
            self.assertNotIn('id_token', text)
            record = json.loads(text)
            if 'operator_id' in record:
                self.assertEqual(set(record), {
                    'session_id', 'operator_id', 'display_name', 'role', 'expires_at'})

    def test_expired_session_and_bad_role_do_not_authenticate(self):
        operator = operators.Operator('user-1', 'Ada', 'admin')
        session = self.store.save_session(operator, self.now)
        cookie = f'{operator_oidc.COOKIE_NAME}={session.session_id}'
        self.assertIsNone(self.store.operator_from_cookie(cookie, session.expires_at))
        fresh = self.store.save_session(operator, self.now)
        path = self.store.root / 'sessions' / f'{fresh.session_id}.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        record['role'] = 'superuser'
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertIsNone(self.store.operator_from_cookie(
            f'{operator_oidc.COOKIE_NAME}={fresh.session_id}', self.now))

    def test_http_exchange_verifies_the_id_token_and_stores_no_secret(self):
        config = operator_oidc.load_config(oidc_env(**{operator_oidc.ENV_MOCK: '0'}))
        discovery = operator_oidc.discovery_url(ISSUER)
        opener = _Opener({
            discovery: {
                'issuer': ISSUER,
                'authorization_endpoint': ISSUER + '/auth',
                'token_endpoint': ISSUER + '/token',
            },
        })
        pending, url = operator_oidc.start_login(
            config, self.store, 'https://evil.example', now=self.now, opener=opener)
        self.assertTrue(url.startswith(ISSUER + '/auth?'))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        token = operator_oidc.sign_hs256({
            'iss': ISSUER,
            'aud': 'desk-web',
            'exp': self.now + 60,
            'nonce': query['nonce'],
            'sub': 'http-user',
            'name': 'Http User',
            'groups': ['desk-reviewer'],
        }, SECRET)
        opener.pages[ISSUER + '/token'] = {'id_token': token}
        operator, _session, nxt = operator_oidc.finish_login(
            config, self.store, 'auth-code', query['state'], now=self.now, opener=opener)
        self.assertEqual(operator.role, 'reviewer')
        self.assertEqual(operator.operator_id, 'http-user')
        self.assertEqual(nxt, '/desk')
        joined = '\n'.join(path.read_text(encoding='utf-8')
                           for path in self.store.root.rglob('*.json'))
        self.assertNotIn(SECRET, joined)
        self.assertNotIn(token, joined)

    def test_discovery_failure_and_rs256_store_no_session(self):
        config = operator_oidc.load_config(oidc_env(**{operator_oidc.ENV_MOCK: '0'}))
        down = _Opener({operator_oidc.discovery_url(ISSUER): urllib.error.URLError('down')})
        with self.assertRaises(operator_oidc.OidcAuthError):
            operator_oidc.start_login(config, self.store, '/desk', now=self.now, opener=down)
        self.assertEqual(list(self.store.root.rglob('*.json')), [])
        discovery = operator_oidc.discovery_url(ISSUER)
        opener = _Opener({
            discovery: {
                'issuer': ISSUER,
                'authorization_endpoint': ISSUER + '/auth',
                'token_endpoint': ISSUER + '/token',
            },
        })
        _pending, url = operator_oidc.start_login(
            config, self.store, '/desk', now=self.now, opener=opener)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        header = _b64url(json.dumps({'alg': 'RS256'}).encode())
        payload = _b64url(json.dumps({'sub': 'x'}).encode())
        opener.pages[ISSUER + '/token'] = {'id_token': f'{header}.{payload}.sig'}
        with self.assertRaises(operator_oidc.OidcAuthError) as caught:
            operator_oidc.finish_login(
                config, self.store, 'auth-code', query['state'], now=self.now, opener=opener)
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(any(path.parent.name == 'sessions'
                             for path in self.store.root.rglob('*.json')))

    def test_demo_resolve_can_skip_operator_json(self):
        store = DemoStore(Path(self.temp.name) / 'demos', DeskDemo)
        try:
            demo_id, _payments, _logger, _cookie, operator = store.resolve(
                '', create=True, mint_operator=False)
            folder = Path(self.temp.name) / 'demos' / demo_id
            self.assertIsNone(operator)
            self.assertTrue((folder / 'payments.sqlite3').is_file())
            self.assertFalse((folder / 'operator.json').exists())
            store.release(demo_id)
        finally:
            store.close()


class OidcHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, oidc_env(), clear=False)
        self._env.start()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self._env.stop()
        self.temp.cleanup()

    def authorize_url(self, **identity):
        start = self.client.get('/auth/oidc/login', base_url=SITE)
        self.assertEqual(start.status_code, 302, start.get_data(as_text=True))
        location = start.headers['Location']
        extra = urllib.parse.urlencode({key: value for key, value in identity.items()
                                         if value is not None})
        joiner = '&' if '?' in location else '?'
        issued = self.client.get(location + (('' if not extra else joiner + extra)),
                                 base_url=SITE)
        self.assertEqual(issued.status_code, 302, issued.get_data(as_text=True))
        return issued.headers['Location']

    def sign_in(self, **identity):
        done = self.client.get(self.authorize_url(**identity), base_url=SITE)
        return done

    def _without_oidc_env(self, updates=None):
        keys = (
            operator_oidc.ENV_ISSUER, operator_oidc.ENV_CLIENT_ID,
            operator_oidc.ENV_CLIENT_SECRET, operator_oidc.ENV_REDIRECT,
            operator_oidc.ENV_MOCK,
        )
        saved = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        if updates:
            os.environ.update(updates)
        return saved

    def _restore_env(self, saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_issuer_off_still_mints_and_login_is_absent(self):
        saved = self._without_oidc_env()
        app = create_app(self.temp.name + '-off', SITE)
        try:
            client = app.test_client()
            page = client.get('/desk', base_url=SITE)
            self.assertEqual(page.status_code, 200)
            self.assertTrue(list(Path(self.temp.name + '-off').rglob('operator.json')))
            missing = client.get('/auth/oidc/login', base_url=SITE)
            self.assertEqual(missing.status_code, 404)
        finally:
            app.extensions['close']()
            self._restore_env(saved)

    def test_incomplete_issuer_does_not_mint_and_bearer_still_works(self):
        tokens = json.dumps({
            'tok-admin': {'id': 'op-token', 'name': 'Token Admin', 'role': 'admin'},
        })
        saved = self._without_oidc_env({
            operator_oidc.ENV_ISSUER: ISSUER,
            operators.ENV_TOKENS: tokens,
        })
        folder = self.temp.name + '-bad'
        app = create_app(folder, SITE)
        try:
            client = app.test_client()
            reply = client.get('/desk', base_url=SITE)
            self.assertEqual(reply.status_code, 503)
            self.assertEqual(reply.json['code'], 'OPERATOR_UNAUTHORIZED')
            self.assertEqual(list(Path(folder).rglob('operator.json')), [])
            reset = client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})
            self.assertEqual(reset.status_code, 401)
            held = client.post(
                '/payouts/PO-1043/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-hold', 'Origin': SITE,
                         'Authorization': 'Bearer tok-admin'})
            self.assertEqual(held.status_code, 201)
            self.assertEqual(held.json['decision']['actor'], 'Token Admin <op-token>')
        finally:
            app.extensions['close']()
            self._restore_env(saved)

    def test_leftover_operator_json_is_ignored(self):
        saved = self._without_oidc_env()
        app = create_app(self.temp.name + '-left', SITE)
        try:
            client = app.test_client()
            minted = client.get('/desk', base_url=SITE)
            self.assertEqual(minted.status_code, 200)
            self.assertTrue(list(Path(self.temp.name + '-left').rglob('operator.json')))
        finally:
            app.extensions['close']()
            self._restore_env(saved)
        with mock.patch.dict(os.environ, oidc_env(), clear=False):
            app = create_app(self.temp.name + '-left', SITE)
            try:
                desk = app.test_client().get('/desk', base_url=SITE)
                self.assertEqual(desk.status_code, 302)
                self.assertTrue(desk.headers['Location'].startswith('/auth/oidc/login'))
            finally:
                app.extensions['close']()

    def test_demo_expiry_does_not_delete_operator_sessions(self):
        import time
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.dict(os.environ, {'DEMO_IDLE_SECONDS': '60', **oidc_env()},
                                 clear=False):
                store = DemoStore(root, DeskDemo)
                try:
                    demo_id, _, _, _, operator = store.resolve('', create=True)
                    self.assertIsNone(operator)
                    store.release(demo_id)
                    sessions = operator_oidc.SessionStore(root)
                    op = operators.Operator('keep-me', 'Keep', 'admin')
                    record = sessions.save_session(op, time.time())
                    old = time.time() - 120
                    os.utime(root / demo_id / '.last-used', (old, old))
                    store.resolve('', create=True)
                    self.assertFalse((root / demo_id).exists())
                    self.assertTrue(
                        (root / '_operator_sessions' / 'sessions' /
                         f'{record.session_id}.json').is_file())
                finally:
                    store.close()

    def test_mock_mode_refuses_to_boot_on_render(self):
        with mock.patch.dict(os.environ, {'RENDER': 'true'}, clear=False):
            with self.assertRaises(RuntimeError):
                create_app(self.temp.name + '-render', SITE)

    def test_unsigned_get_redirects_and_post_is_401_without_operator_json(self):
        page = self.client.get('/desk', base_url=SITE)
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers['Location'].startswith('/auth/oidc/login'))
        posted = self.client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})
        self.assertEqual(posted.status_code, 401)
        self.assertEqual(posted.json['code'], 'OPERATOR_UNAUTHORIZED')
        root = Path(self.temp.name)
        self.assertEqual(list(root.rglob('operator.json')), [])
        self.assertTrue(list(root.rglob('payments.sqlite3')))

    def test_admin_reviewer_and_neither_group(self):
        admin = self.sign_in(mock_sub='User@Example', mock_name='Ada Admin',
                             mock_groups='desk-admin,desk-reviewer')
        self.assertEqual(admin.status_code, 302)
        self.assertEqual(urllib.parse.urlsplit(admin.headers['Location']).path, '/desk')
        page = self.client.get('/desk', base_url=SITE)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Ada Admin · admin', page.get_data(as_text=True))
        me = self.client.get('/operator', base_url=SITE)
        self.assertEqual(me.json['operator']['operator_id'], 'User@Example')
        self.assertEqual(me.json['operator']['role'], 'admin')
        reset = self.client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})
        self.assertEqual(reset.status_code, 200)
        hold = self.client.post(
            '/payouts/PO-1043/hold', json={}, base_url=SITE,
            headers={'Idempotency-Key': 'oidc-hold', 'Origin': SITE})
        self.assertEqual(hold.status_code, 201)
        self.assertEqual(hold.json['decision']['actor'], 'Ada Admin <User@Example>')

        reviewer = self.sign_in(mock_sub='rev-1', mock_name='Bea', mock_groups='desk-reviewer')
        self.assertEqual(reviewer.status_code, 302)
        self.assertEqual(self.client.get('/operator', base_url=SITE).json['operator']['role'],
                         'reviewer')
        refused = self.client.post('/payouts/reset', base_url=SITE, headers={'Origin': SITE})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json['code'], 'OPERATOR_FORBIDDEN')
        held = self.client.post(
            '/payouts/PO-1042/hold', json={'reviewer': 'Forged'}, base_url=SITE,
            headers={'Idempotency-Key': 'oidc-rev', 'Origin': SITE})
        self.assertEqual(held.status_code, 422)
        self.assertEqual(held.json['code'], 'REVIEWER_NOT_ACCEPTED')
        held = self.client.post(
            '/payouts/PO-1042/hold', json={}, base_url=SITE,
            headers={'Idempotency-Key': 'oidc-rev', 'Origin': SITE})
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.json['decision']['actor'], 'Bea <rev-1>')

        outsider = self.app.test_client()
        start = outsider.get('/auth/oidc/login', base_url=SITE)
        extra = urllib.parse.urlencode({
            'mock_sub': 'outsider', 'mock_name': 'No Group', 'mock_groups': ''})
        issued = outsider.get(start.headers['Location'] + '&' + extra, base_url=SITE)
        forbidden = outsider.get(issued.headers['Location'], base_url=SITE)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json['code'], 'OPERATOR_FORBIDDEN')
        again = outsider.get('/desk', base_url=SITE)
        self.assertEqual(again.status_code, 302)
        self.assertNotIn('desk_operator=', ''.join(again.headers.getlist('Set-Cookie')))
        self.assertEqual(self.client.get('/operator', base_url=SITE).json['operator']['role'],
                         'reviewer')
        self.assertEqual(list(Path(self.temp.name).rglob('operator.json')), [])

    def test_long_subject_can_decide_and_unsafe_subject_cannot(self):
        sub = 'S' + ('a' * 255)
        name = 'N' * 80
        self.assertEqual(len(f'{name} <{sub}>'), operators.MAX_ACTOR_LENGTH)
        done = self.sign_in(mock_sub=sub, mock_name=name, mock_groups='desk-admin')
        self.assertEqual(done.status_code, 302)
        self.client.get('/desk', base_url=SITE)
        hold = self.client.post(
            '/payouts/PO-1043/hold', json={}, base_url=SITE,
            headers={'Idempotency-Key': 'long-sub', 'Origin': SITE})
        self.assertEqual(hold.status_code, 201)
        self.assertEqual(hold.json['decision']['actor'], f'{name} <{sub}>')
        bad = self.sign_in(mock_sub='bad\nsub', mock_name='Nope', mock_groups='desk-admin')
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.json['code'], 'OPERATOR_UNAUTHORIZED')

    def test_callback_replay_and_provider_error(self):
        callback = self.authorize_url(mock_sub='once', mock_name='Once', mock_groups='desk-admin')
        first = self.client.get(callback, base_url=SITE)
        self.assertEqual(first.status_code, 302)
        second = self.client.get(callback, base_url=SITE)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(second.json['code'], 'OPERATOR_UNAUTHORIZED')
        start = self.client.get('/auth/oidc/login?next=https://evil.example/phish', base_url=SITE)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers['Location']).query))
        refused = self.client.get(
            '/auth/oidc/callback?error=access_denied&state=' + query['state'], base_url=SITE)
        self.assertEqual(refused.status_code, 401)

    def test_open_redirect_is_replaced_with_desk(self):
        start = self.client.get('/auth/oidc/login?next=https://evil.example', base_url=SITE)
        extra = urllib.parse.urlencode({
            'mock_sub': 'redir-2', 'mock_name': 'Redir', 'mock_groups': 'desk-reviewer'})
        issued = self.client.get(start.headers['Location'] + '&' + extra, base_url=SITE)
        done = self.client.get(issued.headers['Location'], base_url=SITE)
        self.assertEqual(done.status_code, 302)
        self.assertEqual(urllib.parse.urlsplit(done.headers['Location']).path, '/desk')
        self.assertNotIn('evil.example', done.headers['Location'])

    def test_bearer_overrides_session_and_unknown_bearer_does_not_fall_through(self):
        tokens = json.dumps({
            'tok-admin': {'id': 'op-token', 'name': 'Token Admin', 'role': 'admin'},
        })
        saved = self._without_oidc_env(oidc_env(**{operators.ENV_TOKENS: tokens}))
        app = create_app(self.temp.name + '-tok', SITE)
        try:
            client = app.test_client()
            opened = client.get('/desk', base_url=SITE)
            self.assertEqual(opened.status_code, 302)
            held = client.post(
                '/payouts/PO-1043/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-hold', 'Origin': SITE,
                         'Authorization': 'Bearer tok-admin'})
            self.assertEqual(held.status_code, 201)
            self.assertEqual(held.json['decision']['actor'], 'Token Admin <op-token>')
            self.assertEqual(list(Path(self.temp.name + '-tok').rglob('operator.json')), [])
            client.get(self._authorize(client, mock_sub='sess', mock_name='Sess',
                                       mock_groups='desk-admin'), base_url=SITE)
            bad = client.post(
                '/payouts/PO-1042/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-bad', 'Origin': SITE,
                         'Authorization': 'Bearer nope'})
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(bad.json['code'], 'OPERATOR_UNAUTHORIZED')
            still = client.post(
                '/payouts/PO-1042/hold', json={}, base_url=SITE,
                headers={'Idempotency-Key': 'tok-ok', 'Origin': SITE})
            self.assertEqual(still.status_code, 201)
            self.assertEqual(still.json['decision']['actor'], 'Sess <sess>')
        finally:
            app.extensions['close']()
            self._restore_env(saved)

    def _authorize(self, client, **identity):
        start = client.get('/auth/oidc/login', base_url=SITE)
        extra = urllib.parse.urlencode(identity)
        issued = client.get(start.headers['Location'] + '&' + extra, base_url=SITE)
        return issued.headers['Location']

    def test_session_cookie_is_not_the_tenant_cookie_or_the_database(self):
        done = self.sign_in(mock_sub='cookie-user', mock_name='Cookie', mock_groups='desk-admin')
        self.assertEqual(done.status_code, 302)
        cookies = done.headers.getlist('Set-Cookie')
        self.assertEqual(len(cookies), 1)
        self.assertIn('desk_operator=', cookies[0])
        self.assertNotIn('payments_demo=', cookies[0])
        self.assertIn('HttpOnly', cookies[0])
        self.assertIn('SameSite=Strict', cookies[0])
        self.assertIn('Secure', cookies[0])
        self.assertNotIn('.', cookies[0].split(';', 1)[0].split('=', 1)[1])
        page = self.client.get('/desk', base_url=SITE)
        demo_cookie = page.headers['Set-Cookie']
        self.assertIn('payments_demo=', demo_cookie)
        self.assertNotIn('desk_operator=', demo_cookie)
        session_id = cookies[0].split(';', 1)[0].split('=', 1)[1]
        database = next(Path(self.temp.name).rglob('payments.sqlite3')).read_bytes()
        self.assertNotIn(session_id.encode(), database)
        self.assertNotIn(SECRET.encode(), database)
        session_file = Path(self.temp.name) / '_operator_sessions' / 'sessions' / f'{session_id}.json'
        self.assertTrue(session_file.is_file())
        record = json.loads(session_file.read_text(encoding='utf-8'))
        self.assertEqual(record['operator_id'], 'cookie-user')
        self.assertNotIn('id_token', record)

    def test_custom_group_claim_does_not_default_to_admin(self):
        env = oidc_env(**{
            operator_oidc.ENV_GROUPS_CLAIM: 'teams',
            operator_oidc.ENV_ADMIN_GROUP: 'admins',
            operator_oidc.ENV_REVIEWER_GROUP: 'reviewers',
        })
        saved = self._without_oidc_env(env)
        app = create_app(self.temp.name + '-teams', SITE)
        try:
            client = app.test_client()
            missed = client.get(self._authorize(
                client, mock_sub='nope', mock_name='Nope', mock_groups='desk-admin'),
                base_url=SITE)
            self.assertEqual(missed.status_code, 403)
            ok = client.get(self._authorize(
                client, mock_sub='yes', mock_name='Yes', mock_groups='admins'),
                base_url=SITE)
            self.assertEqual(ok.status_code, 302)
            self.assertEqual(client.get('/operator', base_url=SITE).json['operator']['role'],
                             'admin')
        finally:
            app.extensions['close']()
            self._restore_env(saved)

    def test_real_issuer_without_a_provider_does_not_mint(self):
        saved = self._without_oidc_env(oidc_env(**{operator_oidc.ENV_MOCK: '0'}))
        app = create_app(self.temp.name + '-live', SITE)
        try:
            client = app.test_client()
            with mock.patch('operator_oidc.urllib.request.urlopen',
                            side_effect=urllib.error.URLError('down')):
                reply = client.get('/auth/oidc/login', base_url=SITE)
            self.assertEqual(reply.status_code, 401)
            self.assertEqual(reply.json['code'], 'OPERATOR_UNAUTHORIZED')
            self.assertEqual(list(Path(self.temp.name + '-live').rglob('operator.json')), [])
            mock_route = client.get('/auth/oidc/mock/authorize', base_url=SITE)
            self.assertEqual(mock_route.status_code, 404)
        finally:
            app.extensions['close']()
            self._restore_env(saved)

    def test_health_stays_anonymous(self):
        reply = self.client.get('/health', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        self.assertNotIn('Set-Cookie', reply.headers)


if __name__ == '__main__':
    unittest.main()
