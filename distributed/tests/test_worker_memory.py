from __future__ import annotations

import asyncio
import logging
from collections import UserDict
from time import sleep

import pytest

import dask.config

import distributed.system
from distributed import Client, Event, Nanny, Worker, wait
from distributed.core import Status
from distributed.spill import has_zict_210
from distributed.utils_test import captured_logger, gen_cluster, inc
from distributed.worker_memory import parse_memory_limit

requires_zict_210 = pytest.mark.skipif(
    not has_zict_210,
    reason="requires zict version >= 2.1.0",
)


def memory_monitor_running(dask_worker: Worker | Nanny) -> bool:
    return "memory_monitor" in dask_worker.periodic_callbacks


def test_parse_memory_limit_zero():
    assert parse_memory_limit(0, 1) is None
    assert parse_memory_limit("0", 1) is None
    assert parse_memory_limit(None, 1) is None


def test_resource_limit(monkeypatch):
    assert parse_memory_limit("250MiB", 1, total_cores=1) == 1024 * 1024 * 250

    new_limit = 1024 * 1024 * 200
    monkeypatch.setattr(distributed.system, "MEMORY_LIMIT", new_limit)
    assert parse_memory_limit("250MiB", 1, total_cores=1) == new_limit


@gen_cluster(nthreads=[("", 1)], worker_kwargs={"memory_limit": "2e3 MB"})
async def test_parse_memory_limit_worker(s, w):
    assert w.memory_manager.memory_limit == 2e9


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    Worker=Nanny,
    worker_kwargs={"memory_limit": "2e3 MB"},
)
async def test_parse_memory_limit_nanny(c, s, n):
    assert n.memory_manager.memory_limit == 2e9
    out = await c.run(lambda dask_worker: dask_worker.memory_manager.memory_limit)
    assert out[n.worker_address] == 2e9


@gen_cluster(
    nthreads=[("127.0.0.1", 1)],
    config={
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.target": False,
    },
)
async def test_dict_data_if_no_spill_to_disk(s, w):
    assert type(w.data) is dict


class CustomError(Exception):
    pass


class FailToPickle:
    def __init__(self, *, reported_size=0):
        self.reported_size = int(reported_size)

    def __getstate__(self):
        raise CustomError()

    def __sizeof__(self):
        return self.reported_size


async def assert_basic_futures(c: Client) -> None:
    futures = c.map(inc, range(10))
    results = await c.gather(futures)
    assert results == list(map(inc, range(10)))


@gen_cluster(client=True)
async def test_fail_to_pickle_target_1(c, s, a, b):
    """Test failure to serialize triggered by key which is individually larger
    than target. The data is lost and the task is marked as failed;
    the worker remains in usable condition.
    """
    x = c.submit(FailToPickle, reported_size=100e9, key="x")
    await wait(x)

    assert x.status == "error"

    with pytest.raises(TypeError, match="Could not serialize"):
        await x

    await assert_basic_futures(c)


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": "1 kiB"},
    config={
        "distributed.worker.memory.target": 0.5,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": False,
    },
)
async def test_fail_to_pickle_target_2(c, s, a):
    """Test failure to spill triggered by key which is individually smaller
    than target, so it is not spilled immediately. The data is retained and
    the task is NOT marked as failed; the worker remains in usable condition.
    """
    x = c.submit(FailToPickle, reported_size=256, key="x")
    await wait(x)
    assert x.status == "finished"
    assert set(a.data.memory) == {"x"}

    y = c.submit(lambda: "y" * 256, key="y")
    await wait(y)
    if has_zict_210:
        assert set(a.data.memory) == {"x", "y"}
    else:
        assert set(a.data.memory) == {"y"}

    assert not a.data.disk

    await assert_basic_futures(c)


