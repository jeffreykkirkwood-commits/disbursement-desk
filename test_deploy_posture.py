# Automated checks for P1 deploy posture helpers (ephemeral DEMO_DIRECTORY warn).
"""Exercise deploy_posture without contacting Render."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deploy_posture
from hosted_app import create_app


class DeployPostureTests(unittest.TestCase):
    def test_tmp_paths_are_ephemeral(self):
        self.assertTrue(deploy_posture.demo_directory_is_ephemeral('/tmp/payments-demos'))
        self.assertTrue(deploy_posture.demo_directory_is_ephemeral('/var/tmp/x'))

    def test_non_tmp_paths_are_not_ephemeral(self):
        self.assertFalse(deploy_posture.demo_directory_is_ephemeral('/var/data/payments-demos'))

    def test_warn_skips_when_not_on_render(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('RENDER', None)
            self.assertFalse(
                deploy_posture.warn_ephemeral_demo_directory('/tmp/payments-demos'))

    def test_warn_emits_on_render_tmp(self):
        with mock.patch.dict(os.environ, {'RENDER': 'true'}, clear=False):
            with mock.patch.object(deploy_posture, 'event') as logged:
                self.assertTrue(
                    deploy_posture.warn_ephemeral_demo_directory('/tmp/payments-demos'))
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[0],
                         'deploy_posture_ephemeral_demo_directory')

    def test_warn_skips_durable_path_on_render(self):
        with mock.patch.dict(os.environ, {'RENDER': 'true'}, clear=False):
            with mock.patch.object(deploy_posture, 'event') as logged:
                self.assertFalse(
                    deploy_posture.warn_ephemeral_demo_directory('/var/data/payments-demos'))
        logged.assert_not_called()

    def test_secret_env_keys_cover_stripe_and_operators(self):
        keys = deploy_posture.secret_env_keys()
        self.assertIn('DESK_OPERATOR_TOKENS', keys)
        self.assertIn('STRIPE_SECRET_KEY', keys)
        self.assertIn('TURSO_AUTH_TOKEN', keys)

    def test_create_app_warns_on_render_tmp(self):
        temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temp.cleanup)
        with mock.patch.dict(os.environ, {
            'RENDER': 'true',
            'PUBLIC_ORIGIN': 'https://demo.example',
        }, clear=False):
            with mock.patch.object(deploy_posture, 'event') as logged:
                app = create_app(temp.name, 'https://demo.example')
                self.addCleanup(app.extensions['close'])
        names = [call.args[0] for call in logged.call_args_list]
        self.assertIn('deploy_posture_ephemeral_demo_directory', names)

    def test_render_yaml_declares_health_and_secrets(self):
        text = Path('render.yaml').read_text(encoding='utf-8')
        self.assertIn('healthCheckPath: /health', text)
        self.assertIn('name: disbursement-desk', text)
        self.assertIn('plan: free', text)
        docs = Path('docs/deploy-posture.md').read_text(encoding='utf-8')
        readme = Path('README.md').read_text(encoding='utf-8')
        for key in deploy_posture.SECRET_ENV_KEYS:
            self.assertIn(f'key: {key}\n        sync: false', text)
            self.assertIn(key, docs)
        self.assertIn('RENDER_EVENTS_SECRET', readme)
        self.assertNotIn('\ndisk:', text)  # free plan cannot attach a disk


if __name__ == '__main__':
    unittest.main()
