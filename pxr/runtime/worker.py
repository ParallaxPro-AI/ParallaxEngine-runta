"""Runs inside the VM. No Runta credentials or SDK are needed here."""

import hashlib
import json
import sys
import tarfile
from pathlib import Path

GAME = Path('/workspace/game')
SKIP = {'build', 'node_modules', '.git', '__pycache__', '.venv'}


def source_files(root):
    paths = [p for p in root.rglob('*') if not SKIP.intersection(p.relative_to(root).parts)]
    if any(p.is_symlink() for p in paths):
        raise ValueError('game contains symbolic links; copy their contents before downloading')
    return sorted(p for p in paths if p.is_file())


def digest(root):
    result = hashlib.sha256()
    for path in source_files(root):
        result.update(path.relative_to(root).as_posix().encode() + b'\0')
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                result.update(chunk)
        result.update(b'\0')
    return result.hexdigest()


def pack():
    if not (GAME / 'game/main.py').is_file():
        raise ValueError('no generated game/main.py to download')
    before = digest(GAME)
    with tarfile.open('/workspace/game.tar.gz', 'w:gz') as archive:
        for path in source_files(GAME):
            archive.add(path, arcname='game/' + path.relative_to(GAME).as_posix(), recursive=False)
    if digest(GAME) != before:
        raise RuntimeError('game changed during backup; retry pull')
    return before


if __name__ == '__main__':
    if sys.argv[1] == 'pack':
        print(json.dumps({'digest': pack()}))
    elif sys.argv[1] == 'digest':
        print(digest(GAME))
