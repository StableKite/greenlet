"""Offline release failure controls; no credentials or production HTTP requests."""
from pathlib import Path
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('greenlet_release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
carrier_spec = importlib.util.spec_from_file_location('greenlet_evidence_carrier', Path(__file__).with_name('prepare_evidence.py'))
carrier = importlib.util.module_from_spec(carrier_spec)
carrier_spec.loader.exec_module(carrier)


class Reply:
    def __init__(self, status=200, data=None, content='application/json', headers=None):
        self.status_code = status
        self.headers = {'Content-Type': content, **(headers or {})}
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.reply


class ReleaseControls(unittest.TestCase):
    def test_unchanged_push_skips_publication(self):
        self.assertEqual(release.release_decision('push', 'refs/heads/master', 'master', '1.2', '1.2'),
                         (False, 'unchanged-version'))

    def test_changed_default_push_publishes(self):
        self.assertEqual(release.release_decision('push', 'refs/heads/master', 'master', '1.2', '1.3'),
                         (True, 'version-change'))

    def test_feature_and_pull_request_never_publish(self):
        for event, ref in [('push', 'refs/heads/feature'), ('pull_request', 'refs/pull/1/merge'),
                           ('workflow_dispatch', 'refs/heads/feature'), ('push', 'refs/heads/Master')]:
            with self.subTest(event=event, ref=ref):
                self.assertFalse(release.release_decision(event, ref, 'master', '1.2', '1.3')[0])

    def test_manual_default_publish_is_explicit(self):
        self.assertTrue(release.release_decision('workflow_dispatch', 'refs/heads/master', 'master', None, '1.2')[0])

    def test_all_publication_phases_refuse_validation_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release.write(root / 'PLAN.json', {'should_publish': False})
            for function, extra in [(release.collision_release, ()), (release.validate_release, (root,)),
                                    (release.upload_release, ()), (release.confirm_release, ())]:
                with self.subTest(phase=function.__name__), self.assertRaisesRegex(ValueError, 'forbidden for validation-only'):
                    function(root, root, *extra)
            self.assertEqual({path.name for path in root.iterdir()}, {'PLAN.json'})

    def test_collision_checks_semantic_order_including_local_versions(self):
        release.check_collision(['1.9', '1.10rc1'], '1.10')
        for versions, candidate in [(['1.10'], '1.10'), (['1.11'], '1.10'),
                                    (['1.10+other'], '1.10'), (['1.10.post1'], '1.10')]:
            with self.subTest(versions=versions), self.assertRaises(ValueError):
                release.check_collision(versions, candidate)

    def test_invalid_server_version_is_not_treated_as_absent(self):
        with self.assertRaises(ValueError):
            release.check_collision(['not-a-version'], '1.10')

    def test_read_phases_reject_write_token_even_with_read_credential(self):
        with patch.dict(os.environ, {'DEVPI_TOKEN': 'offline-placeholder',
                                   'DEVPI_READ_TOKEN': 'offline-placeholder'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Write-only'):
                release.read_session()

    def test_missing_read_credential_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'separately authorized'):
            release.read_session()

    def test_read_session_tls_and_credential_separation(self):
        with patch.dict(os.environ, {'DEVPI_READ_TOKEN': 'offline-placeholder'}, clear=True):
            with release.read_session() as session:
                self.assertIs(session.verify, True)
                self.assertFalse(session.trust_env)
                self.assertEqual(session.auth, ('', 'offline-placeholder'))

    def test_only_canonical_https_urls_are_accepted(self):
        self.assertEqual(release.safe_url(release.INDEX + '+api'), release.INDEX + '+api')
        for url in ['http://devpi.stablekite.com/StableKite/index/+api',
                    'https://devpi.stablekite.com:4438/StableKite/index/+api',
                    'https://other.example/StableKite/index/+api',
                    'https://devpi.stablekite.com/StableKite/index/../other',
                    'https://devpi.stablekite.com/StableKite/index/%2e%2e/other',
                    'https://devpi.stablekite.com/StableKite/index/file?redirect=other']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                release.safe_url(url)

    def test_redirect_auth_html_and_invalid_schema_fail_closed(self):
        for reply in [Reply(302), Reply(401), Reply(403), Reply(500), Reply(content='text/html'),
                      Reply(data={'type': 'different', 'result': {}})]:
            session = Session(reply)
            with self.subTest(status=reply.status_code), self.assertRaises(ValueError):
                release.get_json(session, release.INDEX + '+api', 'apiconfig')
            self.assertIs(session.calls[0][1]['allow_redirects'], False)
            self.assertEqual(session.calls[0][1]['timeout'], (10, 30))

    def test_only_api_verified_404_means_project_absent(self):
        with self.assertRaises(ValueError):
            release.get_json(Session(Reply(404)), release.INDEX + 'greenlet', 'projectconfig', True)
        self.assertEqual(release.get_json(Session(Reply(404, headers={'X-DEVPI-API-VERSION': '2'})),
                         release.INDEX + 'greenlet', 'projectconfig', True), {})

    def test_authentication_must_confirm_identity_and_exact_index(self):
        for result in [{'authstatus': ['noauth', '', []], 'index': release.INDEX},
                       {'authstatus': ['ok', 'owner', []], 'index': 'https://other.example/index/'}]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                release.authenticate(Session(Reply(data={'type': 'apiconfig', 'result': result})))
        release.authenticate(Session(Reply(data={'type': 'apiconfig', 'result':
                             {'authstatus': ['ok', 'owner', []], 'index': release.INDEX.rstrip('/')}})))

    def test_cross_platform_manifest_paths_and_escape_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'distributions').mkdir()
            path = root / 'distributions/a.whl'
            path.write_bytes(b'not interpreted as a wheel')
            self.assertEqual(release.safe_file(root, 'distributions\\a.whl'), path.resolve())
            for relative in ['../a.whl', 'C:/a.whl', '/a.whl', 'distributions/../../a.whl']:
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    release.safe_file(root, relative)

    def test_missing_musl_targets_cannot_form_a_publish_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in release.TARGETS[:3]:
                (root / ('acceptance-' + target)).mkdir()
            with self.assertRaisesRegex(ValueError, 'All five exact'):
                release.collect_artifacts(root, {})

    def test_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'src/greenlet').mkdir(parents=True)
            (root / 'pyproject.toml').write_text('[project]\nname="greenlet"\nversion="1.2"\n')
            (root / 'src/greenlet/__init__.py').write_text('__version__ = "1.3"\n')
            with self.assertRaisesRegex(ValueError, 'versions differ'):
                release.source_version(root)

    def test_evidence_preserves_failed_receipts_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'original', root / 'carrier'
            source.mkdir()
            (source / 'RECEIPT.json').write_text('{"status":"FAILED","exit_code":1}\n')
            (source / 'native.whl').write_bytes(bytes(range(256)))
            (source / 'qualified-release/consumer/bin').mkdir(parents=True)
            if os.name == 'posix':
                (source / 'qualified-release/consumer/bin/python').symlink_to('/this-container-only-sdk-does-not-exist/python')
                (source / 'foreign-link').symlink_to(root / 'outside-original')
            else:
                (source / 'qualified-release/consumer/bin/python').write_bytes(b'excluded environment')
            before = {name: release.sha(source / name) for name in ('RECEIPT.json', 'native.whl')}
            manifest = carrier.prepare(source, destination)
            self.assertEqual({name: release.sha(source / name) for name in before}, before)
            self.assertEqual({name: release.sha(destination / name) for name in before}, before)
            self.assertEqual(release.read(destination / 'RECEIPT.json')['status'], 'FAILED')
            self.assertFalse((destination / 'qualified-release/consumer').exists())
            self.assertEqual(set(manifest['files']), set(before))
            self.assertIn('qualified-release/consumer', {item['path'] for item in manifest['excluded']})
            if os.name == 'posix':
                self.assertFalse((destination / 'foreign-link').exists())
                self.assertIn('foreign-link', {item['path'] for item in manifest['excluded']})

    def test_evidence_carrier_refuses_recursive_or_existing_destinations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'original'
            source.mkdir()
            for destination in (source, source / 'nested', root):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    carrier.prepare(source, destination)
            existing = root / 'existing'
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                carrier.prepare(source, existing)


if __name__ == '__main__':
    unittest.main(verbosity=2)