@requires_zict_210
@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": "1 kB"},
    config={
        "distributed.worker.memory.target": False,
        "distributed.worker.memory.spill": 0.7,
        "distributed.worker.memory.monitor-interval": "10ms",
    },
)
async def test_fail_to_pickle_spill(c, s, a):
    """Test failure to evict a key, triggered by the spill threshold"""
    a.monitor.get_process_memory = lambda: 701 if a.data.fast else 0

    with captured_logger(logging.getLogger("distributed.spill")) as logs:
        bad = c.submit(FailToPickle, key="bad")
        await wait(bad)

        # Must wait for memory monitor to kick in
        while True:
            logs_value = logs.getvalue()
            if logs_value:
                break
            await asyncio.sleep(0.01)

    assert "Failed to pickle" in logs_value
    assert "Traceback" in logs_value

    # key is in fast
    assert bad.status == "finished"
    assert bad.key in a.data.fast

    await assert_basic_futures(c)


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": 1200 / 0.6},
    config={
        "distributed.worker.memory.target": 0.6,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": False,
    },
)
async def test_spill_target_threshold(c, s, a):
    """Test distributed.worker.memory.target threshold. Note that in this test we
    disabled spill and pause thresholds, which work on the process memory, and just left
    the target threshold, which works on managed memory so it is unperturbed by the
    several hundreds of MB of unmanaged memory that are typical of the test suite.
    """
    assert not memory_monitor_running(a)

    x = c.submit(lambda: "x" * 500, key="x")
    await wait(x)
    y = c.submit(lambda: "y" * 500, key="y")
    await wait(y)

    assert set(a.data) == {"x", "y"}
    assert set(a.data.memory) == {"x", "y"}

    z = c.submit(lambda: "z" * 500, key="z")
    await wait(z)
    assert set(a.data) == {"x", "y", "z"}
    assert set(a.data.memory) == {"y", "z"}
    assert set(a.data.disk) == {"x"}

    await x
    assert set(a.data.memory) == {"x", "z"}
    assert set(a.data.disk) == {"y"}


@requires_zict_210
@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": 1600},
    config={
        "distributed.worker.memory.target": 0.6,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": False,
        "distributed.worker.memory.max-spill": 600,
    },
)
async def test_spill_constrained(c, s, w):
    """Test distributed.worker.memory.max-spill parameter"""
    # spills starts at 1600*0.6=960 bytes of managed memory

    # size in memory ~200; size on disk ~400
    x = c.submit(lambda: "x" * 200, key="x")
    await wait(x)
    # size in memory ~500; size on disk ~700
    y = c.submit(lambda: "y" * 500, key="y")
    await wait(y)

    assert set(w.data) == {x.key, y.key}
    assert set(w.data.memory) == {x.key, y.key}

    z = c.submit(lambda: "z" * 500, key="z")
    await wait(z)

    assert set(w.data) == {x.key, y.key, z.key}

    # max_spill has not been reached
    assert set(w.data.memory) == {y.key, z.key}
    assert set(w.data.disk) == {x.key}

    # zb is individually larger than max_spill
    zb = c.submit(lambda: "z" * 1700, key="zb")
    await wait(zb)

    assert set(w.data.memory) == {y.key, z.key, zb.key}
    assert set(w.data.disk) == {x.key}

    del zb
    while "zb" in w.data:
        await asyncio.sleep(0.01)

    # zc is individually smaller than max_spill, but the evicted key together with
    # x it exceeds max_spill
    zc = c.submit(lambda: "z" * 500, key="zc")
    await wait(zc)
    assert set(w.data.memory) == {y.key, z.key, zc.key}
    assert set(w.data.disk) == {x.key}


@gen_cluster(
    nthreads=[("", 1)],
    client=True,
    worker_kwargs={"memory_limit": "1000 MB"},
    config={
        "distributed.worker.memory.target": False,
        "distributed.worker.memory.spill": 0.7,
        "distributed.worker.memory.pause": False,
        "distributed.worker.memory.monitor-interval": "10ms",
    },
)
async def test_spill_spill_threshold(c, s, a):
    """Test distributed.worker.memory.spill threshold.
    Test that the spill threshold uses the process memory and not the managed memory
    reported by sizeof(), which may be inaccurate.
    """
    assert memory_monitor_running(a)
    a.monitor.get_process_memory = lambda: 800_000_000 if a.data.fast else 0
    x = c.submit(inc, 0, key="x")
    while not a.data.disk:
        await asyncio.sleep(0.01)
    assert await x == 1


