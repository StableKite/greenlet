"""Sixth-job release gates. Credentials are confined to separate network phases."""
from pathlib import Path, PurePosixPath
import argparse
import ast
import base64
import csv
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import time
import tomllib
from urllib.parse import quote, unquote, urlsplit
import zipfile

INDEX = 'https://devpi.stablekite.com/StableKite/index/'
TARGETS = ('windows-x86_64', 'glibc-x86_64', 'glibc-aarch64',
           'musl-x86_64', 'musl-aarch64')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args],
                                   stderr=subprocess.PIPE).decode().strip()


def literal_version(text):
    values = [node.value.value for node in ast.parse(text).body
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and any(isinstance(name, ast.Name) and name.id == '__version__'
                      for name in node.targets)]
    require(len(values) == 1 and isinstance(values[0], str), 'Expected one literal __version__.')
    return values[0]


def source_version(source):
    project = tomllib.loads((source / 'pyproject.toml').read_text())['project']
    version = project['version']
    require(isinstance(version, str) and project['name'] == 'greenlet', 'Canonical project metadata mismatch.')
    require(version == literal_version((source / 'src/greenlet/__init__.py').read_text()),
            'Canonical and runtime versions differ.')
    return version


def previous_version(source, commit):
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None and set(commit) != {'0'},
            'A real previous commit is required for a push.')
    project = tomllib.loads(git(source, 'show', commit + ':pyproject.toml'))
    runtime = literal_version(git(source, 'show', commit + ':src/greenlet/__init__.py'))
    canonical = project.get('project', {}).get('version')
    if canonical is not None:
        require(canonical == runtime, 'Previous canonical and runtime versions differ.')
        return canonical, 'pyproject.toml project.version'
    # The pre-port upstream default used only __version__; never substitute a
    # guessed previous version when adopting the canonical static metadata.
    return runtime, 'legacy literal src/greenlet/__init__.py __version__'


def release_decision(event_name, ref, default, previous, current):
    on_default = ref == 'refs/heads/' + default
    publish = on_default and (event_name == 'workflow_dispatch' or
                             (event_name == 'push' and previous != current))
    reason = ('manual-default-publish' if publish and event_name == 'workflow_dispatch'
              else 'version-change' if publish else 'unchanged-version' if
              event_name == 'push' and previous == current else 'non-publishing-ref-or-event')
    return publish, reason


def runtime_policy():
    import urllib.request
    require('PYTHON_GIL' not in os.environ and sys.flags.gil is None and
            not sys._is_gil_enabled(), 'Natural GIL OFF is required.')
    require(sysconfig.get_config_var('Py_GIL_DISABLED') == 1 and
            sys.version_info.releaselevel == 'final', 'Stable free-threaded CPython required.')
    require(os.environ.get('PYTHON_JIT') == '1', 'PYTHON_JIT=1 required.')
    available = sys._jit.is_available()
    require(not available or sys._jit.is_enabled(), 'Available JIT must be enabled.')
    with urllib.request.urlopen('https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false', timeout=30) as response:
        versions = [tuple(map(int, match[1].split('.'))) for item in json.load(response)
                    if item['is_published'] and not item['pre_release'] and
                    (match := re.fullmatch(r'Python (3\.\d+\.\d+)', item['name']))]
    require(sys.version_info[:3] == max(versions), 'Publisher must retain newest stable FT runtime.')
    return {'status': 'PASS_LATEST_STABLE_FT_RUNTIME_POLICY',
            'version': sys.version, 'latest_final': max(versions), 'GIL': False,
            'gil_flag': None, 'JIT_mode': 'enabled' if available else
            'unavailable in selected build; latest stable t retained'}


def plan_release(source, out):
    event = read(os.environ['GITHUB_EVENT_PATH'])
    event_name, ref = os.environ['GITHUB_EVENT_NAME'], os.environ['GITHUB_REF']
    require(event['repository']['full_name'] == os.environ['GITHUB_REPOSITORY'] ==
            'StableKite/greenlet', 'Unexpected repository.')
    head = os.environ['GITHUB_SHA']
    require(git(source, 'rev-parse', 'HEAD') == head, 'Checkout/head mismatch.')
    default = event['repository']['default_branch']
    current = source_version(source)
    previous, origin = (previous_version(source, event['before']) if event_name == 'push'
                        else (None, 'not a push'))
    publish, reason = release_decision(event_name, ref, default, previous, current)
    result = {'status': 'PUBLISH_PLANNED' if publish else 'PASS_VALIDATION_ONLY_NO_REPUBLISH',
              'git_head': head, 'default_branch': default, 'event': event_name, 'ref': ref,
              'previous_version': previous, 'previous_version_origin': origin,
              'current_version': current, 'should_publish': publish, 'reason': reason,
              'runtime': runtime_policy(), 'endpoint': INDEX}
    write(out / 'PLAN.json', result)
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        stream.write('should_publish=' + str(publish).lower() + '\n')
    print(json.dumps(result))


