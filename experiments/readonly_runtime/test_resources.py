from pathlib import Path
import tempfile
import unittest

from ezmcd.errors import EzMcdError
from ezmcd.storage import secure_directory
import resources


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.directory=self.root/'capture'
        self.paths=[self.root/'.sampling-leaf-fixture-api'/'api.pem',
                    self.root/'.sampling-leaf-fixture-img'/'img.pem']
        for p in self.paths:
            secure_directory(p.parent);p.write_bytes(b'owned-synthetic-leaf');p.chmod(0o600)
        resources.record(self.directory,self.paths)

    def test_only_recorded_leaves_removed_and_repeat_is_noop(self):
        unrelated=self.root/'.sampling-leaf-other';secure_directory(unrelated)
        (unrelated/'api.pem').write_bytes(b'unknown-must-stay')
        self.assertFalse(resources.removed(self.root,self.directory))
        resources.cleanup_previous(self.root,self.directory)
        self.assertTrue(resources.removed(self.root,self.directory))
        self.assertEqual((unrelated/'api.pem').read_bytes(),b'unknown-must-stay')
        resources.cleanup_previous(self.root,self.directory)

    def test_changed_leaf_preserves_whole_set(self):
        self.paths[1].write_bytes(b'changed')
        with self.assertRaises(EzMcdError):resources.cleanup_previous(self.root,self.directory)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_unknown_files_and_symlink_do_not_get_deleted(self):
        extra=self.paths[1].parent/'user-file';extra.write_bytes(b'keep')
        with self.assertRaises(EzMcdError):resources.cleanup_previous(self.root,self.directory)
        self.assertTrue(all(p.exists() for p in self.paths));self.assertTrue(extra.exists())
        extra.unlink();self.paths[1].unlink();self.paths[1].symlink_to(self.paths[0])
        with self.assertRaises(EzMcdError):resources.cleanup_previous(self.root,self.directory)
        self.assertTrue(self.paths[0].is_file());self.assertTrue(self.paths[1].is_symlink())


if __name__=='__main__':unittest.main()
