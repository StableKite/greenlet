from pathlib import Path
import importlib.util,sys
spec=importlib.util.spec_from_file_location('original_evidence_carrier',Path(__file__).parents[1]/'acceptance/prepare_evidence.py')
carrier=importlib.util.module_from_spec(spec);spec.loader.exec_module(carrier)
source,destination=map(Path,sys.argv[1:])
venvs={p.name for p in source.iterdir() if p.is_dir() and (p/'pyvenv.cfg').is_file()}
assert len(venvs) in (0, 1, 2)
carrier.EXCLUDED_ENVIRONMENTS=carrier.EXCLUDED_ENVIRONMENTS|venvs
result=carrier.prepare(source,destination)
assert all(any(row['path']==name for row in result['excluded']) for name in venvs)
print('Original source, native binaries and logs retained; the private reproducible environment excluded with recorded inventories.')