@pytest.mark.parametrize(
    "target,managed,expect_spilled",
    [
        # no target -> no hysteresis
        # Over-report managed memory to test that the automated LRU eviction based on
        # target is never triggered
        (False, int(10e9), 1),
        # Under-report managed memory, so that we reach the spill threshold for process
        # memory without first reaching the target threshold for managed memory
        # target == spill -> no hysteresis
        (0.7, 0, 1),
        # target < spill -> hysteresis from spill to target
        (0.4, 0, 7),
    ],
)
@gen_cluster(
    nthreads=[],
    client=True,
    config={
        "distributed.worker.memory.spill": 0.7,
        "distributed.worker.memory.pause": False,
        "distributed.worker.memory.monitor-interval": "10ms",
    },
)
async def test_spill_hysteresis(c, s, target, managed, expect_spilled):
    """
    1. Test that you can enable the spill threshold while leaving the target threshold
       to False
    2. Test the hysteresis system where, once you reach the spill threshold, the worker
       won't stop spilling until the target threshold is reached
    """

    class C:
        def __sizeof__(self):
            return managed

    with dask.config.set({"distributed.worker.memory.target": target}):
        async with Worker(s.address, memory_limit="1000 MB") as a:
            a.monitor.get_process_memory = lambda: 50_000_000 * len(a.data.fast)

            # Add 500MB (reported) process memory. Spilling must not happen.
            futures = [c.submit(C, pure=False) for _ in range(10)]
            await wait(futures)
            await asyncio.sleep(0.1)
            assert not a.data.disk

            # Add another 250MB unmanaged memory. This must trigger the spilling.
            futures += [c.submit(C, pure=False) for _ in range(5)]
            await wait(futures)

            # Wait until spilling starts. Then, wait until it stops.
            prev_n = 0
            while not a.data.disk or len(a.data.disk) > prev_n:
                prev_n = len(a.data.disk)
                await asyncio.sleep(0)

            assert len(a.data.disk) == expect_spilled


@gen_cluster(
    nthreads=[("", 1)],
    client=True,
    config={
        "distributed.worker.memory.target": False,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": False,
    },
)
async def test_pause_executor_manual(c, s, a):
    assert not memory_monitor_running(a)

    # Task that is running when the worker pauses
    ev_x = Event()

    def f(ev):
        ev.wait()
        return 1

    # Task that is running on the worker when the worker pauses
    x = c.submit(f, ev_x, key="x")
    while a.executing_count != 1:
        await asyncio.sleep(0.01)

    # Task that is queued on the worker when the worker pauses
    y = c.submit(inc, 1, key="y")
    while "y" not in a.tasks:
        await asyncio.sleep(0.01)

    a.status = Status.paused
    # Wait for sync to scheduler
    while s.workers[a.address].status != Status.paused:
        await asyncio.sleep(0.01)

    # Task that is queued on the scheduler when the worker pauses.
    # It is not sent to the worker.
    z = c.submit(inc, 2, key="z")
    while "z" not in s.tasks or s.tasks["z"].state != "no-worker":
        await asyncio.sleep(0.01)
    assert s.unrunnable == {s.tasks["z"]}

    # Test that a task that already started when the worker paused can complete
    # and its output can be retrieved. Also test that the now free slot won't be
    # used by other tasks.
    await ev_x.set()
    assert await x == 1
    await asyncio.sleep(0.05)

    assert a.executing_count == 0
    assert len(a.ready) == 1
    assert a.tasks["y"].state == "ready"
    assert "z" not in a.tasks

    # Unpause. Tasks that were queued on the worker are executed.
    # Tasks that were stuck on the scheduler are sent to the worker and executed.
    a.status = Status.running
    assert await y == 2
    assert await z == 3


