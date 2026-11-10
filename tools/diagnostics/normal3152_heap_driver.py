"""One original native/test cell, observed through read-only matched-libc GDB."""
from pathlib import Path
from hashlib import sha256
import ast
import json
import os
import re
import shutil
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
debug_root = out / 'symbols'
debug_file = debug_root / '.build-id' / identifier[:2] / (identifier[2:] + '.debug')
debug_file.parent.mkdir(parents=True)
url = 'https://debuginfod.ubuntu.com/buildid/' + identifier + '/debuginfo'
with urllib.request.urlopen(url, timeout=90) as response, debug_file.open('xb') as stream:
    final_url = response.url
    while data := response.read(1024 * 1024):
        stream.write(data)
assert build_id(debug_file) == identifier
(out / 'MATCHED_LIBC_DEBUG.json').write_text(json.dumps({'libc': str(libc), 'libc_sha256': libc_sha,
    'build_id': identifier, 'debug_file': str(debug_file), 'debug_file_sha256': sha256(debug_file.read_bytes()).hexdigest(),
    'url': url, 'final_url': final_url, 'runtime_or_libc_replaced': False,
    'reference': 'https://www.sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html'}, indent=2) + '\n')
debug_setting = 'set debug-file-directory ' + str(debug_root) + ':/usr/lib/debug'
run('matched-libc-types-preflight', ['gdb', '-nh', '-nx', '--batch', '-ex', 'set debuginfod enabled off',
    '-ex', debug_setting, '-ex', 'file ' + str(libc), '-ex', 'ptype struct malloc_state',
    '-ex', 'ptype tcache_perthread_struct'])
assert 'struct malloc_state {' in (out / 'matched-libc-types-preflight.stdout.log').read_text()

archive = out / 'canonical-source.tar'
with archive.open('xb') as stream:
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'archive', '--format=tar', '--prefix=canonical/', head], stdout=stream, check=True)
with tarfile.open(archive) as stream:
    stream.extractall(out, filter='data')
directory = out / 'canonical'
venv = out / 'original-env'
python = venv / 'bin/python'
run('private-venv', [sys.executable, '-I', '-B', '-m', 'venv', venv])
run('build-tools', [python, '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])
run('original-native-editable-build', [python, '-m', 'pip', 'install', '-U', '-e', str(directory) + '[test,docs]'],
    environment=dict(env, CFLAGS='-Ofast -UNDEBUG -Wall', CPPFLAGS='-Ofast -UNDEBUG -Wall'))
run('original-environment-inventory', [python, '-m', 'pip', 'freeze', '--all'])
tests = directory / 'src/greenlet/tests'
before = {str(p.relative_to(tests)): sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}

# This independent marker has a void(int) public C ABI and no allocator/Python
# operations. It gives GDB a stable stop after the original USS read. The
# canonical Greenlet extension and every original test byte remain unchanged.
marker_source = out / 'inert_snapshot_marker.c'
marker_source.write_text('__attribute__((noinline,visibility("default"))) void sk_malloc_snapshot_marker(int phase) { __asm__ __volatile__("" : : "r"(phase) : "memory"); }\n')
marker = out / 'inert_snapshot_marker.so'
run('compile-inert-marker', ['cc', '-g', '-O0', '-shared', '-fPIC', '-o', marker, marker_source])
probe_original = (source / 'tools/diagnostics/normal3152_uss_probe.py').read_text()
probe = probe_original.replace('original=TestCase.get_process_uss;sequence=0;profile_memory_reads=0',
    "marker=ctypes.CDLL(os.environ['SK_HEAP_MARKER']);marker.sk_malloc_snapshot_marker.argtypes=[ctypes.c_int];marker.sk_malloc_snapshot_marker.restype=None\noriginal=TestCase.get_process_uss;sequence=0;profile_memory_reads=0;heap_snapshot_phase=0")
assert probe != probe_original
probe = probe.replace('global sequence,profile_memory_reads', 'global sequence,profile_memory_reads,heap_snapshot_phase')
needle = "        with (out/'USS_OBSERVATIONS.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\\n')"
assert probe.count(needle) == 1
probe = probe.replace(needle, needle + "\n        if self.id().endswith('unfinished_thread_dealloc_in_thread') and heap_snapshot_phase<6:\n            marker.sk_malloc_snapshot_marker(heap_snapshot_phase)\n            heap_snapshot_phase+=1")
ast.parse(probe)
probe_path = out / 'read_only_marker_probe.py'
probe_path.write_text(probe)
observations = out / 'original-observations'
gdb_output = out / 'heap-snapshots'
commands = out / 'gdb.commands'
commands.write_text('\n'.join(['set pagination off', 'set confirm off', 'set breakpoint pending on',
    'set disable-randomization off', 'set debuginfod enabled off', 'set auto-load python-scripts off',
    'set print thread-events off', 'set follow-fork-mode parent', 'set detach-on-fork on', debug_setting,
    'handle SIGALRM nostop noprint pass', 'handle SIGUSR1 nostop noprint pass',
    'handle SIGUSR2 nostop noprint pass', 'handle SIGPIPE nostop noprint pass',
    'source ' + str(source / 'tools/diagnostics/normal3152_heap_gdb.py'),
    'run', 'info program', 'thread apply all bt 6']) + '\n')
run('one-original-full-suite-under-read-only-gdb', ['gdb', '-nh', '-nx', '--batch', '-x', commands,
    '--args', python, '-B', probe_path, observations], cwd=directory,
    environment=dict(env, SK_HEAP_GDB_OUT=str(gdb_output), SK_HEAP_MARKER=str(marker)), required=False, timeout=1800)
assert before == {str(p.relative_to(tests)): sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
assert sha256(libc.read_bytes()).hexdigest() == libc_sha
snapshots = [json.loads(p.read_text()) for p in sorted(gdb_output.glob('phase-*.json'))] if gdb_output.is_dir() else []
result = {'status': 'DIAGNOSTIC_ONLY_NOT_ACCEPTANCE', 'native_source': head, 'test_source': head,
          'original_test_hashes_unchanged': before, 'snapshots': len(snapshots),
          'successful_snapshots': sum(s['status'].startswith('PASS_') for s in snapshots),
          'original_assertions': json.loads((observations / 'RESULT.json').read_text()) if (observations / 'RESULT.json').is_file() else None,
          'threshold_workload_allocator_settings_changed': False, 'malloc_trim_or_inferior_function_calls': False,
          'canonical_source_or_SDK_changed': False, 'libc_bytes_unchanged': True,
          'original_test_bytes_unchanged': True, 'acceptance_claimed': False}
(out / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({key: value for key, value in result.items() if key != 'original_test_hashes_unchanged'}), flush=True)
