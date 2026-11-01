"""Opt-in local App lifecycle for capture; no UI, credentials or API access."""
import hashlib
import os
from pathlib import Path
import plistlib
import signal
import subprocess

from .errors import EzMcdError
from .protocol.signing import BUILD


class SamplingApp:
    def __init__(self, binary):
        self.binary = Path(binary).expanduser().absolute()
        self.launcher = None
        self.launch_attempted = False
        self.signalled = set()

    @staticmethod
    def command(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise EzMcdError("sampling_app_state_unreadable") from None

    @staticmethod
    def verify_binary(path):
        try:
            if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
                raise EzMcdError("sampling_managed_app_unsupported")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != BUILD:
                    raise EzMcdError("sampling_managed_app_unsupported")
        except OSError:
            raise EzMcdError("sampling_managed_app_unsupported") from None

    def validate(self):
        """Match the configured binary to the bundle we will ask macOS to open."""
        self.verify_binary(self.binary)
        bundles = [p for p in self.binary.parents if p.name == "McdApp.app"]
        if self.binary.name != "McdApp" or not bundles:
            raise EzMcdError("sampling_managed_app_unsupported")
        bundle = bundles[-1]
        candidates = (bundle / "Info.plist", bundle / "WrappedBundle" / "Info.plist")
        try:
            info_path = next(p for p in candidates if p.is_file())
            info = plistlib.loads(info_path.read_bytes())
            if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.mcdonalds.gma.cn"
                    or info.get("CFBundleExecutable") != "McdApp"
                    or (info_path.parent / "McdApp").resolve() != self.binary.resolve()):
                raise EzMcdError("sampling_managed_app_unsupported")
        except (OSError, ValueError, StopIteration):
            raise EzMcdError("sampling_managed_app_unsupported") from None
        return bundle

    def pids(self):
        result = self.command(["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "McdApp"])
        values = result.stdout.split()
        if result.returncode == 1 and not values:
            return []
        if result.returncode != 0 or not values or any(not v.isdecimal() or int(v) <= 1 for v in values):
            raise EzMcdError("sampling_app_state_unreadable")
        return sorted(set(map(int, values)))

    def identity(self, pid):
        # lstart detects PID reuse; comm retains paths with spaces, including
        # macOS-translocated wrappers. Never signal by process name alone.
        result = self.command(["/bin/ps", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "comm="])
        if result.returncode == 1 and not result.stdout.strip():
            return None
        fields = result.stdout.strip().split(maxsplit=6)
        if (result.returncode or len(fields) != 7 or fields[0] != str(os.getuid())):
            raise EzMcdError("sampling_managed_app_process_changed")
        path = Path(fields[-1])
        if not path.is_absolute() or path.name != "McdApp" or path.parent.name != "McdApp.app":
            raise EzMcdError("sampling_managed_app_process_changed")
        return (pid, *fields)

    def request_close(self):
        """One SIGTERM per verified process identity; never SIGKILL or retry."""
        verified = []
        for pid in self.pids():
            identity = self.identity(pid)
            if identity is None or identity in self.signalled:
                continue
            self.verify_binary(Path(identity[-1]))
            verified.append(identity)
        # Verify the whole set before touching any process.
        for identity in verified:
            current = self.identity(identity[0])
            if current is None:
                continue
            if current != identity:
                raise EzMcdError("sampling_managed_app_process_changed")
            self.signalled.add(identity)
            try:
                os.kill(identity[0], signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                raise EzMcdError("sampling_managed_app_close_failed") from None

    def start(self):
        if self.launch_attempted:
            raise EzMcdError("sampling_managed_app_launch_already_attempted")
        bundle = self.validate()
        if self.pids():
            raise EzMcdError("sampling_environment_changed")
        self.launch_attempted = True
        try:
            # -W keeps a handle until LaunchServices finishes AND the App exits.
            # A delayed launch must not outlive the blocking proxy's cleanup.
            self.launcher = subprocess.Popen(["/usr/bin/open", "-W", "-a", str(bundle)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise EzMcdError("sampling_managed_app_launch_failed") from None

    def launch_pending(self):
        return self.launcher is not None and self.launcher.poll() is None

    def launch_failed(self):
        return self.launcher is not None and self.launcher.poll() not in (None, 0)
