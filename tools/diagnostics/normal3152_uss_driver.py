"""Separate native-source and test-tree factors; never an acceptance gate."""
from pathlib import Path
from hashlib import sha256
import ast,json,os,subprocess,tarfile

source=Path(os.environ['GITHUB_WORKSPACE']);out=source/'uss-factorial';out.mkdir();rows=[]
assert 'PYTHON_GIL' not in os.environ
import sys,sysconfig
assert sys.version_info[:3]==(3,15,2) and sysconfig.get_config_var('Py_GIL_DISABLED')==0
env=dict(os.environ,PYTHONDEVMODE='1',PYTHONHASHSEED='1042466059',PYTHONDONTWRITEBYTECODE='1',
         PYTHONFAULTHANDLER='1',ZOPE_INTERFACE_STRICT_IRO='1',PYTHONUNBUFFERED='1',PYTHON_JIT='1')
os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
heads={'before':'eb651bdeb4cfb8dd66467be3214598ee38ea950a','current':'2fc8d4e30645e08180c846f2c01bd921793da455'}

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
for native_label,test_label in [('before','before'),('current','before'),('before','current'),('current','current')]:
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
    run(label+'-observed-original-full-suite',[python,'-B',source/'tools/diagnostics/normal3152_uss_probe.py',observations],cwd=directory,required=False)
    assert test_hashes=={str(p.relative_to(tests)):sha256(p.read_bytes()).hexdigest() for p in tests.rglob('*.py')}
    receipts.append(dict(label=label,native_source=heads[native_label],original_test_source=heads[test_label],test_hashes=test_hashes,method=method,result=json.loads((observations/'RESULT.json').read_bytes())))
    (out/'FACTOR_CELLS.json').write_text(json.dumps(receipts,indent=2)+'\n')
assert all(r['method']==receipts[0]['method'] for r in receipts)
(out/'SCOPE.json').write_text(json.dumps(dict(status='DIAGNOSTIC_FACTORIAL_NOT_ACCEPTANCE',same_original_USS_method_AST=True,
distinct_factor_cells=len(receipts),each_cell_executed_once=True,threshold_or_workload_changed=False,
native_or_SDK_modified=False,allocator_settings_changed=False,malloc_trim_called=False),indent=2)+'\n')
