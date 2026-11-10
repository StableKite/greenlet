"""Original suite once; stop only after a real main-thread USS increase exists."""
from pathlib import Path
from hashlib import sha256
import json
import os
import signal
import sys
import sysconfig
import threading
import unittest

out = Path(sys.argv[1])
out.mkdir()
assert sys.version_info[:3] == (3, 15, 2) and sysconfig.get_config_var('Py_GIL_DISABLED') == 0
assert 'PYTHON_GIL' not in os.environ and sys._is_gil_enabled()
import greenlet
import greenlet.tests
from greenlet.tests import TestCase

original = TestCase.get_process_uss
baseline = None
main_observations = []
requested = False


def observed_uss(self):
    global baseline, requested
    value = original(self)
    if self.id().endswith('unfinished_thread_dealloc_in_thread') and threading.get_native_id() == os.getpid():
        main_observations.append(value)
        if baseline is None:
            baseline = value
        elif not requested and value - baseline > 3 * 1024 * 1024:
            requested = True
            # The actual original reading is already complete. No debugger was
            # present during the workload producing it. Stop the process only
            # now, let the parent read libc structures, then return this exact
            # value and retain all original assertions/attempts.
            (out / 'PRE_ATTACH.maps').write_bytes(Path('/proc/self/maps').read_bytes())
            (out / 'PRE_ATTACH.smaps').write_bytes(Path('/proc/self/smaps').read_bytes())
            (out / 'GDB_ATTACH_REQUEST.json').write_text(json.dumps({'pid': os.getpid(),
                'native_thread': threading.get_native_id(), 'USS': value, 'baseline_USS': baseline,
                'delta': value - baseline, 'test': self.id(), 'main_observation': len(main_observations) - 1,
                'allocator_or_original_test_settings_changed': False}) + '\n')
            os.kill(os.getpid(), signal.SIGSTOP)
    return value


TestCase.get_process_uss = observed_uss
native = Path(greenlet._greenlet.__file__)
(out / 'IDENTITY.json').write_text(json.dumps({'runtime': sys.version, 'executable': sys.executable,
    'native': str(native), 'native_sha256': sha256(native.read_bytes()).hexdigest(),
    'Py_GIL_DISABLED': 0, 'Py_DEBUG': sysconfig.get_config_var('Py_DEBUG'),
    'GIL': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil,
    'JIT_available': sys._jit.is_available(), 'JIT_enabled': sys._jit.is_enabled()}, indent=2) + '\n')
suite = unittest.defaultTestLoader.discover('greenlet.tests')
result = unittest.TextTestRunner(verbosity=2).run(suite)
(out / 'RESULT.json').write_text(json.dumps({'status': 'DIAGNOSTIC_ONLY_ORIGINAL_ASSERTIONS_RETAINED',
    'tests': result.testsRun, 'failures': [(t.id(), trace) for t, trace in result.failures],
    'errors': [(t.id(), trace) for t, trace in result.errors], 'skips': [(t.id(), reason) for t, reason in result.skipped],
    'main_USS_observations': main_observations, 'late_attach_requested': requested,
    'before_workload_debugger_or_extra_allocator_observations': False,
    'threshold_or_workload_changed': False, 'malloc_trim_called': False, 'acceptance_claim': False}, indent=2) + '\n')
print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                  'late_attach_requested': requested}), flush=True)
