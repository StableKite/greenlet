"""Retained Python frames survive abandoned native stacks and parallel readers."""


def check_frames(rounds, reader_count):
    import gc
    import json
    import sys
    import threading
    import time
    from types import CodeType, FrameType

    import greenlet

    assert not sys._is_gil_enabled() and sys.flags.gil is None
    greenlet.getcurrent()
    baseline = greenlet._greenlet.get_total_main_greenlets()
    records = []
    for number in range(rounds):
        frames, children, errors = [], [], []

        def worker():
            try:
                def suspended():
                    payload = ['retained-frame-payload', 713]
                    greenlet.getcurrent().parent.switch()
                    return payload

                child = greenlet.greenlet(suspended)
                child.switch()
                frames.append(child.gr_frame)
                # Hold the child outside the worker until its native thread
                # has exited. Releasing it inside that thread tests graceful
                # unwinding instead of abandoned-stack ownership transfer.
                children.append(child)
                del child
            except BaseException as error:
                errors.append(repr(error))

        owner = threading.Thread(target=worker)
        owner.start()
        owner.join(5)
        assert not owner.is_alive() and not errors, errors
        assert len(frames) == 1 and len(children) == 1
        started = [threading.Event() for _ in range(reader_count)]
        cleanup_done = threading.Event()
        reads = [0] * reader_count

        def read_frame(frame):
            assert isinstance(frame, FrameType)
            assert isinstance(frame.f_code, CodeType)
            assert frame.f_code.co_name == 'suspended'
            assert frame.f_back is None or isinstance(frame.f_back, FrameType)
            assert frame.f_locals['payload'] == ['retained-frame-payload', 713]

        def reader(index):
            try:
                frame = frames[0]
                deadline = time.monotonic() + 8
                while not cleanup_done.is_set() or reads[index] < 128:
                    assert time.monotonic() < deadline, 'reader deadline'
                    read_frame(frame)
                    reads[index] += 1
                    started[index].set()
                read_frame(frame)
            except BaseException as error:
                errors.append(repr(error))
                started[index].set()

        readers = [threading.Thread(target=reader, args=(index,))
                   for index in range(reader_count)]
        for thread in readers:
            thread.start()
        try:
            assert all(event.wait(5) for event in started) and not errors, errors
            children.clear()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                greenlet.getcurrent()
                gc.collect()
                if greenlet._greenlet.get_pending_cleanup_count() == 0:
                    break
                time.sleep(.001)
            assert greenlet._greenlet.get_pending_cleanup_count() == 0
            read_frame(frames[0])
        finally:
            cleanup_done.set()
            for thread in readers:
                thread.join(9)
        assert all(not thread.is_alive() for thread in readers) and not errors, errors
        assert all(count >= 128 for count in reads), reads
        frames.clear()
        gc.collect()
        gc.collect()
        assert greenlet._greenlet.get_total_main_greenlets() == baseline
        records.append({'round': number, 'parallel_reads': reads,
                        'main_count_returned_to_baseline': True})
    assert not sys._is_gil_enabled() and sys.flags.gil is None
    return json.dumps({'status': 'PASS_RETAINED_FRAMES_WITH_PARALLEL_READERS',
                       'rounds': records, 'GIL_after': False, 'gil_flag': None})


if __name__ == '__main__':
    from concurrent import interpreters
    from pathlib import Path
    import faulthandler
    import json
    import os
    import sys

    out = Path(sys.argv[1])
    out.mkdir(exist_ok=False)
    order = sys.argv[2]
    assert order in ('main-first', 'child-first')
    assert 'PYTHON_GIL' not in os.environ
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    faulthandler.dump_traceback_later(40, exit=True)
    main = json.loads(check_frames(8, 2)) if order == 'main-first' else None
    children = []
    for _ in range(2):
        child = interpreters.create()
        try:
            children.append(json.loads(child.call(check_frames, 4, 2)))
        finally:
            child.close()
    if main is None:
        main = json.loads(check_frames(8, 2))
    result = {'status': 'PASS_RETAINED_FRAMES_IN_MAIN_AND_REAL_CHILDREN',
              'order': order, 'main': main, 'children': children,
              'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil}
    (out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps({'status': result['status'], 'order': order}))
