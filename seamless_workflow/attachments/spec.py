"""Durable, immutable file attachment configuration."""
from dataclasses import dataclass, asdict
import os
from seamless.checksum.canonical import FILE_CELLTYPES, DIRECTORY_CELLTYPES


@dataclass(frozen=True)
class AttachmentSpec:
    path: str
    mode: str = 'rw'
    authority: str = 'file'
    persistent: bool = True
    driver: str = 'file'

    def __post_init__(self):
        path = os.fspath(self.path)
        if not isinstance(path, str) or not path or '\0' in path:
            raise ValueError('mount path must be a nonempty text path')
        object.__setattr__(self, 'path', path)
        if self.mode not in {'r', 'w', 'rw'}:
            raise ValueError('mode must be r, w or rw')
        if self.authority not in {'file', 'cell', 'file-strict'}:
            raise ValueError('invalid mount authority')
        if type(self.persistent) is not bool:
            raise TypeError('persistent must be bool')
        if self.authority == 'file-strict' and ('r' not in self.mode or not self.persistent):
            raise ValueError('file-strict requires sensing and persistent=True')
        if self.mode == 'w' and self.authority == 'file':
            object.__setattr__(self, 'authority', 'cell')

    def to_graph(self):
        if self.driver != 'file': raise ValueError('Only file mounts are serializable')
        result = asdict(self)
        result.pop('driver')
        return result


def validate_celltype(celltype):
    if celltype not in FILE_CELLTYPES | DIRECTORY_CELLTYPES:
        raise TypeError(f'Celltype {celltype!r} is not mountable')
