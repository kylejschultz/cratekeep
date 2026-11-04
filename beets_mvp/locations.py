"""Library-contained folder selection; never creates directories while browsing."""
import os
import re
from pathlib import Path


def directory(root: Path, relative: str, *, allow_root=False) -> Path:
    if not isinstance(relative, str) or '\\' in relative or '\0' in relative:
        raise ValueError('Choose a folder inside the Library.')
    parts = relative.split('/') if relative else []
    if len(relative) > 4096 or any(part in {'', '.', '..'} or len(os.fsencode(part)) > 255 for part in parts):
        raise ValueError('Choose a folder inside the Library; relative traversal is not allowed.')
    if not parts and not allow_root:
        raise ValueError('Choose an album folder inside the Library, not the Library root.')
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError('The destination must contain only real folders, without symbolic links.')
    if target != root and root not in target.resolve().parents:
        raise ValueError('Choose a folder inside the Library.')
    return target


def source_directory(root: Path, items, manual='') -> Path:
    parents = {Path(os.fsdecode(item.path)).parent for item in items}
    if not parents:
        raise ValueError('This album has no files to move.')
    if manual:
        base = directory(root, manual)
        if all(parent == base or base in parent.parents for parent in parents):
            return base
        raise ValueError('Files are outside the remembered album folder. Use Beets naming to organize them first.')
    base = Path(os.path.commonpath(list(parents)))
    disc = re.compile(r'(?:cd|disc|disk)[ _.-]*\d+|\d+', re.IGNORECASE)
    # A one-disc subset in Album/Disc 1 still keeps its disc subfolder.
    if len(parents) == 1 and disc.fullmatch(base.name):
        base = base.parent
    if base == root or root not in base.parents or any(
        len(parent.relative_to(base).parts) > 1 or
        any(not disc.fullmatch(part) for part in parent.relative_to(base).parts)
        for parent in parents
    ):
        raise ValueError('Files span separate album folders. Use Beets naming to organize them before choosing a manual folder.')
    return base


def browse(root: Path, relative: str) -> dict:
    folder = directory(root, relative, allow_root=True)
    if not folder.is_dir():
        raise ValueError('That Library folder is unavailable.')
    entries = [{'name': child.name, 'path': str(child.relative_to(root))}
               for child in sorted(folder.iterdir(), key=lambda child: child.name.casefold())
               if child.is_dir() and not child.is_symlink()]
    return {'path': relative, 'entries': entries}
