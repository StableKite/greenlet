"""Preserve installed, RECORD-declared auditwheel libraries in a source fixture."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import sys
import zipfile


def copy_support_libraries(wheel, installed_site, fixture_src):
    libraries = {}
    installed_site, fixture_src = installed_site.resolve(), fixture_src.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            relative = PurePosixPath(item.filename)
            if not relative.parts or relative.parts[0] != 'greenlet.libs' or item.is_dir():
                continue
            if relative.is_absolute() or '..' in relative.parts or '\\' in item.filename:
                raise ValueError('Unsafe wheel support-library path.')
            installed = installed_site.joinpath(*relative.parts)
            target = fixture_src.joinpath(*relative.parts)
            if not installed.resolve().is_relative_to(installed_site) or installed.is_symlink():
                raise ValueError('Support library escaped the installed distribution.')
            if not target.resolve().is_relative_to(fixture_src):
                raise ValueError('Support library escaped the test fixture.')
            payload = archive.read(item)
            if installed.read_bytes() != payload:
                raise ValueError('Installed support library differs from original wheel bytes.')
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(payload)
            libraries[item.filename] = {'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    return libraries


if __name__ == '__main__':
    wheel, installed, fixture, result = map(Path, sys.argv[1:])
    libraries = copy_support_libraries(wheel, installed, fixture)
    with result.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'COPIED_EXACT_INSTALLED_WHEEL_SUPPORT_LIBRARIES', 'files': libraries}, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': 'COPIED_EXACT_INSTALLED_WHEEL_SUPPORT_LIBRARIES', 'files': len(libraries)}))
