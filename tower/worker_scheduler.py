"""Session-wide worker permits, fair bounded queues and live mode changes.

The UI and Sampler's timer coordinator do not execute work here. Reducing the
budget drains running task trees; already accepted queued roots retain their
Futures and run under the new budget. No transition cancels or replays work.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor, FIRST_COMPLETED, ALL_COMPLETED, wait
from dataclasses import dataclass
import threading
import time

_CURRENT = threading.local()


def current_scheduler():
    """Scheduler governing this executing task, never another session/thread."""
    return getattr(_CURRENT, "scheduler", None)


@contextmanager
def collection_lane(*, workers=4, mode="multi"):
    """Borrow the session source budget or own a bounded headless scheduler.

Native adapters must use gather_children rather than an uncooperative wait
inside a worker. Closing cancels pending children and collects accepted results.
    """
    scheduler = current_scheduler()
    owned = scheduler is None
    if owned:
        scheduler = WorkerScheduler(workers=workers, mode=mode, lanes=("source",))
    lane = scheduler.lane("source")
    try:
        yield scheduler, lane
    finally:
        lane.shutdown(wait=True, cancel_futures=True)
        if owned:
            scheduler.shutdown(wait=True, cancel_futures=True)


class WorkerQueueFull(RuntimeError):
    """A request was rejected before acceptance; an accepted task is never lost."""


class _Future(Future):
    def __init__(self, scheduler):
        super().__init__()
        self.scheduler = scheduler

    def cancel(self):
        return self.scheduler._cancel(self)

    def result(self, timeout=None):
        # Public timed waits must not start an opaque callable whose duration
        # could exceed the caller's timeout. Untimed waits can safely help.
        if timeout is None and self.scheduler._stack():
            return self.scheduler._result(self)
        return super().result(timeout)


@dataclass(eq=False)
class _Task:
    future: _Future
    fn: object
    args: tuple
    kwargs: dict
    lane: str
    owner: object
    root: int
    sequence: int = 0
    descendant: bool = False
    phase: str = "queued"
    physical: bool = False
    borrowed: bool = False
    parked: bool = False


class WorkerLane:
    """Executor-compatible, owner-scoped view of a shared scheduler."""
    def __init__(self, scheduler, name, owner):
        self.scheduler, self.name, self.owner = scheduler, name, owner
        self.closed = False
        self._work_queue = _OwnerQueue(self)
        self._max_workers = scheduler._limits[name]

    def submit(self, fn, /, *args, **kwargs):
        return self.scheduler._submit(self, fn, args, kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.scheduler._close_lane(self, wait, cancel_futures)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


class _OwnerQueue:
    """Read-only compatibility with executor queue-size diagnostics."""
    def __init__(self, lane):
        self.lane = lane

    def qsize(self):
        with self.lane.scheduler._lock:
            return sum(task.phase == "queued" and task.owner is self.lane.owner
                       for task in self.lane.scheduler._tasks.values())


class WorkerScheduler:
    current = staticmethod(current_scheduler)

    def __init__(self, workers=4, mode="multi", *, max_queue=512, lanes=None, aggregate_limit=None):
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("workers must be an integer from 1 to 32")
        if mode not in ("single", "multi"):
            raise ValueError("worker mode must be single or multi")
        if type(max_queue) is not int or max_queue < 1:
            raise ValueError("worker queue must have a positive integer limit")
        if aggregate_limit is not None and (type(aggregate_limit) is not int or aggregate_limit < 1):
            raise ValueError("aggregate worker limit must be a positive integer")
        self._limits = dict(source=workers, gpu=min(workers, 4), research=1, notification=1)
        if lanes is not None:
            if not lanes or any(name not in self._limits for name in lanes):
                raise ValueError("choose one or more known worker lanes")
            self._limits = {name: self._limits[name] for name in dict.fromkeys(lanes)}
        self.multi_limit = sum(self._limits.values())
        if aggregate_limit is not None:
            self.multi_limit = min(self.multi_limit,aggregate_limit)
        self._executor = ThreadPoolExecutor(max_workers=self.multi_limit, thread_name_prefix="tower-worker")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._local = threading.local()
        self._queues = {name: deque() for name in self._limits}
        self._rotation = deque(self._limits)
        self._tasks = {}
        self._roots = {}
        self._running_roots = {}
        self._active = {name: 0 for name in self._limits}
        self._resuming = {name: 0 for name in self._limits}
        self._lane_queued = {name: 0 for name in self._limits}
        self._child_queued = {}
        self._running = self._queued = self._seq = self._generation = self._rejected = 0
        self._mode = self._target = mode
        self._draining = set()
        self._closed = False
        self.max_queue = max_queue

    def _stack(self):
        return getattr(self._local, "stack", ())

    def lane(self, name, *, owner=None, limit=None):
        name = "notification" if name == "notify" else name
        with self._lock:
            if self._closed:
                raise RuntimeError("worker scheduler is closed")
            if name not in self._limits:
                raise ValueError("unknown worker lane: " + str(name))
            if limit is not None and limit != self._limits[name]:
                raise ValueError("lane limits are fixed by the session worker budget")
        return WorkerLane(self, name, object() if owner is None else owner)

    def status(self):
        with self._lock:
            return dict(mode=self._mode, target=self._target,
                        pending=self._mode != self._target,
                        limit=1 if self._mode == "single" else self.multi_limit,
                        target_limit=1 if self._target == "single" else self.multi_limit,
                        running=self._running, queued=self._queued,
                        draining=len(self._draining), generation=self._generation,
                        closed=self._closed, rejected=self._rejected,
                        lanes={name: dict(running=self._active[name], queued=self._lane_queued[name], limit=limit)
                               for name, limit in self._limits.items()})

    def request_mode(self, mode):
        if mode not in ("single", "multi"):
            raise ValueError("worker mode must be single or multi")
        with self._lock:
            if self._closed:
                raise RuntimeError("worker scheduler is closed")
            if mode != self._target:
                self._generation += 1
                self._target = mode
                if mode == "multi":
                    self._mode = mode
                    self._draining.clear()
                elif self._mode != "single":
                    self._draining = set(self._running_roots)
                    self._settle_mode()
        self._pump()
        return self.status()

    def _settle_mode(self):
        self._draining.intersection_update(self._roots)
        if self._target == "single" and not self._draining:
            self._mode = "single"

    def _submit(self, lane, fn, args, kwargs):
        if not callable(fn):
            raise TypeError("worker task must be callable")
        with self._lock:
            if self._closed or lane.closed:
                raise RuntimeError("worker lane is closed")
            stack = self._stack()
            reserve = bool(stack and self._child_queued.get(stack[-1].root, 0) < 4
                           and self._queued < self.max_queue + self.multi_limit * 4)
            if self._queued >= self.max_queue and not reserve:
                self._rejected += 1
                raise WorkerQueueFull("background queue is full; this request was not accepted")
            self._seq += 1
            root = stack[-1].root if stack else self._seq
            future = _Future(self)
            task = _Task(future, fn, args, kwargs, lane.name, lane.owner, root,
                         sequence=self._seq, descendant=bool(stack))
            self._tasks[future] = task
            self._roots[root] = self._roots.get(root, 0) + 1
            self._queues[lane.name].append(future)
            self._queued += 1
            self._lane_queued[lane.name] += 1
            if task.descendant:
                self._child_queued[root] = self._child_queued.get(root, 0) + 1
        self._pump()
        return future

    def _eligible(self, task):
        return not (self._mode != self._target and task.root not in self._draining)

    def _claim(self, task, *, physical, borrowed=False):
        if task.phase != "queued":
            return False
        task.phase, task.physical, task.borrowed = "running", physical, borrowed
        try:
            self._queues[task.lane].remove(task.future)
        except ValueError:
            pass
        self._queued -= 1
        self._lane_queued[task.lane] -= 1
        self._unqueue_child(task)
        if not task.future.set_running_or_notify_cancel():
            raise RuntimeError("a cancelled worker task remained in the admission queue")
        if not borrowed:
            self._active[task.lane] += 1
        if physical:
            self._running += 1
            self._running_roots[task.root] = self._running_roots.get(task.root, 0) + 1
        return True

    def _next(self):
        for _ in range(len(self._rotation)):
            name = self._rotation[0]
            self._rotation.rotate(-1)
            if self._active[name] >= self._limits[name] or self._resuming[name]:
                continue
            queue = self._queues[name]
            for _ in range(len(queue)):
                future = queue.popleft()
                task = self._tasks.get(future)
                if task is None or task.phase != "queued":
                    continue
                if self._eligible(task):
                    return task
                queue.append(future)
        return None

    def _pump(self):
        with self._lock:
            budget = 1 if self._mode == "single" else self.multi_limit
            while self._running < budget:
                task = self._next()
                if task is None:
                    return
                self._claim(task, physical=True)
                # Only admitted permits enter the underlying executor queue.
                # This makes request_mode independent of private executor state.
                self._executor.submit(self._execute, task)

    def _execute(self, task):
        stack = list(self._stack())
        self._local.stack = stack + [task]
        previous_scheduler = current_scheduler()
        _CURRENT.scheduler = self
        thread = threading.current_thread()
        old_name = thread.name
        thread.name = "tower-" + dict(source="src", gpu="gpu", research="research", notification="notify")[task.lane]
        try:
            try:
                result = task.fn(*task.args, **task.kwargs)
            except BaseException as exc:
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)
        finally:
            # Future callbacks execute in this task context. Children registered
            # by a callback join its drain tree before the parent retires.
            self._local.stack = stack
            _CURRENT.scheduler = previous_scheduler
            thread.name = old_name
            with self._lock:
                close = self._retire(task)
            self._pump()
            if close:
                self._executor.shutdown(wait=False)

    def _unqueue_child(self, task):
        if task.descendant:
            left = self._child_queued[task.root] - 1
            if left:
                self._child_queued[task.root] = left
            else:
                self._child_queued.pop(task.root, None)

    def _retire(self, task):
        self._tasks.pop(task.future, None)
        if task.phase == "running":
            if not task.borrowed and not task.parked:
                self._active[task.lane] -= 1
            if task.physical:
                self._running -= 1
                left = self._running_roots[task.root] - 1
                if left:
                    self._running_roots[task.root] = left
                else:
                    self._running_roots.pop(task.root, None)
        task.phase = "done"
        left = self._roots[task.root] - 1
        if left:
            self._roots[task.root] = left
        else:
            self._roots.pop(task.root, None)
        self._settle_mode()
        self._condition.notify_all()
        return self._closed and not self._tasks

    def _help(self, futures):
        stack = self._stack()
        if not stack:
            return False
        with self._lock:
            ordered = sorted(futures, key=lambda future: self._tasks[future].sequence if future in self._tasks else 0)
            for future in ordered:
                task = self._tasks.get(future)
                if task is None or task.phase != "queued":
                    continue
                borrowed = (self._active[task.lane] >= self._limits[task.lane] and
                            any(parent.lane == task.lane and not parent.parked and not parent.borrowed
                                for parent in stack))
                if self._active[task.lane] >= self._limits[task.lane] and not borrowed:
                    continue
                if self._mode != self._target and stack[-1].root in self._draining:
                    # An explicitly awaited cross-root dependency is itself part
                    # of the running root's drain; it cannot be held behind it.
                    self._draining.add(task.root)
                self._claim(task, physical=False, borrowed=borrowed)
                break
            else:
                return False
        self._execute(task)
        return True

    @contextmanager
    def _cooperate(self):
        """Park only a waiting task's lane permit, retaining its worker permit.

        Opposite-lane parents can then collect independent children without a
        resource-admission cycle. Reacquisition precedes any parent execution.
        """
        task = self._stack()[-1]
        released = not task.borrowed and not task.parked
        if released:
            with self._lock:
                task.parked = True
                self._active[task.lane] -= 1
                self._condition.notify_all()
            self._pump()
        try:
            yield
        finally:
            if released:
                with self._condition:
                    self._resuming[task.lane] += 1
                    try:
                        self._condition.wait_for(lambda: self._active[task.lane] < self._limits[task.lane])
                        self._active[task.lane] += 1
                        task.parked = False
                    finally:
                        self._resuming[task.lane] -= 1

    def _result(self, future):
        if future.done():
            return Future.result(future)
        if any(task.future is future for task in self._stack()) and not future.done():
            raise RuntimeError("cyclic worker dependency")
        with self._cooperate():
            while not future.done():
                if not self._help((future,)):
                    with future._condition:
                        if not future.done():
                            future._condition.wait(.02)
        return Future.result(future)

    def gather_children(self, futures, *, timeout=.5, return_when=FIRST_COMPLETED):
        """Wait or execute one bounded child using this worker's existing permit.

        This explicit native-operation API may spend longer than timeout when
        helping an opaque child. Public Future.result(timeout) remains strict.
        The caller's subprocess timeouts and stop checks bound native work.
        """
        futures = set(futures)
        if not self._stack():
            return wait(futures, timeout=timeout, return_when=return_when)
        done = {future for future in futures if future.done()}
        if not futures or len(done) == len(futures) or return_when == FIRST_COMPLETED and done:
            return done, futures - done
        if any(task.future in futures for task in self._stack()):
            raise RuntimeError("cyclic worker dependency")
        with self._cooperate():
            return self._gather(futures, timeout, return_when)

    def _gather(self, futures, timeout, return_when):
        deadline = None if timeout is None else time.monotonic() + max(0., timeout)
        helped = False
        while True:
            done = {future for future in futures if future.done()}
            if (not futures or len(done) == len(futures) or
                    return_when == FIRST_COMPLETED and done):
                return done, futures - done
            if helped and deadline is not None and time.monotonic() >= deadline:
                return done, futures - done
            if self._help(futures - done):
                helped = True
                continue
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return done, futures - done
            wait(futures - done, timeout=min(.02, remaining) if remaining is not None else .02,
                 return_when=FIRST_COMPLETED)

    def _cancel(self, future):
        with self._lock:
            task = self._tasks.get(future)
            if task is None:
                return Future.cancel(future)
            if task.phase == "cancelling":
                return Future.cancel(future)
            if task.phase != "queued":
                return False
            task.phase = "cancelling"
            self._queued -= 1
            self._lane_queued[task.lane] -= 1
            self._unqueue_child(task)
            self._queues[task.lane].remove(future)
        try:
            cancelled = Future.cancel(future)
            if cancelled:
                # stdlib wait/as_completed require CANCELLED_AND_NOTIFIED.
                future.set_running_or_notify_cancel()
            return cancelled
        finally:
            with self._lock:
                close = self._retire(task)
            self._pump()
            if close:
                self._executor.shutdown(wait=False)

    def _close_lane(self, lane, wait_for, cancel_futures):
        if wait_for and any(task.owner is lane.owner for task in self._stack()):
            raise RuntimeError("cannot wait for a worker lane from its own task")
        with self._lock:
            lane.closed = True
            futures = [task.future for task in self._tasks.values() if task.owner is lane.owner]
        if cancel_futures:
            for future in futures:
                future.cancel()
        if wait_for:
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    pass
            with self._condition:
                self._condition.wait_for(lambda: not any(task.owner is lane.owner for task in self._tasks.values()))

    def shutdown(self, wait=True, *, cancel_futures=False):
        if wait and self._stack():
            raise RuntimeError("cannot wait for the scheduler from its own worker")
        with self._lock:
            self._closed = True
            futures = list(self._tasks)
        if cancel_futures:
            for future in futures:
                future.cancel()
        # Workers can still submit bounded children of an accepted running root
        # only before close; shutdown rejects new work like a normal executor.
        if wait:
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    pass
            with self._condition:
                self._condition.wait_for(lambda: not self._tasks)
        if wait or cancel_futures:
            self._executor.shutdown(wait=wait, cancel_futures=False)
        elif not futures:
            self._executor.shutdown(wait=False)
