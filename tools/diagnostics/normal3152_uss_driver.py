from pathlib import Path
from hashlib import sha256
import ast,json,os,subprocess,tarfile

source=Path(os.environ['GITHUB_WORKSPACE']);out=source/'uss-diagnostic';out.mkdir();rows=[]
assert 'PYTHON_GIL' not in os.environ
import sys,sysconfig
assert sys.version_info[:3]==(3,15,2) and sysconfig.get_config_var('Py_GIL_DISABLED')==0
env=dict(os.environ,PYTHONDEVMODE='1',PYTHONHASHSEED='1042466059',PYTHONDONTWRITEBYTECODE='1',
         PYTHONFAULTHANDLER='1',ZOPE_INTERFACE_STRICT_IRO='1',PYTHONUNBUFFERED='1',PYTHON_JIT='1')
os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
def run(name,args,cwd=source,environment=env,required=True,timeout=600):
    argv=list(map(str,args))
    with (out/(name+'.stdout.log')).open('wb') as stdout,(out/(name+'.stderr.log')).open('wb') as stderr:
        result=subprocess.run(argv,cwd=cwd,env=environment,stdout=stdout,stderr=stderr,timeout=timeout)
    rows.append(dict(name=name,argv=argv,exit_code=result.returncode));(out/'STAGES.json').write_text(json.dumps(rows,indent=2)+'\n');print(name,result.returncode,flush=True)
    if required:result.check_returncode()
    return result.returncode
methods={}
for label,head in [('before-unification','eb651bdeb4cfb8dd66467be3214598ee38ea950a'),('current-native','2fc8d4e30645e08180c846f2c01bd921793da455')]:
    archive=out/(label+'.tar')
    with archive.open('wb') as f:subprocess.run(['git','-c','core.autocrlf=false','-c','core.eol=lf','archive','--format=tar','--prefix='+label+'/',head],stdout=f,check=True)
    with tarfile.open(archive) as stream:stream.extractall(out,filter='data')
    directory=out/label;venv=out/(label+'-env');python=venv/'bin/python'
    tree=ast.parse((directory/'src/greenlet/tests/test_leaks.py').read_bytes())
    methods[label]={n.name:ast.dump(n,include_attributes=False) for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in ('_check_untracked_memory_thread','test_untracked_memory_doesnt_increase_unfinished_thread_dealloc_in_thread')}
    run(label+'-venv',[sys.executable,'-I','-B','-m','venv',venv])
    run(label+'-build-tools',[python,'-m','pip','install','--upgrade','pip','setuptools','wheel'])
    buildenv=dict(env,CFLAGS='-Ofast -UNDEBUG -Wall',CPPFLAGS='-Ofast -UNDEBUG -Wall')
    run(label+'-original-editable-build',[python,'-m','pip','install','-U','-e',str(directory)+'[test,docs]'],environment=buildenv)
    run(label+'-observed-original-full-suite',[python,'-B',source/'tools/diagnostics/normal3152_uss_probe.py',out/(label+'-observations')],cwd=directory,required=False)
assert methods['before-unification']==methods['current-native'] and len(methods['current-native'])==2
(out/'SCOPE.json').write_text(json.dumps(dict(status='DIAGNOSTIC_NO_RELEASE_OR_ACCEPTANCE_CLAIM',same_original_USS_method_AST=True,
sidecar='Reads glibc malloc_info, allocated block counts and native lifetime counters; original USS returned unchanged',
native_or_SDK_modified=False,allocator_settings_changed=False,malloc_trim_called=False),indent=2)+'\n')
