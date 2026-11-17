"""Check the actual Qt/OS lock used to exclude simultaneous app processes."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QLockFile


class SingleInstanceLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_another_process_is_excluded_until_the_owner_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / '.upscale.lock')
            script = (
                'import sys; from PySide6.QtCore import QCoreApplication, QLockFile; '
                'app=QCoreApplication([]); lock=QLockFile(sys.argv[1]); '
                'lock.setStaleLockTime(0); print(lock.tryLock(0), flush=True); '
                'sys.stdin.readline(); lock.unlock()'
            )
            owner = subprocess.Popen([sys.executable, '-c', script, lock_path],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'True')
                contender = QLockFile(lock_path)
                contender.setStaleLockTime(0)
                self.assertFalse(contender.tryLock(0))
                self.assertEqual(contender.error(), QLockFile.LockError.LockFailedError)
                owner.communicate('\n', timeout=10)
                self.assertEqual(owner.returncode, 0)
                self.assertTrue(contender.tryLock(0))
                contender.unlock()
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate()


if __name__ == '__main__':
    unittest.main()
