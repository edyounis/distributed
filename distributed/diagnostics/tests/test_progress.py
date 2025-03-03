import asyncio

import pytest

from distributed import Nanny
from distributed.client import wait
from distributed.compatibility import LINUX
from distributed.diagnostics.progress import (
    AllProgress,
    GroupTiming,
    MultiProgress,
    Progress,
    SchedulerPlugin,
)
from distributed.scheduler import COMPILED
from distributed.utils_test import dec, div, gen_cluster, inc, nodebug, slowdec, slowinc


def f(*args):
    pass


def g(*args):
    pass


def h(*args):
    pass


@nodebug
@pytest.mark.flaky(reruns=10, reruns_delay=5)
@gen_cluster(client=True)
async def test_many_Progress(c, s, a, b):
    x = c.submit(f, 1)
    y = c.submit(g, x)
    z = c.submit(h, y)

    bars = [Progress(keys=[z], scheduler=s) for _ in range(10)]
    await asyncio.gather(*(bar.setup() for bar in bars))
    await z

    while not all(b.status == "finished" for b in bars):
        await asyncio.sleep(0.01)


@gen_cluster(client=True)
async def test_multiprogress(c, s, a, b):
    x1 = c.submit(f, 1)
    x2 = c.submit(f, x1)
    x3 = c.submit(f, x2)
    y1 = c.submit(g, x3)
    y2 = c.submit(g, y1)

    p = MultiProgress([y2], scheduler=s, complete=True)
    await p.setup()

    assert p.all_keys == {
        "f": {f.key for f in [x1, x2, x3]},
        "g": {f.key for f in [y1, y2]},
    }

    await x3

    assert p.keys["f"] == set()

    await y2

    assert p.keys == {"f": set(), "g": set()}

    assert p.status == "finished"


@gen_cluster(client=True)
async def test_robust_to_bad_plugin(c, s, a, b):
    class Bad(SchedulerPlugin):
        def transition(self, key, start, finish, **kwargs):
            raise Exception()

    bad = Bad()
    s.add_plugin(bad)

    x = c.submit(inc, 1)
    y = c.submit(inc, x)
    result = await y
    assert result == 3


def check_bar_completed(capsys, width=40):
    out, err = capsys.readouterr()
    bar, percent, time = (i.strip() for i in out.split("\r")[-1].split("|"))
    assert bar == "[" + "#" * width + "]"
    assert percent == "100% Completed"


@pytest.mark.flaky(condition=not COMPILED and LINUX, reruns=10, reruns_delay=5)
@pytest.mark.skipif(COMPILED, reason="Fails with cythonized scheduler")
@gen_cluster(client=True, Worker=Nanny)
async def test_AllProgress(c, s, a, b):
    x, y, z = c.map(inc, [1, 2, 3])
    xx, yy, zz = c.map(dec, [x, y, z])

    await wait([x, y, z])
    p = AllProgress(s)
    assert p.all["inc"] == {x.key, y.key, z.key}
    assert p.state["memory"]["inc"] == {x.key, y.key, z.key}
    assert p.state["released"] == {}
    assert p.state["erred"] == {}
    assert "inc" in p.nbytes
    assert isinstance(p.nbytes["inc"], int)
    assert p.nbytes["inc"] > 0

    await wait([xx, yy, zz])
    assert p.all["dec"] == {xx.key, yy.key, zz.key}
    assert p.state["memory"]["dec"] == {xx.key, yy.key, zz.key}
    assert p.state["released"] == {}
    assert p.state["erred"] == {}
    assert p.nbytes["inc"] == p.nbytes["dec"]

    t = c.submit(sum, [x, y, z])
    await t

    keys = {x.key, y.key, z.key}
    del x, y, z
    import gc

    gc.collect()

    while any(k in s.who_has for k in keys):
        await asyncio.sleep(0.01)

    assert p.state["released"]["inc"] == keys
    assert p.all["inc"] == keys
    assert p.all["dec"] == {xx.key, yy.key, zz.key}
    if "inc" in p.nbytes:
        assert p.nbytes["inc"] == 0

    xxx = c.submit(div, 1, 0)
    await wait([xxx])
    assert p.state["erred"] == {"div": {xxx.key}}

    tkey = t.key
    del xx, yy, zz, t
    import gc

    gc.collect()

    while tkey in s.tasks:
        await asyncio.sleep(0.01)

    for coll in [p.all, p.nbytes] + list(p.state.values()):
        assert "inc" not in coll
        assert "dec" not in coll

    def f(x):
        return x

    for i in range(4):
        future = c.submit(f, i)
    import gc

    gc.collect()

    await asyncio.sleep(1)

    await wait([future])
    assert p.state["memory"] == {"f": {future.key}}

    await c._restart()

    for coll in [p.all] + list(p.state.values()):
        assert not coll

    x = c.submit(div, 1, 2)
    await wait([x])
    assert set(p.all) == {"div"}
    assert all(set(d) == {"div"} for d in p.state.values())


@pytest.mark.flaky(condition=LINUX, reruns=10, reruns_delay=5)
@gen_cluster(client=True, Worker=Nanny)
async def test_AllProgress_lost_key(c, s, a, b):
    p = AllProgress(s)
    futures = c.map(inc, range(5))
    await wait(futures)
    assert len(p.state["memory"]["inc"]) == 5

    await a.close()
    await b.close()

    while len(p.state["memory"]["inc"]) > 0:
        await asyncio.sleep(0.01)


@gen_cluster(client=True, Worker=Nanny)
async def test_group_timing(c, s, a, b):
    p = GroupTiming(s)
    s.add_plugin(p)

    assert len(p.time) == 2
    assert len(p.nthreads) == 2

    futures1 = c.map(slowinc, range(10), delay=0.3)
    futures2 = c.map(slowdec, range(10), delay=0.3)
    await wait(futures1 + futures2)

    assert len(p.time) > 2
    assert len(p.nthreads) == len(p.time)
    assert all([nt == s.total_nthreads for nt in p.nthreads])
    assert "slowinc" in p.compute
    assert "slowdec" in p.compute
    assert all([len(v) == len(p.time) for v in p.compute.values()])
    assert s.task_groups.keys() == p.compute.keys()
    assert all(
        [
            abs(s.task_groups[k].all_durations["compute"] - sum(v)) < 1.0e-12
            for k, v in p.compute.items()
        ]
    )

    await s.restart()
    assert len(p.time) == 2
    assert len(p.nthreads) == 2
    assert len(p.compute) == 0
