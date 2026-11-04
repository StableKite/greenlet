"""Build one source snapshot and preserve every native acceptance command."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import urllib.request

origin, out = map(lambda value: Path(value).resolve(), sys.argv[1:3])
out.mkdir(parents=True, exist_ok=False)
source = out/'source'
excluded = ['.git', '__pycache__', 'build', 'dist', 'evidence', '*.egg-info', '*.pyd', '*.so']
if out.is_relative_to(origin):
    excluded.append(out.relative_to(origin).parts[0])
shutil.copytree(origin, source, ignore=shutil.ignore_patterns(*excluded))
tools = source/'tools/acceptance'
records = []
started = datetime.now(timezone.utc).isoformat()
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
inputs = {str(path.relative_to(source)): digest(path) for path in source.rglob('*') if path.is_file()}
(out/'SOURCE_SHA256.json').write_text(json.dumps(inputs, indent=2)+'\n')
def persist(status):
    (out/'RECEIPT.json').write_text(json.dumps({'status': status, 'started_utc': started,
        'updated_utc': datetime.now(timezone.utc).isoformat(), 'source_manifest_sha256': digest(out/'SOURCE_SHA256.json'),
        'runtime': sys.version, 'GIL': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil,
        'stages': records}, indent=2)+'\n')
def run(name, argv, limit=90, cwd=source):
    argv = list(map(str, argv))
    record = {'name': name, 'argv': argv, 'cwd': str(cwd), 'timeout_seconds': limit}
    records.append(record)
    persist('RUNNING')
    with (out/(name+'.stdout.log')).open('xb') as stdout, (out/(name+'.stderr.log')).open('xb') as stderr:
        process = subprocess.run(argv, cwd=cwd, stdout=stdout, stderr=stderr, timeout=limit, shell=False)
    record.update(exit_code=process.returncode, stdout_sha256=digest(out/(name+'.stdout.log')),
        stderr_sha256=digest(out/(name+'.stderr.log')))
    persist('RUNNING')
    if process.returncode:
        for suffix in ('stdout', 'stderr'):
            print('Failed stage raw log tail: '+name+'.'+suffix, file=sys.stderr)
            print('\n'.join((out/(name+'.'+suffix+'.log')).read_text(errors='replace').splitlines()[-80:]), file=sys.stderr)
    assert process.returncode == 0, (name, process.returncode)

try:
    assert 'PYTHON_GIL' not in os.environ and sys.flags.gil is None and not sys._is_gil_enabled()
    assert sysconfig.get_config_var('Py_GIL_DISABLED') == 1
    assert sys.version_info.releaselevel == 'final'
    assert os.environ.get('PYTHON_JIT') == '1'
    if sys._jit.is_available():
        assert sys._jit.is_enabled()
    with urllib.request.urlopen('https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false', timeout=30) as response:
        releases = json.load(response)
    import re
    versions = [tuple(map(int, match[1].split('.'))) for item in releases
        if item['is_published'] and not item['pre_release'] and (match := re.fullmatch(r'Python (3\.\d+\.\d+)', item['name']))]
    assert sys.version_info[:3] == max(versions), (sys.version, max(versions))
    (out/'RUNTIME.json').write_text(json.dumps({'status': 'PASS_LATEST_STABLE_FT_RUNTIME_POLICY',
        'version': sys.version, 'latest_final': max(versions), 'GIL': False, 'gil_flag': None,
        'JIT_mode': 'enabled' if sys._jit.is_available() else 'unavailable in selected build; latest stable t retained'}, indent=2)+'\n')
    run('build-original-sdist-and-wheel', [sys.executable, '-m', 'build', '--outdir', out/'distributions'], 180)
    wheel = next((out/'distributions').glob('*.whl'))
    run('ordinary-wheel-test-extras-install', [sys.executable, '-m', 'pip', 'install', str(wheel)+'[test]'], 120)
    import greenlet
    expected = tomllib.loads((source/'pyproject.toml').read_text())['project']['version']
    assert greenlet.__version__ == expected == importlib.metadata.version('greenlet')
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    # The original -S subprocess tests require their native fixtures alongside
    # authentic source files. Use the exact installed payload, never a rebuild.
    package = Path(greenlet.__file__).parent
    for path in package.rglob('*'):
        if path.is_file() and path.suffix in ('.pyd', '.so'):
            target = source/'src/greenlet'/path.relative_to(package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    run('all-original-tests', [sys.executable, '-I', '-B', tools/'run_original_suite.py', out/'original-tests', '--source-fixture', source], 175)
    run('original-wheel-and-installed-RECORD', [sys.executable, '-I', '-B', tools/'audit_wheel.py', wheel, out/'artifact-audit'])
    run('strict-Pyright-runtime-stubs-and-type-consumer', [sys.executable, '-m', 'pyright', '--project', source/'pyrightconfig.json', '--pythonpath', sys.executable, '--outputjson'])
    run('negative-public-typing-controls', [sys.executable, '-I', '-B', tools/'check_typing_controls.py', out/'typing-controls'])
    client = out/'native-client'
    shutil.copytree(tools/'native_client', client)
    shutil.copyfile(source/'src/greenlet/greenlet.h', client/'greenlet.h')
    run('compile-real-original-header-C-client', [sys.executable, '-I', '-B', 'setup.py', 'build_ext', '--inplace'], 90, client)
    for order in ('main-first', 'child-first'):
        run('real-SI-'+order, [sys.executable, '-I', '-B', tools/'check_subinterpreters.py', out/order, order, client], 55)
        run('retained-frames-'+order, [sys.executable, '-I', '-B', tools/'check_retained_frames.py', out/('retained-frames-'+order), order], 50)
    run('real-failed-init-recovery', [sys.executable, '-I', '-B', tools/'check_failed_initialization.py', out/'failed-init', client], 90)
    run('real-suspended-child-teardown', [sys.executable, '-I', '-B', tools/'check_si_teardown.py', out/'suspended-teardown'], 50)
    run('pip-dependency-consistency', [sys.executable, '-m', 'pip', 'check'])
    run('qualify-final-distributable-ABI-and-runtime', [sys.executable, '-I', '-B',
        tools/'qualify_release_wheel.py', wheel, source, out/'qualified-release', client], 360)
    qualified = json.loads((out/'qualified-release/RESULT.json').read_text())
    assert qualified['status'] == 'PASS_ABI_DISTRIBUTABLE_RECORD_FULL_SUITE_AND_REAL_SI'
    distributable = Path(qualified['distributable']['path'])
    sdist, = (out/'distributions').glob('*.tar.gz')
    (out/'PUBLISH_ARTIFACTS.json').write_text(json.dumps({
        'status': 'QUALIFIED_ARTIFACTS_NOT_YET_PUBLISHED', 'version': expected,
        'git_head': os.environ.get('GITHUB_SHA'),
        'files': {path.relative_to(out).as_posix(): {'sha256': digest(path), 'bytes': path.stat().st_size}
                  for path in (distributable, sdist)}}, indent=2)+'\n')
    assert inputs == {name: digest(source/name) for name in inputs}
    (out/'ARTIFACTS_SHA256.json').write_text(json.dumps({path.name: {'bytes': path.stat().st_size,
        'sha256': digest(path)} for path in (out/'distributions').iterdir()}, indent=2)+'\n')
    persist('PASSED_ALL_DECLARED_COMPONENT_CHECKS')
except BaseException as error:
    (out/'FAILURE.txt').write_text(repr(error)+'\n')
    persist('FAILED')
    raise
