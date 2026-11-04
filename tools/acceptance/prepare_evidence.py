"""Copy immutable evidence bytes without dereferencing container-only venv links."""
from pathlib import Path
import hashlib
import json
import os
import sys

EXCLUDED_ENVIRONMENTS = {'consumer', 'qualified-release/consumer',
                         'acceptance/qualified-release/consumer'}


def prepare(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Evidence source and carrier must be separate directories.')
    if not source.is_dir():
        raise ValueError('Original evidence directory is missing.')
    destination.mkdir(exist_ok=False)
    files, excluded = {}, []
    for current, directories, names in os.walk(source, followlinks=False):
        current = Path(current)
        for name in list(directories):
            path = current / name
            relative = path.relative_to(source).as_posix()
            if relative in EXCLUDED_ENVIRONMENTS or path.is_symlink():
                directories.remove(name)
                excluded.append({'path': relative, 'reason': 'reproducible consumer venv' if
                                 relative in EXCLUDED_ENVIRONMENTS else 'directory symlink; never followed'})
        for name in names:
            path = current / name
            relative = path.relative_to(source)
            if path.is_symlink():
                excluded.append({'path': relative.as_posix(), 'reason': 'symlink; never followed',
                                 'link_target': os.readlink(path)})
                continue
            data = path.read_bytes()
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
            digest = hashlib.sha256(data).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Evidence copy checksum mismatch.')
            files[relative.as_posix()] = {'sha256': digest, 'bytes': len(data)}
    manifest = {'status': 'EVIDENCE_BYTE_COPY_COMPLETE_NOT_AN_ACCEPTANCE_CLAIM',
                'source': str(source), 'git_head': os.environ.get('GITHUB_SHA'),
                'files': files, 'excluded': excluded,
                'scope': 'Original logs, receipts, source and wheels retained. Reproducible consumer venvs excluded; their commands, runtime reports and installed RECORD audits remain.'}
    with (destination / 'EVIDENCE_CARRIER.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
    return manifest


if __name__ == '__main__':
    result = prepare(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps({'status': result['status'], 'files': len(result['files']),
                      'excluded_paths': len(result['excluded'])}))
