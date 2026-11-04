"""mitmdump entry point. Loaded only by the explicit local capture command."""
import os
from pathlib import Path
from ezmcd.sampling_guard import RuntimeGuard
from ezmcd.storage import read_private

config = read_private(Path(os.environ["EZMCD_RUNTIME_GUARD_CONFIG"]))
addons = [RuntimeGuard(config)]
if config.get("transport"):
    from ezmcd.sampling_local import LocalCaptureLifecycle
    addons.append(LocalCaptureLifecycle(config))
