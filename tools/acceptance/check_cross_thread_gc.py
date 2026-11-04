"""Collect after the last external reference, then resume owner-thread cleanup."""
from pathlib import Path
import faulthandler
import gc
import json
import os
import sys
import threading
import traceback

import greenlet

out = Path(sys.argv[1])
out.mkdir(exist_ok=False)
assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
faulthandler.dump_traceback_later(45, exit=True)
rounds = []
for number in range(32):
    greenlet.getcurrent()  # Drain completed owner-thread state at a public boundary.
    seen, sole_external, errors = [], [], []
    ready, released, checked, finish = [threading.Event() for _ in range(4)]

    def suspended():
        try:
            greenlet.getcurrent().parent.switch()
        except greenlet.GreenletExit:
            seen.append('GreenletExit')
            raise

    def owner():
        try:
            child = greenlet.greenlet(suspended)
            child.switch()
            sole_external.append(child)
            del child
            gc.collect()
            ready.set()
            assert released.wait(5)
            greenlet.greenlet()  # Original API trigger for the owner deletion queue.
            checked.set()
            assert finish.wait(5)
            greenlet.greenlet()
        except BaseException:
            errors.append(traceback.format_exc())
            ready.set()
            checked.set()

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert ready.wait(10) and not errors
        assert len(sole_external) == 1 and seen == []
        sole_external.clear()
        collected = gc.collect()
        released.set()
        assert checked.wait(10)
        observation = {'round': number, 'seen_before_owner_exit': list(seen),
                       'errors': list(errors), 'collected': collected}
    finally:
        released.set()
        finish.set()
        thread.join(10)
    assert not thread.is_alive()
    rounds.append(observation)
    (out / 'PROGRESS.json').write_text(json.dumps({'rounds': rounds}, indent=2) + '\n')
    assert observation['seen_before_owner_exit'] == ['GreenletExit'] and not observation['errors']
greenlet.getcurrent()
gc.collect()
assert not sys._is_gil_enabled() and sys.flags.gil is None
(out / 'RESULT.json').write_text(json.dumps({
    'status': 'PASS_32_OWNER_THREAD_GC_FINALIZER_TRANSITIONS', 'rounds': rounds,
    'runtime': sys.version, 'native': greenlet._greenlet.__file__, 'GIL': False, 'gil_flag': None,
    'scope': 'Additional deterministic regression; original 172 test methods/identities remain unchanged.'
}, indent=2) + '\n')
faulthandler.cancel_dump_traceback_later()
print(json.dumps({'status': 'PASS_32_OWNER_THREAD_GC_FINALIZER_TRANSITIONS', 'rounds': 32}))