def checked_plan(source, out, *, compatibility_required=True):
    plan = read(out / 'PLAN.json')
    require(plan['should_publish'] is True, 'Network/artifact publication phase forbidden for validation-only.')
    require(plan['git_head'] == os.environ['GITHUB_SHA'] == git(source, 'rev-parse', 'HEAD'),
            'Publication plan/head mismatch.')
    require(plan['ref'] == os.environ['GITHUB_REF'] == 'refs/heads/' + plan['default_branch'],
            'Publication must use the exact default branch.')
    require(plan['current_version'] == source_version(source) and plan['endpoint'] == INDEX,
            'Publication metadata changed after planning.')
    if compatibility_required:
        proof = read(out / 'ORIGINAL_COMPATIBILITY.json')
        require(proof['git_head'] == plan['git_head'] and
                proof['branch'] == plan['default_branch'] and
                proof['workflow'] == '.github/workflows/tests.yml' and
                proof['conclusion'] == 'success',
                'Successful original compatibility CI for this exact head is required.')
    return plan


def compatibility_run(runs, head, branch, workflow_id):
    matches = [run for run in runs if run.get('head_sha') == head and
               run.get('head_branch') == branch and run.get('event') == 'push' and
               run.get('workflow_id') == workflow_id and
               run.get('repository', {}).get('full_name') == 'StableKite/greenlet']
    require(bool(matches), 'No original push compatibility run for this exact head.')
    # Never replace a newer failing/pending run with an older successful one.
    return max(matches, key=lambda run: (run['run_number'], run['run_attempt']))


def compatibility_release(source, out):
    plan = checked_plan(source, out, compatibility_required=False)
    require('DEVPI_TOKEN' not in os.environ and 'DEVPI_READ_TOKEN' not in os.environ,
            'Devpi credentials are forbidden during compatibility validation.')
    import requests
    base = 'https://api.github.com/repos/StableKite/greenlet/'
    deadline = time.monotonic() + 1800
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({'Accept': 'application/vnd.github+json',
                                'X-GitHub-Api-Version': '2022-11-28'})
        token = os.environ.get('GH_TOKEN')
        if token:
            session.headers['Authorization'] = 'Bearer ' + token

        def fetch(path, params=None):
            response = session.get(base + path, params=params, timeout=(10, 30),
                                   allow_redirects=False)
            require(response.status_code == 200, 'Original compatibility metadata unavailable.')
            return response.json()

        workflow = fetch('actions/workflows/tests.yml')
        require(workflow['path'] == '.github/workflows/tests.yml' and
                workflow['state'] == 'active', 'Original compatibility workflow mismatch.')
        while True:
            data = fetch('actions/workflows/tests.yml/runs', {
                'head_sha': plan['git_head'], 'branch': plan['default_branch'],
                'event': 'push', 'per_page': 100})
            require(data['total_count'] <= 100, 'Ambiguous compatibility run history.')
            run = compatibility_run(data['workflow_runs'], plan['git_head'],
                                    plan['default_branch'], workflow['id'])
            if run['status'] == 'completed':
                proof = {'git_head': plan['git_head'], 'branch': plan['default_branch'],
                         'workflow': workflow['path'], 'workflow_id': workflow['id'],
                         'run_id': run['id'], 'run_attempt': run['run_attempt'],
                         'url': run['html_url'], 'conclusion': run['conclusion'],
                         'checked_utc': datetime.now(timezone.utc).isoformat()}
                write(out / 'ORIGINAL_COMPATIBILITY.json', proof)
                require(run['conclusion'] == 'success', 'Original compatibility CI did not succeed.')
                return
            require(time.monotonic() < deadline, 'Original compatibility CI is still pending; publication withheld.')
            time.sleep(15)


