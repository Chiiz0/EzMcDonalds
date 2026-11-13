"""Record and remove only fingerprinted leaf keys owned by this capture."""
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from ezmcd.errors import EzMcdError
from ezmcd.storage import read_private,write_private


def record(directory,paths):
    target=directory/'resources.json'
    if target.exists():
        write_private(directory/'resources-history'/(uuid.uuid4().hex+'.json'),read_private(target))
    write_private(target,{'leaves':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]})


def owned_paths(store_root,directory):
    manifest=directory/'resources.json'
    if not manifest.exists():return []
    source=read_private(manifest)
    if not isinstance(source.get('leaves'),list) or len(source['leaves'])!=2:
        raise EzMcdError('readonly_capture_resource_record_invalid')
    paths=[]
    for leaf in source['leaves']:
        p=Path(leaf['path'])
        if (not p.is_absolute() or p.parent.parent!=store_root or p.name not in ('api.pem','img.pem')
                or not re.fullmatch(r'\.sampling-leaf-[A-Za-z0-9_-]+',p.parent.name)
                or any(x.is_symlink() for x in (p,*p.parents))):
            raise EzMcdError('readonly_capture_resource_record_invalid')
        if p.exists():
            s=p.stat()
            if (not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_size>65536
                    or hashlib.sha256(p.read_bytes()).hexdigest()!=leaf['sha256']
                    or list(p.parent.iterdir())!=[p]):
                raise EzMcdError('readonly_capture_resource_changed_do_not_delete')
        paths.append(p)
    if len(set(paths))!=2:raise EzMcdError('readonly_capture_resource_record_invalid')
    return paths


def cleanup_previous(store_root,directory):
    """Caller holds account/CA leases and has verified old children stopped."""
    paths=owned_paths(store_root,directory)
    for p in paths:
        if p.exists():p.unlink();p.parent.rmdir()


def removed(store_root,directory):
    return all(not p.exists() for p in owned_paths(store_root,directory))
