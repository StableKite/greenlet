"""Read-only allocation observations around the original USS tests; no GO claim."""
from pathlib import Path
from hashlib import sha256
import ctypes,json,os,sys,sysconfig,unittest

out=Path(sys.argv[1]);out.mkdir()
assert sys.version_info[:3]==(3,15,2) and sysconfig.get_config_var('Py_GIL_DISABLED')==0
assert sys._is_gil_enabled() and 'PYTHON_GIL' not in os.environ
assert not sys._jit.is_available() or sys._jit.is_enabled()
assert not any(os.environ.get(k) for k in ('GREENLET_MANYLINUX','GREENLET_SKIP_LEAKCHECKS','GREENLET_SKIP_FAILING_LEAKCHECKS','GREENLET_ONLY_FAILING_LEAKCHECKS'))
import greenlet,greenlet.tests
from greenlet.tests import TestCase

libc=ctypes.CDLL(None)
libc.fopen.argtypes=[ctypes.c_char_p,ctypes.c_char_p];libc.fopen.restype=ctypes.c_void_p
libc.fclose.argtypes=[ctypes.c_void_p];libc.fclose.restype=ctypes.c_int
libc.malloc_info.argtypes=[ctypes.c_int,ctypes.c_void_p];libc.malloc_info.restype=ctypes.c_int
original=TestCase.get_process_uss;sequence=0;profile_memory_reads=0
def observed_uss(self):
    global sequence,profile_memory_reads
    value=original(self)
    if 'untracked_memory' in self.id():
        number=sequence;sequence+=1
        filename=out/f'malloc-{number:04}.xml'
        handle=libc.fopen(os.fsencode(filename),b'w');assert handle
        try:assert libc.malloc_info(0,handle)==0
        finally:assert libc.fclose(handle)==0
        row=dict(sequence=number,test=self.id(),USS=value,allocated_blocks=sys.getallocatedblocks(),
                 native_main_greenlets=greenlet._greenlet.get_total_main_greenlets(),
                 native_pending=greenlet._greenlet.get_pending_cleanup_count())
        if os.environ.get('GREENLET_OWNER_ADDRESS_DIAGNOSTIC')=='1' and self.id().endswith('unfinished_thread_dealloc_in_thread') and profile_memory_reads<5:
            maps=Path('/proc/self/maps').read_bytes();(out/f'maps-{number:04}.txt').write_bytes(maps)
            row['owner_profile_mapping']=f'maps-{number:04}.txt';profile_memory_reads+=1
        with (out/'USS_OBSERVATIONS.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    return value
TestCase.get_process_uss=observed_uss
tests=Path(greenlet.tests.__file__).parent
before={str(p.relative_to(tests)):sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
identity=dict(runtime=sys.version,executable=sys.executable,Py_GIL_DISABLED=0,GIL=True,
              JIT_available=sys._jit.is_available(),JIT_enabled=sys._jit.is_enabled(),
              native=greenlet._greenlet.__file__,native_sha256=sha256(Path(greenlet._greenlet.__file__).read_bytes()).hexdigest())
(out/'IDENTITY.json').write_text(json.dumps(identity,indent=2)+'\n')
suite=unittest.defaultTestLoader.discover('greenlet.tests')
result=unittest.TextTestRunner(verbosity=2).run(suite)
assert before=={str(p.relative_to(tests)):sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
(out/'RESULT.json').write_text(json.dumps(dict(status='DIAGNOSTIC_ONLY_ORIGINAL_ASSERTIONS_RETAINED',
tests=result.testsRun,failures=[(t.id(),trace) for t,trace in result.failures],errors=[(t.id(),trace) for t,trace in result.errors],
skips=[(t.id(),reason) for t,reason in result.skipped],observations=sequence,
original_test_bytes_unchanged=True,threshold_or_workload_changed=False,allocator_configuration_changed=False,
malloc_trim_called=False,acceptance_claim=False),indent=2)+'\n')
print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),observations=sequence)),flush=True)