@gen_cluster(
    nthreads=[("", 1)],
    client=True,
    worker_kwargs={"memory_limit": "1000 MB"},
    config={
        "distributed.worker.memory.target": False,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": 0.8,
        "distributed.worker.memory.monitor-interval": "10ms",
    },
)
async def test_pause_executor_with_memory_monitor(c, s, a):
    assert memory_monitor_running(a)
    mocked_rss = 0
    a.monitor.get_process_memory = lambda: mocked_rss

    # Task that is running when the worker pauses
    ev_x = Event()

    def f(ev):
        ev.wait()
        return 1

    # Task that is running on the worker when the worker pauses
    x = c.submit(f, ev_x, key="x")
    while a.executing_count != 1:
        await asyncio.sleep(0.01)

    with captured_logger(logging.getLogger("distributed.worker_memory")) as logger:
        # Task that is queued on the worker when the worker pauses
        y = c.submit(inc, 1, key="y")
        while "y" not in a.tasks:
            await asyncio.sleep(0.01)

        # Hog the worker with 900MB unmanaged memory
        mocked_rss = 900_000_000
        while s.workers[a.address].status != Status.paused:
            await asyncio.sleep(0.01)

        assert "Pausing worker" in logger.getvalue()

        # Task that is queued on the scheduler when the worker pauses.
        # It is not sent to the worker.
        z = c.submit(inc, 2, key="z")
        while "z" not in s.tasks or s.tasks["z"].state != "no-worker":
            await asyncio.sleep(0.01)
        assert s.unrunnable == {s.tasks["z"]}

        # Test that a task that already started when the worker paused can complete
        # and its output can be retrieved. Also test that the now free slot won't be
        # used by other tasks.
        await ev_x.set()
        assert await x == 1
        await asyncio.sleep(0.05)

        assert a.executing_count == 0
        assert len(a.ready) == 1
        assert a.tasks["y"].state == "ready"
        assert "z" not in a.tasks

        # Release the memory. Tasks that were queued on the worker are executed.
        # Tasks that were stuck on the scheduler are sent to the worker and executed.
        mocked_rss = 0
        assert await y == 2
        assert await z == 3

        assert a.status == Status.running
        assert "Resuming worker" in logger.getvalue()


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": 0},
    config={"distributed.worker.memory.monitor-interval": "10ms"},
)
async def test_avoid_memory_monitor_if_zero_limit_worker(c, s, a):
    assert type(a.data) is dict
    assert not memory_monitor_running(a)

    future = c.submit(inc, 1)
    assert await future == 2
    await asyncio.sleep(0.05)
    assert await c.submit(inc, 2) == 3  # worker doesn't pause


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    Worker=Nanny,
    worker_kwargs={"memory_limit": 0},
    config={"distributed.worker.memory.monitor-interval": "10ms"},
)
async def test_avoid_memory_monitor_if_zero_limit_nanny(c, s, nanny):
    typ = await c.run(lambda dask_worker: type(dask_worker.data))
    assert typ == {nanny.worker_address: dict}
    assert not memory_monitor_running(nanny)
    assert not (await c.run(memory_monitor_running))[nanny.worker_address]

    future = c.submit(inc, 1)
    assert await future == 2
    await asyncio.sleep(0.02)
    assert await c.submit(inc, 2) == 3  # worker doesn't pause


@gen_cluster(nthreads=[])
async def test_override_data_worker(s):
    # Use a UserDict to sidestep potential special case handling for dict
    async with Worker(s.address, data=UserDict) as w:
        assert type(w.data) is UserDict

    data = UserDict({"x": 1})
    async with Worker(s.address, data=data) as w:
        assert w.data is data
        assert w.data == {"x": 1}


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    Worker=Nanny,
    worker_kwargs={"data": UserDict},
)
async def test_override_data_nanny(c, s, n):
    r = await c.run(lambda dask_worker: type(dask_worker.data))
    assert r[n.worker_address] is UserDict


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": "1 GB", "data": UserDict},
    config={"distributed.worker.memory.monitor-interval": "10ms"},
)
async def test_override_data_vs_memory_monitor(c, s, a):
    a.monitor.get_process_memory = lambda: 801_000_000 if a.data else 0
    assert memory_monitor_running(a)

    # Push a key that would normally trip both the target and the spill thresholds
    class C:
        def __sizeof__(self):
            return 801_000_000

    # Capture output of log_errors()
    with captured_logger(logging.getLogger("distributed.utils")) as logger:
        x = c.submit(C)
        await wait(x)

        # The pause subsystem of the memory monitor has been tripped.
        # The spill subsystem hasn't.
        while a.status != Status.paused:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    # This would happen if memory_monitor() tried to blindly call SpillBuffer.evict()
    assert "Traceback" not in logger.getvalue()

    assert type(a.data) is UserDict
    assert a.data.keys() == {x.key}


