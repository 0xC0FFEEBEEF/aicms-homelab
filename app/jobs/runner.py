from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable


class ImmediateFuture(Future[Any]):
    def __init__(self, result: Any = None):
        super().__init__()
        self.set_result(result)


class JobRunner:
    def __init__(self, *, sync: bool = False, max_workers: int = 4):
        self.sync = sync
        self.max_workers = max_workers
        self._lock = Lock()
        self._executor = None if sync else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicms")
        self._futures: set[Future[Any]] = set()

    def submit(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        if self.sync:
            result = func(*args, **kwargs)
            return ImmediateFuture(result)

        assert self._executor is not None
        future = self._executor.submit(func, *args, **kwargs)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future

    def _discard_future(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
