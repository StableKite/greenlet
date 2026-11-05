"""Measure real errno phases under the failing multi-interpreter topology."""


def exercise(native_path, target):
    import importlib.machinery
    import json
    import os
    import sys
    import threading
    import traceback

    sys.path.insert(0, native_path)
    import _listdir_errno_probe as native
    assert 'greenlet' not in sys.modules and 'greenlet._greenlet' not in sys.modules
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    with os.scandir(target) as entries:
        expected = sum(1 for _ in entries) + 2
    records, errors = [], []

    def worker(worker_id):
        for case in range(64):
            try:
                for corrected in (False, True):
                    count, detached, operation, attached, reported = native.scan(target, corrected)
                    if reported or count != expected or detached or operation != attached:
                        records.append({'worker': worker_id, 'case': case, 'corrected_C_implementation': corrected,
                            'count': count, 'expected': expected, 'detached_errno': detached,
                            'operation_errno': operation, 'attached_errno': attached, 'reported_errno': reported})
                # Preserve the actual Python allocation/import-cache workload;
                # finding an extension spec never executes that extension.
                assert len(os.listdir(target)) >= 20
                finder = importlib.machinery.FileFinder(target,
                    (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES))
                assert finder.find_spec('_greenlet') is not None
                assert 'greenlet._greenlet' not in sys.modules
            except BaseException as error:
                errors.append({'worker': worker_id, 'case': case, 'attempt': 1,
                               'error': repr(error), 'traceback': traceback.format_exc()})

    # A main-interpreter collector cannot establish child-local GC contention.
    # Keep the unchanged native scanner but collect inside each actual child.
    import gc, time
    child_stop = threading.Event()
    child_collections = []
    def child_collector():
        while not child_stop.is_set() and len(child_collections) < 512:
            child_collections.append(gc.collect())
            time.sleep(.0001)
    child_gc = threading.Thread(target=child_collector)
    child_gc.start()
    workers = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    child_stop.set()
    child_gc.join(10)
    assert not child_gc.is_alive()
    assert not any(worker.is_alive() for worker in workers)
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    assert 'greenlet' not in sys.modules and 'greenlet._greenlet' not in sys.modules
    return json.dumps({'observations': records, 'errors': errors, 'fixed_cases': 256, 'child_collections': len(child_collections),
                       'C_scans_per_case': 2, 'GIL': False, 'gil_flag': None})


if __name__ == '__main__':
    from concurrent import interpreters
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    import faulthandler
    import gc
    import json
    import os
    import resource
    import sys
    import threading
    import time

    out, target = Path(sys.argv[1]), sys.argv[2]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
    faulthandler.enable()
    faulthandler.dump_traceback_later(60, exit=True)
    stop = threading.Event()
    collections = []

    def collector():
        while not stop.is_set() and len(collections) < 2048:
            collections.append(gc.collect())
            time.sleep(.0001)

    collecting = threading.Thread(target=collector)
    collecting.start()
    rounds = []
    try:
        for number in range(16):
            children = [interpreters.create() for _ in range(4)]
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(child.call, exercise, str(out), target) for child in children]
                    batch = [json.loads(future.result()) for future in futures]
            finally:
                for child in children:
                    child.close()
            rounds.append({'round': number, 'children': batch})
            (out / 'PROGRESS.json').write_text(json.dumps({'rounds': rounds}, indent=2) + '\n')
    finally:
        stop.set()
        collecting.join(10)
    assert not collecting.is_alive()
    observations = [item for batch in rounds for child in batch['children'] for item in child['observations']]
    errors = [item for batch in rounds for child in batch['children'] for item in child['errors']]
    transitions = [item for item in observations if item['detached_errno'] or
                   item['operation_errno'] != item['attached_errno']]
    result = {'status': 'ERRNO_PHASE_TRANSITION_OBSERVED' if transitions else 'NO_ERRNO_PHASE_TRANSITION_IN_FIXED_SAMPLE',
              'runtime': sys.version, 'children_closed': 64, 'fixed_cases': 16384, 'C_scans': 32768,
              'concurrent_collections': len(collections), 'observations': observations, 'Python_errors': errors,
              'Greenlet_imported': False, 'GIL': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil,
              'scope': 'Native errno phase diagnostic only, no SDK patch, importer replacement, retries or runtime acceptance.'}
    (out / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps({'status': result['status'], 'observations': len(observations),
                      'Python_errors': len(errors), 'collections': len(collections)}))