class ManualEvictDict(UserDict):
    """A MutableMapping which implements distributed.spill.ManualEvictProto"""

    def __init__(self):
        super().__init__()
        self.evicted = set()

    @property
    def fast(self):
        # Any Sized of bool will do
        return self.keys() - self.evicted

    def evict(self):
        # Evict a random key
        k = next(iter(self.fast))
        self.evicted.add(k)
        return 1


@gen_cluster(
    client=True,
    nthreads=[("", 1)],
    worker_kwargs={"memory_limit": "1 GB", "data": ManualEvictDict},
    config={
        "distributed.worker.memory.pause": False,
        "distributed.worker.memory.monitor-interval": "10ms",
    },
)
async def test_manual_evict_proto(c, s, a):
    """data is a third-party dict-like which respects the ManualEvictProto duck-type
    API. spill threshold is respected.
    """
    a.monitor.get_process_memory = lambda: 701_000_000 if a.data else 0
    assert memory_monitor_running(a)
    assert isinstance(a.data, ManualEvictDict)

    futures = await c.scatter({"x": None, "y": None, "z": None})
    while a.data.evicted != {"x", "y", "z"}:
        await asyncio.sleep(0.01)


@pytest.mark.slow
@gen_cluster(
    nthreads=[("", 1)],
    client=True,
    Worker=Nanny,
    worker_kwargs={"memory_limit": "400 MiB"},
    config={"distributed.worker.memory.monitor-interval": "10ms"},
)
async def test_nanny_terminate(c, s, a):
    def leak():
        L = []
        while True:
            L.append(b"0" * 5_000_000)
            sleep(0.01)

    before = a.process.pid
    with captured_logger(logging.getLogger("distributed.worker_memory")) as logger:
        future = c.submit(leak)
        while a.process.pid == before:
            await asyncio.sleep(0.01)

    out = logger.getvalue()
    assert "restart" in out.lower()
    assert "memory" in out.lower()


@pytest.mark.parametrize(
    "cls,name,value",
    [
        (Worker, "memory_limit", 123e9),
        (Worker, "memory_target_fraction", 0.789),
        (Worker, "memory_spill_fraction", 0.789),
        (Worker, "memory_pause_fraction", 0.789),
        (Nanny, "memory_limit", 123e9),
        (Nanny, "memory_terminate_fraction", 0.789),
    ],
)
@gen_cluster(nthreads=[])
async def test_deprecated_attributes(s, cls, name, value):
    async with cls(s.address) as a:
        with pytest.warns(FutureWarning, match=name):
            setattr(a, name, value)
        with pytest.warns(FutureWarning, match=name):
            assert getattr(a, name) == value
        assert getattr(a.memory_manager, name) == value


@gen_cluster(nthreads=[("", 1)])
async def test_deprecated_memory_monitor_method_worker(s, a):
    with pytest.warns(FutureWarning, match="memory_monitor"):
        await a.memory_monitor()


@gen_cluster(nthreads=[("", 1)], Worker=Nanny)
async def test_deprecated_memory_monitor_method_nanny(s, a):
    with pytest.warns(FutureWarning, match="memory_monitor"):
        a.memory_monitor()


@pytest.mark.parametrize(
    "name",
    ["memory_target_fraction", "memory_spill_fraction", "memory_pause_fraction"],
)
@gen_cluster(nthreads=[])
async def test_deprecated_params(s, name):
    with pytest.warns(FutureWarning, match=name):
        async with Worker(s.address, **{name: 0.789}) as a:
            assert getattr(a.memory_manager, name) == 0.789
