"""Real child/main imports, APIs, threads, GC and repeated child teardown."""
from pathlib import Path
import faulthandler
import hashlib
import json
import os
import sys

def exercise(expect_heap, client_dir):
    import contextvars
    import gc
    import importlib.util
    import json
    import sys
    import threading
    import time
    import weakref
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    import greenlet
    from greenlet import _greenlet as native
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    assert bool(greenlet.greenlet.__flags__ & (1 << 9)) == expect_heap
    assert greenlet.greenlet.__flags__ & (1 << 14)
    current = greenlet.getcurrent()
    assert current.parent is None
    baseline = native.get_total_main_greenlets()
    assert baseline >= 1
    events = []
    previous = greenlet.settrace(lambda event, args: events.append(event))
    assert previous is None and callable(greenlet.gettrace())
    variable = contextvars.ContextVar('greenlet-context', default=13)
    variable.set(29)
    child = greenlet.greenlet(lambda: (variable.get(), greenlet.getcurrent().parent.switch(713)))
    child.gr_context = contextvars.copy_context()
    assert child.switch() == 713 and not child.dead
    assert child.gr_frame is not None
    assert child.switch(917) == (29, 917) and child.dead
    assert events.count('switch') >= 4
    assert callable(greenlet.settrace(None)) and greenlet.gettrace() is None
    def failing():
        raise ValueError('real-child-error')
    try:
        greenlet.greenlet(failing).switch()
    except ValueError as error:
        assert str(error) == 'real-child-error'
    else:
        raise AssertionError('exception was lost')
    class Client(greenlet.greenlet):
        pass
    subclass = weakref.ref(Client)
    cycle = Client(lambda: 31)
    cycle.self = cycle
    reference = weakref.ref(cycle)
    assert cycle.switch() == 31
    del cycle, Client
    gc.collect()
    gc.collect()
    assert reference() is None and subclass() is None
    failures = []
    worker_mains = []
    def worker():
        try:
            own_main = greenlet.getcurrent()
            worker_mains.append(weakref.ref(own_main))
            assert own_main is not current and greenlet.gettrace() is None
            for value in range(32):
                assert greenlet.greenlet(lambda value=value: value).switch() == value
            assert not sys._is_gil_enabled() and sys.flags.gil is None
        except BaseException as error:
            failures.append(repr(error))
    workers = [threading.Thread(target=worker) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(5)
    assert all(not thread.is_alive() for thread in workers) and not failures, failures
    deadline = time.monotonic()+4
    while time.monotonic() < deadline:
        greenlet.getcurrent()
        if native.get_pending_cleanup_count() == 0 and native.get_total_main_greenlets() == baseline:
            break
        gc.collect()
        time.sleep(.001)
    gc.collect()
    gc.collect()
    assert native.get_pending_cleanup_count() == 0
    assert native.get_total_main_greenlets() == baseline
    assert len(worker_mains) == 2 and all(item() is None for item in worker_mains)
    # A reexecuted module view shares the interpreter's execution domain but
    # must not retain that view after its own references are released.
    duplicate = importlib.util.module_from_spec(native.__spec__)
    native.__spec__.loader.exec_module(duplicate)
    assert duplicate.greenlet is greenlet.greenlet
    assert duplicate.getcurrent() is current
    duplicate_reference = weakref.ref(duplicate)
    del duplicate
    gc.collect()
    gc.collect()
    assert duplicate_reference() is None
    if client_dir:
        sys.path.insert(0, client_dir)
        import _greenlet_si_client as client
        client.bind()
        for value in range(32):
            assert client.run(lambda value=value: value) == value
            assert client.Client(lambda value=value: value).switch() == value
        try:
            client.throw(lambda: None)
        except ValueError:
            pass
        else:
            raise AssertionError('C API throw did not propagate')
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    return json.dumps({'type_id': id(greenlet.greenlet), 'error_id': id(greenlet.error),
        'main_id': id(current), 'heap': expect_heap, 'worker_threads': 2, 'switches': 64,
        'trace_events': len(events), 'cycle_and_subclass_collected': True,
        'duplicate_module_collected': True, 'pending_cleanup': 0,
        'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil})

if __name__ == '__main__':
    from concurrent import interpreters
    from concurrent.futures import ThreadPoolExecutor
    out, order = Path(sys.argv[1]), sys.argv[2]
    client_dir = sys.argv[3] if len(sys.argv) > 3 else None
    out.mkdir(exist_ok=False)
    assert order in ('main-first', 'child-first')
    assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    faulthandler.dump_traceback_later(45, exit=True)
    main = json.loads(exercise(False, client_dir)) if order == 'main-first' else None
    rounds = []
    for number in range(4):
        children = [interpreters.create() for _ in range(3)]
        try:
            # First call synchronously: the same native thread enters a child,
            # returns to main and later enters a different execution domain.
            sequential = [json.loads(child.call(exercise, True, client_dir)) for child in children]
            if main is None:
                main = json.loads(exercise(False, client_dir))
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda child: json.loads(child.call(exercise, True, client_dir)), children))
            identities = [main, *results]
            assert len({item['type_id'] for item in identities}) == 4
            assert len({item['error_id'] for item in identities}) == 4
            rounds.append({'round': number, 'sequential': sequential, 'parallel': results})
            (out/'ROUNDS.json').write_text(json.dumps(rounds, indent=2)+'\n')
        finally:
            for child in children:
                child.close()
    import greenlet
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    result = {'status': 'PASS_BOUNDED_REAL_GREENLET_SI_COMPONENT', 'order': order,
        'main': main, 'rounds': rounds, 'children_created_and_closed': 12,
        'native': greenlet._greenlet.__file__,
        'native_sha256': hashlib.sha256(Path(greenlet._greenlet.__file__).read_bytes()).hexdigest(),
        'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil,
        'real_native_C_API_and_C_subclass': bool(client_dir),
        'scope': 'Real interpreter API component; full gevent, typing, CI and default delivery remain separate.'}
    (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps({'status': result['status'], 'order': order, 'children': 12}))
