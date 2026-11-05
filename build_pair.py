"""Run inside a private resource-bounded container; never edit an installed SDK."""
from pathlib import Path
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tarfile

task = Path('/task')
task.mkdir()
evidence = task / 'evidence'
evidence.mkdir()
archive = Path('/input/cpython-v3.15.2.tar.gz')
with tarfile.open(archive) as bundle:
    bundle.extractall(task, filter='data')
source = task / 'cpython-3.15.2'
env = dict(os.environ)
for key in ('PYTHON_GIL', 'PYTHONPATH', 'PYTHONHOME'):
    env.pop(key, None)
env.update(PYTHON_JIT='1', LD_LIBRARY_PATH=str(source), PYTHONDONTWRITEBYTECODE='1')
commands = []


def run(name, argv, cwd=source, timeout=120, extra_env=None):
    with (evidence / (name + '.stdout.log')).open('xb') as stdout, \
         (evidence / (name + '.stderr.log')).open('xb') as stderr:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, env=extra_env or env,
                                stdout=stdout, stderr=stderr, timeout=timeout)
    commands.append({'name': name, 'argv': list(map(str, argv)), 'cwd': str(cwd),
                     'exit_code': result.returncode})
    (evidence / 'COMMANDS.json').write_text(json.dumps(commands, indent=2) + '\n')
    print(name, result.returncode, flush=True)
    assert result.returncode == 0, name


run('configure-original-release-FT', ['./configure', '--disable-gil', '--enable-shared',
                                     '--with-ensurepip=no', '--without-static-libpython'], timeout=180)
run('compile-original-release-FT', ['make', '-j2'], timeout=1200)
python = source / 'python'
run('build-stateless-probe', ['gcc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
    '-I' + str(source / 'Include'), '-I' + str(source), '/input/probe.c',
    '-o', str(task / '_listdir_errno_probe.so')])
probe_source = Path('/input/exercise.py').read_text()
assert 'child_collector' in probe_source
stages = []
state = source / 'Python/pystate.c'
original = state.read_text()


def diagnostic(stage):
    out = evidence / stage
    out.mkdir()
    shutil.copyfile(task / '_listdir_errno_probe.so', out / '_listdir_errno_probe.so')
    (out / 'exercise.py').write_text(probe_source)
    for path in (python, source / 'libpython3.15t.so.1.0', state):
        destination = out / path.name
        shutil.copyfile(path, destination)
    run(stage + '-runtime', [python, '-I', '-B', '-c',
        'import json,os,sys,sysconfig; assert sys.version_info[:3]==(3,15,2); '
        'assert sysconfig.get_config_var("Py_GIL_DISABLED")==1 and not hasattr(sys,"gettotalrefcount"); '
        'assert not sys._is_gil_enabled() and sys.flags.gil is None and "PYTHON_GIL" not in os.environ; '
        'print(json.dumps({"version":sys.version,"GIL":False,"gil_flag":None,"Py_DEBUG":sysconfig.get_config_var("Py_DEBUG"),'
        '"JIT_available":sys._jit.is_available(),"JIT_enabled":sys._jit.is_enabled()}))'])
    run(stage + '-unchanged-diagnostic', [python, '-I', '-B', out / 'exercise.py', out,
        '/input/fixture/src/greenlet'], timeout=80)
    result = json.loads((out / 'RESULT.json').read_text())
    stages.append({'stage': stage, 'Python_errors': len(result['Python_errors']),
                   'phase_transitions': len(result['observations']),
                   'source_sha256': hashlib.sha256(state.read_bytes()).hexdigest(),
                   'libpython_sha256': hashlib.sha256((out / 'libpython3.15t.so.1.0').read_bytes()).hexdigest()})
    (evidence / 'STAGES.json').write_text(json.dumps(stages, indent=2) + '\n')


diagnostic('before')
start = original.index('void\n_PyThreadState_Attach(PyThreadState *tstate)')
end = original.index('\nstatic void\ndetach_thread(', start)
segment = original[start:end]
assert segment.count('#if defined(Py_DEBUG)') == 2
# Remove only the two Py_DEBUG guards; keep the FT and Py_STATS conditionals.
fixed_segment = segment.replace('#if defined(Py_DEBUG)\n    // This is called', '    // This is called', 1)
fixed_segment = fixed_segment.replace('    int err = errno;\n#endif', '    int err = errno;', 1)
fixed_segment = fixed_segment.replace('#if defined(Py_DEBUG)\n    errno = err;\n#endif', '    errno = err;', 1)
fixed = original[:start] + fixed_segment + original[end:]
state.write_text(fixed)
(evidence / 'CPYTHON_ERRNO.patch').write_text(''.join(difflib.unified_diff(
    original.splitlines(True), fixed.splitlines(True), fromfile='a/Python/pystate.c', tofile='b/Python/pystate.c')))
run('compile-source-errno-fix', ['make', '-j2'], timeout=180)
diagnostic('after')
after = json.loads((evidence / 'after/RESULT.json').read_text())
before = json.loads((evidence / 'before/RESULT.json').read_text())
record = {'status': 'SOURCE_BUILD_CAUSAL_PAIR_COMPLETE_NOT_GREENLET_RELEASE_ACCEPTANCE',
          'source_tag': 'v3.15.2', 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
          'before_Python_errors': len(before['Python_errors']), 'before_observations': len(before['observations']),
          'after_Python_errors': len(after['Python_errors']), 'after_observations': len(after['observations']),
          'same_diagnostic_sha256': hashlib.sha256(probe_source.encode()).hexdigest(),
          'installed_SDK_modified': False, 'Greenlet_imported': False}
(evidence / 'PAIR_RESULT.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record), flush=True)
assert not after['Python_errors'] and not after['observations']
