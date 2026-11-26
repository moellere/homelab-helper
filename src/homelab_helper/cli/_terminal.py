"""Keep a long-running command going after the terminal that started it is gone.

The first live run did everything it was asked from an SSH session that dropped
as the last step returned; SIGHUP killed the process before its commit, so the
hangup is ignored. A later run survived the hangup and then died on its next
progress line: writing to a closed terminal raises ``EIO``, and that stopped
the run between a node's update and its reboot. So the run's stdout and stderr
also stop writing once the terminal is gone. The receipts hold what happened;
the operator reads them with ``show``.
"""

from __future__ import annotations

import contextlib
import signal
import sys
from typing import Any, TextIO


class _HangupTolerantStream:
    """A text stream that goes quiet, instead of raising, once its terminal is gone."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.gone = False

    def write(self, text: str) -> int:
        if not self.gone:
            try:
                return self._stream.write(text)
            except OSError:
                self.gone = True
        return len(text)

    def flush(self) -> None:
        if not self.gone:
            try:
                self._stream.flush()
            except OSError:
                self.gone = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def outlive_the_terminal() -> None:
    """Ignore SIGHUP and stop printing, without failing, once the terminal is gone."""
    # Not the main thread, or a platform without SIGHUP: nothing to arrange.
    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if not isinstance(sys.stdout, _HangupTolerantStream):
        sys.stdout = _HangupTolerantStream(sys.stdout)
    if not isinstance(sys.stderr, _HangupTolerantStream):
        sys.stderr = _HangupTolerantStream(sys.stderr)


__all__ = ["outlive_the_terminal"]
