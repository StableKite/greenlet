"""Prove incorrect public API arguments are actually rejected by strict typing."""
from pathlib import Path
import json
import subprocess
import sys

out = Path(sys.argv[1])
out.mkdir(exist_ok=False)
fixture = out/'negative.py'
fixture.write_text('from greenlet import greenlet, getcurrent, settrace\nsettrace("invalid")\ngreenlet(run=7)\ngetcurrent().gr_context = 9\n')
config = out/'pyrightconfig.json'
config.write_text(json.dumps({'include': [str(fixture.resolve())], 'typeCheckingMode': 'strict', 'pythonVersion': '3.15'}))
argv = [sys.executable, '-m', 'pyright', '--project', str(config), '--pythonpath', sys.executable, '--outputjson']
run = subprocess.run(argv, capture_output=True, timeout=30)
(out/'pyright.stdout.json').write_bytes(run.stdout)
(out/'pyright.stderr.log').write_bytes(run.stderr)
report = json.loads(run.stdout)
errors = [item for item in report['generalDiagnostics'] if item['severity'] == 'error']
assert run.returncode == 1 and report['summary']['warningCount'] == 0
assert len(errors) == 3 and {item['range']['start']['line'] for item in errors} == {1, 2, 3}, errors
assert all(Path(item['file']).resolve() == fixture.resolve() for item in errors)
(out/'RESULT.json').write_text(json.dumps({'status': 'PASS_THREE_REAL_NEGATIVE_PUBLIC_TYPING_CONTROLS',
    'argv': argv, 'expected_Pyright_exit_code': 1, 'actual_Pyright_exit_code': run.returncode,
    'actual_diagnostics': errors}, indent=2)+'\n')
