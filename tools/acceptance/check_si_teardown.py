"""Close real interpreters with suspended stacks and native TLS cleanup pending."""
from pathlib import Path
import faulthandler
import json
import os
import sys

def leave_live_state(receipt):
    import atexit
    import json
    import sys
    import threading
    import greenlet
    from greenlet import _greenlet as native
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    failures = []
    def worker():
        try:
            for value in range(32):
                assert greenlet.greenlet(lambda value=value: value).switch() == value
        except BaseException as error:
            failures.append(repr(error))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(4)
    assert not failures and not any(thread.is_alive() for thread in threads), failures
    class Suspended(greenlet.greenlet):
        pass
    def suspended():
        saved = [object() for _ in range(16)]
        greenlet.getcurrent().parent.switch(saved)
        return 713
    native._acceptance_suspended = Suspended(suspended)
    assert len(native._acceptance_suspended.switch()) == 16
    assert not native._acceptance_suspended.dead
    native._acceptance_suspended.cycle = native._acceptance_suspended
    # Do not query counters or call getcurrent after worker joins: those API
    # calls would deliberately drain the very exit queue being qualified.
    def ordinary_python_atexit():
        assert not sys._is_gil_enabled() and sys.flags.gil is None
        assert greenlet.greenlet(lambda: 917).switch() == 917
        greenlet.settrace(None)
        assert greenlet.gettrace() is None
        with open(receipt, 'x', encoding='utf8') as output:
            json.dump({'status': 'PASS_REAL_CHILD_ATEXIT_API', 'GIL': False, 'flag': None}, output)
    atexit.register(ordinary_python_atexit)
    return 'READY_WITH_REAL_SUSPENDED_STACK_AND_WORKER_TLS'

if __name__ == '__main__':
    from concurrent import interpreters
    from concurrent.futures import ThreadPoolExecutor
    out = Path(sys.argv[1])
    out.mkdir(exist_ok=False)
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
    faulthandler.enable()
    faulthandler.dump_traceback_later(40, exit=True)
    records = []
    for number in range(12):
        children = [interpreters.create() for _ in range(4)]
        receipts = [out/f'child-{number:02}-{index}.atexit.json' for index in range(4)]
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda pair: pair[0].call(leave_live_state, str(pair[1])), zip(children, receipts)))
            assert results == ['READY_WITH_REAL_SUSPENDED_STACK_AND_WORKER_TLS']*4
        finally:
            for child in children:
                child.close()
        closed = [json.loads(path.read_text()) for path in receipts]
        assert all(item['status'] == 'PASS_REAL_CHILD_ATEXIT_API' for item in closed)
        records.append({'round': number, 'closed': 4, 'actual_atexit_receipts': list(map(str, receipts))})
        (out/'ROUNDS.json').write_text(json.dumps(records, indent=2)+'\n')
    result = {'status': 'PASS_REAL_SUSPENDED_CHILD_AND_NATIVE_TLS_TEARDOWN',
        'children_closed': 48, 'worker_threads': 192, 'worker_switches': 6144,
        'ordinary_child_atexit_API_receipts': 48, 'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil}
    (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(result))
