"""Offline release failure controls; no credentials or production HTTP requests."""
from pathlib import Path
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('greenlet_release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
carrier_spec = importlib.util.spec_from_file_location('greenlet_evidence_carrier', Path(__file__).with_name('prepare_evidence.py'))
carrier = importlib.util.module_from_spec(carrier_spec)
carrier_spec.loader.exec_module(carrier)
libraries_spec = importlib.util.spec_from_file_location('greenlet_wheel_libraries', Path(__file__).with_name('copy_wheel_support_libs.py'))
libraries = importlib.util.module_from_spec(libraries_spec)
libraries_spec.loader.exec_module(libraries)


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
    def test_original_ci_selects_exact_repository_workflow_head_and_branch(self):
        good = {'id': 1, 'run_number': 10, 'run_attempt': 1, 'head_sha': 'a' * 40,
                'head_branch': 'master', 'event': 'push', 'workflow_id': 7,
                'repository': {'full_name': 'StableKite/greenlet'},
                'status': 'completed', 'conclusion': 'success'}
        other = [dict(good, **change) for change in (
            {'head_sha': 'b' * 40}, {'head_branch': 'feature'},
            {'event': 'pull_request'}, {'workflow_id': 8},
            {'repository': {'full_name': 'other/greenlet'}})]
        for wrong in other:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                release.compatibility_run([wrong], 'a' * 40, 'master', 7)
        self.assertIs(release.compatibility_run(other + [good], 'a' * 40, 'master', 7), good)

    def test_original_ci_never_falls_back_to_an_older_success(self):
        old = {'id': 1, 'run_number': 10, 'run_attempt': 1, 'head_sha': 'a' * 40,
               'head_branch': 'master', 'event': 'push', 'workflow_id': 7,
               'repository': {'full_name': 'StableKite/greenlet'},
               'status': 'completed', 'conclusion': 'success'}
        for status, conclusion in [('completed', 'failure'), ('in_progress', None),
                                   ('completed', 'cancelled'), ('completed', 'skipped')]:
            latest = dict(old, id=2, run_attempt=2, status=status, conclusion=conclusion)
            with self.subTest(conclusion=conclusion):
                self.assertIs(release.compatibility_run([latest, old], 'a' * 40, 'master', 7), latest)
        with self.assertRaises(ValueError):
            release.compatibility_run([], 'a' * 40, 'master', 7)

    def test_compatibility_phase_refuses_validation_only_before_any_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release.write(root / 'PLAN.json', {'should_publish': False})
            with patch('requests.Session') as session, self.assertRaisesRegex(ValueError, 'validation-only'):
                release.compatibility_release(root, root)
            session.assert_not_called()

    def test_live_compatibility_phase_records_success_or_refuses_actual_failures(self):
        plan = {'git_head': 'a' * 40, 'default_branch': 'master'}
        for conclusion in ('success', 'failure', 'cancelled', 'skipped', 'timed_out'):
            run = {'id': 1, 'run_number': 10, 'run_attempt': 1, 'head_sha': 'a' * 40,
                   'head_branch': 'master', 'event': 'push', 'workflow_id': 7,
                   'repository': {'full_name': 'StableKite/greenlet'},
                   'status': 'completed', 'conclusion': conclusion, 'html_url': 'https://github.com/StableKite/greenlet/actions/runs/1'}
            with tempfile.TemporaryDirectory() as temporary, self.subTest(conclusion=conclusion):
                root = Path(temporary)
                with patch.object(release, 'checked_plan', return_value=plan), \
                     patch.dict(os.environ, {}, clear=True), patch('requests.Session') as factory:
                    factory.return_value.__enter__.return_value.get.side_effect = [
                        Reply(data={'id': 7, 'path': '.github/workflows/tests.yml', 'state': 'active'}),
                        Reply(data={'total_count': 1, 'workflow_runs': [run]})]
                    if conclusion == 'success':
                        release.compatibility_release(root, root)
                    else:
                        with self.assertRaisesRegex(ValueError, 'did not succeed'):
                            release.compatibility_release(root, root)
                proof = release.read(root / 'ORIGINAL_COMPATIBILITY.json')
                self.assertEqual(proof['conclusion'], conclusion)
                self.assertEqual(proof['git_head'], plan['git_head'])

    def test_all_devpi_phases_require_the_exact_successful_compatibility_receipt(self):
        head = 'a' * 40
        plan = {'should_publish': True, 'git_head': head, 'ref': 'refs/heads/master',
                'default_branch': 'master', 'current_version': '1.3', 'endpoint': release.INDEX}
        env = {'GITHUB_SHA': head, 'GITHUB_REF': 'refs/heads/master'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release.write(root / 'PLAN.json', plan)
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(release, 'git', return_value=head), \
                 patch.object(release, 'source_version', return_value='1.3'):
                phases = ((release.collision_release, ()), (release.validate_release, (root,)),
                          (release.upload_release, ()), (release.confirm_release, ()))
                for function, extra in phases:
                    with self.subTest(phase=function.__name__), self.assertRaises(FileNotFoundError):
                        function(root, root, *extra)
                good = {'git_head': head, 'branch': 'master',
                        'workflow': '.github/workflows/tests.yml', 'conclusion': 'success'}
                for change in ({'git_head': 'b' * 40}, {'branch': 'feature'},
                               {'workflow': 'other.yml'}, {'conclusion': 'failure'}):
                    with patch.object(release, 'read', side_effect=[plan, dict(good, **change)]), \
                         self.subTest(change=change), self.assertRaises(ValueError):
                        release.checked_plan(root, root)
                release.write(root / 'ORIGINAL_COMPATIBILITY.json', good)
                self.assertEqual(release.checked_plan(root, root), plan)

    def test_pending_original_ci_times_out_without_a_success_receipt(self):
        run = {'id': 1, 'run_number': 10, 'run_attempt': 1, 'head_sha': 'a' * 40,
               'head_branch': 'master', 'event': 'push', 'workflow_id': 7,
               'repository': {'full_name': 'StableKite/greenlet'},
               'status': 'in_progress', 'conclusion': None}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(release, 'checked_plan', return_value={
                    'git_head': 'a' * 40, 'default_branch': 'master'}), \
                 patch.dict(os.environ, {}, clear=True), patch('requests.Session') as factory, \
                 patch.object(release.time, 'monotonic', side_effect=[0, 1801]), \
                 patch.object(release.time, 'sleep') as sleep:
                factory.return_value.__enter__.return_value.get.side_effect = [
                    Reply(data={'id': 7, 'path': '.github/workflows/tests.yml', 'state': 'active'}),
                    Reply(data={'total_count': 1, 'workflow_runs': [run]})]
                with self.assertRaisesRegex(ValueError, 'still pending'):
                    release.compatibility_release(root, root)
                sleep.assert_not_called()
            self.assertFalse((root / 'ORIGINAL_COMPATIBILITY.json').exists())

    def test_original_ci_metadata_failure_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(release, 'checked_plan', return_value={
                    'git_head': 'a' * 40, 'default_branch': 'master'}), \
                 patch.dict(os.environ, {}, clear=True), patch('requests.Session') as factory:
                factory.return_value.__enter__.return_value.get.return_value = Reply(status=403)
                with self.assertRaisesRegex(ValueError, 'metadata unavailable'):
                    release.compatibility_release(root, root)
            self.assertFalse((root / 'ORIGINAL_COMPATIBILITY.json').exists())

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

    def test_wheel_support_libraries_keep_the_installed_relative_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed, fixture = root / 'installed', root / 'fixture'
            installed.mkdir()
            fixture.mkdir()
            wheel = root / 'layout-control.whl'
            payload = b'layout fixture, not a native runtime acceptance wheel'
            (installed / 'greenlet.libs').mkdir()
            (installed / 'greenlet.libs/libexample-hash.so.6').write_bytes(payload)
            with zipfile.ZipFile(wheel, 'x') as archive:
                archive.writestr('greenlet.libs/libexample-hash.so.6', payload)
                archive.writestr('greenlet/original_test.py', b'original source must remain unchanged')
            result = libraries.copy_support_libraries(wheel, installed, fixture)
            self.assertEqual(set(result), {'greenlet.libs/libexample-hash.so.6'})
            self.assertEqual((fixture / 'greenlet.libs/libexample-hash.so.6').read_bytes(), payload)
            self.assertFalse((fixture / 'greenlet/original_test.py').exists())

    def test_changed_or_escaping_support_library_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed, fixture = root / 'installed', root / 'fixture'
            (installed / 'greenlet.libs').mkdir(parents=True)
            fixture.mkdir()
            (installed / 'greenlet.libs/libexample.so').write_bytes(b'changed after installation')
            for name in ('greenlet.libs/libexample.so', 'greenlet.libs/../outside.so'):
                wheel = root / ('changed.whl' if '..' not in name else 'escaping.whl')
                with zipfile.ZipFile(wheel, 'x') as archive:
                    archive.writestr(name, b'original wheel bytes')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    libraries.copy_support_libraries(wheel, installed, fixture)
            self.assertEqual(list(fixture.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
