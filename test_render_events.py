"""Closed POST /internal/render-events gate for Render deploy notifications."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import metrics
from hosted_app import create_app

SITE = 'https://demo.example'
SECRET = 'render-events-test-secret'
HEADER = 'X-Render-Events-Secret'
PATH = '/internal/render-events'
COUNTER = 'desk_render_deploy_event_total'


def _failed_deploy():
    return json.dumps({
        'type': 'deploy_ended',
        'timestamp': '2025-02-25T16:22:19Z',
        'data': {
            'id': 'evt-cuuuses015js70180jk0',
            'serviceId': 'srv-cukouhrtq21c73e9scng',
            'serviceName': 'disbursement-desk',
            'status': 'failed',
            'secret': SECRET,
        },
    }).encode()


class RenderEventsTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, SITE)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['close']()
        self.temp.cleanup()
        metrics.reset()

    def post(self, body=b'{}', headers=None, secret_env=None):
        env = {} if secret_env is None else {'RENDER_EVENTS_SECRET': secret_env}
        with mock.patch.dict(os.environ, env, clear=False):
            if secret_env is None:
                os.environ.pop('RENDER_EVENTS_SECRET', None)
            return self.client.post(
                PATH, data=body, base_url=SITE,
                content_type='application/json', headers=headers or {})

    def metrics_rows(self):
        reply = self.client.get('/metrics', base_url=SITE)
        self.assertEqual(reply.status_code, 200)
        return reply.get_json()['metrics'][COUNTER]

    def log_text(self):
        path = Path(self.temp.name) / '_server_logs' / 'api.log'
        if not path.is_file():
            return ''
        return path.read_text(encoding='utf-8')

    def render_logs(self):
        rows = []
        for line in self.log_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('event') == 'render_deploy_event':
                rows.append(row)
        return rows

    def test_unset_secret_matches_an_unknown_path(self):
        missing = self.client.post('/not-a-route', data=b'{}', base_url=SITE)
        for method in ('POST', 'GET', 'OPTIONS'):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('RENDER_EVENTS_SECRET', None)
                closed = self.client.open(PATH, method=method, data=b'{}', base_url=SITE)
            self.assertEqual(closed.status_code, 404)
            self.assertEqual(closed.get_json(), missing.get_json())
            self.assertNotIn('Set-Cookie', closed.headers)
        with mock.patch.dict(os.environ, {'RENDER_EVENTS_SECRET': '   '}, clear=False):
            blank = self.client.post(PATH, data=_failed_deploy(), base_url=SITE,
                                     headers={HEADER: SECRET})
        self.assertEqual(blank.status_code, 404)
        self.assertEqual(blank.get_json(), missing.get_json())
        self.assertEqual(self.metrics_rows(), [])
        self.assertEqual(self.render_logs(), [])
        self.assertEqual(list(Path(self.temp.name).rglob('payments.sqlite3')), [])

    def test_wrong_header_stays_closed(self):
        missing = self.client.post('/not-a-route', data=b'{}', base_url=SITE)
        for headers in ({}, {HEADER: 'nope'}, {HEADER: SECRET + 'x'}):
            reply = self.post(_failed_deploy(), headers=headers, secret_env=SECRET)
            self.assertEqual(reply.status_code, 404)
            self.assertEqual(reply.get_json(), missing.get_json())
            self.assertNotIn(SECRET, reply.get_data(as_text=True))
        self.assertEqual(self.metrics_rows(), [])
        self.assertEqual(self.render_logs(), [])
        self.assertNotIn(SECRET, self.log_text())

    def test_accepted_deploy_failure_is_counted_and_logged(self):
        reply = self.post(_failed_deploy(), headers={HEADER: f'  {SECRET}  '},
                          secret_env=f'{SECRET}\n')
        self.assertEqual(reply.status_code, 204)
        self.assertEqual(reply.data, b'')
        self.assertNotIn('Set-Cookie', reply.headers)
        rows = self.metrics_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['value'], 1)
        self.assertEqual(rows[0]['labels'], {
            'event_type': 'deploy_ended',
            'status': 'failed',
        })
        logged = self.render_logs()
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]['event_type'], 'deploy_ended')
        self.assertEqual(logged[0]['status'], 'failed')
        self.assertEqual(logged[0]['service_id'], 'srv-cukouhrtq21c73e9scng')
        self.assertEqual(logged[0]['service_name'], 'disbursement-desk')
        self.assertEqual(logged[0]['render_event_id'], 'evt-cuuuses015js70180jk0')
        self.assertNotIn('secret', logged[0])
        self.assertNotIn(SECRET, self.log_text())
        self.assertEqual(list(Path(self.temp.name).rglob('payments.sqlite3')), [])
        again = self.post(_failed_deploy(), headers={HEADER: SECRET}, secret_env=SECRET)
        self.assertEqual(again.status_code, 204)
        self.assertEqual(self.metrics_rows()[0]['value'], 2)
        page = self.client.get('/desk', base_url=SITE)
        self.assertNotIn(PATH, page.get_data(as_text=True))

    def test_bad_json_does_not_count(self):
        reply = self.post(b'not-json', headers={HEADER: SECRET}, secret_env=SECRET)
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(reply.get_json()['error'], 'Invalid render event.')
        self.assertNotIn(SECRET, reply.get_data(as_text=True))
        array = self.post(b'[]', headers={HEADER: SECRET}, secret_env=SECRET)
        self.assertEqual(array.status_code, 400)
        self.assertEqual(self.metrics_rows(), [])
        self.assertEqual(self.render_logs(), [])

    def test_unknown_type_and_unsafe_name_are_bounded(self):
        body = json.dumps({
            'type': 'deploy_failed',
            'data': {
                'status': 'exploded',
                'serviceName': 'bad\nname ' + SECRET,
                'serviceId': 'not a service',
            },
        }).encode()
        reply = self.post(body, headers={HEADER: SECRET}, secret_env=SECRET)
        self.assertEqual(reply.status_code, 204)
        rows = self.metrics_rows()
        self.assertEqual(rows[0]['labels'], {'event_type': 'other'})
        self.assertEqual(rows[0]['value'], 1)
        logged = self.render_logs()[0]
        self.assertEqual(logged['event_type'], 'other')
        self.assertNotIn('status', logged)
        self.assertNotIn('service_name', logged)
        self.assertNotIn('service_id', logged)
        self.assertNotIn(SECRET, self.log_text())
        failed = self.post(
            json.dumps({'type': 'server_failed', 'data': {'id': 'evt-1'}}).encode(),
            headers={HEADER: SECRET}, secret_env=SECRET)
        self.assertEqual(failed.status_code, 204)
        labels = {tuple(sorted(row['labels'].items())): row['value']
                  for row in self.metrics_rows()}
        self.assertEqual(labels[(('event_type', 'other'),)], 1)
        self.assertEqual(labels[(('event_type', 'server_failed'),)], 1)

    def test_other_methods_need_the_secret_and_then_reject(self):
        missing = self.client.get('/not-a-route', base_url=SITE)
        with mock.patch.dict(os.environ, {'RENDER_EVENTS_SECRET': SECRET}, clear=False):
            hidden = self.client.get(PATH, base_url=SITE)
            refused = self.client.get(PATH, base_url=SITE, headers={HEADER: SECRET})
            posted = self.client.open(PATH, method='OPTIONS', base_url=SITE,
                                      headers={HEADER: SECRET})
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.get_json(), missing.get_json())
        self.assertEqual(refused.status_code, 405)
        self.assertEqual(posted.status_code, 405)
        self.assertEqual(self.metrics_rows(), [])
        self.assertEqual(self.render_logs(), [])


if __name__ == '__main__':
    unittest.main()
