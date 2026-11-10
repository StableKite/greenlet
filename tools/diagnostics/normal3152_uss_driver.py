"""Separate native-source and test-tree factors; never an acceptance gate."""
from pathlib import Path
from hashlib import sha256
import ast,json,os,subprocess,tarfile

source=Path(os.environ['GITHUB_WORKSPACE']);out=source/'uss-native-cuts';out.mkdir();rows=[]
assert 'PYTHON_GIL' not in os.environ
import sys,sysconfig
assert sys.version_info[:3]==(3,15,2) and sysconfig.get_config_var('Py_GIL_DISABLED')==0
env=dict(os.environ,PYTHONDEVMODE='1',PYTHONHASHSEED='1042466059',PYTHONDONTWRITEBYTECODE='1',
         PYTHONFAULTHANDLER='1',ZOPE_INTERFACE_STRICT_IRO='1',PYTHONUNBUFFERED='1',PYTHON_JIT='1')
os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
heads={'before':'eb651bdeb4cfb8dd66467be3214598ee38ea950a','merge':'320108d82f6c02eb9e562ad73cd57b5bb7125b05','holder':'30eea380004e2c2fba694979fb7a91886d6260df','current':'2fc8d4e30645e08180c846f2c01bd921793da455','profile':'2fc8d4e30645e08180c846f2c01bd921793da455'}

def run(name,args,cwd=source,environment=env,required=True,timeout=1200):
    argv=list(map(str,args))
    with (out/(name+'.stdout.log')).open('wb') as stdout,(out/(name+'.stderr.log')).open('wb') as stderr:
        result=subprocess.run(argv,cwd=cwd,env=environment,stdout=stdout,stderr=stderr,timeout=timeout)
    rows.append(dict(name=name,argv=argv,exit_code=result.returncode));(out/'STAGES.json').write_text(json.dumps(rows,indent=2)+'\n');print(name,result.returncode,flush=True)
    if required:result.check_returncode()
    return result.returncode

templates={}
for label,head in heads.items():
    archive=out/(label+'.tar')
    with archive.open('wb') as f:subprocess.run(['git','-c','core.autocrlf=false','-c','core.eol=lf','archive','--format=tar','--prefix='+label+'/',head],stdout=f,check=True)
    with tarfile.open(archive) as stream:stream.extractall(out,filter='data')
    templates[label]=out/label