def safe_file(root, relative):
    # Existing Windows producer manifests use native path separators.
    relative = relative.replace('\\', '/')
    name = PurePosixPath(relative)
    require(not name.is_absolute() and '..' not in name.parts and ':' not in relative,
            'Unsafe artifact path.')
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()) and path.is_file() and not (root / relative).is_symlink(),
            'Artifact missing or outside its root.')
    return path


def check_wheel(path, version, target, abi):
    from packaging.utils import parse_wheel_filename
    from packaging.tags import parse_tag
    name, parsed, _, tags = parse_wheel_filename(path.name)
    require(name == 'greenlet' and str(parsed) == version, 'Wheel filename metadata mismatch.')
    architecture = 'aarch64' if target.endswith('aarch64') else 'x86_64'
    for tag in tags:
        platform_ok = (tag.platform == 'win_amd64' if target == 'windows-x86_64' else
                       re.fullmatch(r'manylinux_\d+_\d+_' + architecture, tag.platform) is not None
                       if target.startswith('glibc-') else
                       re.fullmatch(r'musllinux_\d+_\d+_' + architecture, tag.platform) is not None)
        require(tag.interpreter == abi and tag.abi == abi + 't' and platform_ok,
                'Wrong FT ABI, architecture or unrepaired platform wheel.')
    with zipfile.ZipFile(path) as archive:
        files = [item.filename for item in archive.infolist() if not item.is_dir()]
        require(len(files) == len(set(files)), 'Duplicate wheel members.')
        require(all(not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts
                    and '\\' not in name for name in files), 'Unsafe wheel member.')
        metadata, = [name for name in files if name.endswith('.dist-info/METADATA')]
        wheel, = [name for name in files if name.endswith('.dist-info/WHEEL')]
        record, = [name for name in files if name.endswith('.dist-info/RECORD')]
        package = BytesParser().parsebytes(archive.read(metadata))
        require(package['Name'] == 'greenlet' and package['Version'] == version,
                'Internal wheel metadata mismatch.')
        wheel_tags = set().union(*(parse_tag(tag) for tag in
                                 BytesParser().parsebytes(archive.read(wheel)).get_all('Tag', [])))
        require(wheel_tags == tags, 'Wheel filename/internal tags differ.')
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
        require(len(rows) == len({row[0] for row in rows}) and {row[0] for row in rows} == set(files),
                'Wheel RECORD does not cover the exact payload.')
        for name, encoded, size in rows:
            if name == record:
                require(encoded == size == '', 'RECORD self-entry must be unhashed.')
                continue
            data = archive.read(name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            require(encoded == 'sha256=' + digest and size == str(len(data)), 'Wheel RECORD checksum mismatch.')
    return {'path': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size,
            'target': target, 'tags': sorted(str(tag) for tag in tags)}


def collect_artifacts(artifacts, plan):
    require({path.name for path in artifacts.iterdir()} == {'acceptance-' + target for target in TARGETS},
            'All five exact platform artifacts are required; extras/missing targets rejected.')
    selected = []
    canonical = None
    abi = 'cp' + ''.join(map(str, sys.version_info[:2]))
    for target in TARGETS:
        root = artifacts / ('acceptance-' + target)
        if target.startswith('musl-'):
            root = root / 'acceptance'
        receipt, runtime = read(root / 'RECEIPT.json'), read(root / 'RUNTIME.json')
        require(receipt['status'] == 'PASSED_ALL_DECLARED_COMPONENT_CHECKS' and
                receipt['stages'] and all(stage.get('exit_code') == 0 for stage in receipt['stages']),
                'Platform acceptance has not passed.')
        require(receipt['GIL'] is False and receipt['gil_flag'] is None and
                runtime['status'] == 'PASS_LATEST_STABLE_FT_RUNTIME_POLICY' and
                runtime['latest_final'] == plan['runtime']['latest_final'] and
                runtime['GIL'] is False and runtime['gil_flag'] is None and
                runtime['JIT_mode'] in ('enabled', 'unavailable in selected build; latest stable t retained'),
                'Platform runtime policy mismatch.')
        require(receipt['source_manifest_sha256'] == sha(root / 'SOURCE_SHA256.json'),
                'Source manifest changed.')
        manifest = read(root / 'PUBLISH_ARTIFACTS.json')
        require(manifest['status'] == 'QUALIFIED_ARTIFACTS_NOT_YET_PUBLISHED' and
                manifest['git_head'] == plan['git_head'] and
                manifest['version'] == plan['current_version'], 'Artifact provenance mismatch.')
        require(len(manifest['files']) == 2, 'Exactly one qualified wheel and one source archive per platform.')
        files = []
        for relative, expected in manifest['files'].items():
            path = safe_file(root, relative)
            require(path.stat().st_size == expected['bytes'] and sha(path) == expected['sha256'],
                    'Declared artifact checksum/size mismatch.')
            files.append(path)
        wheel, = [path for path in files if path.suffix == '.whl']
        sdist, = [path for path in files if path.name.endswith('.tar.gz')]
        qualified = read(root / 'qualified-release/RESULT.json')
        require(qualified['status'] == 'PASS_ABI_DISTRIBUTABLE_RECORD_FULL_SUITE_AND_REAL_SI' and
                qualified['distributable']['sha256'] == sha(wheel), 'Distributable runtime gate mismatch.')
        selected.append(check_wheel(wheel, plan['current_version'], target, abi))
        if target == 'glibc-x86_64':
            canonical = root
            with tarfile.open(sdist, 'r:gz') as archive:
                members = archive.getmembers()
                require(all(not PurePosixPath(item.name).is_absolute() and
                            '..' not in PurePosixPath(item.name).parts and
                            not item.issym() and not item.islnk() for item in members), 'Unsafe sdist member.')
                info, = [item for item in members if item.name.count('/') == 1 and item.name.endswith('/PKG-INFO')]
                metadata = BytesParser().parsebytes(archive.extractfile(info).read())
                require(metadata['Name'] == 'greenlet' and metadata['Version'] == plan['current_version'],
                        'Source distribution metadata mismatch.')
            selected.append({'path': str(sdist), 'sha256': sha(sdist),
                             'bytes': sdist.stat().st_size, 'target': 'canonical-source'})
    require(len(selected) == 6 and len({Path(item['path']).name for item in selected}) == 6,
            'Publish set must be five distinct ABI-qualified wheels and one canonical source archive.')
    return selected, canonical


def run_logged(out, name, argv, cwd, timeout):
    with (out / (name + '.stdout.log')).open('xb') as stdout, (out / (name + '.stderr.log')).open('xb') as stderr:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, stdout=stdout, stderr=stderr,
                                timeout=timeout, shell=False)
    write(out / (name + '.command.json'), {'argv': list(map(str, argv)), 'cwd': str(cwd),
          'exit_code': result.returncode, 'stdout_sha256': sha(out / (name + '.stdout.log')),
          'stderr_sha256': sha(out / (name + '.stderr.log'))})
    require(result.returncode == 0, 'Integration command failed: ' + name)


