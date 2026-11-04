"""Choose numeric newest final free-threaded CPython from current uv inventory."""
from pathlib import Path
import json
import platform
import re
import subprocess
import sys

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
items = json.loads(subprocess.check_output(['uv', 'python', 'list', '--all-versions', '--all-platforms',
    '--all-arches', '--only-downloads', '--output-format', 'json'], text=True))
(out/'uv-inventory.json').write_text(json.dumps(items, indent=2)+'\n')
arch = platform.machine()
assert arch in ('x86_64', 'aarch64')
matches = [item for item in items if item['implementation'] == 'cpython' and item['variant'] == 'freethreaded'
    and item['os'] == 'linux' and item['libc'] == 'musl' and item['arch'] == arch
    and re.fullmatch(r'3\.\d+\.\d+', item['version'])]
selected = max(matches, key=lambda item: tuple(map(int, item['version'].split('.'))))
(out/'SELECTED.json').write_text(json.dumps(selected, indent=2)+'\n')
subprocess.run(['uv', 'venv', '--managed-python', '--python', selected['key'], '--seed', '/opt/greenlet-acceptance-env'], check=True)
python = '/opt/greenlet-acceptance-env/bin/python'
subprocess.run([python, '-m', 'pip', 'install', '--upgrade', 'build', 'setuptools',
                'pyright==1.1.414', 'auditwheel==6.8.2'], check=True)
subprocess.run([python, '-I', '-B', '/source/tools/acceptance/run.py', '/source', str(out/'acceptance')], check=True)
