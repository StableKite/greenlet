"""Bind the exact prototype wheel RECORD to every installed native payload."""
from importlib import metadata
from pathlib import Path
import base64
import csv
import hashlib
import io
import json
import os
import sys
import zipfile

wheel = Path(sys.argv[1])
out = Path(sys.argv[2])
out.mkdir(exist_ok=False)
assert 'PYTHON_GIL' not in os.environ and not sys._is_gil_enabled() and sys.flags.gil is None
import greenlet
distribution = metadata.distribution('greenlet')
assert distribution.version == greenlet.__version__
installed = {}
for item in distribution.files:
    if item.hash is None:
        continue
    data = distribution.locate_file(item).read_bytes()
    assert item.hash.mode == 'sha256' and item.size == len(data)
    actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
    assert actual == item.hash.value, str(item)
    installed[str(item)] = hashlib.sha256(data).hexdigest()
original = {}
native = {}
with zipfile.ZipFile(wheel) as archive:
    assert archive.testzip() is None
    from email.parser import Parser
    metadata_name = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
    wheel_metadata = Parser().parsestr(archive.read(metadata_name).decode())
    assert wheel_metadata['Version'] == distribution.version
    record = next(name for name in archive.namelist() if name.endswith('.dist-info/RECORD'))
    raw = archive.read(record)
    (out/'ORIGINAL_WHEEL_RECORD.csv').write_bytes(raw)
    for name, encoded, size in csv.reader(io.StringIO(raw.decode())):
        if not encoded:
            assert name == record
            continue
        data = archive.read(name)
        algorithm, expected = encoded.split('=', 1)
        assert algorithm == 'sha256' and int(size) == len(data)
        assert base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode() == expected
        original[name] = hashlib.sha256(data).hexdigest()
        if name.endswith(('.pyd', '.so')):
            installed_path = distribution.locate_file(name)
            assert installed_path.read_bytes() == data
            native[name] = {'installed_path': str(installed_path), 'sha256': original[name], 'bytes': len(data)}
assert len(native) == 3
assert not sys._is_gil_enabled() and sys.flags.gil is None
result = {'status': 'PASS_ORIGINAL_WHEEL_AND_INSTALLED_RECORD_PROTOTYPE_ONLY',
    'wheel': str(wheel), 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
    'wheel_bytes': wheel.stat().st_size, 'original_RECORD': original, 'installed_RECORD': installed,
    'native': native, 'GIL_after': False, 'gil_flag': None, 'runtime': sys.version,
    'release_status': 'SOURCE_BUILT_WHEEL_PUBLICATION_QUALIFIED_SEPARATELY',
    'SI': 'This payload/RECORD audit does not determine SI support; genuine interpreter evidence is recorded separately.'}
(out/'RESULT.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps({'status': result['status'], 'wheel_sha256': result['wheel_sha256'], 'native_files': len(native)}))