def validate_release(source, out, artifacts):
    plan = checked_plan(source, out)
    require(read(out / 'COLLISION.json')['status'] == 'PASS_AUTHENTICATED_COLLISION_CHECK',
            'Authenticated preflight must precede artifact validation.')
    files, canonical = collect_artifacts(artifacts, plan)
    release = out / 'distributions'
    release.mkdir()
    for item in files:
        target = release / Path(item['path']).name
        shutil.copyfile(item['path'], target)
        item['path'] = str(target)
    run_logged(out, 'twine-check', [sys.executable, '-m', 'twine', 'check', *[item['path'] for item in files]], source, 30)
    consumer = out / 'consumer'
    python = consumer / 'bin/python'
    run_logged(out, 'consumer-create', [sys.executable, '-I', '-B', '-m', 'venv', consumer], source, 90)
    wheel, = [item['path'] for item in files if item['target'] == 'glibc-x86_64']
    run_logged(out, 'ordinary-pip-install', [python, '-m', 'pip', 'install', wheel + '[test]'], source, 120)
    tools = source / 'tools/acceptance'
    client = out / 'native-client'
    shutil.copytree(tools / 'native_client', client)
    shutil.copyfile(source / 'src/greenlet/greenlet.h', client / 'greenlet.h')
    run_logged(out, 'original-header-C-client', [python, '-I', '-B', 'setup.py', 'build_ext', '--inplace'], client, 90)
    run_logged(out, 'wheel-installed-RECORD', [python, '-I', '-B', tools / 'audit_wheel.py', wheel, out / 'record'], source, 30)
    for order in ('main-first', 'child-first'):
        run_logged(out, 'integration-SI-' + order, [python, '-I', '-B', tools / 'check_subinterpreters.py',
                   out / order, order, client], source, 55)
    run_logged(out, 'pip-check', [python, '-m', 'pip', 'check'], source, 30)
    write(out / 'PUBLISH_SET.json', {'status': 'PASS_EXACT_FIVE_PLATFORM_SET_AND_INTEGRATION',
          'git_head': plan['git_head'], 'version': plan['current_version'], 'files': files,
          'canonical_source_artifact': str(canonical), 'plan_sha256': sha(out / 'PLAN.json'),
          'collision_sha256': sha(out / 'COLLISION.json')})


