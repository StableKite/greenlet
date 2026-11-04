"""Preserve the raw wheel, audit/repair ABI tags, and test the distributable."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

raw, source, out, client = [Path(value).resolve() for value in sys.argv[1:]]
out.mkdir(exist_ok=False)
assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
environment = dict(os.environ)
environment['PATH'] = str(Path(sys.executable).parent)+os.pathsep+environment.get('PATH', '')
records = []

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def persist(status):
    (out/'RECEIPT.json').write_text(json.dumps({'status': status,
        'updated_utc': datetime.now(timezone.utc).isoformat(),
        'raw_wheel': str(raw), 'raw_sha256': sha(raw), 'stages': records}, indent=2)+'\n')

def run(name, argv, timeout=90, cwd=None):
    argv = list(map(str, argv))
    cwd = out if cwd is None else cwd
    record = {'name': name, 'argv': argv, 'cwd': str(cwd), 'timeout_seconds': timeout}
    records.append(record)
    persist('RUNNING')
    with (out/(name+'.stdout.log')).open('xb') as stdout, (out/(name+'.stderr.log')).open('xb') as stderr:
        result = subprocess.run(argv, cwd=cwd, env=environment, stdout=stdout, stderr=stderr, timeout=timeout)
    record.update(exit_code=result.returncode, stdout_sha256=sha(out/(name+'.stdout.log')),
                  stderr_sha256=sha(out/(name+'.stderr.log')))
    persist('RUNNING')
    if result.returncode:
        for suffix in ('stdout', 'stderr'):
            print('Failed ABI stage raw log tail: '+name+'.'+suffix, file=sys.stderr)
            print('\n'.join((out/(name+'.'+suffix+'.log')).read_text(errors='replace').splitlines()[-80:]), file=sys.stderr)
    assert result.returncode == 0, (name, result.returncode)

try:
    tools = Path(__file__).resolve().parent
    release = out/'distributions'
    release.mkdir()
    if sys.platform == 'linux':
        run('original-ELF-and-symbol-ABI', [sys.executable, '-m', 'auditwheel', 'show', raw], 30)
        run('ABI-policy-repair', [sys.executable, '-m', 'auditwheel', 'repair', '--plat', 'auto',
                                 '--wheel-dir', release, raw], 60)
    else:
        assert sys.platform == 'win32' and raw.name.endswith('-win_amd64.whl')
        shutil.copyfile(raw, release/raw.name)
    wheel, = release.glob('*.whl')
    if sys.platform == 'linux':
        assert 'manylinux_' in wheel.name or 'musllinux_' in wheel.name
        assert '-linux_' not in wheel.name
        run('repaired-ELF-and-symbol-ABI', [sys.executable, '-m', 'auditwheel', 'show', wheel], 30)
    before = sha(raw)
    consumer = out/'consumer'
    python = consumer/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    run('fresh-distributable-consumer', [sys.executable, '-I', '-B', '-m', 'venv', consumer], 90)
    run('ordinary-distributable-install', [python, '-m', 'pip', 'install', str(wheel)+'[test]'], 120)
    run('installed-native-location', [python, '-I', '-B', '-c',
        'import greenlet,json,sys; assert not sys._is_gil_enabled() and sys.flags.gil is None; print(json.dumps(greenlet.__file__))'])
    package = Path(json.loads((out/'installed-native-location.stdout.log').read_text())).parent
    fixture = out/'source-fixture'
    shutil.copytree(source, fixture, ignore=shutil.ignore_patterns('.git', 'build', 'dist', 'evidence',
                    '__pycache__', '*.egg-info', '*.pyd', '*.so'))
    payloads = {}
    for path in package.rglob('*'):
        if path.is_file() and path.suffix in ('.pyd', '.so'):
            relative = path.relative_to(package)
            target = fixture/'src/greenlet'/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            payloads[str(relative)] = sha(path)
    assert len(payloads) == 3
    run('preserve-installed-wheel-support-libraries', [python, '-I', '-B',
        tools/'copy_wheel_support_libs.py', wheel, package.parent, fixture/'src', out/'support-libraries.json'])
    run('distributable-full-original172', [python, '-I', '-B', tools/'run_original_suite.py',
                                         out/'original-tests', '--source-fixture', fixture], 175, fixture)
    run('distributable-original-and-installed-RECORD', [python, '-I', '-B', tools/'audit_wheel.py',
                                                     wheel, out/'artifact-audit'])
    for order in ('main-first', 'child-first'):
        run('distributable-SI-'+order, [python, '-I', '-B', tools/'check_subinterpreters.py', out/order, order, client], 55)
        run('distributable-retained-frames-'+order, [python, '-I', '-B', tools/'check_retained_frames.py',
            out/('frames-'+order), order], 50)
    run('pip-dependency-consistency', [python, '-m', 'pip', 'check'])
    assert sha(raw) == before
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith('.dist-info/WHEEL'))
        (out/'WHEEL.txt').write_bytes(archive.read(metadata))
    result = {'status': 'PASS_ABI_DISTRIBUTABLE_RECORD_FULL_SUITE_AND_REAL_SI',
              'raw': {'path': str(raw), 'sha256': before},
              'distributable': {'path': str(wheel), 'sha256': sha(wheel), 'bytes': wheel.stat().st_size},
              'native_payloads': payloads, 'consumer': str(python), 'GIL_override': False,
              'support_libraries': json.loads((out/'support-libraries.json').read_text())['files'],
              'publication': 'Not uploaded by this qualification tool.'}
    (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    persist('PASSED_ALL_DECLARED_DISTRIBUTABLE_CHECKS')
    print(json.dumps(result))
except BaseException as error:
    (out/'FAILURE.txt').write_text(repr(error)+'\n')
    persist('FAILED')
    raise
