"""Background worker: runs blocking calls in threads and delivers results on the Tk thread."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional, Tuple

from intermux.errors import InterMuxError

log = logging.getLogger("intermux.ui")

DoneCallback = Callable[[Any, Optional[Exception]], None]


class Worker:
    """Runs functions in daemon threads and calls on_done(result, error) from the Tk loop."""

    POLL_MS = 50

    def __init__(self, root: Any) -> None:
        self._root = root
        self._results: "queue.Queue[Tuple[DoneCallback, Any, Optional[Exception]]]" = queue.Queue()
        self._active = True
        self._root.after(self.POLL_MS, self._poll)

    def submit(self, fn: Callable[..., Any], *args: Any, on_done: DoneCallback, **kwargs: Any) -> None:
        """Run fn(*args, **kwargs) off the Tk thread; on_done receives the result or the exception."""

        def runner() -> None:
            result: Any = None
            error: Optional[Exception] = None
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, InterMuxError):
                    log.info("%s", exc)
                else:
                    log.exception("background task failed")
                error = exc
            self._results.put((on_done, result, error))

        threading.Thread(target=runner, name="intermux-worker", daemon=True).start()

    def shutdown(self) -> None:
        """Stop delivering results (called when the window closes)."""
        self._active = False

    def _poll(self) -> None:
        while True:
            try:
                on_done, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                on_done(result, error)
            except Exception:
                log.exception("UI callback failed")
        if self._active:
            try:
                self._root.after(self.POLL_MS, self._poll)
            except Exception:
                # The Tk root is gone; stop polling.
                self._active = False