def safe_url(url):
    parts = urlsplit(url)
    decoded = unquote(parts.path)
    require(parts.scheme == 'https' and parts.netloc == 'devpi.stablekite.com' and
            decoded.startswith('/StableKite/index/') and '..' not in decoded.split('/') and
            '\\' not in decoded and not parts.fragment and not parts.query,
            'Refusing non-canonical or foreign devpi URL.')
    return url


def read_session():
    import requests
    require('DEVPI_TOKEN' not in os.environ, 'Write-only upload token is forbidden in read phases.')
    token = os.environ.get('DEVPI_READ_TOKEN', '')
    require(bool(token), 'A separately authorized DEVPI_READ_TOKEN is required.')
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    session.auth = ('', token)
    return session


def get_json(session, url, expected_type, allow_absent=False):
    response = session.get(safe_url(url), headers={'Accept': 'application/json'},
                           allow_redirects=False, timeout=(10, 30))
    if allow_absent and response.status_code == 404:
        require(response.headers.get('X-DEVPI-API-VERSION') == '2', 'Unverified project absence.')
        return {}
    require(response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', ''),
            'Expected authenticated devpi JSON response; status ' + str(response.status_code))
    data = response.json()
    require(data.get('type') == expected_type and isinstance(data.get('result'), dict), 'Unexpected devpi response schema.')
    return data['result']


def authenticate(session):
    api = get_json(session, INDEX + '+api', 'apiconfig')
    status = api.get('authstatus')
    require(isinstance(status, list) and len(status) >= 2 and status[0] == 'ok' and status[1],
            'Read authentication was not confirmed.')
    require(isinstance(api.get('index'), str) and api['index'].rstrip('/') + '/' == INDEX,
            'Authenticated index does not match canonical endpoint.')


def check_collision(versions, current):
    from packaging.version import Version
    require(all(Version(version) < Version(current) for version in versions),
            'Existing or newer version found; refusing overwrite/downgrade.')


def collision_release(source, out):
    plan = checked_plan(source, out)
    with read_session() as session:
        authenticate(session)
        project = get_json(session, INDEX + 'greenlet', 'projectconfig', allow_absent=True)
    check_collision(project, plan['current_version'])
    write(out / 'COLLISION.json', {'status': 'PASS_AUTHENTICATED_COLLISION_CHECK',
          'git_head': plan['git_head'], 'version': plan['current_version'],
          'existing_versions': sorted(project), 'endpoint': INDEX,
          'credential': 'separate read token; never DEVPI_TOKEN'})