receipts=[]
# Four distinct native/test factor combinations, each in a fresh private build.
# No acceptance retries: each cell executes once and keeps its original failure.
for native_label,test_label in [(label,'current') for label in heads]:
    label=native_label+'-native_'+test_label+'-tests'
    directory=out/label;directory.mkdir()
    with tarfile.open(out/(native_label+'.tar')) as archive:
        for member in archive.getmembers():
            relative=Path(member.name).relative_to(native_label)
            target=directory/relative
            if member.isdir():target.mkdir(parents=True,exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as stream:target.write_bytes(stream.read())
    tests=directory/'src/greenlet/tests'
    # Install and qualify the actual native build before selecting external
    # original Python test bytes. C/C++ test clients remain the native revision's.
    venv=out/(label+'-env');python=venv/'bin/python'
    if native_label=='profile':
        import difflib
        patches=[]
        def modify(name,changes):
            p=directory/name;original=p.read_text();updated=original
            for old,new in changes:
                assert updated.count(old)==1,(name,old)
                updated=updated.replace(old,new)
            p.write_text(updated)
            patches.extend(difflib.unified_diff(original.splitlines(True),updated.splitlines(True),fromfile='a/'+name,tofile='b/'+name))
        helper=r'''static void __attribute__((noinline)) TraceNativeOwner(const char* kind, const void* pointer, size_t value) noexcept
{
    const int saved_errno = errno;
    char buffer[192];
    const int length = std::snprintf(buffer, sizeof(buffer), "OWNER_ADDRESS kind=%s thread=%ld pointer=%p value=%zu\n", kind, syscall(SYS_gettid), pointer, value);
    if (length > 0 && size_t(length) < sizeof(buffer)) syscall(SYS_write, 2, buffer, size_t(length));
    errno = saved_errno;
}

'''
        modify('src/greenlet/TGreenletGlobals.cpp',[
            ('#include <ctime>','#include <ctime>\n#include <cstdio>\n#include <cerrno>\n#include <unistd.h>\n#include <sys/syscall.h>'),
            ('namespace greenlet {','namespace greenlet {\n'+helper),
            ('struct ThreadStateHandle {','struct ThreadStateHandle {\n    ~ThreadStateHandle() { TraceNativeOwner("handle.final", this, 0); }'),
        ])
        modify('src/greenlet/TThreadState.hpp',[
            ('void* p = std::malloc(count);','void* p = std::malloc(count);\n        TraceNativeOwner("state.new", p, count);'),
            ('std::free(ptr);','TraceNativeOwner("state.free", ptr, sizeof(ThreadState));\n        std::free(ptr);'),
            ('~ThreadState()\n    {','~ThreadState()\n    {\n        TraceNativeOwner("state.destructor", this, sizeof(*this));'),
        ])
        modify('src/greenlet/TThreadStateCreator.hpp',[
            ('auto handle = std::make_shared<ThreadStateHandle>();','auto handle = std::make_shared<ThreadStateHandle>();\n            TraceNativeOwner("handle.new", handle.get(), sizeof(ThreadStateHandle));'),
            ('entries.push_back({owner, handle});','entries.push_back({owner, handle});\n            TraceNativeOwner("entries.buffer", entries.data(), entries.capacity() * sizeof(Entry));'),
        ])
        (out/'PROFILE_NATIVE_LOGGING.patch').write_text(''.join(patches))
    run(label+'-venv',[sys.executable,'-I','-B','-m','venv',venv])
    run(label+'-build-tools',[python,'-m','pip','install','--upgrade','pip','setuptools','wheel'])
    buildenv=dict(env,CFLAGS='-Ofast -UNDEBUG -Wall',CPPFLAGS='-Ofast -UNDEBUG -Wall')
    run(label+'-original-editable-build',[python,'-m','pip','install','-U','-e',str(directory)+'[test,docs]'],environment=buildenv)
    for name in ('test_interpreter_shutdown.py','test_leaks.py','test_sanitizer_fibers.py'):
        original=templates[test_label]/'src/greenlet/tests'/name
        if original.is_file():(tests/name).write_bytes(original.read_bytes())
        elif (tests/name).exists():(tests/name).unlink()
    test_hashes={str(p.relative_to(tests)):sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
    expected={str(p.relative_to(templates[test_label]/'src/greenlet/tests')):sha256(p.read_bytes()).hexdigest() for p in (templates[test_label]/'src/greenlet/tests').rglob('*.py')}
    assert test_hashes==expected
    tree=ast.parse((tests/'test_leaks.py').read_bytes())
    method={n.name:ast.dump(n,include_attributes=False) for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_check_untracked_memory_thread'}
    assert len(method)==1
    observations=out/(label+'-observations')
    run(label+'-observed-original-full-suite',[python,'-B',source/'tools/diagnostics/normal3152_uss_probe.py',observations],cwd=directory,environment=dict(env, GREENLET_OWNER_ADDRESS_DIAGNOSTIC='1') if native_label=='profile' else env,required=False)
    run(label+'-original-environment-inventory',[python,'-m','pip','freeze','--all'],cwd=directory)
    layout='import greenlet,json; from concurrent.futures import ThreadPoolExecutor; '+\
        'exec(\"def work():\\n parent=greenlet.getcurrent()\\n g=greenlet.greenlet(lambda: parent.switch(73))\\n assert g.switch()==73\\n saved=g._stack_saved\\n g.switch()\\n assert g.dead\\n return saved\"); '+\
        'pool=ThreadPoolExecutor(max_workers=1); saved=pool.submit(work).result(); pool.shutdown(); print(json.dumps(dict(saved_C_stack_bytes=saved)))'
    run(label+'-read-only-stack-layout-after-full-suite',[python,'-B','-c',layout],cwd=directory)
    assert test_hashes=={str(p.relative_to(tests)):sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
    receipts.append(dict(label=label,native_source=heads[native_label],original_test_source=heads[test_label],test_hashes=test_hashes,method=method,diagnostic_native_logging=(native_label=='profile'),result=json.loads((observations/'RESULT.json').read_bytes())))
    (out/'FACTOR_CELLS.json').write_text(json.dumps(receipts,indent=2)+'\n')
assert all(r['method']==receipts[0]['method'] for r in receipts)
(out/'SCOPE.json').write_text(json.dumps(dict(status='DIAGNOSTIC_DISTINCT_NATIVE_CUTS_PLUS_ADDRESS_LOGGING_NOT_ACCEPTANCE',same_original_USS_method_AST=True,
distinct_factor_cells=len(receipts),each_cell_executed_once=True,threshold_or_workload_changed=False,
canonical_native_or_SDK_modified=False,private_profile_cell_native_logging=True,allocator_settings_changed=False,malloc_trim_called=False),indent=2)+'\n')
