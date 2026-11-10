"""One original native/test cell, observed through read-only matched-libc GDB."""
from pathlib import Path
from hashlib import sha256
import ast
import json
import os
import re
import shutil
import signal
import time
import subprocess
import sys
import sysconfig
import tarfile
import urllib.request

source = Path(os.environ['GITHUB_WORKSPACE'])
out = source / 'uss-heap-gdb'
out.mkdir()
head = '2fc8d4e30645e08180c846f2c01bd921793da455'
assert sys.version_info[:3] == (3, 15, 2) and sysconfig.get_config_var('Py_GIL_DISABLED') == 0
assert 'PYTHON_GIL' not in os.environ and sys._is_gil_enabled()
assert shutil.which('readelf') and shutil.which('cc')
env = dict(os.environ, PYTHONDEVMODE='1', PYTHONHASHSEED='1042466059', PYTHONDONTWRITEBYTECODE='1',
           PYTHONFAULTHANDLER='1', ZOPE_INTERFACE_STRICT_IRO='1', PYTHONUNBUFFERED='1', PYTHON_JIT='1')
assert not any(key in env for key in ('GLIBC_TUNABLES', 'MALLOC_ARENA_MAX', 'MALLOC_TRIM_THRESHOLD_', 'LD_PRELOAD'))
os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
rows = []


