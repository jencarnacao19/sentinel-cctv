"""A thread pool that only vendor camera calls may use.

Every proprietary camera library Sentinel talks to is synchronous and blocking,
so its calls have to be pushed onto threads. The obvious way to do that --
``asyncio.to_thread`` -- puts them on the interpreter's *default* executor,
which is the same pool that ffprobe, snapshot capture, export muxing, storage
statistics and ONVIF PTZ all use. That pool has ``min(32, cpu_count + 4)``
workers: twelve on the reference host.

The problem is that a vendor call which times out does not stop. Sentinel
abandons the wait, but the worker thread is still inside the library, still
holding an open TLS socket, and only returns when the operating system's own
socket timeout fires -- which can be minutes. Each abandoned call therefore
occupies a pool slot for far longer than the request that started it.

Measured on the reference installation: with twelve such calls in flight, an
unrelated ``asyncio.to_thread`` that normally costs 10 milliseconds took
**18 seconds**. At that point requests stop being answered inside a reverse
proxy's origin window and the proxy substitutes its own error page, so a single
unresponsive camera reads to the operator as the whole product failing.

Keeping vendor work on a small dedicated pool contains that. The blast radius of
an unresponsive camera becomes "vendor calls are slow", which is true, instead
of "Sentinel is down", which is not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_executor: ThreadPoolExecutor | None = None


def get_executor(max_workers: int) -> ThreadPoolExecutor:
    """The process-wide vendor pool, created on first use.

    ``max_workers`` is honoured only by the call that creates the pool; later
    callers get the existing one. There is deliberately no resize: changing the
    worker count while calls are in flight is not something a camera control
    request should be able to do.
    """
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="vendor",
        )
    return _executor


async def run[T](func: Callable[[], T], *, max_workers: int) -> T:
    """Run one blocking vendor call on the dedicated pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_executor(max_workers), func)


def shutdown() -> None:
    """Drop the pool without waiting for abandoned calls to return.

    Called at application shutdown. ``wait=False`` with ``cancel_futures=True``
    is the honest option: a thread stuck inside a vendor library cannot be
    interrupted, and blocking shutdown on it would hold the recorders' own
    shutdown -- and therefore the finalisation of the segment they are writing --
    behind a camera that has already stopped answering. The threads are daemons
    and the process is exiting.
    """
    global _executor
    if _executor is None:
        return
    _executor.shutdown(wait=False, cancel_futures=True)
    _executor = None
