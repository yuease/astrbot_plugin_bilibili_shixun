"""Prevent concurrent plugin instances from using the same runtime."""

import os

from .errors import PluginError


class RuntimeLease:
    def __init__(self, root):
        self.path = root / "instance.lock"
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise PluginError("RUNTIME_BUSY", status=409) from None
        self.stream = stream

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
