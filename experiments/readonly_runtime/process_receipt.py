"""Read-only identity checks for the specifically recorded sampler child."""
import os
import subprocess
import time

from ezmcd.errors import EzMcdError
from ezmcd.storage import read_private,write_private


def identity(pid):
    if type(pid) is not int or pid<=1:raise ValueError('invalid_child_pid')
    r=subprocess.run(['/bin/ps','-p',str(pid),'-o','uid=','-o','lstart=','-o','comm='],
        capture_output=True,text=True,timeout=5)
    if r.returncode==1 and not r.stdout.strip():return None
    fields=r.stdout.strip().split(maxsplit=6)
    if r.returncode or len(fields)!=7 or not fields[0].isdecimal():
        raise EzMcdError('readonly_capture_child_identity_unreadable')
    return {'pid':pid,'fields':fields}


def record_self(directory):
    current=identity(os.getpid())
    if current is None or current['fields'][0]!=str(os.getuid()):
        raise EzMcdError('readonly_capture_child_identity_unreadable')
    write_private(directory/'owned-child.json',current)


def require_previous_children_stopped(directory,*,seconds=7):
    receipts=[read_private(p) for p in directory.glob('*/owned-child.json')]
    until=time.monotonic()+seconds
    while True:
        live=[]
        for r in receipts:
            current=identity(r['pid'])
            if current==r:live.append(r)
            # Missing PID or a different start time is evidence that this
            # particular child is terminal. Never signal an unrelated process.
        if not live:return
        if time.monotonic()>=until:
            raise EzMcdError('readonly_capture_previous_child_still_running')
        time.sleep(.1)