def run(name, argv, cwd=source, environment=env, required=True, timeout=1200):
    with (out / (name + '.stdout.log')).open('xb') as stdout, (out / (name + '.stderr.log')).open('xb') as stderr:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, env=environment, stdout=stdout, stderr=stderr, timeout=timeout)
    rows.append({'name': name, 'argv': list(map(str, argv)), 'exit_code': result.returncode})
    (out / 'STAGES.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(name, result.returncode, flush=True)
    if required:
        result.check_returncode()
    return result.returncode


def build_id(path):
    data = subprocess.check_output(['readelf', '-n', str(path)], text=True)
    matches = re.findall(r'Build ID: ([a-f0-9]+)', data)
    assert len(matches) == 1
    return matches[0]


libc = Path('/lib/x86_64-linux-gnu/libc.so.6').resolve()
libc_sha = sha256(libc.read_bytes()).hexdigest()
runtime_sha = sha256(Path(sys.executable).read_bytes()).hexdigest()
if not shutil.which('gdb'):
    # Some hosted images omit GDB. Install only new diagnostic-tool packages;
    # reject any proposed upgrade of an existing library before changing it.
    run('diagnostic-package-indices', ['sudo', 'apt-get', 'update'], timeout=300)
    packages = ['apt-get', '--no-upgrade', '--no-remove', '--no-install-recommends', 'install', 'gdb']
    run('gdb-new-packages-plan', [packages[0], '--simulate', *packages[1:]], timeout=180)
    plan = (out / 'gdb-new-packages-plan.stdout.log').read_text()
    assert not any(re.match(r'Inst \S+ \[', line) for line in plan.splitlines()), plan
    run('install-new-diagnostic-tool-only', ['sudo', packages[0], '--yes', *packages[1:]], timeout=300)
    assert sha256(libc.read_bytes()).hexdigest() == libc_sha
    assert sha256(Path(sys.executable).read_bytes()).hexdigest() == runtime_sha
assert shutil.which('gdb')
run('host-libc-version', ['getconf', 'GNU_LIBC_VERSION'])
assert (out / 'host-libc-version.stdout.log').read_text().strip() == 'glibc 2.39'
identifier = build_id(libc)
# The previous diagnostic's Ubuntu debuginfod request returned HTTP404 before
# any native test ran. Download the exact installed package's symbols instead;
# extract them privately without installing/upgrading libc or any runtime.
run('installed-libc-package-version', ['dpkg-query', '-W', '-f=${Version}', 'libc6:amd64'])
package_version = (out / 'installed-libc-package-version.stdout.log').read_text().strip()
(out / 'LIBC_BEFORE_SYMBOL_DOWNLOAD.json').write_text(json.dumps({'libc': str(libc),
    'sha256': libc_sha, 'build_id': identifier, 'package': package_version}, indent=2) + '\n')
run('download-exact-libc-debug-package', ['apt-get', 'download', 'libc6-dbg=' + package_version], cwd=out)
package, = out.glob('libc6-dbg_*.deb')
debug_package = out / 'debug-package'
run('extract-debug-symbols-only', ['dpkg-deb', '--extract', package, debug_package])
debug_root = debug_package / 'usr/lib/debug'
debug_file = debug_root / '.build-id' / identifier[:2] / (identifier[2:] + '.debug')
assert debug_file.is_file()
assert build_id(debug_file) == identifier
(out / 'MATCHED_LIBC_DEBUG.json').write_text(json.dumps({'libc': str(libc), 'libc_sha256': libc_sha,
    'build_id': identifier, 'debug_file': str(debug_file), 'debug_file_sha256': sha256(debug_file.read_bytes()).hexdigest(),
    'debug_package': str(package), 'package_sha256': sha256(package.read_bytes()).hexdigest(),
    'runtime_or_libc_replaced': False,
    'reference': 'https://www.sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html'}, indent=2) + '\n')
debug_setting = 'set debug-file-directory ' + str(debug_root) + ':/usr/lib/debug'
run('matched-libc-types-preflight', ['gdb', '-nh', '-nx', '--batch', '-ex', 'set debuginfod enabled off',
    '-ex', debug_setting, '-ex', 'file ' + str(libc), '-ex', 'ptype struct malloc_state',
    '-ex', 'ptype tcache_perthread_struct'])
assert 'struct malloc_state {' in (out / 'matched-libc-types-preflight.stdout.log').read_text()

# Each cell executes the unchanged original suite exactly once on this
# same normal315 runner. Stop only after the first real worker cohort has
# already produced its original main-thread USS reading, for either source.
heads = {'before': '2fc8d4e30645e08180c846f2c01bd921793da455',
         'after': '2e09ddbe7e708576209b74a1d8b1e64f1022120b'}
cells = []
test_hashes = None
for label, head in heads.items():
    cell = out / label
    cell.mkdir()
    archive = cell / 'source.tar'
    with archive.open('xb') as stream:
        subprocess.run(['git', '-c', 'core.autocrlf=false', 'archive', '--format=tar', '--prefix=source/', head], stdout=stream, check=True)
    with tarfile.open(archive) as stream:
        stream.extractall(cell, filter='data')
    directory = cell / 'source'
    venv = out / (label + '-env')
    python = venv / 'bin/python'
    run(label + '-private-env', [sys.executable, '-I', '-B', '-m', 'venv', venv])
    run(label + '-build-tools', [python, '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])
    run(label + '-original-native-editable-build', [python, '-m', 'pip', 'install', '-U', '-e', str(directory) + '[test,docs]'],
        environment=dict(env, CFLAGS='-Ofast -UNDEBUG -Wall', CPPFLAGS='-Ofast -UNDEBUG -Wall'))
    run(label + '-original-environment-inventory', [python, '-m', 'pip', 'freeze', '--all'])
    tests = directory / 'src/greenlet/tests'
    hashes = {str(p.relative_to(tests)): sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
    if test_hashes is None:
        test_hashes = hashes
    else:
        assert test_hashes == hashes
    observations = cell / 'original-observations'
    gdb_output = cell / 'heap-snapshots'
    request_file = observations / 'GDB_ATTACH_REQUEST.json'
    probe = source / 'tools/diagnostics/normal3152_error_status_pair_probe.py'
    argv = [str(python), '-B', str(probe), str(observations)]
    started = time.monotonic()
    attached = False
    with (cell / 'full-original-suite.stdout.log').open('xb') as stdout, (cell / 'full-original-suite.stderr.log').open('xb') as stderr:
        process = subprocess.Popen(argv, cwd=directory, env=env, stdout=stdout, stderr=stderr)
        try:
            while process.poll() is None:
                assert time.monotonic() - started < 1800, 'Original diagnostic suite timeout.'
                if request_file.is_file() and not attached:
                    request = json.loads(request_file.read_text())
                    assert request['pid'] == request['native_thread'] == process.pid and request['main_observation'] == 1
                    status = Path('/proc/' + str(process.pid) + '/status').read_text()
                    if re.search(r'^State:\s+T', status, re.MULTILINE):
                        attached = True
                        try:
                            run(label + '-read-only-first-cohort-snapshot', ['sudo', '-n', 'env',
                                'SK_HEAP_GDB_OUT=' + str(gdb_output), 'SK_HEAP_GDB_ATTACH_ONLY=1',
                                'gdb', '-nh', '-nx', '--batch',
                                '-iex', 'set debuginfod enabled off',
                                '-iex', 'set auto-load python-scripts off', '-iex', debug_setting,
                                '-p', str(process.pid), '-ex', 'set pagination off', '-ex', 'set confirm off',
                                '-ex', 'source ' + str(source / 'tools/diagnostics/normal3152_heap_gdb.py'),
                                '-ex', 'python next(t for t in gdb.selected_inferior().threads() if t.ptid[1] == gdb.selected_inferior().pid).switch()',
                                '-ex', 'python snapshot(0)', '-ex', 'detach'], required=False, timeout=180)
                        finally:
                            os.kill(process.pid, signal.SIGCONT)
                time.sleep(0.05)
            code = process.wait()
        finally:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGCONT)
                process.terminate()
                process.wait(timeout=30)
    assert hashes == {str(p.relative_to(tests)): sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
    original_result = json.loads((observations / 'RESULT.json').read_text())
    assert original_result['tests'] == 177
    snapshot = json.loads((gdb_output / 'phase-0.json').read_text()) if gdb_output.is_dir() else None
    cells.append({'label': label, 'native_source': head, 'original_tests_unchanged': True,
        'original_result': original_result, 'request': json.loads(request_file.read_text()) if request_file.is_file() else None,
        'snapshot_status': snapshot['status'] if snapshot else None,
        'retained_TLS_pins': snapshot.get('retained_pin_TLS_matches') if snapshot else None,
        'snapshot_path': str(gdb_output / 'phase-0.json') if snapshot else None,
        'debugger_during_original_first_cohort': False})
    (out / 'PAIR_CELLS.json').write_text(json.dumps(cells, indent=2) + '\n')
    print(json.dumps({'cell': label, 'failures': len(original_result['failures']), 'errors': len(original_result['errors']),
        'first_main_USS': cells[-1]['request'], 'retained_TLS_pins': cells[-1]['retained_TLS_pins']}), flush=True)
assert sha256(libc.read_bytes()).hexdigest() == libc_sha
before, after = [cell['original_result'] for cell in cells]
target = 'test_untracked_memory_doesnt_increase_unfinished_thread_dealloc_in_thread'
causal = len(before['failures']) == 1 and before['failures'][0][0].endswith(target) and not before['errors'] and not after['failures'] and not after['errors']
result = {'status': 'PASS_CAUSAL_ORIGINAL_USS_BEFORE_FAIL_AFTER_PASS' if causal else 'OBSERVED_ORIGINAL_PAIR_NOT_CAUSAL_BEFORE_FAILURE',
    'cells': cells, 'same_original177_test_hashes': test_hashes,
    'each_source_full_original_suite_executed_once': True, 'debugger_attaches_only_after_original_first_cohort': True,
    'threshold_workload_allocator_settings_changed': False, 'warmup_or_inferior_function_calls': False,
    'canonical_source_default_SDK_libc_modified': False, 'release_or_default_acceptance_claim': False}
(out / 'PAIR_RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'status': result['status'], 'before_failures': len(before['failures']),
    'after_failures': len(after['failures']), 'release_claim': False}), flush=True)
