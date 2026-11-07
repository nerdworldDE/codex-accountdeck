"""PTY process helpers shared by terminal tests on Linux and macOS."""
import os
import select
import subprocess
import time


def wait_for_exit(process, descriptor, timeout=5):
    """Keep reading while waiting: macOS PTYs have much smaller output buffers."""
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if select.select([descriptor], [], [], min(.05, remaining))[0]:
            try:
                os.read(descriptor, 65536)
            except OSError:
                pass  # The PTY can reach EOF just before waitpid observes exit.
    return process.wait(timeout=0)