def upload_release(source, out):
    plan = checked_plan(source, out)
    from requests.adapters import HTTPAdapter
    from twine.package import PackageFile
    from twine.repository import Repository
    manifest = read(out / 'PUBLISH_SET.json')
    require(manifest['status'] == 'PASS_EXACT_FIVE_PLATFORM_SET_AND_INTEGRATION' and
            manifest['plan_sha256'] == sha(out / 'PLAN.json') and
            manifest['collision_sha256'] == sha(out / 'COLLISION.json') and
            manifest['git_head'] == plan['git_head'] and manifest['version'] == plan['current_version'],
            'Publish set is not bound to the reviewed plan/preflight.')
    require('DEVPI_READ_TOKEN' not in os.environ, 'Read credential is forbidden in upload phase.')
    token = os.environ.get('DEVPI_TOKEN', '')
    require(bool(token), 'Upload credential missing.')
    require(len(manifest['files']) == 6 and
            {item['target'] for item in manifest['files']} == set(TARGETS) | {'canonical-source'},
            'Upload requires the exact six-artifact set.')
    paths = []
    for item in manifest['files']:
        path = safe_file(out / 'distributions', Path(item['path']).name)
        require(path.stat().st_size == item['bytes'] and sha(path) == item['sha256'],
                'Publication bytes changed after qualification.')
        paths.append(path)
    require(len({path.name for path in paths}) == 6, 'Duplicate upload filenames.')
    require(git(source, 'ls-remote', 'origin', 'refs/heads/' + plan['default_branch']).split()[0] ==
            plan['git_head'], 'Default branch advanced before upload.')

    class BoundedUploadAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            kwargs['timeout'] = (10, 90)
            return super().send(request, **kwargs)

    repository = Repository(INDEX, '', token, disable_progress_bar=True)
    repository.session.trust_env = False
    repository.session.verify = True
    # Twine normally retries 5xx. Each file here gets one non-redirecting POST.
    # Bound the socket wait without changing Requests' certificate verification.
    repository.session.mount('https://', BoundedUploadAdapter(max_retries=0))
    uploaded = []
    try:
        for item, path in zip(manifest['files'], paths):
            response = repository.upload(PackageFile.from_filename(str(path), None), max_redirects=1)
            require(response.status_code == 200 and not response.history and response.url == INDEX,
                    'Upload failed or attempted a redirect; status ' + str(response.status_code))
            uploaded.append({'name': path.name, 'sha256': item['sha256'], 'bytes': item['bytes'], 'http_status': 200})
            write(out / ('UPLOADED-' + path.name + '.json'), uploaded[-1])
    finally:
        repository.close()
    write(out / 'UPLOAD.json', {'status': 'UPLOADED_CONFIRMATION_STILL_REQUIRED',
          'git_head': plan['git_head'], 'files': uploaded, 'publish_set_sha256': sha(out / 'PUBLISH_SET.json')})


def confirm_release(source, out):
    plan = checked_plan(source, out)
    uploaded, manifest = read(out / 'UPLOAD.json'), read(out / 'PUBLISH_SET.json')
    require(uploaded['publish_set_sha256'] == sha(out / 'PUBLISH_SET.json') and
            uploaded['git_head'] == plan['git_head'], 'Upload/manifest mismatch.')
    expected = {Path(item['path']).name: item for item in manifest['files']}
    destination = out / 'confirmed-downloads'
    destination.mkdir()
    records = []
    with read_session() as session:
        authenticate(session)
        metadata = get_json(session, INDEX + 'greenlet/' + quote(plan['current_version'], safe=''), 'versiondata')
        require(metadata.get('name') == 'greenlet' and metadata.get('version') == plan['current_version'],
                'Published version metadata mismatch.')
        links = [link for link in metadata.get('+links', []) if link.get('rel') == 'releasefile']
        names = [unquote(PurePosixPath(urlsplit(safe_url(link['href'])).path).name) for link in links]
        require(len(names) == len(set(names)) == 6 and set(names) == set(expected), 'Published exact file set mismatch.')
        for link, name in zip(links, names):
            item = expected[name]
            if link.get('hash_spec'):
                require(link['hash_spec'] == 'sha256=' + item['sha256'], 'Server declared a different checksum.')
            with session.get(safe_url(link['href']), allow_redirects=False, timeout=(10, 30), stream=True) as response:
                require(response.status_code == 200, 'Published file readback failed.')
                count = 0
                with (destination / name).open('xb') as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        count += len(chunk)
                        require(count <= item['bytes'], 'Readback exceeded expected artifact size.')
                        stream.write(chunk)
            require(count == item['bytes'] and sha(destination / name) == item['sha256'],
                    'Published server bytes differ from qualified originals.')
            records.append({'name': name, 'sha256': item['sha256'], 'bytes': count, 'url': link['href']})
    write(out / 'RESULT.json', {'status': 'PASS_AUTH_COLLISION_INTEGRATION_EXACT_SET_UPLOAD_AND_SERVER_BYTES',
          'git_head': plan['git_head'], 'version': plan['current_version'], 'files': records,
          'confirmed_utc': datetime.now(timezone.utc).isoformat()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('plan', 'compatibility', 'collision', 'validate', 'upload', 'confirm'))
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    source, out = args.source.resolve(), args.output.resolve()
    out.mkdir(exist_ok=True)
    if args.phase == 'validate':
        require(args.artifacts is not None, '--artifacts is required.')
        validate_release(source, out, args.artifacts.resolve())
    else:
        {'plan': plan_release, 'compatibility': compatibility_release, 'collision': collision_release,
         'upload': upload_release, 'confirm': confirm_release}[args.phase](source, out)


if __name__ == '__main__':
    main()
