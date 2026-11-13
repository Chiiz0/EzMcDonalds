"""Experimental owned child entrypoint; not included in the installed package."""
import os
from pathlib import Path
import time

from ezmcd.models import fingerprint
from ezmcd.storage import Store, read_private, write_private
from transaction import Transaction
from handler import RequestHandler
from transport_fence import Fence, Lifecycle
from process_receipt import record_self

config=read_private(Path(os.environ['EZMCD_READONLY_CONFIG']))
assert config['purpose']=='readonly_runtime_experiment'
record_self(Path(config['phase_directory']))
store=Store(Path(config['store_root']))
account=store.account(config['alias'])
if config['phase']=='collect':
    assert fingerprint(account)==config['account_fingerprint']
    transaction=Transaction.prepare(config['transaction_directory'],account,
        config['original_config'].encode('utf-8'),seconds=config['seconds'])
elif config['phase']=='recover':
    transaction=Transaction(config['transaction_directory'],account)
else:
    raise ValueError('invalid_phase')
write_private(Path(config['phase_directory'])/'guard-identity.json',
    {'epoch':transaction.epoch,'phase':config['phase']})
addons=[Fence(config,time.time),RequestHandler(transaction),Lifecycle(config)]
