"""Real module-exec allocation failures, recovery, API and interpreter close."""
from pathlib import Path
import faulthandler
import json
import os
import subprocess
import sys
import sysconfig

def exercise(native_path, client_path, position):
    import gc
    import importlib.util
    import json
    import sys
    sys.path.insert(0, client_path)
    import _greenlet_si_client as client
    assert 'greenlet._greenlet' not in sys.modules
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    spec = importlib.util.spec_from_file_location('greenlet._greenlet', native_path)
    view = importlib.util.module_from_spec(spec)
    outcome = 'success'
    try:
        client.exec_with_failure(view, position)
    except MemoryError:
        outcome = 'MemoryError'
    except BaseException as error:
        outcome = type(error).__name__+':'+str(error)
    count, injected = client.failure_counts()
    # The allocator wrapper has already been removed in C before any Python
    # diagnostics or recovery. Retry the actual same failed module object.
    if outcome != 'success':
        client.exec_with_failure(view, 0)
    assert view.greenlet(lambda: 713).switch() == 713
    import greenlet
    assert greenlet.greenlet is view.greenlet
    assert greenlet.getcurrent() is view.getcurrent()
    assert greenlet.greenlet(lambda: 917).switch() == 917
    gc.collect()
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    return json.dumps({'position': position, 'outcome': outcome, 'injected': bool(injected),
        'allocations_observed': count, 'same_module_retry': outcome != 'success',
        'ordinary_package_import_and_API_afterwards': True, 'GIL_after': False, 'gil_flag': None})

if __name__ == '__main__':
    assert not sys._is_gil_enabled() and sys.flags.gil is None and 'PYTHON_GIL' not in os.environ
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    out = Path(sys.argv[1])
    client_path = sys.argv[2]
    native = str(next((Path(sysconfig.get_path('platlib'))/'greenlet').glob('_greenlet.*'+('.pyd' if sys.platform == 'win32' else '.so'))))
    if len(sys.argv) > 3:
        position = int(sys.argv[3])
        out.mkdir(exist_ok=False)
        faulthandler.enable()
        result = json.loads(exercise(native, client_path, position))
        (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result))
        raise SystemExit(0)
    out.mkdir(exist_ok=False)
    from concurrent import interpreters
    faulthandler.enable()
    faulthandler.dump_traceback_later(75, exit=True)
    children = []
    for position in range(1, 97):
        child = interpreters.create()
        try:
            record = json.loads(child.call(exercise, native, client_path, position))
        finally:
            child.close()
        children.append(record)
        (out/'CHILDREN.json').write_text(json.dumps(children, indent=2)+'\n')
    # Each main-interpreter attempt requires a fresh process so initialization
    # has never succeeded before the selected allocation failure.
    mains = []
    for position in range(1, 65):
        packet = out/f'main-{position:03}'
        argv = [sys.executable, '-I', '-B', __file__, str(packet), client_path, str(position)]
        with (out/f'main-{position:03}.stdout.log').open('wb') as stdout, (out/f'main-{position:03}.stderr.log').open('wb') as stderr:
            run = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=7)
        record = {'position': position, 'argv': argv, 'exit_code_including_finalize': run.returncode,
            'result': json.loads((packet/'RESULT.json').read_text()) if (packet/'RESULT.json').exists() else None}
        mains.append(record)
        (out/'MAINS.json').write_text(json.dumps(mains, indent=2)+'\n')
        assert run.returncode == 0
    result = {'status': 'PASS_REAL_FAILED_MODULE_EXEC_RECOVERY', 'children_created_closed': len(children),
        'child_failures_injected': sum(x['injected'] for x in children),
        'child_exec_failures_recovered': sum(x['same_module_retry'] for x in children),
        'fresh_main_processes': len(mains),
        'main_exec_failures_recovered': sum(x['result']['same_module_retry'] for x in mains),
        'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil}
    (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(result))
