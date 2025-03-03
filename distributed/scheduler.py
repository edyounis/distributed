import asyncio
import heapq
import inspect
import itertools
import json
import logging
import math
import operator
import os
import pickle
import random
import sys
import uuid
import warnings
import weakref
from collections import defaultdict, deque
from collections.abc import (
    Callable,
    Collection,
    Container,
    Hashable,
    Iterable,
    Iterator,
    Mapping,
    Set,
)
from contextlib import suppress
from datetime import timedelta
from functools import partial
from numbers import Number
from typing import Any, ClassVar, Dict, Literal
from typing import cast as pep484_cast

import psutil
from sortedcontainers import SortedDict, SortedSet
from tlz import (
    compose,
    first,
    groupby,
    merge,
    merge_sorted,
    merge_with,
    partition,
    pluck,
    second,
    valmap,
)
from tornado.ioloop import IOLoop, PeriodicCallback

import dask
from dask.highlevelgraph import HighLevelGraph
from dask.utils import format_bytes, format_time, parse_bytes, parse_timedelta, tmpfile
from dask.widgets import get_template

from distributed import cluster_dump, preloading, profile
from distributed import versions as version_module
from distributed.active_memory_manager import ActiveMemoryManagerExtension, RetireWorker
from distributed.batched import BatchedSend
from distributed.comm import (
    Comm,
    CommClosedError,
    get_address_host,
    normalize_address,
    resolve_address,
    unparse_host_port,
)
from distributed.comm.addressing import addresses_from_user_args
from distributed.core import Status, clean_exception, rpc, send_recv
from distributed.diagnostics.memory_sampler import MemorySamplerExtension
from distributed.diagnostics.plugin import SchedulerPlugin, _get_plugin_name
from distributed.event import EventExtension
from distributed.http import get_handlers
from distributed.lock import LockExtension
from distributed.metrics import time
from distributed.multi_lock import MultiLockExtension
from distributed.node import ServerNode
from distributed.proctitle import setproctitle
from distributed.protocol.pickle import dumps, loads
from distributed.publish import PublishExtension
from distributed.pubsub import PubSubSchedulerExtension
from distributed.queues import QueueExtension
from distributed.recreate_tasks import ReplayTaskScheduler
from distributed.security import Security
from distributed.semaphore import SemaphoreExtension
from distributed.stealing import WorkStealing
from distributed.stories import scheduler_story
from distributed.utils import (
    All,
    TimeoutError,
    empty_context,
    get_fileno_limit,
    key_split,
    key_split_group,
    log_errors,
    no_default,
    recursive_to_dict,
    validate_key,
)
from distributed.utils_comm import (
    gather_from_workers,
    retry_operation,
    scatter_to_workers,
)
from distributed.utils_perf import disable_gc_diagnosis, enable_gc_diagnosis
from distributed.variable import VariableExtension

try:
    from cython import compiled
except ImportError:
    compiled = False

if compiled:
    from cython import (
        Py_hash_t,
        Py_ssize_t,
        bint,
        cast,
        ccall,
        cclass,
        cfunc,
        declare,
        double,
        exceptval,
        final,
        inline,
        nogil,
    )
else:
    from ctypes import c_double as double
    from ctypes import c_ssize_t as Py_hash_t
    from ctypes import c_ssize_t as Py_ssize_t

    bint = bool

    def cast(T, v, *a, **k):
        return v

    def ccall(func):
        return func

    def cclass(cls):
        return cls

    def cfunc(func):
        return func

    def declare(*a, **k):
        if len(a) == 2:
            return a[1]
        else:
            pass

    def exceptval(*a, **k):
        def wrapper(func):
            return func

        return wrapper

    def final(cls):
        return cls

    def inline(func):
        return func

    def nogil(func):
        return func


logger = logging.getLogger(__name__)


LOG_PDB = dask.config.get("distributed.admin.pdb-on-err")
DEFAULT_DATA_SIZE = declare(
    Py_ssize_t, parse_bytes(dask.config.get("distributed.scheduler.default-data-size"))
)

DEFAULT_EXTENSIONS = {
    "locks": LockExtension,
    "multi_locks": MultiLockExtension,
    "publish": PublishExtension,
    "replay-tasks": ReplayTaskScheduler,
    "queues": QueueExtension,
    "variables": VariableExtension,
    "pubsub": PubSubSchedulerExtension,
    "semaphores": SemaphoreExtension,
    "events": EventExtension,
    "amm": ActiveMemoryManagerExtension,
    "memory_sampler": MemorySamplerExtension,
    "stealing": WorkStealing,
}

ALL_TASK_STATES = declare(
    set, {"released", "waiting", "no-worker", "processing", "erred", "memory"}
)
globals()["ALL_TASK_STATES"] = ALL_TASK_STATES
COMPILED = declare(bint, compiled)
globals()["COMPILED"] = COMPILED


@final
@cclass
class ClientState:
    """
    A simple object holding information about a client.

    .. attribute:: client_key: str

       A unique identifier for this client.  This is generally an opaque
       string generated by the client itself.

    .. attribute:: wants_what: {TaskState}

       A set of tasks this client wants kept in memory, so that it can
       download its result when desired.  This is the reverse mapping of
       :class:`TaskState.who_wants`.

       Tasks are typically removed from this set when the corresponding
       object in the client's space (for example a ``Future`` or a Dask
       collection) gets garbage-collected.

    """

    _client_key: str
    _hash: Py_hash_t
    _wants_what: set
    _last_seen: double
    _versions: dict

    __slots__ = ("_client_key", "_hash", "_wants_what", "_last_seen", "_versions")

    def __init__(self, client: str, versions: dict = None):
        self._client_key = client
        self._hash = hash(client)
        self._wants_what = set()
        self._last_seen = time()
        self._versions = versions or {}

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        typ_self: type = type(self)
        typ_other: type = type(other)
        if typ_self == typ_other:
            other_cs: ClientState = other
            return self._client_key == other_cs._client_key
        else:
            return False

    def __repr__(self):
        return "<Client '%s'>" % self._client_key

    def __str__(self):
        return self._client_key

    @property
    def client_key(self):
        return self._client_key

    @property
    def wants_what(self):
        return self._wants_what

    @property
    def last_seen(self):
        return self._last_seen

    @property
    def versions(self):
        return self._versions

    def _to_dict_no_nest(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        TaskState._to_dict
        """
        return recursive_to_dict(
            self,
            exclude=set(exclude) | {"versions"},  # type: ignore
            members=True,
        )


@final
@cclass
class MemoryState:
    """Memory readings on a worker or on the whole cluster.

    managed
        Sum of the output of sizeof() for all dask keys held by the worker in memory,
        plus number of bytes spilled to disk
    managed_in_memory
        Sum of the output of sizeof() for the dask keys held in RAM. Note that this may
        be inaccurate, which may cause inaccurate unmanaged memory (see below).
    managed_spilled
        Number of bytes  for the dask keys spilled to the hard drive.
        Note that this is the size on disk; size in memory may be different due to
        compression and inaccuracies in sizeof(). In other words, given the same keys,
        'managed' will change depending if the keys are in memory or spilled.
    process
        Total RSS memory measured by the OS on the worker process.
        This is always exactly equal to managed_in_memory + unmanaged.
    unmanaged
        process - managed_in_memory. This is the sum of

        - Python interpreter and modules
        - global variables
        - memory temporarily allocated by the dask tasks that are currently running
        - memory fragmentation
        - memory leaks
        - memory not yet garbage collected
        - memory not yet free()'d by the Python memory manager to the OS

    unmanaged_old
        Minimum of the 'unmanaged' measures over the last
        ``distributed.memory.recent-to-old-time`` seconds
    unmanaged_recent
        unmanaged - unmanaged_old; in other words process memory that has been recently
        allocated but is not accounted for by dask; hopefully it's mostly a temporary
        spike.
    optimistic
        managed_in_memory + unmanaged_old; in other words the memory held long-term by
        the process under the hopeful assumption that all unmanaged_recent memory is a
        temporary spike
    """

    __slots__ = ("_process", "_managed_in_memory", "_managed_spilled", "_unmanaged_old")

    _process: Py_ssize_t
    _managed_in_memory: Py_ssize_t
    _managed_spilled: Py_ssize_t
    _unmanaged_old: Py_ssize_t

    def __init__(
        self,
        *,
        process: Py_ssize_t,
        unmanaged_old: Py_ssize_t,
        managed_in_memory: Py_ssize_t,
        managed_spilled: Py_ssize_t,
    ):
        # Some data arrives with the heartbeat, some other arrives in realtime as the
        # tasks progress. Also, sizeof() is not guaranteed to return correct results.
        # This can cause glitches where a partial measure is larger than the whole, so
        # we need to force all numbers to add up exactly by definition.
        self._process = process
        self._managed_in_memory = min(self._process, managed_in_memory)
        self._managed_spilled = managed_spilled
        # Subtractions between unsigned ints guaranteed by construction to be >= 0
        self._unmanaged_old = min(unmanaged_old, process - self._managed_in_memory)

    @property
    def process(self) -> Py_ssize_t:
        return self._process

    @property
    def managed_in_memory(self) -> Py_ssize_t:
        return self._managed_in_memory

    @property
    def managed_spilled(self) -> Py_ssize_t:
        return self._managed_spilled

    @property
    def unmanaged_old(self) -> Py_ssize_t:
        return self._unmanaged_old

    @classmethod
    def sum(cls, *infos: "MemoryState") -> "MemoryState":
        process = 0
        unmanaged_old = 0
        managed_in_memory = 0
        managed_spilled = 0
        ms: MemoryState
        for ms in infos:
            process += ms._process
            unmanaged_old += ms._unmanaged_old
            managed_spilled += ms._managed_spilled
            managed_in_memory += ms._managed_in_memory
        return MemoryState(
            process=process,
            unmanaged_old=unmanaged_old,
            managed_in_memory=managed_in_memory,
            managed_spilled=managed_spilled,
        )

    @property
    def managed(self) -> Py_ssize_t:
        return self._managed_in_memory + self._managed_spilled

    @property
    def unmanaged(self) -> Py_ssize_t:
        # This is never negative thanks to __init__
        return self._process - self._managed_in_memory

    @property
    def unmanaged_recent(self) -> Py_ssize_t:
        # This is never negative thanks to __init__
        return self._process - self._managed_in_memory - self._unmanaged_old

    @property
    def optimistic(self) -> Py_ssize_t:
        return self._managed_in_memory + self._unmanaged_old

    def __repr__(self) -> str:
        return (
            f"Process memory (RSS)  : {format_bytes(self._process)}\n"
            f"  - managed by Dask   : {format_bytes(self._managed_in_memory)}\n"
            f"  - unmanaged (old)   : {format_bytes(self._unmanaged_old)}\n"
            f"  - unmanaged (recent): {format_bytes(self.unmanaged_recent)}\n"
            f"Spilled to disk       : {format_bytes(self._managed_spilled)}\n"
        )

    def _to_dict(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        """
        return recursive_to_dict(self, exclude=exclude, members=True)


@final
@cclass
class WorkerState:
    """
    A simple object holding information about a worker.

    .. attribute:: address: str

       This worker's unique key.  This can be its connected address
       (such as ``'tcp://127.0.0.1:8891'``) or an alias (such as ``'alice'``).

    .. attribute:: processing: {TaskState: cost}

       A dictionary of tasks that have been submitted to this worker.
       Each task state is associated with the expected cost in seconds
       of running that task, summing both the task's expected computation
       time and the expected communication time of its result.

       If a task is already executing on the worker and the excecution time is
       twice the learned average TaskGroup duration, this will be set to twice
       the current executing time. If the task is unknown, the default task
       duration is used instead of the TaskGroup average.

       Multiple tasks may be submitted to a worker in advance and the worker
       will run them eventually, depending on its execution resources
       (but see :doc:`work-stealing`).

       All the tasks here are in the "processing" state.

       This attribute is kept in sync with :attr:`TaskState.processing_on`.

    .. attribute:: executing: {TaskState: duration}

       A dictionary of tasks that are currently being run on this worker.
       Each task state is asssociated with the duration in seconds which
       the task has been running.

    .. attribute:: has_what: {TaskState}

       An insertion-sorted set-like of tasks which currently reside on this worker.
       All the tasks here are in the "memory" state.

       This is the reverse mapping of :class:`TaskState.who_has`.

    .. attribute:: nbytes: int

       The total memory size, in bytes, used by the tasks this worker
       holds in memory (i.e. the tasks in this worker's :attr:`has_what`).

    .. attribute:: nthreads: int

       The number of CPU threads made available on this worker.

    .. attribute:: resources: {str: Number}

       The available resources on this worker like ``{'gpu': 2}``.
       These are abstract quantities that constrain certain tasks from
       running at the same time on this worker.

    .. attribute:: used_resources: {str: Number}

       The sum of each resource used by all tasks allocated to this worker.
       The numbers in this dictionary can only be less or equal than
       those in this worker's :attr:`resources`.

    .. attribute:: occupancy: double

       The total expected runtime, in seconds, of all tasks currently
       processing on this worker.  This is the sum of all the costs in
       this worker's :attr:`processing` dictionary.

    .. attribute:: status: Status

       Read-only worker status, synced one way from the remote Worker object

    .. attribute:: nanny: str

       Address of the associated Nanny, if present

    .. attribute:: last_seen: Py_ssize_t

       The last time we received a heartbeat from this worker, in local
       scheduler time.

    .. attribute:: actors: {TaskState}

       A set of all TaskStates on this worker that are actors.  This only
       includes those actors whose state actually lives on this worker, not
       actors to which this worker has a reference.

    """

    # XXX need a state field to signal active/removed?

    _actors: set
    _address: str
    _bandwidth: double
    _executing: dict
    _extra: dict
    # _has_what is a dict with all values set to None as rebalance() relies on the
    # property of Python >=3.7 dicts to be insertion-sorted.
    _has_what: dict
    _hash: Py_hash_t
    _last_seen: double
    _local_directory: str
    _memory_limit: Py_ssize_t
    _memory_other_history: "deque[tuple[float, Py_ssize_t]]"
    _memory_unmanaged_old: Py_ssize_t
    _metrics: dict
    _name: object
    _nanny: str
    _nbytes: Py_ssize_t
    _nthreads: Py_ssize_t
    _occupancy: double
    _pid: Py_ssize_t
    _processing: dict
    _long_running: set
    _resources: dict
    _services: dict
    _status: Status
    _time_delay: double
    _used_resources: dict
    _versions: dict

    __slots__ = (
        "_actors",
        "_address",
        "_bandwidth",
        "_extra",
        "_executing",
        "_has_what",
        "_hash",
        "_last_seen",
        "_local_directory",
        "_memory_limit",
        "_memory_other_history",
        "_memory_unmanaged_old",
        "_metrics",
        "_name",
        "_nanny",
        "_nbytes",
        "_nthreads",
        "_occupancy",
        "_pid",
        "_processing",
        "_long_running",
        "_resources",
        "_services",
        "_status",
        "_time_delay",
        "_used_resources",
        "_versions",
    )

    def __init__(
        self,
        *,
        address: str,
        status: Status,
        pid: Py_ssize_t,
        name: object,
        nthreads: Py_ssize_t = 0,
        memory_limit: Py_ssize_t,
        local_directory: str,
        nanny: str,
        services: "dict | None" = None,
        versions: "dict | None" = None,
        extra: "dict | None" = None,
    ):
        self._address = address
        self._pid = pid
        self._name = name
        self._nthreads = nthreads
        self._memory_limit = memory_limit
        self._local_directory = local_directory
        self._services = services or {}
        self._versions = versions or {}
        self._nanny = nanny
        self._status = status

        self._hash = hash(address)
        self._nbytes = 0
        self._occupancy = 0
        self._memory_unmanaged_old = 0
        self._memory_other_history = deque()
        self._metrics = {}
        self._last_seen = 0
        self._time_delay = 0
        self._bandwidth = float(
            parse_bytes(dask.config.get("distributed.scheduler.bandwidth"))
        )

        self._actors = set()
        self._has_what = {}
        self._processing = {}
        self._long_running = set()
        self._executing = {}
        self._resources = {}
        self._used_resources = {}

        self._extra = extra or {}

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        typ_self: type = type(self)
        typ_other: type = type(other)
        if typ_self == typ_other:
            other_ws: WorkerState = other
            return self._address == other_ws._address
        else:
            return False

    @property
    def actors(self):
        return self._actors

    @property
    def address(self) -> str:
        return self._address

    @property
    def bandwidth(self):
        return self._bandwidth

    @property
    def executing(self):
        return self._executing

    @property
    def extra(self):
        return self._extra

    @property
    def has_what(self) -> "Set[TaskState]":
        return self._has_what.keys()

    @property
    def host(self):
        return get_address_host(self._address)

    @property
    def last_seen(self):
        return self._last_seen

    @property
    def local_directory(self):
        return self._local_directory

    @property
    def memory_limit(self):
        return self._memory_limit

    @property
    def metrics(self):
        return self._metrics

    @property
    def memory(self) -> MemoryState:
        return MemoryState(
            # metrics["memory"] is None if the worker sent a heartbeat before its
            # SystemMonitor ever had a chance to run
            process=self._metrics["memory"] or 0,
            # self._nbytes is instantaneous; metrics may lag behind by a heartbeat
            managed_in_memory=max(
                0, self._nbytes - self._metrics["spilled_nbytes"]["memory"]
            ),
            managed_spilled=self._metrics["spilled_nbytes"]["disk"],
            unmanaged_old=self._memory_unmanaged_old,
        )

    @property
    def name(self):
        return self._name

    @property
    def nanny(self):
        return self._nanny

    @property
    def nbytes(self):
        return self._nbytes

    @nbytes.setter
    def nbytes(self, v: Py_ssize_t):
        self._nbytes = v

    @property
    def nthreads(self):
        return self._nthreads

    @property
    def occupancy(self):
        return self._occupancy

    @occupancy.setter
    def occupancy(self, v: double):
        self._occupancy = v

    @property
    def pid(self):
        return self._pid

    @property
    def processing(self):
        return self._processing

    @property
    def resources(self):
        return self._resources

    @property
    def services(self):
        return self._services

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, new_status):
        if not isinstance(new_status, Status):
            raise TypeError(f"Expected Status; got {new_status!r}")
        self._status = new_status

    @property
    def time_delay(self):
        return self._time_delay

    @property
    def used_resources(self):
        return self._used_resources

    @property
    def versions(self):
        return self._versions

    @ccall
    def clean(self):
        """Return a version of this object that is appropriate for serialization"""
        ws: WorkerState = WorkerState(
            address=self._address,
            status=self._status,
            pid=self._pid,
            name=self._name,
            nthreads=self._nthreads,
            memory_limit=self._memory_limit,
            local_directory=self._local_directory,
            services=self._services,
            nanny=self._nanny,
            extra=self._extra,
        )
        ts: TaskState
        ws._processing = {ts._key: cost for ts, cost in self._processing.items()}
        ws._executing = {ts._key: duration for ts, duration in self._executing.items()}
        return ws

    def __repr__(self):
        name = f", name: {self.name}" if self.name != self.address else ""
        return (
            f"<WorkerState {self._address!r}{name}, "
            f"status: {self._status.name}, "
            f"memory: {len(self._has_what)}, "
            f"processing: {len(self._processing)}>"
        )

    def _repr_html_(self):
        return get_template("worker_state.html.j2").render(
            address=self.address,
            name=self.name,
            status=self.status.name,
            has_what=self._has_what,
            processing=self.processing,
        )

    @ccall
    @exceptval(check=False)
    def identity(self) -> dict:
        return {
            "type": "Worker",
            "id": self._name,
            "host": self.host,
            "resources": self._resources,
            "local_directory": self._local_directory,
            "name": self._name,
            "nthreads": self._nthreads,
            "memory_limit": self._memory_limit,
            "last_seen": self._last_seen,
            "services": self._services,
            "metrics": self._metrics,
            "nanny": self._nanny,
            **self._extra,
        }

    def _to_dict_no_nest(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        TaskState._to_dict
        """
        return recursive_to_dict(
            self,
            exclude=set(exclude) | {"versions"},  # type: ignore
            members=True,
        )


@final
@cclass
class Computation:
    """
    Collection tracking a single compute or persist call

    See also
    --------
    TaskPrefix
    TaskGroup
    TaskState
    """

    _start: double
    _groups: set
    _code: object
    _id: object

    def __init__(self):
        self._start = time()
        self._groups = set()
        self._code = SortedSet()
        self._id = uuid.uuid4()

    @property
    def code(self):
        return self._code

    @property
    def start(self):
        return self._start

    @property
    def stop(self):
        if self.groups:
            return max(tg.stop for tg in self.groups)
        else:
            return -1

    @property
    def states(self):
        tg: TaskGroup
        return merge_with(sum, [tg._states for tg in self._groups])

    @property
    def groups(self):
        return self._groups

    def __repr__(self):
        return (
            f"<Computation {self._id}: "
            + "Tasks: "
            + ", ".join(
                "%s: %d" % (k, v) for (k, v) in sorted(self.states.items()) if v
            )
            + ">"
        )

    def _repr_html_(self):
        return get_template("computation.html.j2").render(
            id=self._id,
            start=self.start,
            stop=self.stop,
            groups=self.groups,
            states=self.states,
            code=self.code,
        )


@final
@cclass
class TaskPrefix:
    """Collection tracking all tasks within a group

    Keys often have a structure like ``("x-123", 0)``
    A group takes the first section, like ``"x"``

    .. attribute:: name: str

       The name of a group of tasks.
       For a task like ``("x-123", 0)`` this is the text ``"x"``

    .. attribute:: states: Dict[str, int]

       The number of tasks in each state,
       like ``{"memory": 10, "processing": 3, "released": 4, ...}``

    .. attribute:: duration_average: float

       An exponentially weighted moving average duration of all tasks with this prefix

    .. attribute:: suspicious: int

       Numbers of times a task was marked as suspicious with this prefix


    See Also
    --------
    TaskGroup
    """

    _name: str
    _all_durations: "defaultdict[str, float]"
    _duration_average: double
    _suspicious: Py_ssize_t
    _groups: list

    def __init__(self, name: str):
        self._name = name
        self._groups = []

        # store timings for each prefix-action
        self._all_durations = defaultdict(float)

        task_durations = dask.config.get("distributed.scheduler.default-task-durations")
        if self._name in task_durations:
            self._duration_average = parse_timedelta(task_durations[self._name])
        else:
            self._duration_average = -1
        self._suspicious = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def all_durations(self) -> "defaultdict[str, float]":
        return self._all_durations

    @ccall
    @exceptval(check=False)
    def add_duration(self, action: str, start: double, stop: double):
        duration = stop - start
        self._all_durations[action] += duration
        if action == "compute":
            old = self._duration_average
            if old < 0:
                self._duration_average = duration
            else:
                self._duration_average = 0.5 * duration + 0.5 * old

    @property
    def duration_average(self) -> double:
        return self._duration_average

    @property
    def suspicious(self) -> Py_ssize_t:
        return self._suspicious

    @property
    def groups(self):
        return self._groups

    @property
    def states(self):
        tg: TaskGroup
        return merge_with(sum, [tg._states for tg in self._groups])

    @property
    def active(self) -> "list[TaskGroup]":
        tg: TaskGroup
        return [
            tg
            for tg in self._groups
            if any([v != 0 for k, v in tg._states.items() if k != "forgotten"])
        ]

    @property
    def active_states(self):
        tg: TaskGroup
        return merge_with(sum, [tg._states for tg in self.active])

    def __repr__(self):
        return (
            "<"
            + self._name
            + ": "
            + ", ".join(
                "%s: %d" % (k, v) for (k, v) in sorted(self.states.items()) if v
            )
            + ">"
        )

    @property
    def nbytes_total(self):
        tg: TaskGroup
        return sum([tg._nbytes_total for tg in self._groups])

    def __len__(self):
        return sum(map(len, self._groups))

    @property
    def duration(self):
        tg: TaskGroup
        return sum([tg._duration for tg in self._groups])

    @property
    def types(self):
        tg: TaskGroup
        return set().union(*[tg._types for tg in self._groups])


@final
@cclass
class TaskGroup:
    """Collection tracking all tasks within a group

    Keys often have a structure like ``("x-123", 0)``
    A group takes the first section, like ``"x-123"``

    .. attribute:: name: str

       The name of a group of tasks.
       For a task like ``("x-123", 0)`` this is the text ``"x-123"``

    .. attribute:: states: Dict[str, int]

       The number of tasks in each state,
       like ``{"memory": 10, "processing": 3, "released": 4, ...}``

    .. attribute:: dependencies: Set[TaskGroup]

       The other TaskGroups on which this one depends

    .. attribute:: nbytes_total: int

       The total number of bytes that this task group has produced

    .. attribute:: duration: float

       The total amount of time spent on all tasks in this TaskGroup

    .. attribute:: types: Set[str]

       The result types of this TaskGroup

    .. attribute:: last_worker: WorkerState

       The worker most recently assigned a task from this group, or None when the group
       is not identified to be root-like by `SchedulerState.decide_worker`.

    .. attribute:: last_worker_tasks_left: int

       If `last_worker` is not None, the number of times that worker should be assigned
       subsequent tasks until a new worker is chosen.

    See also
    --------
    TaskPrefix
    """

    _name: str
    _prefix: TaskPrefix  # TaskPrefix | None
    _states: dict
    _dependencies: set
    _nbytes_total: Py_ssize_t
    _duration: double
    _types: set
    _start: double
    _stop: double
    _all_durations: "defaultdict[str, float]"
    _last_worker: WorkerState  # WorkerState | None
    _last_worker_tasks_left: Py_ssize_t

    def __init__(self, name: str):
        self._name = name
        self._prefix = None  # type: ignore
        self._states = {state: 0 for state in ALL_TASK_STATES}
        self._states["forgotten"] = 0
        self._dependencies = set()
        self._nbytes_total = 0
        self._duration = 0
        self._types = set()
        self._start = 0.0
        self._stop = 0.0
        self._all_durations = defaultdict(float)
        self._last_worker = None  # type: ignore
        self._last_worker_tasks_left = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def prefix(self) -> "TaskPrefix | None":
        return self._prefix

    @property
    def states(self) -> dict:
        return self._states

    @property
    def dependencies(self) -> set:
        return self._dependencies

    @property
    def nbytes_total(self):
        return self._nbytes_total

    @property
    def duration(self) -> double:
        return self._duration

    @ccall
    @exceptval(check=False)
    def add_duration(self, action: str, start: double, stop: double):
        duration = stop - start
        self._all_durations[action] += duration
        if action == "compute":
            if self._stop < stop:
                self._stop = stop
            self._start = self._start or start
        self._duration += duration
        self._prefix.add_duration(action, start, stop)

    @property
    def types(self) -> set:
        return self._types

    @property
    def all_durations(self) -> "defaultdict[str, float]":
        return self._all_durations

    @property
    def start(self) -> double:
        return self._start

    @property
    def stop(self) -> double:
        return self._stop

    @property
    def last_worker(self) -> "WorkerState | None":
        return self._last_worker

    @property
    def last_worker_tasks_left(self) -> int:
        return self._last_worker_tasks_left

    @ccall
    def add(self, other: "TaskState"):
        self._states[other._state] += 1
        other._group = self

    def __repr__(self):
        return (
            "<"
            + (self._name or "no-group")
            + ": "
            + ", ".join(
                "%s: %d" % (k, v) for (k, v) in sorted(self._states.items()) if v
            )
            + ">"
        )

    def __len__(self):
        return sum(self._states.values())

    def _to_dict_no_nest(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        TaskState._to_dict
        """
        return recursive_to_dict(self, exclude=exclude, members=True)


@final
@cclass
class TaskState:
    """
    A simple object holding information about a task.
    Not to be confused with :class:`distributed.worker_state_machine.TaskState`, which
    holds similar information on the Worker side.

    .. attribute:: key: str

       The key is the unique identifier of a task, generally formed
       from the name of the function, followed by a hash of the function
       and arguments, like ``'inc-ab31c010444977004d656610d2d421ec'``.

    .. attribute:: prefix: TaskPrefix

       The broad class of tasks to which this task belongs like "inc" or
       "read_csv"

    .. attribute:: run_spec: object

       A specification of how to run the task.  The type and meaning of this
       value is opaque to the scheduler, as it is only interpreted by the
       worker to which the task is sent for executing.

       As a special case, this attribute may also be ``None``, in which case
       the task is "pure data" (such as, for example, a piece of data loaded
       in the scheduler using :meth:`Client.scatter`).  A "pure data" task
       cannot be computed again if its value is lost.

    .. attribute:: priority: tuple

       The priority provides each task with a relative ranking which is used
       to break ties when many tasks are being considered for execution.

       This ranking is generally a 2-item tuple.  The first (and dominant)
       item corresponds to when it was submitted.  Generally, earlier tasks
       take precedence.  The second item is determined by the client, and is
       a way to prioritize tasks within a large graph that may be important,
       such as if they are on the critical path, or good to run in order to
       release many dependencies.  This is explained further in
       :doc:`Scheduling Policy <scheduling-policies>`.

    .. attribute:: state: str

       This task's current state.  Valid states include ``released``,
       ``waiting``, ``no-worker``, ``processing``, ``memory``, ``erred``
       and ``forgotten``.  If it is ``forgotten``, the task isn't stored
       in the ``tasks`` dictionary anymore and will probably disappear
       soon from memory.

    .. attribute:: dependencies: {TaskState}

       The set of tasks this task depends on for proper execution.  Only
       tasks still alive are listed in this set.  If, for whatever reason,
       this task also depends on a forgotten task, the
       :attr:`has_lost_dependencies` flag is set.

       A task can only be executed once all its dependencies have already
       been successfully executed and have their result stored on at least
       one worker.  This is tracked by progressively draining the
       :attr:`waiting_on` set.

    .. attribute:: dependents: {TaskState}

       The set of tasks which depend on this task.  Only tasks still alive
       are listed in this set.

       This is the reverse mapping of :attr:`dependencies`.

    .. attribute:: has_lost_dependencies: bool

       Whether any of the dependencies of this task has been forgotten.
       For memory consumption reasons, forgotten tasks are not kept in
       memory even though they may have dependent tasks.  When a task is
       forgotten, therefore, each of its dependents has their
       :attr:`has_lost_dependencies` attribute set to ``True``.

       If :attr:`has_lost_dependencies` is true, this task cannot go
       into the "processing" state anymore.

    .. attribute:: waiting_on: {TaskState}

       The set of tasks this task is waiting on *before* it can be executed.
       This is always a subset of :attr:`dependencies`.  Each time one of the
       dependencies has finished processing, it is removed from the
       :attr:`waiting_on` set.

       Once :attr:`waiting_on` becomes empty, this task can move from the
       "waiting" state to the "processing" state (unless one of the
       dependencies errored out, in which case this task is instead
       marked "erred").

    .. attribute:: waiters: {TaskState}

       The set of tasks which need this task to remain alive.  This is always
       a subset of :attr:`dependents`.  Each time one of the dependents
       has finished processing, it is removed from the :attr:`waiters`
       set.

       Once both :attr:`waiters` and :attr:`who_wants` become empty, this
       task can be released (if it has a non-empty :attr:`run_spec`) or
       forgotten (otherwise) by the scheduler, and by any workers
       in :attr:`who_has`.

       .. note:: Counter-intuitively, :attr:`waiting_on` and
          :attr:`waiters` are not reverse mappings of each other.

    .. attribute:: who_wants: {ClientState}

       The set of clients who want this task's result to remain alive.
       This is the reverse mapping of :attr:`ClientState.wants_what`.

       When a client submits a graph to the scheduler it also specifies
       which output tasks it desires, such that their results are not released
       from memory.

       Once a task has finished executing (i.e. moves into the "memory"
       or "erred" state), the clients in :attr:`who_wants` are notified.

       Once both :attr:`waiters` and :attr:`who_wants` become empty, this
       task can be released (if it has a non-empty :attr:`run_spec`) or
       forgotten (otherwise) by the scheduler, and by any workers
       in :attr:`who_has`.

    .. attribute:: who_has: {WorkerState}

       The set of workers who have this task's result in memory.
       It is non-empty iff the task is in the "memory" state.  There can be
       more than one worker in this set if, for example, :meth:`Client.scatter`
       or :meth:`Client.replicate` was used.

       This is the reverse mapping of :attr:`WorkerState.has_what`.

    .. attribute:: processing_on: WorkerState (or None)

       If this task is in the "processing" state, which worker is currently
       processing it.  Otherwise this is ``None``.

       This attribute is kept in sync with :attr:`WorkerState.processing`.

    .. attribute:: retries: int

       The number of times this task can automatically be retried in case
       of failure.  If a task fails executing (the worker returns with
       an error), its :attr:`retries` attribute is checked.  If it is
       equal to 0, the task is marked "erred".  If it is greater than 0,
       the :attr:`retries` attribute is decremented and execution is
       attempted again.

    .. attribute:: nbytes: int (or None)

       The number of bytes, as determined by ``sizeof``, of the result
       of a finished task.  This number is used for diagnostics and to
       help prioritize work.

    .. attribute:: type: str

       The type of the object as a string.  Only present for tasks that have
       been computed.

    .. attribute:: exception: object

       If this task failed executing, the exception object is stored here.
       Otherwise this is ``None``.

    .. attribute:: traceback: object

       If this task failed executing, the traceback object is stored here.
       Otherwise this is ``None``.

    .. attribute:: exception_blame: TaskState (or None)

       If this task or one of its dependencies failed executing, the
       failed task is stored here (possibly itself).  Otherwise this
       is ``None``.

    .. attribute:: erred_on: set(str)

        Worker addresses on which errors appeared causing this task to be in an error state.

    .. attribute:: suspicious: int

       The number of times this task has been involved in a worker death.

       Some tasks may cause workers to die (such as calling ``os._exit(0)``).
       When a worker dies, all of the tasks on that worker are reassigned
       to others.  This combination of behaviors can cause a bad task to
       catastrophically destroy all workers on the cluster, one after
       another.  Whenever a worker dies, we mark each task currently
       processing on that worker (as recorded by
       :attr:`WorkerState.processing`) as suspicious.

       If a task is involved in three deaths (or some other fixed constant)
       then we mark the task as ``erred``.

    .. attribute:: host_restrictions: {hostnames}

       A set of hostnames where this task can be run (or ``None`` if empty).
       Usually this is empty unless the task has been specifically restricted
       to only run on certain hosts.  A hostname may correspond to one or
       several connected workers.

    .. attribute:: worker_restrictions: {worker addresses}

       A set of complete worker addresses where this can be run (or ``None``
       if empty).  Usually this is empty unless the task has been specifically
       restricted to only run on certain workers.

       Note this is tracking worker addresses, not worker states, since
       the specific workers may not be connected at this time.

    .. attribute:: resource_restrictions: {resource: quantity}

       Resources required by this task, such as ``{'gpu': 1}`` or
       ``{'memory': 1e9}`` (or ``None`` if empty).  These are user-defined
       names and are matched against the contents of each
       :attr:`WorkerState.resources` dictionary.

    .. attribute:: loose_restrictions: bool

       If ``False``, each of :attr:`host_restrictions`,
       :attr:`worker_restrictions` and :attr:`resource_restrictions` is
       a hard constraint: if no worker is available satisfying those
       restrictions, the task cannot go into the "processing" state and
       will instead go into the "no-worker" state.

       If ``True``, the above restrictions are mere preferences: if no worker
       is available satisfying those restrictions, the task can still go
       into the "processing" state and be sent for execution to another
       connected worker.

    .. attribute:: metadata: dict

       Metadata related to task.

    .. attribute:: actor: bool

       Whether or not this task is an Actor.

    .. attribute:: group: TaskGroup

        The group of tasks to which this one belongs.

    .. attribute:: annotations: dict

        Task annotations
    """

    _key: str
    _hash: Py_hash_t
    _prefix: TaskPrefix
    _run_spec: object
    _priority: tuple  # tuple | None
    _state: str  # str | None
    _dependencies: set  # set[TaskState]
    _dependents: set  # set[TaskState]
    _has_lost_dependencies: bint
    _waiting_on: set  # set[TaskState]
    _waiters: set  # set[TaskState]
    _who_wants: set  # set[ClientState]
    _who_has: set  # set[WorkerState]
    _processing_on: WorkerState  # WorkerState | None
    _retries: Py_ssize_t
    _nbytes: Py_ssize_t
    _type: str  # str | None
    _exception: object
    _exception_text: str
    _traceback: object
    _traceback_text: str
    _exception_blame: "TaskState"  # TaskState | None"
    _erred_on: set
    _suspicious: Py_ssize_t
    _host_restrictions: set  # set[str] | None
    _worker_restrictions: set  # set[str] | None
    _resource_restrictions: dict  # dict | None
    _loose_restrictions: bint
    _metadata: dict
    _annotations: dict
    _actor: bint
    _group: TaskGroup  # TaskGroup | None
    _group_key: str

    __slots__ = (
        # === General description ===
        "_actor",
        # Key name
        "_key",
        # Hash of the key name
        "_hash",
        # Key prefix (see key_split())
        "_prefix",
        # How to run the task (None if pure data)
        "_run_spec",
        # Alive dependents and dependencies
        "_dependencies",
        "_dependents",
        # Compute priority
        "_priority",
        # Restrictions
        "_host_restrictions",
        "_worker_restrictions",  # not WorkerStates but addresses
        "_resource_restrictions",
        "_loose_restrictions",
        # === Task state ===
        "_state",
        # Whether some dependencies were forgotten
        "_has_lost_dependencies",
        # If in 'waiting' state, which tasks need to complete
        # before we can run
        "_waiting_on",
        # If in 'waiting' or 'processing' state, which tasks needs us
        # to complete before they can run
        "_waiters",
        # In in 'processing' state, which worker we are processing on
        "_processing_on",
        # If in 'memory' state, Which workers have us
        "_who_has",
        # Which clients want us
        "_who_wants",
        "_exception",
        "_exception_text",
        "_traceback",
        "_traceback_text",
        "_erred_on",
        "_exception_blame",
        "_suspicious",
        "_retries",
        "_nbytes",
        "_type",
        "_group_key",
        "_group",
        "_metadata",
        "_annotations",
    )

    def __init__(self, key: str, run_spec: object):
        self._key = key
        self._hash = hash(key)
        self._run_spec = run_spec
        self._state = None  # type: ignore
        self._exception = None
        self._exception_blame = None  # type: ignore
        self._traceback = None
        self._exception_text = ""
        self._traceback_text = ""
        self._suspicious = 0
        self._retries = 0
        self._nbytes = -1
        self._priority = None  # type: ignore
        self._who_wants = set()
        self._dependencies = set()
        self._dependents = set()
        self._waiting_on = set()
        self._waiters = set()
        self._who_has = set()
        self._processing_on = None  # type: ignore
        self._has_lost_dependencies = False
        self._host_restrictions = None  # type: ignore
        self._worker_restrictions = None  # type: ignore
        self._resource_restrictions = None  # type: ignore
        self._loose_restrictions = False
        self._actor = False
        self._type = None  # type: ignore
        self._group_key = key_split_group(key)
        self._group = None  # type: ignore
        self._metadata = {}
        self._annotations = {}
        self._erred_on = set()

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        typ_self: type = type(self)
        typ_other: type = type(other)
        if typ_self == typ_other:
            other_ts: TaskState = other
            return self._key == other_ts._key
        else:
            return False

    @property
    def key(self):
        return self._key

    @property
    def prefix(self):
        return self._prefix

    @property
    def run_spec(self):
        return self._run_spec

    @property
    def priority(self) -> "tuple | None":
        return self._priority

    @property
    def state(self) -> "str | None":
        return self._state

    @state.setter
    def state(self, value: str):
        self._group._states[self._state] -= 1
        self._group._states[value] += 1
        self._state = value

    @property
    def dependencies(self) -> "set[TaskState]":
        return self._dependencies

    @property
    def dependents(self) -> "set[TaskState]":
        return self._dependents

    @property
    def has_lost_dependencies(self):
        return self._has_lost_dependencies

    @property
    def waiting_on(self) -> "set[TaskState]":
        return self._waiting_on

    @property
    def waiters(self) -> "set[TaskState]":
        return self._waiters

    @property
    def who_wants(self) -> "set[ClientState]":
        return self._who_wants

    @property
    def who_has(self) -> "set[WorkerState]":
        return self._who_has

    @property
    def processing_on(self) -> "WorkerState | None":
        return self._processing_on

    @processing_on.setter
    def processing_on(self, v: WorkerState) -> None:
        self._processing_on = v

    @property
    def retries(self):
        return self._retries

    @property
    def nbytes(self):
        return self._nbytes

    @nbytes.setter
    def nbytes(self, v: Py_ssize_t):
        self._nbytes = v

    @property
    def type(self) -> "str | None":
        return self._type

    @property
    def exception(self):
        return self._exception

    @property
    def exception_text(self):
        return self._exception_text

    @property
    def traceback(self):
        return self._traceback

    @property
    def traceback_text(self):
        return self._traceback_text

    @property
    def exception_blame(self) -> "TaskState | None":
        return self._exception_blame

    @property
    def suspicious(self):
        return self._suspicious

    @property
    def host_restrictions(self) -> "set[str] | None":
        return self._host_restrictions

    @property
    def worker_restrictions(self) -> "set[str] | None":
        return self._worker_restrictions

    @property
    def resource_restrictions(self) -> "dict | None":
        return self._resource_restrictions

    @property
    def loose_restrictions(self):
        return self._loose_restrictions

    @property
    def metadata(self):
        return self._metadata

    @property
    def annotations(self):
        return self._annotations

    @property
    def actor(self):
        return self._actor

    @property
    def group(self) -> "TaskGroup | None":
        return self._group

    @property
    def group_key(self) -> str:
        return self._group_key

    @property
    def prefix_key(self):
        return self._prefix._name

    @property
    def erred_on(self):
        return self._erred_on

    @ccall
    def add_dependency(self, other: "TaskState"):
        """Add another task as a dependency of this task"""
        self._dependencies.add(other)
        self._group._dependencies.add(other._group)
        other._dependents.add(self)

    @ccall
    @inline
    @nogil
    def get_nbytes(self) -> Py_ssize_t:
        return self._nbytes if self._nbytes >= 0 else DEFAULT_DATA_SIZE

    @ccall
    def set_nbytes(self, nbytes: Py_ssize_t):
        diff: Py_ssize_t = nbytes
        old_nbytes: Py_ssize_t = self._nbytes
        if old_nbytes >= 0:
            diff -= old_nbytes
        self._group._nbytes_total += diff
        ws: WorkerState
        for ws in self._who_has:
            ws._nbytes += diff
        self._nbytes = nbytes

    def __repr__(self):
        return f"<TaskState {self._key!r} {self._state}>"

    def _repr_html_(self):
        return get_template("task_state.html.j2").render(
            state=self._state,
            nbytes=self._nbytes,
            key=self._key,
        )

    @ccall
    def validate(self):
        try:
            for cs in self._who_wants:
                assert isinstance(cs, ClientState), (repr(cs), self._who_wants)
            for ws in self._who_has:
                assert isinstance(ws, WorkerState), (repr(ws), self._who_has)
            for ts in self._dependencies:
                assert isinstance(ts, TaskState), (repr(ts), self._dependencies)
            for ts in self._dependents:
                assert isinstance(ts, TaskState), (repr(ts), self._dependents)
            validate_task_state(self)
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()

    def get_nbytes_deps(self):
        nbytes: Py_ssize_t = 0
        ts: TaskState
        for ts in self._dependencies:
            nbytes += ts.get_nbytes()
        return nbytes

    def _to_dict_no_nest(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict

        Notes
        -----
        This class uses ``_to_dict_no_nest`` instead of ``_to_dict``.
        When a task references another task, or when a WorkerState.tasks contains tasks,
        this method is not executed for the inner task, even if the inner task was never
        seen before; you get a repr instead. All tasks should neatly appear under
        Scheduler.tasks. This also prevents a RecursionError during particularly heavy
        loads, which have been observed to happen whenever there's an acyclic dependency
        chain of ~200+ tasks.
        """
        return recursive_to_dict(self, exclude=exclude, members=True)


class _StateLegacyMapping(Mapping):
    """
    A mapping interface mimicking the former Scheduler state dictionaries.
    """

    def __init__(self, states, accessor):
        self._states = states
        self._accessor = accessor

    def __iter__(self):
        return iter(self._states)

    def __len__(self):
        return len(self._states)

    def __getitem__(self, key):
        return self._accessor(self._states[key])

    def __repr__(self):
        return f"{self.__class__}({dict(self)})"


class _OptionalStateLegacyMapping(_StateLegacyMapping):
    """
    Similar to _StateLegacyMapping, but a false-y value is interpreted
    as a missing key.
    """

    # For tasks etc.

    def __iter__(self):
        accessor = self._accessor
        for k, v in self._states.items():
            if accessor(v):
                yield k

    def __len__(self):
        accessor = self._accessor
        return sum(bool(accessor(v)) for v in self._states.values())

    def __getitem__(self, key):
        v = self._accessor(self._states[key])
        if v:
            return v
        else:
            raise KeyError


class _StateLegacySet(Set):
    """
    Similar to _StateLegacyMapping, but exposes a set containing
    all values with a true value.
    """

    # For loose_restrictions

    def __init__(self, states, accessor):
        self._states = states
        self._accessor = accessor

    def __iter__(self):
        return (k for k, v in self._states.items() if self._accessor(v))

    def __len__(self):
        return sum(map(bool, map(self._accessor, self._states.values())))

    def __contains__(self, k):
        st = self._states.get(k)
        return st is not None and bool(self._accessor(st))

    def __repr__(self):
        return f"{self.__class__}({set(self)})"


def _legacy_task_key_set(tasks):
    """
    Transform a set of task states into a set of task keys.
    """
    ts: TaskState
    return {ts._key for ts in tasks}


def _legacy_client_key_set(clients):
    """
    Transform a set of client states into a set of client keys.
    """
    cs: ClientState
    return {cs._client_key for cs in clients}


def _legacy_worker_key_set(workers):
    """
    Transform a set of worker states into a set of worker keys.
    """
    ws: WorkerState
    return {ws._address for ws in workers}


def _legacy_task_key_dict(task_dict: dict):
    """
    Transform a dict of {task state: value} into a dict of {task key: value}.
    """
    ts: TaskState
    return {ts._key: value for ts, value in task_dict.items()}


def _task_key_or_none(task: TaskState):
    return task._key if task is not None else None


@cclass
class SchedulerState:
    """Underlying task state of dynamic scheduler

    Tracks the current state of workers, data, and computations.

    Handles transitions between different task states. Notifies the
    Scheduler of changes by messaging passing through Queues, which the
    Scheduler listens to responds accordingly.

    All events are handled quickly, in linear time with respect to their
    input (which is often of constant size) and generally within a
    millisecond. Additionally when Cythonized, this can be faster still.
    To accomplish this the scheduler tracks a lot of state.  Every
    operation maintains the consistency of this state.

    Users typically do not interact with ``Transitions`` directly. Instead
    users interact with the ``Client``, which in turn engages the
    ``Scheduler`` affecting different transitions here under-the-hood. In
    the background ``Worker``s also engage with the ``Scheduler``
    affecting these state transitions as well.

    **State**

    The ``Transitions`` object contains the following state variables.
    Each variable is listed along with what it stores and a brief
    description.

    * **tasks:** ``{task key: TaskState}``
        Tasks currently known to the scheduler
    * **unrunnable:** ``{TaskState}``
        Tasks in the "no-worker" state

    * **workers:** ``{worker key: WorkerState}``
        Workers currently connected to the scheduler
    * **idle:** ``{WorkerState}``:
        Set of workers that are not fully utilized
    * **saturated:** ``{WorkerState}``:
        Set of workers that are not over-utilized
    * **running:** ``{WorkerState}``:
        Set of workers that are currently in running state

    * **clients:** ``{client key: ClientState}``
        Clients currently connected to the scheduler

    * **task_duration:** ``{key-prefix: time}``
        Time we expect certain functions to take, e.g. ``{'sum': 0.25}``
    """

    _aliases: dict
    _bandwidth: double
    _clients: dict  # dict[str, ClientState]
    _computations: object
    _extensions: dict
    _host_info: dict
    _idle: "SortedDict[str, WorkerState]"
    _idle_dv: dict  # dict[str, WorkerState]
    _n_tasks: Py_ssize_t
    _resources: dict
    _saturated: set  # set[WorkerState]
    _running: set  # set[WorkerState]
    _tasks: dict
    _task_groups: dict
    _task_prefixes: dict
    _task_metadata: dict
    _replicated_tasks: set
    _total_nthreads: Py_ssize_t
    _total_occupancy: double
    _transitions_table: dict
    _unknown_durations: dict
    _unrunnable: set
    _validate: bint
    _workers: "SortedDict[str, WorkerState]"
    _workers_dv: dict  # dict[str, WorkerState]
    _transition_counter: Py_ssize_t
    _plugins: dict  # dict[str, SchedulerPlugin]

    # Variables from dask.config, cached by __init__ for performance
    UNKNOWN_TASK_DURATION: double
    MEMORY_RECENT_TO_OLD_TIME: double
    MEMORY_REBALANCE_MEASURE: str
    MEMORY_REBALANCE_SENDER_MIN: double
    MEMORY_REBALANCE_RECIPIENT_MAX: double
    MEMORY_REBALANCE_HALF_GAP: double

    def __init__(
        self,
        aliases: dict,
        clients: "dict[str, ClientState]",
        workers: "SortedDict[str, WorkerState]",
        host_info: dict,
        resources: dict,
        tasks: dict,
        unrunnable: set,
        validate: bint,
        plugins: "Iterable[SchedulerPlugin]" = (),
        **kwargs,  # Passed verbatim to Server.__init__()
    ):
        self._aliases = aliases
        self._bandwidth = parse_bytes(
            dask.config.get("distributed.scheduler.bandwidth")
        )
        self._clients = clients
        self._clients["fire-and-forget"] = ClientState("fire-and-forget")
        self._extensions = {}
        self._host_info = host_info
        self._idle = SortedDict()
        # Note: cython.cast, not typing.cast!
        self._idle_dv = cast(dict, self._idle)
        self._n_tasks = 0
        self._resources = resources
        self._saturated = set()
        self._tasks = tasks
        self._replicated_tasks = {
            ts for ts in self._tasks.values() if len(ts._who_has) > 1
        }
        self._computations = deque(
            maxlen=dask.config.get("distributed.diagnostics.computations.max-history")
        )
        self._task_groups = {}
        self._task_prefixes = {}
        self._task_metadata = {}
        self._total_nthreads = 0
        self._total_occupancy = 0
        self._transitions_table = {
            ("released", "waiting"): self.transition_released_waiting,
            ("waiting", "released"): self.transition_waiting_released,
            ("waiting", "processing"): self.transition_waiting_processing,
            ("waiting", "memory"): self.transition_waiting_memory,
            ("processing", "released"): self.transition_processing_released,
            ("processing", "memory"): self.transition_processing_memory,
            ("processing", "erred"): self.transition_processing_erred,
            ("no-worker", "released"): self.transition_no_worker_released,
            ("no-worker", "waiting"): self.transition_no_worker_waiting,
            ("no-worker", "memory"): self.transition_no_worker_memory,
            ("released", "forgotten"): self.transition_released_forgotten,
            ("memory", "forgotten"): self.transition_memory_forgotten,
            ("erred", "released"): self.transition_erred_released,
            ("memory", "released"): self.transition_memory_released,
            ("released", "erred"): self.transition_released_erred,
        }
        self._unknown_durations = {}
        self._unrunnable = unrunnable
        self._validate = validate
        self._workers = workers
        # Note: cython.cast, not typing.cast!
        self._workers_dv = cast(dict, self._workers)
        self._running = {
            ws for ws in self._workers.values() if ws.status == Status.running
        }
        self._plugins = {} if not plugins else {_get_plugin_name(p): p for p in plugins}

        # Variables from dask.config, cached by __init__ for performance
        self.UNKNOWN_TASK_DURATION = parse_timedelta(
            dask.config.get("distributed.scheduler.unknown-task-duration")
        )
        self.MEMORY_RECENT_TO_OLD_TIME = parse_timedelta(
            dask.config.get("distributed.worker.memory.recent-to-old-time")
        )
        self.MEMORY_REBALANCE_MEASURE = dask.config.get(
            "distributed.worker.memory.rebalance.measure"
        )
        self.MEMORY_REBALANCE_SENDER_MIN = dask.config.get(
            "distributed.worker.memory.rebalance.sender-min"
        )
        self.MEMORY_REBALANCE_RECIPIENT_MAX = dask.config.get(
            "distributed.worker.memory.rebalance.recipient-max"
        )
        self.MEMORY_REBALANCE_HALF_GAP = (
            dask.config.get("distributed.worker.memory.rebalance.sender-recipient-gap")
            / 2.0
        )
        self._transition_counter = 0

        # Call Server.__init__()
        super().__init__(**kwargs)  # type: ignore

    @property
    def aliases(self):
        return self._aliases

    @property
    def bandwidth(self):
        return self._bandwidth

    @property
    def clients(self):
        return self._clients

    @property
    def computations(self):
        return self._computations

    @property
    def extensions(self):
        return self._extensions

    @property
    def host_info(self):
        return self._host_info

    @property
    def idle(self):
        return self._idle

    @property
    def n_tasks(self):
        return self._n_tasks

    @property
    def resources(self):
        return self._resources

    @property
    def saturated(self) -> "set[WorkerState]":
        return self._saturated

    @property
    def running(self) -> "set[WorkerState]":
        return self._running

    @property
    def tasks(self):
        return self._tasks

    @property
    def task_groups(self):
        return self._task_groups

    @property
    def task_prefixes(self):
        return self._task_prefixes

    @property
    def task_metadata(self):
        return self._task_metadata

    @property
    def replicated_tasks(self):
        return self._replicated_tasks

    @property
    def total_nthreads(self):
        return self._total_nthreads

    @property
    def total_occupancy(self):
        return self._total_occupancy

    @total_occupancy.setter
    def total_occupancy(self, v: double):
        self._total_occupancy = v

    @property
    def transition_counter(self):
        return self._transition_counter

    @property
    def unknown_durations(self):
        return self._unknown_durations

    @property
    def unrunnable(self):
        return self._unrunnable

    @property
    def validate(self):
        return self._validate

    @validate.setter
    def validate(self, v: bint):
        self._validate = v

    @property
    def workers(self):
        return self._workers

    @property
    def plugins(self) -> "dict[str, SchedulerPlugin]":
        return self._plugins

    @property
    def memory(self) -> MemoryState:
        return MemoryState.sum(*(w.memory for w in self.workers.values()))

    @property
    def __pdict__(self):
        return {
            "bandwidth": self._bandwidth,
            "resources": self._resources,
            "saturated": self._saturated,
            "unrunnable": self._unrunnable,
            "n_tasks": self._n_tasks,
            "unknown_durations": self._unknown_durations,
            "validate": self._validate,
            "tasks": self._tasks,
            "task_groups": self._task_groups,
            "task_prefixes": self._task_prefixes,
            "total_nthreads": self._total_nthreads,
            "total_occupancy": self._total_occupancy,
            "extensions": self._extensions,
            "clients": self._clients,
            "workers": self._workers,
            "idle": self._idle,
            "host_info": self._host_info,
        }

    @ccall
    @exceptval(check=False)
    def new_task(
        self, key: str, spec: object, state: str, computation: Computation = None
    ) -> TaskState:
        """Create a new task, and associated states"""
        ts: TaskState = TaskState(key, spec)
        ts._state = state

        tp: TaskPrefix
        prefix_key = key_split(key)
        tp = self._task_prefixes.get(prefix_key)  # type: ignore
        if tp is None:
            self._task_prefixes[prefix_key] = tp = TaskPrefix(prefix_key)
        ts._prefix = tp

        group_key = ts._group_key
        tg: TaskGroup = self._task_groups.get(group_key)  # type: ignore
        if tg is None:
            self._task_groups[group_key] = tg = TaskGroup(group_key)
            if computation:
                computation.groups.add(tg)
            tg._prefix = tp
            tp._groups.append(tg)
        tg.add(ts)

        self._tasks[key] = ts

        return ts

    #####################
    # State Transitions #
    #####################

    def _transition(self, key, finish: str, *args, **kwargs):
        """Transition a key from its current state to the finish state

        Examples
        --------
        >>> self._transition('x', 'waiting')
        {'x': 'processing'}

        Returns
        -------
        Dictionary of recommendations for future transitions

        See Also
        --------
        Scheduler.transitions : transitive version of this function
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState
        start: str
        start_finish: tuple
        finish2: str
        recommendations: dict
        worker_msgs: dict
        client_msgs: dict
        msgs: list
        new_msgs: list
        dependents: set
        dependencies: set
        try:
            recommendations = {}
            worker_msgs = {}
            client_msgs = {}

            ts = parent._tasks.get(key)  # type: ignore
            if ts is None:
                return recommendations, client_msgs, worker_msgs
            start = ts._state
            if start == finish:
                return recommendations, client_msgs, worker_msgs

            if self.plugins:
                dependents = set(ts._dependents)
                dependencies = set(ts._dependencies)

            start_finish = (start, finish)
            func = self._transitions_table.get(start_finish)
            if func is not None:
                recommendations, client_msgs, worker_msgs = func(key, *args, **kwargs)
                self._transition_counter += 1
            elif "released" not in start_finish:
                assert not args and not kwargs, (args, kwargs, start_finish)
                a_recs: dict
                a_cmsgs: dict
                a_wmsgs: dict
                a: tuple = self._transition(key, "released")
                a_recs, a_cmsgs, a_wmsgs = a

                v = a_recs.get(key, finish)
                func = self._transitions_table["released", v]
                b_recs: dict
                b_cmsgs: dict
                b_wmsgs: dict
                b: tuple = func(key)
                b_recs, b_cmsgs, b_wmsgs = b

                recommendations.update(a_recs)
                for c, new_msgs in a_cmsgs.items():
                    msgs = client_msgs.get(c)  # type: ignore
                    if msgs is not None:
                        msgs.extend(new_msgs)
                    else:
                        client_msgs[c] = new_msgs
                for w, new_msgs in a_wmsgs.items():
                    msgs = worker_msgs.get(w)  # type: ignore
                    if msgs is not None:
                        msgs.extend(new_msgs)
                    else:
                        worker_msgs[w] = new_msgs

                recommendations.update(b_recs)
                for c, new_msgs in b_cmsgs.items():
                    msgs = client_msgs.get(c)  # type: ignore
                    if msgs is not None:
                        msgs.extend(new_msgs)
                    else:
                        client_msgs[c] = new_msgs
                for w, new_msgs in b_wmsgs.items():
                    msgs = worker_msgs.get(w)  # type: ignore
                    if msgs is not None:
                        msgs.extend(new_msgs)
                    else:
                        worker_msgs[w] = new_msgs

                start = "released"
            else:
                raise RuntimeError("Impossible transition from %r to %r" % start_finish)

            finish2 = ts._state
            # FIXME downcast antipattern
            scheduler = pep484_cast(Scheduler, self)
            scheduler.transition_log.append(
                (key, start, finish2, recommendations, time())
            )
            if parent._validate:
                logger.debug(
                    "Transitioned %r %s->%s (actual: %s).  Consequence: %s",
                    key,
                    start,
                    finish2,
                    ts._state,
                    dict(recommendations),
                )
            if self.plugins:
                # Temporarily put back forgotten key for plugin to retrieve it
                if ts._state == "forgotten":
                    ts._dependents = dependents
                    ts._dependencies = dependencies
                    parent._tasks[ts._key] = ts
                for plugin in list(self.plugins.values()):
                    try:
                        plugin.transition(key, start, finish2, *args, **kwargs)
                    except Exception:
                        logger.info("Plugin failed with exception", exc_info=True)
                if ts._state == "forgotten":
                    del parent._tasks[ts._key]

            tg: TaskGroup = ts._group
            if ts._state == "forgotten" and tg._name in parent._task_groups:
                # Remove TaskGroup if all tasks are in the forgotten state
                all_forgotten: bint = True
                for s in ALL_TASK_STATES:
                    if tg._states.get(s):
                        all_forgotten = False
                        break
                if all_forgotten:
                    ts._prefix._groups.remove(tg)
                    del parent._task_groups[tg._name]

            return recommendations, client_msgs, worker_msgs
        except Exception:
            logger.exception("Error transitioning %r from %r to %r", key, start, finish)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def _transitions(self, recommendations: dict, client_msgs: dict, worker_msgs: dict):
        """Process transitions until none are left

        This includes feedback from previous transitions and continues until we
        reach a steady state
        """
        keys: set = set()
        recommendations = recommendations.copy()
        msgs: list
        new_msgs: list
        new: tuple
        new_recs: dict
        new_cmsgs: dict
        new_wmsgs: dict
        while recommendations:
            key, finish = recommendations.popitem()
            keys.add(key)

            new = self._transition(key, finish)
            new_recs, new_cmsgs, new_wmsgs = new

            recommendations.update(new_recs)
            for c, new_msgs in new_cmsgs.items():
                msgs = client_msgs.get(c)  # type: ignore
                if msgs is not None:
                    msgs.extend(new_msgs)
                else:
                    client_msgs[c] = new_msgs
            for w, new_msgs in new_wmsgs.items():
                msgs = worker_msgs.get(w)  # type: ignore
                if msgs is not None:
                    msgs.extend(new_msgs)
                else:
                    worker_msgs[w] = new_msgs

        if self._validate:
            # FIXME downcast antipattern
            scheduler = pep484_cast(Scheduler, self)
            for key in keys:
                scheduler.validate_key(key)

    def transition_released_waiting(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert ts._run_spec
                assert not ts._waiting_on
                assert not ts._who_has
                assert not ts._processing_on
                assert not any([dts._state == "forgotten" for dts in ts._dependencies])

            if ts._has_lost_dependencies:
                recommendations[key] = "forgotten"
                return recommendations, client_msgs, worker_msgs

            ts.state = "waiting"

            dts: TaskState
            for dts in ts._dependencies:
                if dts._exception_blame:
                    ts._exception_blame = dts._exception_blame
                    recommendations[key] = "erred"
                    return recommendations, client_msgs, worker_msgs

            for dts in ts._dependencies:
                dep = dts._key
                if not dts._who_has:
                    ts._waiting_on.add(dts)
                if dts._state == "released":
                    recommendations[dep] = "waiting"
                else:
                    dts._waiters.add(ts)

            ts._waiters = {dts for dts in ts._dependents if dts._state == "waiting"}

            if not ts._waiting_on:
                if self._workers_dv:
                    recommendations[key] = "processing"
                else:
                    self._unrunnable.add(ts)
                    ts.state = "no-worker"

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_no_worker_waiting(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert ts in self._unrunnable
                assert not ts._waiting_on
                assert not ts._who_has
                assert not ts._processing_on

            self._unrunnable.remove(ts)

            if ts._has_lost_dependencies:
                recommendations[key] = "forgotten"
                return recommendations, client_msgs, worker_msgs

            for dts in ts._dependencies:
                dep = dts._key
                if not dts._who_has:
                    ts._waiting_on.add(dts)
                if dts._state == "released":
                    recommendations[dep] = "waiting"
                else:
                    dts._waiters.add(ts)

            ts.state = "waiting"

            if not ts._waiting_on:
                if self._workers_dv:
                    recommendations[key] = "processing"
                else:
                    self._unrunnable.add(ts)
                    ts.state = "no-worker"

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_no_worker_memory(
        self, key, nbytes=None, type=None, typename: str = None, worker=None
    ):
        try:
            ws: WorkerState = self._workers_dv[worker]
            ts: TaskState = self._tasks[key]
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert not ts._processing_on
                assert not ts._waiting_on
                assert ts._state == "no-worker"

            self._unrunnable.remove(ts)

            if nbytes is not None:
                ts.set_nbytes(nbytes)

            self.check_idle_saturated(ws)

            _add_to_memory(
                self, ts, ws, recommendations, client_msgs, type=type, typename=typename
            )
            ts.state = "memory"

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    @ccall
    @exceptval(check=False)
    def decide_worker(self, ts: TaskState) -> WorkerState:  # -> WorkerState | None
        """
        Decide on a worker for task *ts*. Return a WorkerState.

        If it's a root or root-like task, we place it with its relatives to
        reduce future data tansfer.

        If it has dependencies or restrictions, we use
        `decide_worker_from_deps_and_restrictions`.

        Otherwise, we pick the least occupied worker, or pick from all workers
        in a round-robin fashion.
        """
        if not self._workers_dv:
            return None  # type: ignore

        ws: WorkerState
        tg: TaskGroup = ts._group
        valid_workers: set = self.valid_workers(ts)

        if (
            valid_workers is not None
            and not valid_workers
            and not ts._loose_restrictions
        ):
            self._unrunnable.add(ts)
            ts.state = "no-worker"
            return None  # type: ignore

        # Group is larger than cluster with few dependencies?
        # Minimize future data transfers.
        if (
            valid_workers is None
            and len(tg) > self._total_nthreads * 2
            and len(tg._dependencies) < 5
            and sum(map(len, tg._dependencies)) < 5
        ):
            ws = tg._last_worker

            if not (
                ws and tg._last_worker_tasks_left and ws._address in self._workers_dv
            ):
                # Last-used worker is full or unknown; pick a new worker for the next few tasks
                ws = min(
                    (self._idle_dv or self._workers_dv).values(),
                    key=partial(self.worker_objective, ts),
                )
                tg._last_worker_tasks_left = math.floor(
                    (len(tg) / self._total_nthreads) * ws._nthreads
                )

            # Record `last_worker`, or clear it on the final task
            tg._last_worker = (
                ws if tg.states["released"] + tg.states["waiting"] > 1 else None
            )
            tg._last_worker_tasks_left -= 1
            return ws

        if ts._dependencies or valid_workers is not None:
            ws = decide_worker(
                ts,
                self._workers_dv.values(),
                valid_workers,
                partial(self.worker_objective, ts),
            )
        else:
            # Fastpath when there are no related tasks or restrictions
            worker_pool = self._idle or self._workers
            # Note: cython.cast, not typing.cast!
            worker_pool_dv = cast(dict, worker_pool)
            wp_vals = worker_pool.values()
            n_workers: Py_ssize_t = len(worker_pool_dv)
            if n_workers < 20:  # smart but linear in small case
                ws = min(wp_vals, key=operator.attrgetter("occupancy"))
                if ws._occupancy == 0:
                    # special case to use round-robin; linear search
                    # for next worker with zero occupancy (or just
                    # land back where we started).
                    wp_i: WorkerState
                    start: Py_ssize_t = self._n_tasks % n_workers
                    i: Py_ssize_t
                    for i in range(n_workers):
                        wp_i = wp_vals[(i + start) % n_workers]
                        if wp_i._occupancy == 0:
                            ws = wp_i
                            break
            else:  # dumb but fast in large case
                ws = wp_vals[self._n_tasks % n_workers]

        if self._validate:
            assert ws is None or isinstance(ws, WorkerState), (
                type(ws),
                ws,
            )
            assert ws._address in self._workers_dv

        return ws

    @ccall
    def set_duration_estimate(self, ts: TaskState, ws: WorkerState) -> double:
        """Estimate task duration using worker state and task state.

        If a task takes longer than twice the current average duration we
        estimate the task duration to be 2x current-runtime, otherwise we set it
        to be the average duration.

        See also ``_remove_from_processing``
        """
        exec_time: double = ws._executing.get(ts, 0)
        duration: double = self.get_task_duration(ts)
        total_duration: double
        if exec_time > 2 * duration:
            total_duration = 2 * exec_time
        else:
            comm: double = self.get_comm_cost(ts, ws)
            total_duration = duration + comm
        old = ws._processing.get(ts, 0)
        ws._processing[ts] = total_duration

        if ts not in ws._long_running:
            self._total_occupancy += total_duration - old
            ws._occupancy += total_duration - old

        return total_duration

    def transition_waiting_processing(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert not ts._waiting_on
                assert not ts._who_has
                assert not ts._exception_blame
                assert not ts._processing_on
                assert not ts._has_lost_dependencies
                assert ts not in self._unrunnable
                assert all([dts._who_has for dts in ts._dependencies])

            ws: WorkerState = self.decide_worker(ts)
            if ws is None:
                return recommendations, client_msgs, worker_msgs
            worker = ws._address

            self.set_duration_estimate(ts, ws)
            ts._processing_on = ws
            ts.state = "processing"
            self.consume_resources(ts, ws)
            self.check_idle_saturated(ws)
            self._n_tasks += 1

            if ts._actor:
                ws._actors.add(ts)

            # logger.debug("Send job to worker: %s, %s", worker, key)

            worker_msgs[worker] = [_task_to_msg(self, ts)]

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_waiting_memory(
        self, key, nbytes=None, type=None, typename: str = None, worker=None, **kwargs
    ):
        try:
            ws: WorkerState = self._workers_dv[worker]
            ts: TaskState = self._tasks[key]
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert not ts._processing_on
                assert ts._waiting_on
                assert ts._state == "waiting"

            ts._waiting_on.clear()

            if nbytes is not None:
                ts.set_nbytes(nbytes)

            self.check_idle_saturated(ws)

            _add_to_memory(
                self, ts, ws, recommendations, client_msgs, type=type, typename=typename
            )

            if self._validate:
                assert not ts._processing_on
                assert not ts._waiting_on
                assert ts._who_has

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_processing_memory(
        self,
        key,
        nbytes=None,
        type=None,
        typename: str = None,
        worker=None,
        startstops=None,
        **kwargs,
    ):
        ws: WorkerState
        wws: WorkerState
        recommendations: dict = {}
        client_msgs: dict = {}
        worker_msgs: dict = {}
        try:
            ts: TaskState = self._tasks[key]

            assert worker
            assert isinstance(worker, str)

            if self._validate:
                assert ts._processing_on
                ws = ts._processing_on
                assert ts in ws._processing
                assert not ts._waiting_on
                assert not ts._who_has, (ts, ts._who_has)
                assert not ts._exception_blame
                assert ts._state == "processing"

            ws = self._workers_dv.get(worker)  # type: ignore
            if ws is None:
                recommendations[key] = "released"
                return recommendations, client_msgs, worker_msgs

            if ws != ts._processing_on:  # someone else has this task
                logger.info(
                    "Unexpected worker completed task. Expected: %s, Got: %s, Key: %s",
                    ts._processing_on,
                    ws,
                    key,
                )
                worker_msgs[ts._processing_on.address] = [
                    {
                        "op": "cancel-compute",
                        "key": key,
                        "stimulus_id": f"processing-memory-{time()}",
                    }
                ]

            #############################
            # Update Timing Information #
            #############################
            if startstops:
                startstop: dict
                for startstop in startstops:
                    ts._group.add_duration(
                        stop=startstop["stop"],
                        start=startstop["start"],
                        action=startstop["action"],
                    )

            s: set = self._unknown_durations.pop(ts._prefix._name, set())
            tts: TaskState
            steal = self.extensions.get("stealing")
            for tts in s:
                if tts._processing_on:
                    self.set_duration_estimate(tts, tts._processing_on)
                    if steal:
                        steal.recalculate_cost(tts)

            ############################
            # Update State Information #
            ############################
            if nbytes is not None:
                ts.set_nbytes(nbytes)

            _remove_from_processing(self, ts)

            _add_to_memory(
                self, ts, ws, recommendations, client_msgs, type=type, typename=typename
            )

            if self._validate:
                assert not ts._processing_on
                assert not ts._waiting_on

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_memory_released(self, key, safe: bint = False):
        ws: WorkerState
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert not ts._waiting_on
                assert not ts._processing_on
                if safe:
                    assert not ts._waiters

            if ts._actor:
                for ws in ts._who_has:
                    ws._actors.discard(ts)
                if ts._who_wants:
                    ts._exception_blame = ts
                    ts._exception = "Worker holding Actor was lost"
                    recommendations[ts._key] = "erred"
                    return (
                        recommendations,
                        client_msgs,
                        worker_msgs,
                    )  # don't try to recreate

            for dts in ts._waiters:
                if dts._state in ("no-worker", "processing"):
                    recommendations[dts._key] = "waiting"
                elif dts._state == "waiting":
                    dts._waiting_on.add(ts)

            # XXX factor this out?
            worker_msg = {
                "op": "free-keys",
                "keys": [key],
                "stimulus_id": f"memory-released-{time()}",
            }
            for ws in ts._who_has:
                worker_msgs[ws._address] = [worker_msg]
            self.remove_all_replicas(ts)

            ts.state = "released"

            report_msg = {"op": "lost-data", "key": key}
            cs: ClientState
            for cs in ts._who_wants:
                client_msgs[cs._client_key] = [report_msg]

            if not ts._run_spec:  # pure data
                recommendations[key] = "forgotten"
            elif ts._has_lost_dependencies:
                recommendations[key] = "forgotten"
            elif ts._who_wants or ts._waiters:
                recommendations[key] = "waiting"

            if self._validate:
                assert not ts._waiting_on

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_released_erred(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            failing_ts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                with log_errors(pdb=LOG_PDB):
                    assert ts._exception_blame
                    assert not ts._who_has
                    assert not ts._waiting_on
                    assert not ts._waiters

            failing_ts = ts._exception_blame

            for dts in ts._dependents:
                dts._exception_blame = failing_ts
                if not dts._who_has:
                    recommendations[dts._key] = "erred"

            report_msg = {
                "op": "task-erred",
                "key": key,
                "exception": failing_ts._exception,
                "traceback": failing_ts._traceback,
            }
            cs: ClientState
            for cs in ts._who_wants:
                client_msgs[cs._client_key] = [report_msg]

            ts.state = "erred"

            # TODO: waiting data?
            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_erred_released(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                with log_errors(pdb=LOG_PDB):
                    assert ts._exception_blame
                    assert not ts._who_has
                    assert not ts._waiting_on
                    assert not ts._waiters

            ts._exception = None
            ts._exception_blame = None
            ts._traceback = None

            for dts in ts._dependents:
                if dts._state == "erred":
                    recommendations[dts._key] = "waiting"

            w_msg = {
                "op": "free-keys",
                "keys": [key],
                "stimulus_id": f"erred-released-{time()}",
            }
            for ws_addr in ts._erred_on:
                worker_msgs[ws_addr] = [w_msg]
            ts._erred_on.clear()

            report_msg = {"op": "task-retried", "key": key}
            cs: ClientState
            for cs in ts._who_wants:
                client_msgs[cs._client_key] = [report_msg]

            ts.state = "released"

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_waiting_released(self, key):
        try:
            ts: TaskState = self._tasks[key]
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert not ts._who_has
                assert not ts._processing_on

            dts: TaskState
            for dts in ts._dependencies:
                if ts in dts._waiters:
                    dts._waiters.discard(ts)
                    if not dts._waiters and not dts._who_wants:
                        recommendations[dts._key] = "released"
            ts._waiting_on.clear()

            ts.state = "released"

            if ts._has_lost_dependencies:
                recommendations[key] = "forgotten"
            elif not ts._exception_blame and (ts._who_wants or ts._waiters):
                recommendations[key] = "waiting"
            else:
                ts._waiters.clear()

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_processing_released(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert ts._processing_on
                assert not ts._who_has
                assert not ts._waiting_on
                assert self._tasks[key].state == "processing"

            w: str = _remove_from_processing(self, ts)
            if w:
                worker_msgs[w] = [
                    {
                        "op": "free-keys",
                        "keys": [key],
                        "stimulus_id": f"processing-released-{time()}",
                    }
                ]

            ts.state = "released"

            if ts._has_lost_dependencies:
                recommendations[key] = "forgotten"
            elif ts._waiters or ts._who_wants:
                recommendations[key] = "waiting"

            if recommendations.get(key) != "waiting":
                for dts in ts._dependencies:
                    if dts._state != "released":
                        dts._waiters.discard(ts)
                        if not dts._waiters and not dts._who_wants:
                            recommendations[dts._key] = "released"
                ts._waiters.clear()

            if self._validate:
                assert not ts._processing_on

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_processing_erred(
        self,
        key: str,
        cause: str = None,
        exception=None,
        traceback=None,
        exception_text: str = None,
        traceback_text: str = None,
        worker: str = None,
        **kwargs,
    ):
        ws: WorkerState
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            failing_ts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert cause or ts._exception_blame
                assert ts._processing_on
                assert not ts._who_has
                assert not ts._waiting_on

            if ts._actor:
                ws = ts._processing_on
                ws._actors.remove(ts)

            w = _remove_from_processing(self, ts)

            ts._erred_on.add(w or worker)
            if exception is not None:
                ts._exception = exception
                ts._exception_text = exception_text  # type: ignore
            if traceback is not None:
                ts._traceback = traceback
                ts._traceback_text = traceback_text  # type: ignore
            if cause is not None:
                failing_ts = self._tasks[cause]
                ts._exception_blame = failing_ts
            else:
                failing_ts = ts._exception_blame  # type: ignore

            for dts in ts._dependents:
                dts._exception_blame = failing_ts
                recommendations[dts._key] = "erred"

            for dts in ts._dependencies:
                dts._waiters.discard(ts)
                if not dts._waiters and not dts._who_wants:
                    recommendations[dts._key] = "released"

            ts._waiters.clear()  # do anything with this?

            ts.state = "erred"

            report_msg = {
                "op": "task-erred",
                "key": key,
                "exception": failing_ts._exception,
                "traceback": failing_ts._traceback,
            }
            cs: ClientState
            for cs in ts._who_wants:
                client_msgs[cs._client_key] = [report_msg]

            cs = self._clients["fire-and-forget"]
            if ts in cs._wants_what:
                _client_releases_keys(
                    self,
                    cs=cs,
                    keys=[key],
                    recommendations=recommendations,
                )

            if self._validate:
                assert not ts._processing_on

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_no_worker_released(self, key):
        try:
            ts: TaskState = self._tasks[key]
            dts: TaskState
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert self._tasks[key].state == "no-worker"
                assert not ts._who_has
                assert not ts._waiting_on

            self._unrunnable.remove(ts)
            ts.state = "released"

            for dts in ts._dependencies:
                dts._waiters.discard(ts)

            ts._waiters.clear()

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    @ccall
    def remove_key(self, key):
        ts: TaskState = self._tasks.pop(key)
        assert ts._state == "forgotten"
        self._unrunnable.discard(ts)
        cs: ClientState
        for cs in ts._who_wants:
            cs._wants_what.remove(ts)
        ts._who_wants.clear()
        ts._processing_on = None
        ts._exception_blame = ts._exception = ts._traceback = None
        self._task_metadata.pop(key, None)

    def transition_memory_forgotten(self, key):
        ws: WorkerState
        try:
            ts: TaskState = self._tasks[key]
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert ts._state == "memory"
                assert not ts._processing_on
                assert not ts._waiting_on
                if not ts._run_spec:
                    # It's ok to forget a pure data task
                    pass
                elif ts._has_lost_dependencies:
                    # It's ok to forget a task with forgotten dependencies
                    pass
                elif not ts._who_wants and not ts._waiters and not ts._dependents:
                    # It's ok to forget a task that nobody needs
                    pass
                else:
                    assert 0, (ts,)

            if ts._actor:
                for ws in ts._who_has:
                    ws._actors.discard(ts)

            _propagate_forgotten(self, ts, recommendations, worker_msgs)

            client_msgs = _task_to_client_msgs(self, ts)
            self.remove_key(key)

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def transition_released_forgotten(self, key):
        try:
            ts: TaskState = self._tasks[key]
            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}

            if self._validate:
                assert ts._state in ("released", "erred")
                assert not ts._who_has
                assert not ts._processing_on
                assert not ts._waiting_on, (ts, ts._waiting_on)
                if not ts._run_spec:
                    # It's ok to forget a pure data task
                    pass
                elif ts._has_lost_dependencies:
                    # It's ok to forget a task with forgotten dependencies
                    pass
                elif not ts._who_wants and not ts._waiters and not ts._dependents:
                    # It's ok to forget a task that nobody needs
                    pass
                else:
                    assert 0, (ts,)

            _propagate_forgotten(self, ts, recommendations, worker_msgs)

            client_msgs = _task_to_client_msgs(self, ts)
            self.remove_key(key)

            return recommendations, client_msgs, worker_msgs
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    ##############################
    # Assigning Tasks to Workers #
    ##############################

    @ccall
    @exceptval(check=False)
    def check_idle_saturated(self, ws: WorkerState, occ: double = -1.0):
        """Update the status of the idle and saturated state

        The scheduler keeps track of workers that are ..

        -  Saturated: have enough work to stay busy
        -  Idle: do not have enough work to stay busy

        They are considered saturated if they both have enough tasks to occupy
        all of their threads, and if the expected runtime of those tasks is
        large enough.

        This is useful for load balancing and adaptivity.
        """
        if self._total_nthreads == 0 or ws.status == Status.closed:
            return
        if occ < 0:
            occ = ws._occupancy

        nc: Py_ssize_t = ws._nthreads
        p: Py_ssize_t = len(ws._processing)
        avg: double = self._total_occupancy / self._total_nthreads

        idle = self._idle
        saturated: set = self._saturated
        if p < nc or occ < nc * avg / 2:
            idle[ws._address] = ws
            saturated.discard(ws)
        else:
            idle.pop(ws._address, None)

            if p > nc:
                pending: double = occ * (p - nc) / (p * nc)
                if 0.4 < pending > 1.9 * avg:
                    saturated.add(ws)
                    return

            saturated.discard(ws)

    @ccall
    def get_comm_cost(self, ts: TaskState, ws: WorkerState) -> double:
        """
        Get the estimated communication cost (in s.) to compute the task
        on the given worker.
        """
        dts: TaskState
        deps: set = ts._dependencies.difference(ws._has_what)
        nbytes: Py_ssize_t = 0
        for dts in deps:
            nbytes += dts._nbytes
        return nbytes / self._bandwidth

    @ccall
    def get_task_duration(self, ts: TaskState) -> double:
        """Get the estimated computation cost of the given task (not including
        any communication cost).

        If no data has been observed, value of
        `distributed.scheduler.default-task-durations` are used. If none is set
        for this task, `distributed.scheduler.unknown-task-duration` is used
        instead.
        """
        duration: double = ts._prefix._duration_average
        if duration >= 0:
            return duration

        s: set = self._unknown_durations.get(ts._prefix._name)  # type: ignore
        if s is None:
            self._unknown_durations[ts._prefix._name] = s = set()
        s.add(ts)
        return self.UNKNOWN_TASK_DURATION

    @ccall
    @exceptval(check=False)
    def valid_workers(self, ts: TaskState) -> set:  # set[WorkerState] | None
        """Return set of currently valid workers for key

        If all workers are valid then this returns ``None``.
        This checks tracks the following state:

        *  worker_restrictions
        *  host_restrictions
        *  resource_restrictions
        """
        s: set = None  # type: ignore

        if ts._worker_restrictions:
            s = {addr for addr in ts._worker_restrictions if addr in self._workers_dv}

        if ts._host_restrictions:
            # Resolve the alias here rather than early, for the worker
            # may not be connected when host_restrictions is populated
            hr: list = [self.coerce_hostname(h) for h in ts._host_restrictions]
            # XXX need HostState?
            sl: list = []
            for h in hr:
                dh: dict = self._host_info.get(h)  # type: ignore
                if dh is not None:
                    sl.append(dh["addresses"])

            ss: set = set.union(*sl) if sl else set()
            if s is None:
                s = ss
            else:
                s |= ss

        if ts._resource_restrictions:
            dw: dict = {}
            for resource, required in ts._resource_restrictions.items():
                dr: dict = self._resources.get(resource)  # type: ignore
                if dr is None:
                    self._resources[resource] = dr = {}

                sw: set = set()
                for addr, supplied in dr.items():
                    if supplied >= required:
                        sw.add(addr)

                dw[resource] = sw

            ww: set = set.intersection(*dw.values())
            if s is None:
                s = ww
            else:
                s &= ww

        if s is None:
            if len(self._running) < len(self._workers_dv):
                return self._running.copy()
        else:
            s = {self._workers_dv[addr] for addr in s}
            if len(self._running) < len(self._workers_dv):
                s &= self._running

        return s

    @ccall
    def consume_resources(self, ts: TaskState, ws: WorkerState):
        if ts._resource_restrictions:
            for r, required in ts._resource_restrictions.items():
                ws._used_resources[r] += required

    @ccall
    def release_resources(self, ts: TaskState, ws: WorkerState):
        if ts._resource_restrictions:
            for r, required in ts._resource_restrictions.items():
                ws._used_resources[r] -= required

    @ccall
    def coerce_hostname(self, host):
        """
        Coerce the hostname of a worker.
        """
        alias = self._aliases.get(host)
        if alias is not None:
            ws: WorkerState = self._workers_dv[alias]
            return ws.host
        else:
            return host

    @ccall
    @exceptval(check=False)
    def worker_objective(self, ts: TaskState, ws: WorkerState) -> tuple:
        """
        Objective function to determine which worker should get the task

        Minimize expected start time.  If a tie then break with data storage.
        """
        dts: TaskState
        nbytes: Py_ssize_t
        comm_bytes: Py_ssize_t = 0
        for dts in ts._dependencies:
            if ws not in dts._who_has:
                nbytes = dts.get_nbytes()
                comm_bytes += nbytes

        stack_time: double = ws._occupancy / ws._nthreads
        start_time: double = stack_time + comm_bytes / self._bandwidth

        if ts._actor:
            return (len(ws._actors), start_time, ws._nbytes)
        else:
            return (start_time, ws._nbytes)

    @ccall
    def add_replica(self, ts: TaskState, ws: WorkerState):
        """Note that a worker holds a replica of a task with state='memory'"""
        if self._validate:
            assert ws not in ts._who_has
            assert ts not in ws._has_what

        ws._nbytes += ts.get_nbytes()
        ws._has_what[ts] = None
        ts._who_has.add(ws)
        if len(ts._who_has) == 2:
            self._replicated_tasks.add(ts)

    @ccall
    def remove_replica(self, ts: TaskState, ws: WorkerState):
        """Note that a worker no longer holds a replica of a task"""
        ws._nbytes -= ts.get_nbytes()
        del ws._has_what[ts]
        ts._who_has.remove(ws)
        if len(ts._who_has) == 1:
            self._replicated_tasks.remove(ts)

    @ccall
    def remove_all_replicas(self, ts: TaskState):
        """Remove all replicas of a task from all workers"""
        ws: WorkerState
        nbytes: Py_ssize_t = ts.get_nbytes()
        for ws in ts._who_has:
            ws._nbytes -= nbytes
            del ws._has_what[ts]
        if len(ts._who_has) > 1:
            self._replicated_tasks.remove(ts)
        ts._who_has.clear()

    @ccall
    @exceptval(check=False)
    def _reevaluate_occupancy_worker(self, ws: WorkerState):
        """See reevaluate_occupancy"""
        ts: TaskState
        old = ws._occupancy
        for ts in ws._processing:
            self.set_duration_estimate(ts, ws)

        self.check_idle_saturated(ws)
        steal = self.extensions.get("stealing")
        if steal is None:
            return
        if ws._occupancy > old * 1.3 or old > ws._occupancy * 1.3:
            for ts in ws._processing:
                steal.recalculate_cost(ts)


class Scheduler(SchedulerState, ServerNode):
    """Dynamic distributed task scheduler

    The scheduler tracks the current state of workers, data, and computations.
    The scheduler listens for events and responds by controlling workers
    appropriately.  It continuously tries to use the workers to execute an ever
    growing dask graph.

    All events are handled quickly, in linear time with respect to their input
    (which is often of constant size) and generally within a millisecond.  To
    accomplish this the scheduler tracks a lot of state.  Every operation
    maintains the consistency of this state.

    The scheduler communicates with the outside world through Comm objects.
    It maintains a consistent and valid view of the world even when listening
    to several clients at once.

    A Scheduler is typically started either with the ``dask-scheduler``
    executable::

         $ dask-scheduler
         Scheduler started at 127.0.0.1:8786

    Or within a LocalCluster a Client starts up without connection
    information::

        >>> c = Client()  # doctest: +SKIP
        >>> c.cluster.scheduler  # doctest: +SKIP
        Scheduler(...)

    Users typically do not interact with the scheduler directly but rather with
    the client object ``Client``.

    **State**

    The scheduler contains the following state variables.  Each variable is
    listed along with what it stores and a brief description.

    * **tasks:** ``{task key: TaskState}``
        Tasks currently known to the scheduler
    * **unrunnable:** ``{TaskState}``
        Tasks in the "no-worker" state

    * **workers:** ``{worker key: WorkerState}``
        Workers currently connected to the scheduler
    * **idle:** ``{WorkerState}``:
        Set of workers that are not fully utilized
    * **saturated:** ``{WorkerState}``:
        Set of workers that are not over-utilized

    * **host_info:** ``{hostname: dict}``:
        Information about each worker host

    * **clients:** ``{client key: ClientState}``
        Clients currently connected to the scheduler

    * **services:** ``{str: port}``:
        Other services running on this scheduler, like Bokeh
    * **loop:** ``IOLoop``:
        The running Tornado IOLoop
    * **client_comms:** ``{client key: Comm}``
        For each client, a Comm object used to receive task requests and
        report task status updates.
    * **stream_comms:** ``{worker key: Comm}``
        For each worker, a Comm object from which we both accept stimuli and
        report results
    * **task_duration:** ``{key-prefix: time}``
        Time we expect certain functions to take, e.g. ``{'sum': 0.25}``
    """

    default_port = 8786
    _instances: "ClassVar[weakref.WeakSet[Scheduler]]" = weakref.WeakSet()

    def __init__(
        self,
        loop=None,
        delete_interval="500ms",
        synchronize_worker_interval="60s",
        services=None,
        service_kwargs=None,
        allowed_failures=None,
        extensions=None,
        validate=None,
        scheduler_file=None,
        security=None,
        worker_ttl=None,
        idle_timeout=None,
        interface=None,
        host=None,
        port=0,
        protocol=None,
        dashboard_address=None,
        dashboard=None,
        http_prefix="/",
        preload=None,
        preload_argv=(),
        plugins=(),
        **kwargs,
    ):
        self._setup_logging(logger)

        # Attributes
        if allowed_failures is None:
            allowed_failures = dask.config.get("distributed.scheduler.allowed-failures")
        self.allowed_failures = allowed_failures
        if validate is None:
            validate = dask.config.get("distributed.scheduler.validate")
        self.proc = psutil.Process()
        self.delete_interval = parse_timedelta(delete_interval, default="ms")
        self.synchronize_worker_interval = parse_timedelta(
            synchronize_worker_interval, default="ms"
        )
        self.digests = None
        self.service_specs = services or {}
        self.service_kwargs = service_kwargs or {}
        self.services = {}
        self.scheduler_file = scheduler_file
        worker_ttl = worker_ttl or dask.config.get("distributed.scheduler.worker-ttl")
        self.worker_ttl = parse_timedelta(worker_ttl) if worker_ttl else None
        idle_timeout = idle_timeout or dask.config.get(
            "distributed.scheduler.idle-timeout"
        )
        if idle_timeout:
            self.idle_timeout = parse_timedelta(idle_timeout)
        else:
            self.idle_timeout = None
        self.idle_since = time()
        self.time_started = self.idle_since  # compatibility for dask-gateway
        self._lock = asyncio.Lock()
        self.bandwidth_workers = defaultdict(float)
        self.bandwidth_types = defaultdict(float)

        if not preload:
            preload = dask.config.get("distributed.scheduler.preload")
        if not preload_argv:
            preload_argv = dask.config.get("distributed.scheduler.preload-argv")
        self.preloads = preloading.process_preloads(self, preload, preload_argv)

        if isinstance(security, dict):
            security = Security(**security)
        self.security = security or Security()
        assert isinstance(self.security, Security)
        self.connection_args = self.security.get_connection_args("scheduler")
        self.connection_args["handshake_overrides"] = {  # common denominator
            "pickle-protocol": 4
        }

        self._start_address = addresses_from_user_args(
            host=host,
            port=port,
            interface=interface,
            protocol=protocol,
            security=security,
            default_port=self.default_port,
        )

        http_server_modules = dask.config.get("distributed.scheduler.http.routes")
        show_dashboard = dashboard or (dashboard is None and dashboard_address)
        # install vanilla route if show_dashboard but bokeh is not installed
        if show_dashboard:
            try:
                import distributed.dashboard.scheduler
            except ImportError:
                show_dashboard = False
                http_server_modules.append("distributed.http.scheduler.missing_bokeh")
        routes = get_handlers(
            server=self, modules=http_server_modules, prefix=http_prefix
        )
        self.start_http_server(routes, dashboard_address, default_port=8787)
        if show_dashboard:
            distributed.dashboard.scheduler.connect(
                self.http_application, self.http_server, self, prefix=http_prefix
            )

        # Communication state
        self.loop = loop or IOLoop.current()
        self.client_comms = {}
        self.stream_comms = {}
        self._worker_coroutines = []
        self._ipython_kernel = None

        # Task state
        tasks = {}
        for old_attr, new_attr, wrap in [
            ("priority", "priority", None),
            ("dependencies", "dependencies", _legacy_task_key_set),
            ("dependents", "dependents", _legacy_task_key_set),
            ("retries", "retries", None),
        ]:
            func = operator.attrgetter(new_attr)
            if wrap is not None:
                func = compose(wrap, func)
            setattr(self, old_attr, _StateLegacyMapping(tasks, func))

        for old_attr, new_attr, wrap in [
            ("nbytes", "nbytes", None),
            ("who_wants", "who_wants", _legacy_client_key_set),
            ("who_has", "who_has", _legacy_worker_key_set),
            ("waiting", "waiting_on", _legacy_task_key_set),
            ("waiting_data", "waiters", _legacy_task_key_set),
            ("rprocessing", "processing_on", None),
            ("host_restrictions", "host_restrictions", None),
            ("worker_restrictions", "worker_restrictions", None),
            ("resource_restrictions", "resource_restrictions", None),
            ("suspicious_tasks", "suspicious", None),
            ("exceptions", "exception", None),
            ("tracebacks", "traceback", None),
            ("exceptions_blame", "exception_blame", _task_key_or_none),
        ]:
            func = operator.attrgetter(new_attr)
            if wrap is not None:
                func = compose(wrap, func)
            setattr(self, old_attr, _OptionalStateLegacyMapping(tasks, func))

        for old_attr, new_attr, wrap in [
            ("loose_restrictions", "loose_restrictions", None)
        ]:
            func = operator.attrgetter(new_attr)
            if wrap is not None:
                func = compose(wrap, func)
            setattr(self, old_attr, _StateLegacySet(tasks, func))

        self.generation = 0
        self._last_client = None
        self._last_time = 0
        unrunnable = set()

        self.datasets = {}

        # Prefix-keyed containers

        # Client state
        clients = {}
        for old_attr, new_attr, wrap in [
            ("wants_what", "wants_what", _legacy_task_key_set)
        ]:
            func = operator.attrgetter(new_attr)
            if wrap is not None:
                func = compose(wrap, func)
            setattr(self, old_attr, _StateLegacyMapping(clients, func))

        # Worker state
        workers = SortedDict()
        for old_attr, new_attr, wrap in [
            ("nthreads", "nthreads", None),
            ("worker_bytes", "nbytes", None),
            ("worker_resources", "resources", None),
            ("used_resources", "used_resources", None),
            ("occupancy", "occupancy", None),
            ("worker_info", "metrics", None),
            ("processing", "processing", _legacy_task_key_dict),
            ("has_what", "has_what", _legacy_task_key_set),
        ]:
            func = operator.attrgetter(new_attr)
            if wrap is not None:
                func = compose(wrap, func)
            setattr(self, old_attr, _StateLegacyMapping(workers, func))

        host_info = {}
        resources = {}
        aliases = {}

        self._task_state_collections = [unrunnable]

        self._worker_collections = [
            workers,
            host_info,
            resources,
            aliases,
        ]

        self.transition_log = deque(
            maxlen=dask.config.get("distributed.scheduler.transition-log-length")
        )
        self.log = deque(
            maxlen=dask.config.get("distributed.scheduler.transition-log-length")
        )
        self.events = defaultdict(
            partial(
                deque, maxlen=dask.config.get("distributed.scheduler.events-log-length")
            )
        )
        self.event_counts = defaultdict(int)
        self.event_subscriber = defaultdict(set)
        self.worker_plugins = {}
        self.nanny_plugins = {}

        worker_handlers = {
            "task-finished": self.handle_task_finished,
            "task-erred": self.handle_task_erred,
            "release-worker-data": self.release_worker_data,
            "add-keys": self.add_keys,
            "missing-data": self.handle_missing_data,
            "long-running": self.handle_long_running,
            "reschedule": self.reschedule,
            "keep-alive": lambda *args, **kwargs: None,
            "log-event": self.log_worker_event,
            "worker-status-change": self.handle_worker_status_change,
        }

        client_handlers = {
            "update-graph": self.update_graph,
            "update-graph-hlg": self.update_graph_hlg,
            "client-desires-keys": self.client_desires_keys,
            "update-data": self.update_data,
            "report-key": self.report_on_key,
            "client-releases-keys": self.client_releases_keys,
            "heartbeat-client": self.client_heartbeat,
            "close-client": self.remove_client,
            "restart": self.restart,
            "subscribe-topic": self.subscribe_topic,
            "unsubscribe-topic": self.unsubscribe_topic,
        }

        self.handlers = {
            "register-client": self.add_client,
            "scatter": self.scatter,
            "register-worker": self.add_worker,
            "register_nanny": self.add_nanny,
            "unregister": self.remove_worker,
            "gather": self.gather,
            "cancel": self.stimulus_cancel,
            "retry": self.stimulus_retry,
            "feed": self.feed,
            "terminate": self.close,
            "broadcast": self.broadcast,
            "proxy": self.proxy,
            "ncores": self.get_ncores,
            "ncores_running": self.get_ncores_running,
            "has_what": self.get_has_what,
            "who_has": self.get_who_has,
            "processing": self.get_processing,
            "call_stack": self.get_call_stack,
            "profile": self.get_profile,
            "performance_report": self.performance_report,
            "get_logs": self.get_logs,
            "logs": self.get_logs,
            "worker_logs": self.get_worker_logs,
            "log_event": self.log_worker_event,
            "events": self.get_events,
            "nbytes": self.get_nbytes,
            "versions": self.versions,
            "add_keys": self.add_keys,
            "rebalance": self.rebalance,
            "replicate": self.replicate,
            "start_ipython": self.start_ipython,
            "run_function": self.run_function,
            "update_data": self.update_data,
            "set_resources": self.add_resources,
            "retire_workers": self.retire_workers,
            "get_metadata": self.get_metadata,
            "set_metadata": self.set_metadata,
            "set_restrictions": self.set_restrictions,
            "heartbeat_worker": self.heartbeat_worker,
            "get_task_status": self.get_task_status,
            "get_task_stream": self.get_task_stream,
            "get_task_prefix_states": self.get_task_prefix_states,
            "register_scheduler_plugin": self.register_scheduler_plugin,
            "register_worker_plugin": self.register_worker_plugin,
            "unregister_worker_plugin": self.unregister_worker_plugin,
            "register_nanny_plugin": self.register_nanny_plugin,
            "unregister_nanny_plugin": self.unregister_nanny_plugin,
            "adaptive_target": self.adaptive_target,
            "workers_to_close": self.workers_to_close,
            "subscribe_worker_status": self.subscribe_worker_status,
            "start_task_metadata": self.start_task_metadata,
            "stop_task_metadata": self.stop_task_metadata,
            "get_cluster_state": self.get_cluster_state,
            "dump_cluster_state_to_url": self.dump_cluster_state_to_url,
            "benchmark_hardware": self.benchmark_hardware,
        }

        connection_limit = get_fileno_limit() / 2

        super().__init__(
            # Arguments to SchedulerState
            aliases=aliases,
            clients=clients,
            workers=workers,
            host_info=host_info,
            resources=resources,
            tasks=tasks,
            unrunnable=unrunnable,
            validate=validate,
            plugins=plugins,
            # Arguments to ServerNode
            handlers=self.handlers,
            stream_handlers=merge(worker_handlers, client_handlers),
            io_loop=self.loop,
            connection_limit=connection_limit,
            deserialize=False,
            connection_args=self.connection_args,
            **kwargs,
        )

        if self.worker_ttl:
            pc = PeriodicCallback(self.check_worker_ttl, self.worker_ttl * 1000)
            self.periodic_callbacks["worker-ttl"] = pc

        if self.idle_timeout:
            pc = PeriodicCallback(self.check_idle, self.idle_timeout * 1000 / 4)
            self.periodic_callbacks["idle-timeout"] = pc

        if extensions is None:
            extensions = DEFAULT_EXTENSIONS.copy()
            if not dask.config.get("distributed.scheduler.work-stealing"):
                if "stealing" in extensions:
                    del extensions["stealing"]

        for name, extension in extensions.items():
            self.extensions[name] = extension(self)

        setproctitle("dask-scheduler [not started]")
        Scheduler._instances.add(self)
        self.rpc.allow_offload = False
        self.status = Status.undefined

    ##################
    # Administration #
    ##################

    def __repr__(self):
        parent: SchedulerState = cast(SchedulerState, self)
        return (
            f"<Scheduler {self.address!r}, "
            f"workers: {len(parent._workers_dv)}, "
            f"cores: {parent._total_nthreads}, "
            f"tasks: {len(parent._tasks)}>"
        )

    def _repr_html_(self):
        parent: SchedulerState = cast(SchedulerState, self)
        return get_template("scheduler.html.j2").render(
            address=self.address,
            workers=parent._workers_dv,
            threads=parent._total_nthreads,
            tasks=parent._tasks,
        )

    def identity(self):
        """Basic information about ourselves and our cluster"""
        parent: SchedulerState = cast(SchedulerState, self)
        d = {
            "type": type(self).__name__,
            "id": str(self.id),
            "address": self.address,
            "services": {key: v.port for (key, v) in self.services.items()},
            "started": self.time_started,
            "workers": {
                worker.address: worker.identity()
                for worker in parent._workers_dv.values()
            },
        }
        return d

    def _to_dict(self, *, exclude: "Container[str]" = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Server.identity
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        """
        info = super()._to_dict(exclude=exclude)
        extra = {
            "transition_log": self.transition_log,
            "log": self.log,
            "tasks": self.tasks,
            "task_groups": self.task_groups,
            # Overwrite dict of WorkerState.identity from info
            "workers": self.workers,
            "clients": self.clients,
            "memory": self.memory,
            "events": self.events,
            "extensions": self.extensions,
        }
        extra = {k: v for k, v in extra.items() if k not in exclude}
        info.update(recursive_to_dict(extra, exclude=exclude))
        return info

    async def get_cluster_state(
        self,
        exclude: "Collection[str]",
    ) -> dict:
        "Produce the state dict used in a cluster state dump"
        # Kick off state-dumping on workers before we block the event loop in `self._to_dict`.
        workers_future = asyncio.gather(
            self.broadcast(
                msg={"op": "dump_state", "exclude": exclude},
                on_error="return",
            ),
            self.broadcast(
                msg={"op": "versions"},
                on_error="ignore",
            ),
        )
        try:
            scheduler_state = self._to_dict(exclude=exclude)

            worker_states, worker_versions = await workers_future
        finally:
            # Ensure the tasks aren't left running if anything fails.
            # Someday (py3.11), use a trio-style TaskGroup for this.
            workers_future.cancel()

        # Convert any RPC errors to strings
        worker_states = {
            k: repr(v) if isinstance(v, Exception) else v
            for k, v in worker_states.items()
        }

        return {
            "scheduler": scheduler_state,
            "workers": worker_states,
            "versions": {"scheduler": self.versions(), "workers": worker_versions},
        }

    async def dump_cluster_state_to_url(
        self,
        url: str,
        exclude: "Collection[str]",
        format: Literal["msgpack", "yaml"],
        **storage_options: Dict[str, Any],
    ) -> None:
        "Write a cluster state dump to an fsspec-compatible URL."
        await cluster_dump.write_state(
            partial(self.get_cluster_state, exclude), url, format, **storage_options
        )

    def get_worker_service_addr(self, worker, service_name, protocol=False):
        """
        Get the (host, port) address of the named service on the *worker*.
        Returns None if the service doesn't exist.

        Parameters
        ----------
        worker : address
        service_name : str
            Common services include 'bokeh' and 'nanny'
        protocol : boolean
            Whether or not to include a full address with protocol (True)
            or just a (host, port) pair
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv[worker]
        port = ws._services.get(service_name)
        if port is None:
            return None
        elif protocol:
            return "%(protocol)s://%(host)s:%(port)d" % {
                "protocol": ws._address.split("://")[0],
                "host": ws.host,
                "port": port,
            }
        else:
            return ws.host, port

    async def start(self):
        """Clear out old state and restart all running coroutines"""
        await super().start()
        assert self.status != Status.running

        enable_gc_diagnosis()

        self.clear_task_state()

        with suppress(AttributeError):
            for c in self._worker_coroutines:
                c.cancel()

        for addr in self._start_address:
            await self.listen(
                addr,
                allow_offload=False,
                handshake_overrides={"pickle-protocol": 4, "compression": None},
                **self.security.get_listen_args("scheduler"),
            )
            self.ip = get_address_host(self.listen_address)
            listen_ip = self.ip

            if listen_ip == "0.0.0.0":
                listen_ip = ""

        if self.address.startswith("inproc://"):
            listen_ip = "localhost"

        # Services listen on all addresses
        self.start_services(listen_ip)

        for listener in self.listeners:
            logger.info("  Scheduler at: %25s", listener.contact_address)
        for k, v in self.services.items():
            logger.info("%11s at: %25s", k, "%s:%d" % (listen_ip, v.port))

        self.loop.add_callback(self.reevaluate_occupancy)

        if self.scheduler_file:
            with open(self.scheduler_file, "w") as f:
                json.dump(self.identity(), f, indent=2)

            fn = self.scheduler_file  # remove file when we close the process

            def del_scheduler_file():
                if os.path.exists(fn):
                    os.remove(fn)

            weakref.finalize(self, del_scheduler_file)

        for preload in self.preloads:
            await preload.start()

        await asyncio.gather(
            *[plugin.start(self) for plugin in list(self.plugins.values())]
        )

        self.start_periodic_callbacks()

        setproctitle(f"dask-scheduler [{self.address}]")
        return self

    async def close(self, fast=False, close_workers=False):
        """Send cleanup signal to all coroutines then wait until finished

        See Also
        --------
        Scheduler.cleanup
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if self.status in (Status.closing, Status.closed):
            await self.finished()
            return

        await asyncio.gather(
            *[plugin.before_close() for plugin in list(self.plugins.values())]
        )

        self.status = Status.closing

        logger.info("Scheduler closing...")
        setproctitle("dask-scheduler [closing]")

        for preload in self.preloads:
            await preload.teardown()

        if close_workers:
            await self.broadcast(msg={"op": "close_gracefully"}, nanny=True)
            for worker in parent._workers_dv:
                # Report would require the worker to unregister with the
                # currently closing scheduler. This is not necessary and might
                # delay shutdown of the worker unnecessarily
                self.worker_send(worker, {"op": "close", "report": False})
            for i in range(20):  # wait a second for send signals to clear
                if parent._workers_dv:
                    await asyncio.sleep(0.05)
                else:
                    break

        await asyncio.gather(
            *[plugin.close() for plugin in list(self.plugins.values())]
        )

        for pc in self.periodic_callbacks.values():
            pc.stop()
        self.periodic_callbacks.clear()

        self.stop_services()

        for ext in parent._extensions.values():
            with suppress(AttributeError):
                ext.teardown()
        logger.info("Scheduler closing all comms")

        futures = []
        for w, comm in list(self.stream_comms.items()):
            if not comm.closed():
                comm.send({"op": "close", "report": False})
                comm.send({"op": "close-stream"})
            with suppress(AttributeError):
                futures.append(comm.close())

        for future in futures:  # TODO: do all at once
            await future

        for comm in self.client_comms.values():
            comm.abort()

        await self.rpc.close()

        self.status = Status.closed
        self.stop()
        await super().close()

        setproctitle("dask-scheduler [closed]")
        disable_gc_diagnosis()

    async def close_worker(self, worker: str, safe: bool = False):
        """Remove a worker from the cluster

        This both removes the worker from our local state and also sends a
        signal to the worker to shut down.  This works regardless of whether or
        not the worker has a nanny process restarting it
        """
        logger.info("Closing worker %s", worker)
        with log_errors():
            self.log_event(worker, {"action": "close-worker"})
            # FIXME: This does not handle nannies
            self.worker_send(worker, {"op": "close", "report": False})
            await self.remove_worker(address=worker, safe=safe)

    ###########
    # Stimuli #
    ###########

    def heartbeat_worker(
        self,
        comm=None,
        *,
        address,
        resolve_address: bool = True,
        now: float = None,
        resources: dict = None,
        host_info: dict = None,
        metrics: dict,
        executing: dict = None,
        extensions: dict = None,
    ):
        parent: SchedulerState = cast(SchedulerState, self)
        address = self.coerce_address(address, resolve_address)
        address = normalize_address(address)
        ws: WorkerState = parent._workers_dv.get(address)  # type: ignore
        if ws is None:
            return {"status": "missing"}

        host = get_address_host(address)
        local_now = time()
        host_info = host_info or {}

        dh: dict = parent._host_info.setdefault(host, {})
        dh["last-seen"] = local_now

        frac = 1 / len(parent._workers_dv)
        parent._bandwidth = (
            parent._bandwidth * (1 - frac) + metrics["bandwidth"]["total"] * frac
        )
        for other, (bw, count) in metrics["bandwidth"]["workers"].items():
            if (address, other) not in self.bandwidth_workers:
                self.bandwidth_workers[address, other] = bw / count
            else:
                alpha = (1 - frac) ** count
                self.bandwidth_workers[address, other] = self.bandwidth_workers[
                    address, other
                ] * alpha + bw * (1 - alpha)
        for typ, (bw, count) in metrics["bandwidth"]["types"].items():
            if typ not in self.bandwidth_types:
                self.bandwidth_types[typ] = bw / count
            else:
                alpha = (1 - frac) ** count
                self.bandwidth_types[typ] = self.bandwidth_types[typ] * alpha + bw * (
                    1 - alpha
                )

        ws._last_seen = local_now
        if executing is not None:
            ws._executing = {
                parent._tasks[key]: duration
                for key, duration in executing.items()
                if key in parent._tasks
            }

        ws._metrics = metrics

        # Calculate RSS - dask keys, separating "old" and "new" usage
        # See MemoryState for details
        max_memory_unmanaged_old_hist_age = local_now - parent.MEMORY_RECENT_TO_OLD_TIME
        memory_unmanaged_old = ws._memory_unmanaged_old
        while ws._memory_other_history:
            timestamp, size = ws._memory_other_history[0]
            if timestamp >= max_memory_unmanaged_old_hist_age:
                break
            ws._memory_other_history.popleft()
            if size == memory_unmanaged_old:
                memory_unmanaged_old = 0  # recalculate min()

        # metrics["memory"] is None if the worker sent a heartbeat before its
        # SystemMonitor ever had a chance to run.
        # ws._nbytes is updated at a different time and sizeof() may not be accurate,
        # so size may be (temporarily) negative; floor it to zero.
        size = max(
            0,
            (metrics["memory"] or 0) - ws._nbytes + metrics["spilled_nbytes"]["memory"],
        )

        ws._memory_other_history.append((local_now, size))
        if not memory_unmanaged_old:
            # The worker has just been started or the previous minimum has been expunged
            # because too old.
            # Note: this algorithm is capped to 200 * MEMORY_RECENT_TO_OLD_TIME elements
            # cluster-wide by heartbeat_interval(), regardless of the number of workers
            ws._memory_unmanaged_old = min(map(second, ws._memory_other_history))
        elif size < memory_unmanaged_old:
            ws._memory_unmanaged_old = size

        if host_info:
            dh = parent._host_info.setdefault(host, {})
            dh.update(host_info)

        if now:
            ws._time_delay = local_now - now

        if resources:
            self.add_resources(worker=address, resources=resources)

        if extensions:
            for name, data in extensions.items():
                self.extensions[name].heartbeat(ws, data)

        return {
            "status": "OK",
            "time": local_now,
            "heartbeat-interval": heartbeat_interval(len(parent._workers_dv)),
        }

    async def add_worker(
        self,
        comm=None,
        *,
        address: str,
        status: str,
        keys=(),
        nthreads=None,
        name=None,
        resolve_address=True,
        nbytes=None,
        types=None,
        now=None,
        resources=None,
        host_info=None,
        memory_limit=None,
        metrics=None,
        pid=0,
        services=None,
        local_directory=None,
        versions=None,
        nanny=None,
        extra=None,
    ):
        """Add a new worker to the cluster"""
        parent: SchedulerState = cast(SchedulerState, self)
        with log_errors():
            address = self.coerce_address(address, resolve_address)
            address = normalize_address(address)
            host = get_address_host(address)

            if address in parent._workers_dv:
                raise ValueError("Worker already exists %s" % address)

            if name in parent._aliases:
                logger.warning(
                    "Worker tried to connect with a duplicate name: %s", name
                )
                msg = {
                    "status": "error",
                    "message": "name taken, %s" % name,
                    "time": time(),
                }
                if comm:
                    await comm.write(msg)
                return

            self.log_event(address, {"action": "add-worker"})
            self.log_event("all", {"action": "add-worker", "worker": address})

            ws: WorkerState
            parent._workers[address] = ws = WorkerState(
                address=address,
                status=Status.lookup[status],  # type: ignore
                pid=pid,
                nthreads=nthreads,
                memory_limit=memory_limit or 0,
                name=name,
                local_directory=local_directory,
                services=services,
                versions=versions,
                nanny=nanny,
                extra=extra,
            )
            if ws._status == Status.running:
                parent._running.add(ws)

            dh: dict = parent._host_info.get(host)  # type: ignore
            if dh is None:
                parent._host_info[host] = dh = {}

            dh_addresses: set = dh.get("addresses")  # type: ignore
            if dh_addresses is None:
                dh["addresses"] = dh_addresses = set()
                dh["nthreads"] = 0

            dh_addresses.add(address)
            dh["nthreads"] += nthreads

            parent._total_nthreads += nthreads
            parent._aliases[name] = address

            self.heartbeat_worker(
                address=address,
                resolve_address=resolve_address,
                now=now,
                resources=resources,
                host_info=host_info,
                metrics=metrics,
            )

            # Do not need to adjust parent._total_occupancy as self.occupancy[ws] cannot
            # exist before this.
            self.check_idle_saturated(ws)

            # for key in keys:  # TODO
            #     self.mark_key_in_memory(key, [address])

            self.stream_comms[address] = BatchedSend(interval="5ms", loop=self.loop)

            if ws._nthreads > len(ws._processing):
                parent._idle[ws._address] = ws

            for plugin in list(self.plugins.values()):
                try:
                    result = plugin.add_worker(scheduler=self, worker=address)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    logger.exception(e)

            recommendations: dict = {}
            client_msgs: dict = {}
            worker_msgs: dict = {}
            if nbytes:
                assert isinstance(nbytes, dict)
                already_released_keys = []
                for key in nbytes:
                    ts: TaskState = parent._tasks.get(key)  # type: ignore
                    if ts is not None and ts.state != "released":
                        if ts.state == "memory":
                            self.add_keys(worker=address, keys=[key])
                        else:
                            t: tuple = parent._transition(
                                key,
                                "memory",
                                worker=address,
                                nbytes=nbytes[key],
                                typename=types[key],
                            )
                            recommendations, client_msgs, worker_msgs = t
                            parent._transitions(
                                recommendations, client_msgs, worker_msgs
                            )
                            recommendations = {}
                    else:
                        already_released_keys.append(key)
                if already_released_keys:
                    if address not in worker_msgs:
                        worker_msgs[address] = []
                    worker_msgs[address].append(
                        {
                            "op": "remove-replicas",
                            "keys": already_released_keys,
                            "stimulus_id": f"reconnect-already-released-{time()}",
                        }
                    )

            if ws._status == Status.running:
                for ts in parent._unrunnable:
                    valid: set = self.valid_workers(ts)
                    if valid is None or ws in valid:
                        recommendations[ts._key] = "waiting"

            if recommendations:
                parent._transitions(recommendations, client_msgs, worker_msgs)

            self.send_all(client_msgs, worker_msgs)

            logger.info("Register worker %s", ws)

            msg = {
                "status": "OK",
                "time": time(),
                "heartbeat-interval": heartbeat_interval(len(parent._workers_dv)),
                "worker-plugins": self.worker_plugins,
            }

            cs: ClientState
            version_warning = version_module.error_message(
                version_module.get_versions(),
                merge(
                    {w: ws._versions for w, ws in parent._workers_dv.items()},
                    {
                        c: cs._versions
                        for c, cs in parent._clients.items()
                        if cs._versions
                    },
                ),
                versions,
                client_name="This Worker",
            )
            msg.update(version_warning)

            if comm:
                await comm.write(msg)

            await self.handle_worker(comm=comm, worker=address)

    async def add_nanny(self, comm):
        msg = {
            "status": "OK",
            "nanny-plugins": self.nanny_plugins,
        }
        return msg

    def update_graph_hlg(
        self,
        client=None,
        hlg=None,
        keys=None,
        dependencies=None,
        restrictions=None,
        priority=None,
        loose_restrictions=None,
        resources=None,
        submitting_task=None,
        retries=None,
        user_priority=0,
        actors=None,
        fifo_timeout=0,
        code=None,
    ):
        unpacked_graph = HighLevelGraph.__dask_distributed_unpack__(hlg)
        dsk = unpacked_graph["dsk"]
        dependencies = unpacked_graph["deps"]
        annotations = unpacked_graph["annotations"]

        # Remove any self-dependencies (happens on test_publish_bag() and others)
        for k, v in dependencies.items():
            deps = set(v)
            if k in deps:
                deps.remove(k)
            dependencies[k] = deps

        if priority is None:
            # Removing all non-local keys before calling order()
            dsk_keys = set(dsk)  # intersection() of sets is much faster than dict_keys
            stripped_deps = {
                k: v.intersection(dsk_keys)
                for k, v in dependencies.items()
                if k in dsk_keys
            }
            priority = dask.order.order(dsk, dependencies=stripped_deps)

        return self.update_graph(
            client,
            dsk,
            keys,
            dependencies,
            restrictions,
            priority,
            loose_restrictions,
            resources,
            submitting_task,
            retries,
            user_priority,
            actors,
            fifo_timeout,
            annotations,
            code=code,
        )

    def update_graph(
        self,
        client=None,
        tasks=None,
        keys=None,
        dependencies=None,
        restrictions=None,
        priority=None,
        loose_restrictions=None,
        resources=None,
        submitting_task=None,
        retries=None,
        user_priority=0,
        actors=None,
        fifo_timeout=0,
        annotations=None,
        code=None,
    ):
        """
        Add new computations to the internal dask graph

        This happens whenever the Client calls submit, map, get, or compute.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        start = time()
        fifo_timeout = parse_timedelta(fifo_timeout)
        keys = set(keys)
        if len(tasks) > 1:
            self.log_event(
                ["all", client], {"action": "update_graph", "count": len(tasks)}
            )

        # Remove aliases
        for k in list(tasks):
            if tasks[k] is k:
                del tasks[k]

        dependencies = dependencies or {}

        if parent._total_occupancy > 1e-9 and parent._computations:
            # Still working on something. Assign new tasks to same computation
            computation = cast(Computation, parent._computations[-1])
        else:
            computation = Computation()
            parent._computations.append(computation)

        if code and code not in computation._code:  # add new code blocks
            computation._code.add(code)

        n = 0
        while len(tasks) != n:  # walk through new tasks, cancel any bad deps
            n = len(tasks)
            for k, deps in list(dependencies.items()):
                if any(
                    dep not in parent._tasks and dep not in tasks for dep in deps
                ):  # bad key
                    logger.info("User asked for computation on lost data, %s", k)
                    del tasks[k]
                    del dependencies[k]
                    if k in keys:
                        keys.remove(k)
                    self.report({"op": "cancelled-key", "key": k}, client=client)
                    self.client_releases_keys(keys=[k], client=client)

        # Avoid computation that is already finished
        ts: TaskState
        already_in_memory = set()  # tasks that are already done
        for k, v in dependencies.items():
            if v and k in parent._tasks:
                ts = parent._tasks[k]
                if ts._state in ("memory", "erred"):
                    already_in_memory.add(k)

        dts: TaskState
        if already_in_memory:
            dependents = dask.core.reverse_dict(dependencies)
            stack = list(already_in_memory)
            done = set(already_in_memory)
            while stack:  # remove unnecessary dependencies
                key = stack.pop()
                ts = parent._tasks[key]
                try:
                    deps = dependencies[key]
                except KeyError:
                    deps = self.dependencies[key]
                for dep in deps:
                    if dep in dependents:
                        child_deps = dependents[dep]
                    else:
                        child_deps = self.dependencies[dep]
                    if all(d in done for d in child_deps):
                        if dep in parent._tasks and dep not in done:
                            done.add(dep)
                            stack.append(dep)

            for d in done:
                tasks.pop(d, None)
                dependencies.pop(d, None)

        # Get or create task states
        stack = list(keys)
        touched_keys = set()
        touched_tasks = []
        while stack:
            k = stack.pop()
            if k in touched_keys:
                continue
            # XXX Have a method get_task_state(self, k) ?
            ts = parent._tasks.get(k)
            if ts is None:
                ts = parent.new_task(
                    k, tasks.get(k), "released", computation=computation
                )
            elif not ts._run_spec:
                ts._run_spec = tasks.get(k)

            touched_keys.add(k)
            touched_tasks.append(ts)
            stack.extend(dependencies.get(k, ()))

        self.client_desires_keys(keys=keys, client=client)

        # Add dependencies
        for key, deps in dependencies.items():
            ts = parent._tasks.get(key)
            if ts is None or ts._dependencies:
                continue
            for dep in deps:
                dts = parent._tasks[dep]
                ts.add_dependency(dts)

        # Compute priorities
        if isinstance(user_priority, Number):
            user_priority = {k: user_priority for k in tasks}

        annotations = annotations or {}
        restrictions = restrictions or {}
        loose_restrictions = loose_restrictions or []
        resources = resources or {}
        retries = retries or {}

        # Override existing taxonomy with per task annotations
        if annotations:
            if "priority" in annotations:
                user_priority.update(annotations["priority"])

            if "workers" in annotations:
                restrictions.update(annotations["workers"])

            if "allow_other_workers" in annotations:
                loose_restrictions.extend(
                    k for k, v in annotations["allow_other_workers"].items() if v
                )

            if "retries" in annotations:
                retries.update(annotations["retries"])

            if "resources" in annotations:
                resources.update(annotations["resources"])

            for a, kv in annotations.items():
                for k, v in kv.items():
                    # Tasks might have been culled, in which case
                    # we have nothing to annotate.
                    ts = parent._tasks.get(k)
                    if ts is not None:
                        ts._annotations[a] = v

        # Add actors
        if actors is True:
            actors = list(keys)
        for actor in actors or []:
            ts = parent._tasks[actor]
            ts._actor = True

        priority = priority or dask.order.order(
            tasks
        )  # TODO: define order wrt old graph

        if submitting_task:  # sub-tasks get better priority than parent tasks
            ts = parent._tasks.get(submitting_task)
            if ts is not None:
                generation = ts._priority[0] - 0.01
            else:  # super-task already cleaned up
                generation = self.generation
        elif self._last_time + fifo_timeout < start:
            self.generation += 1  # older graph generations take precedence
            generation = self.generation
            self._last_time = start
        else:
            generation = self.generation

        for key in set(priority) & touched_keys:
            ts = parent._tasks[key]
            if ts._priority is None:
                ts._priority = (-(user_priority.get(key, 0)), generation, priority[key])

        # Ensure all runnables have a priority
        runnables = [ts for ts in touched_tasks if ts._run_spec]
        for ts in runnables:
            if ts._priority is None and ts._run_spec:
                ts._priority = (self.generation, 0)

        if restrictions:
            # *restrictions* is a dict keying task ids to lists of
            # restriction specifications (either worker names or addresses)
            for k, v in restrictions.items():
                if v is None:
                    continue
                ts = parent._tasks.get(k)
                if ts is None:
                    continue
                ts._host_restrictions = set()
                ts._worker_restrictions = set()
                # Make sure `v` is a collection and not a single worker name / address
                if not isinstance(v, (list, tuple, set)):
                    v = [v]
                for w in v:
                    try:
                        w = self.coerce_address(w)
                    except ValueError:
                        # Not a valid address, but perhaps it's a hostname
                        ts._host_restrictions.add(w)
                    else:
                        ts._worker_restrictions.add(w)

            if loose_restrictions:
                for k in loose_restrictions:
                    ts = parent._tasks[k]
                    ts._loose_restrictions = True

        if resources:
            for k, v in resources.items():
                if v is None:
                    continue
                assert isinstance(v, dict)
                ts = parent._tasks.get(k)
                if ts is None:
                    continue
                ts._resource_restrictions = v

        if retries:
            for k, v in retries.items():
                assert isinstance(v, int)
                ts = parent._tasks.get(k)
                if ts is None:
                    continue
                ts._retries = v

        # Compute recommendations
        recommendations: dict = {}

        for ts in sorted(runnables, key=operator.attrgetter("priority"), reverse=True):
            if ts._state == "released" and ts._run_spec:
                recommendations[ts._key] = "waiting"

        for ts in touched_tasks:
            for dts in ts._dependencies:
                if dts._exception_blame:
                    ts._exception_blame = dts._exception_blame
                    recommendations[ts._key] = "erred"
                    break

        for plugin in list(self.plugins.values()):
            try:
                plugin.update_graph(
                    self,
                    client=client,
                    tasks=tasks,
                    keys=keys,
                    restrictions=restrictions or {},
                    dependencies=dependencies,
                    priority=priority,
                    loose_restrictions=loose_restrictions,
                    resources=resources,
                    annotations=annotations,
                )
            except Exception as e:
                logger.exception(e)

        self.transitions(recommendations)

        for ts in touched_tasks:
            if ts._state in ("memory", "erred"):
                self.report_on_key(ts=ts, client=client)

        end = time()
        if self.digests is not None:
            self.digests["update-graph-duration"].add(end - start)

        # TODO: balance workers

    def stimulus_task_finished(self, key=None, worker=None, **kwargs):
        """Mark that a task has finished execution on a particular worker"""
        parent: SchedulerState = cast(SchedulerState, self)
        logger.debug("Stimulus task finished %s, %s", key, worker)

        recommendations: dict = {}
        client_msgs: dict = {}
        worker_msgs: dict = {}

        ws: WorkerState = parent._workers_dv[worker]
        ts: TaskState = parent._tasks.get(key)
        if ts is None or ts._state == "released":
            logger.debug(
                "Received already computed task, worker: %s, state: %s"
                ", key: %s, who_has: %s",
                worker,
                ts._state if ts else "forgotten",
                key,
                ts._who_has if ts else {},
            )
            worker_msgs[worker] = [
                {
                    "op": "free-keys",
                    "keys": [key],
                    "stimulus_id": f"already-released-or-forgotten-{time()}",
                }
            ]
        elif ts._state == "memory":
            self.add_keys(worker=worker, keys=[key])
        else:
            ts._metadata.update(kwargs["metadata"])
            r: tuple = parent._transition(key, "memory", worker=worker, **kwargs)
            recommendations, client_msgs, worker_msgs = r

            if ts._state == "memory":
                assert ws in ts._who_has
        return recommendations, client_msgs, worker_msgs

    def stimulus_task_erred(
        self, key=None, worker=None, exception=None, traceback=None, **kwargs
    ):
        """Mark that a task has erred on a particular worker"""
        parent: SchedulerState = cast(SchedulerState, self)
        logger.debug("Stimulus task erred %s, %s", key, worker)

        ts: TaskState = parent._tasks.get(key)
        if ts is None or ts._state != "processing":
            return {}, {}, {}

        if ts._retries > 0:
            ts._retries -= 1
            return parent._transition(key, "waiting")
        else:
            return parent._transition(
                key,
                "erred",
                cause=key,
                exception=exception,
                traceback=traceback,
                worker=worker,
                **kwargs,
            )

    def stimulus_retry(self, keys, client=None):
        parent: SchedulerState = cast(SchedulerState, self)
        logger.info("Client %s requests to retry %d keys", client, len(keys))
        if client:
            self.log_event(client, {"action": "retry", "count": len(keys)})

        stack = list(keys)
        seen = set()
        roots = []
        ts: TaskState
        dts: TaskState
        while stack:
            key = stack.pop()
            seen.add(key)
            ts = parent._tasks[key]
            erred_deps = [dts._key for dts in ts._dependencies if dts._state == "erred"]
            if erred_deps:
                stack.extend(erred_deps)
            else:
                roots.append(key)

        recommendations: dict = {key: "waiting" for key in roots}
        self.transitions(recommendations)

        if parent._validate:
            for key in seen:
                assert not parent._tasks[key].exception_blame

        return tuple(seen)

    async def remove_worker(self, address, safe=False, close=True):
        """
        Remove worker from cluster

        We do this when a worker reports that it plans to leave or when it
        appears to be unresponsive.  This may send its tasks back to a released
        state.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        with log_errors():
            if self.status == Status.closed:
                return

            address = self.coerce_address(address)

            if address not in parent._workers_dv:
                return "already-removed"

            host = get_address_host(address)

            ws: WorkerState = parent._workers_dv[address]

            event_msg = {
                "action": "remove-worker",
                "processing-tasks": dict(ws._processing),
            }
            self.log_event(address, event_msg.copy())
            event_msg["worker"] = address
            self.log_event("all", event_msg)

            logger.info("Remove worker %s", ws)
            if close:
                with suppress(AttributeError, CommClosedError):
                    self.stream_comms[address].send({"op": "close", "report": False})

            self.remove_resources(address)

            dh: dict = parent._host_info[host]
            dh_addresses: set = dh["addresses"]
            dh_addresses.remove(address)
            dh["nthreads"] -= ws._nthreads
            parent._total_nthreads -= ws._nthreads
            if not dh_addresses:
                del parent._host_info[host]

            self.rpc.remove(address)
            del self.stream_comms[address]
            del parent._aliases[ws._name]
            parent._idle.pop(ws._address, None)
            parent._saturated.discard(ws)
            del parent._workers[address]
            ws.status = Status.closed
            parent._running.discard(ws)
            parent._total_occupancy -= ws._occupancy

            recommendations: dict = {}

            ts: TaskState
            for ts in list(ws._processing):
                k = ts._key
                recommendations[k] = "released"
                if not safe:
                    ts._suspicious += 1
                    ts._prefix._suspicious += 1
                    if ts._suspicious > self.allowed_failures:
                        del recommendations[k]
                        e = pickle.dumps(
                            KilledWorker(task=k, last_worker=ws.clean()), protocol=4
                        )
                        r = self.transition(k, "erred", exception=e, cause=k)
                        recommendations.update(r)
                        logger.info(
                            "Task %s marked as failed because %d workers died"
                            " while trying to run it",
                            ts._key,
                            self.allowed_failures,
                        )

            for ts in list(ws._has_what):
                parent.remove_replica(ts, ws)
                if not ts._who_has:
                    if ts._run_spec:
                        recommendations[ts._key] = "released"
                    else:  # pure data
                        recommendations[ts._key] = "forgotten"

            self.transitions(recommendations)

            for plugin in list(self.plugins.values()):
                try:
                    result = plugin.remove_worker(scheduler=self, worker=address)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    logger.exception(e)

            if not parent._workers_dv:
                logger.info("Lost all workers")

            for w in parent._workers_dv:
                self.bandwidth_workers.pop((address, w), None)
                self.bandwidth_workers.pop((w, address), None)

            def remove_worker_from_events():
                # If the worker isn't registered anymore after the delay, remove from events
                if address not in parent._workers_dv and address in self.events:
                    del self.events[address]

            cleanup_delay = parse_timedelta(
                dask.config.get("distributed.scheduler.events-cleanup-delay")
            )
            self.loop.call_later(cleanup_delay, remove_worker_from_events)
            logger.debug("Removed worker %s", ws)

        return "OK"

    def stimulus_cancel(self, comm, keys=None, client=None, force=False):
        """Stop execution on a list of keys"""
        logger.info("Client %s requests to cancel %d keys", client, len(keys))
        if client:
            self.log_event(
                client, {"action": "cancel", "count": len(keys), "force": force}
            )
        for key in keys:
            self.cancel_key(key, client, force=force)

    def cancel_key(self, key, client, retries=5, force=False):
        """Cancel a particular key and all dependents"""
        # TODO: this should be converted to use the transition mechanism
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks.get(key)
        dts: TaskState
        try:
            cs: ClientState = parent._clients[client]
        except KeyError:
            return
        if ts is None or not ts._who_wants:  # no key yet, lets try again in a moment
            if retries:
                self.loop.call_later(
                    0.2, lambda: self.cancel_key(key, client, retries - 1)
                )
            return
        if force or ts._who_wants == {cs}:  # no one else wants this key
            for dts in list(ts._dependents):
                self.cancel_key(dts._key, client, force=force)
        logger.info("Scheduler cancels key %s.  Force=%s", key, force)
        self.report({"op": "cancelled-key", "key": key})
        clients = list(ts._who_wants) if force else [cs]
        for cs in clients:
            self.client_releases_keys(keys=[key], client=cs._client_key)

    def client_desires_keys(self, keys=None, client=None):
        parent: SchedulerState = cast(SchedulerState, self)
        cs: ClientState = parent._clients.get(client)
        if cs is None:
            # For publish, queues etc.
            parent._clients[client] = cs = ClientState(client)
        ts: TaskState
        for k in keys:
            ts = parent._tasks.get(k)
            if ts is None:
                # For publish, queues etc.
                ts = parent.new_task(k, None, "released")
            ts._who_wants.add(cs)
            cs._wants_what.add(ts)

            if ts._state in ("memory", "erred"):
                self.report_on_key(ts=ts, client=client)

    def client_releases_keys(self, keys=None, client=None):
        """Remove keys from client desired list"""

        parent: SchedulerState = cast(SchedulerState, self)
        if not isinstance(keys, list):
            keys = list(keys)
        cs: ClientState = parent._clients[client]
        recommendations: dict = {}

        _client_releases_keys(parent, keys=keys, cs=cs, recommendations=recommendations)
        self.transitions(recommendations)

    def client_heartbeat(self, client=None):
        """Handle heartbeats from Client"""
        parent: SchedulerState = cast(SchedulerState, self)
        cs: ClientState = parent._clients[client]
        cs._last_seen = time()

    ###################
    # Task Validation #
    ###################

    def validate_released(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        dts: TaskState
        assert ts._state == "released"
        assert not ts._waiters
        assert not ts._waiting_on
        assert not ts._who_has
        assert not ts._processing_on
        assert not any([ts in dts._waiters for dts in ts._dependencies])
        assert ts not in parent._unrunnable

    def validate_waiting(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        dts: TaskState
        assert ts._waiting_on
        assert not ts._who_has
        assert not ts._processing_on
        assert ts not in parent._unrunnable
        for dts in ts._dependencies:
            # We are waiting on a dependency iff it's not stored
            assert bool(dts._who_has) != (dts in ts._waiting_on)
            assert ts in dts._waiters  # XXX even if dts._who_has?

    def validate_processing(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        dts: TaskState
        assert not ts._waiting_on
        ws: WorkerState = ts._processing_on
        assert ws
        assert ts in ws._processing
        assert not ts._who_has
        for dts in ts._dependencies:
            assert dts._who_has
            assert ts in dts._waiters

    def validate_memory(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        dts: TaskState
        assert ts._who_has
        assert bool(ts in parent._replicated_tasks) == (len(ts._who_has) > 1)
        assert not ts._processing_on
        assert not ts._waiting_on
        assert ts not in parent._unrunnable
        for dts in ts._dependents:
            assert (dts in ts._waiters) == (dts._state in ("waiting", "processing"))
            assert ts not in dts._waiting_on

    def validate_no_worker(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        dts: TaskState
        assert ts in parent._unrunnable
        assert not ts._waiting_on
        assert ts in parent._unrunnable
        assert not ts._processing_on
        assert not ts._who_has
        for dts in ts._dependencies:
            assert dts._who_has

    def validate_erred(self, key):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState = parent._tasks[key]
        assert ts._exception_blame
        assert not ts._who_has

    def validate_key(self, key, ts: TaskState = None):
        parent: SchedulerState = cast(SchedulerState, self)
        try:
            if ts is None:
                ts = parent._tasks.get(key)
            if ts is None:
                logger.debug("Key lost: %s", key)
            else:
                ts.validate()
                try:
                    func = getattr(self, "validate_" + ts._state.replace("-", "_"))
                except AttributeError:
                    logger.error(
                        "self.validate_%s not found", ts._state.replace("-", "_")
                    )
                else:
                    func(key)
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def validate_state(self, allow_overlap=False):
        parent: SchedulerState = cast(SchedulerState, self)
        validate_state(parent._tasks, parent._workers, parent._clients)

        if not (set(parent._workers_dv) == set(self.stream_comms)):
            raise ValueError("Workers not the same in all collections")

        ws: WorkerState
        for w, ws in parent._workers_dv.items():
            assert isinstance(w, str), (type(w), w)
            assert isinstance(ws, WorkerState), (type(ws), ws)
            assert ws._address == w
            if not ws._processing:
                assert not ws._occupancy
                assert ws._address in parent._idle_dv
            assert (ws._status == Status.running) == (ws in parent._running)

        for ws in parent._running:
            assert ws._status == Status.running
            assert ws._address in parent._workers_dv

        ts: TaskState
        for k, ts in parent._tasks.items():
            assert isinstance(ts, TaskState), (type(ts), ts)
            assert ts._key == k
            assert bool(ts in parent._replicated_tasks) == (len(ts._who_has) > 1)
            self.validate_key(k, ts)

        for ts in parent._replicated_tasks:
            assert ts._state == "memory"
            assert ts._key in parent._tasks

        c: str
        cs: ClientState
        for c, cs in parent._clients.items():
            # client=None is often used in tests...
            assert c is None or type(c) == str, (type(c), c)
            assert type(cs) == ClientState, (type(cs), cs)
            assert cs._client_key == c

        a = {w: ws._nbytes for w, ws in parent._workers_dv.items()}
        b = {
            w: sum(ts.get_nbytes() for ts in ws._has_what)
            for w, ws in parent._workers_dv.items()
        }
        assert a == b, (a, b)

        actual_total_occupancy = 0
        for worker, ws in parent._workers_dv.items():
            assert abs(sum(ws._processing.values()) - ws._occupancy) < 1e-8
            actual_total_occupancy += ws._occupancy

        assert abs(actual_total_occupancy - parent._total_occupancy) < 1e-8, (
            actual_total_occupancy,
            parent._total_occupancy,
        )

    ###################
    # Manage Messages #
    ###################

    def report(self, msg: dict, ts: TaskState = None, client: str = None):
        """
        Publish updates to all listening Queues and Comms

        If the message contains a key then we only send the message to those
        comms that care about the key.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if ts is None:
            msg_key = msg.get("key")
            if msg_key is not None:
                tasks: dict = parent._tasks
                ts = tasks.get(msg_key)

        cs: ClientState
        client_comms: dict = self.client_comms
        client_keys: list
        if ts is None:
            # Notify all clients
            client_keys = list(client_comms)
        elif client is None:
            # Notify clients interested in key
            client_keys = [cs._client_key for cs in ts._who_wants]
        else:
            # Notify clients interested in key (including `client`)
            client_keys = [
                cs._client_key for cs in ts._who_wants if cs._client_key != client
            ]
            client_keys.append(client)

        k: str
        for k in client_keys:
            c = client_comms.get(k)
            if c is None:
                continue
            try:
                c.send(msg)
                # logger.debug("Scheduler sends message to client %s", msg)
            except CommClosedError:
                if self.status == Status.running:
                    logger.critical(
                        "Closed comm %r while trying to write %s", c, msg, exc_info=True
                    )

    async def add_client(self, comm: Comm, client: str, versions: dict) -> None:
        """Add client to network

        We listen to all future messages from this Comm.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        assert client is not None
        comm.name = "Scheduler->Client"
        logger.info("Receive client connection: %s", client)
        self.log_event(["all", client], {"action": "add-client", "client": client})
        parent._clients[client] = ClientState(client, versions=versions)

        for plugin in list(self.plugins.values()):
            try:
                plugin.add_client(scheduler=self, client=client)
            except Exception as e:
                logger.exception(e)

        try:
            bcomm = BatchedSend(interval="2ms", loop=self.loop)
            bcomm.start(comm)
            self.client_comms[client] = bcomm
            msg = {"op": "stream-start"}
            ws: WorkerState
            version_warning = version_module.error_message(
                version_module.get_versions(),
                {w: ws._versions for w, ws in parent._workers_dv.items()},
                versions,
            )
            msg.update(version_warning)
            bcomm.send(msg)

            try:
                await self.handle_stream(comm=comm, extra={"client": client})
            finally:
                self.remove_client(client=client)
                logger.debug("Finished handling client %s", client)
        finally:
            if not comm.closed():
                self.client_comms[client].send({"op": "stream-closed"})
            try:
                if not sys.is_finalizing():
                    await self.client_comms[client].close()
                    del self.client_comms[client]
                    if self.status == Status.running:
                        logger.info("Close client connection: %s", client)
            except TypeError:  # comm becomes None during GC
                pass

    def remove_client(self, client: str) -> None:
        """Remove client from network"""
        parent: SchedulerState = cast(SchedulerState, self)
        if self.status == Status.running:
            logger.info("Remove client %s", client)
        self.log_event(["all", client], {"action": "remove-client", "client": client})
        try:
            cs: ClientState = parent._clients[client]
        except KeyError:
            # XXX is this a legitimate condition?
            pass
        else:
            ts: TaskState
            self.client_releases_keys(
                keys=[ts._key for ts in cs._wants_what], client=cs._client_key
            )
            del parent._clients[client]

            for plugin in list(self.plugins.values()):
                try:
                    plugin.remove_client(scheduler=self, client=client)
                except Exception as e:
                    logger.exception(e)

        def remove_client_from_events():
            # If the client isn't registered anymore after the delay, remove from events
            if client not in parent._clients and client in self.events:
                del self.events[client]

        cleanup_delay = parse_timedelta(
            dask.config.get("distributed.scheduler.events-cleanup-delay")
        )
        self.loop.call_later(cleanup_delay, remove_client_from_events)

    def send_task_to_worker(self, worker, ts: TaskState, duration: double = -1):
        """Send a single computational task to a worker"""
        parent: SchedulerState = cast(SchedulerState, self)
        try:
            msg: dict = _task_to_msg(parent, ts, duration)
            self.worker_send(worker, msg)
        except Exception as e:
            logger.exception(e)
            if LOG_PDB:
                import pdb

                pdb.set_trace()
            raise

    def handle_uncaught_error(self, **msg):
        logger.exception(clean_exception(**msg)[1])

    def handle_task_finished(self, key=None, worker=None, **msg):
        parent: SchedulerState = cast(SchedulerState, self)
        if worker not in parent._workers_dv:
            return
        validate_key(key)

        recommendations: dict
        client_msgs: dict
        worker_msgs: dict

        r: tuple = self.stimulus_task_finished(key=key, worker=worker, **msg)
        recommendations, client_msgs, worker_msgs = r
        parent._transitions(recommendations, client_msgs, worker_msgs)

        self.send_all(client_msgs, worker_msgs)

    def handle_task_erred(self, key=None, **msg):
        parent: SchedulerState = cast(SchedulerState, self)
        recommendations: dict
        client_msgs: dict
        worker_msgs: dict
        r: tuple = self.stimulus_task_erred(key=key, **msg)
        recommendations, client_msgs, worker_msgs = r
        parent._transitions(recommendations, client_msgs, worker_msgs)

        self.send_all(client_msgs, worker_msgs)

    def handle_missing_data(self, key=None, errant_worker=None, **kwargs):
        """Signal that `errant_worker` does not hold `key`

        This may either indicate that `errant_worker` is dead or that we may be
        working with stale data and need to remove `key` from the workers
        `has_what`.

        If no replica of a task is available anymore, the task is transitioned
        back to released and rescheduled, if possible.

        Parameters
        ----------
        key : str, optional
            Task key that could not be found, by default None
        errant_worker : str, optional
            Address of the worker supposed to hold a replica, by default None
        """
        parent: SchedulerState = cast(SchedulerState, self)
        logger.debug("handle missing data key=%s worker=%s", key, errant_worker)
        self.log_event(errant_worker, {"action": "missing-data", "key": key})
        ts: TaskState = parent._tasks.get(key)
        if ts is None:
            return
        ws: WorkerState = parent._workers_dv.get(errant_worker)

        if ws is not None and ws in ts._who_has:
            parent.remove_replica(ts, ws)
        if ts.state == "memory" and not ts._who_has:
            if ts._run_spec:
                self.transitions({key: "released"})
            else:
                self.transitions({key: "forgotten"})

    def release_worker_data(self, key, worker):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv.get(worker)
        ts: TaskState = parent._tasks.get(key)
        if not ws or not ts:
            return
        recommendations: dict = {}
        if ws in ts._who_has:
            parent.remove_replica(ts, ws)
            if not ts._who_has:
                recommendations[ts._key] = "released"
        if recommendations:
            self.transitions(recommendations)

    def handle_long_running(self, key=None, worker=None, compute_duration=None):
        """A task has seceded from the thread pool

        We stop the task from being stolen in the future, and change task
        duration accounting as if the task has stopped.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if key not in parent._tasks:
            logger.debug("Skipping long_running since key %s was already released", key)
            return
        ts: TaskState = parent._tasks[key]
        steal = parent._extensions.get("stealing")
        if steal is not None:
            steal.remove_key_from_stealable(ts)

        ws: WorkerState = ts._processing_on
        if ws is None:
            logger.debug("Received long-running signal from duplicate task. Ignoring.")
            return

        if compute_duration:
            old_duration: double = ts._prefix._duration_average
            new_duration: double = compute_duration
            avg_duration: double
            if old_duration < 0:
                avg_duration = new_duration
            else:
                avg_duration = 0.5 * old_duration + 0.5 * new_duration

            ts._prefix._duration_average = avg_duration

        occ: double = ws._processing[ts]
        ws._occupancy -= occ
        parent._total_occupancy -= occ
        # Cannot remove from processing since we're using this for things like
        # idleness detection. Idle workers are typically targeted for
        # downscaling but we should not downscale workers with long running
        # tasks
        ws._processing[ts] = 0
        ws._long_running.add(ts)
        self.check_idle_saturated(ws)

    def handle_worker_status_change(self, status: str, worker: str) -> None:
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv.get(worker)  # type: ignore
        if not ws:
            return
        prev_status = ws._status
        ws._status = Status.lookup[status]  # type: ignore
        if ws._status == prev_status:
            return

        self.log_event(
            ws._address,
            {
                "action": "worker-status-change",
                "prev-status": prev_status.name,
                "status": status,
            },
        )

        if ws._status == Status.running:
            parent._running.add(ws)

            recs = {}
            ts: TaskState
            for ts in parent._unrunnable:
                valid: set = self.valid_workers(ts)
                if valid is None or ws in valid:
                    recs[ts._key] = "waiting"
            if recs:
                client_msgs: dict = {}
                worker_msgs: dict = {}
                parent._transitions(recs, client_msgs, worker_msgs)
                self.send_all(client_msgs, worker_msgs)

        else:
            parent._running.discard(ws)

    async def handle_worker(self, comm=None, worker=None):
        """
        Listen to responses from a single worker

        This is the main loop for scheduler-worker interaction

        See Also
        --------
        Scheduler.handle_client: Equivalent coroutine for clients
        """
        comm.name = "Scheduler connection to worker"
        worker_comm = self.stream_comms[worker]
        worker_comm.start(comm)
        logger.info("Starting worker compute stream, %s", worker)
        try:
            await self.handle_stream(comm=comm, extra={"worker": worker})
        finally:
            if worker in self.stream_comms:
                worker_comm.abort()
                await self.remove_worker(address=worker)

    def add_plugin(
        self,
        plugin: SchedulerPlugin,
        *,
        idempotent: bool = False,
        name: "str | None" = None,
        **kwargs,
    ):
        """Add external plugin to scheduler.

        See https://distributed.readthedocs.io/en/latest/plugins.html

        Parameters
        ----------
        plugin : SchedulerPlugin
            SchedulerPlugin instance to add
        idempotent : bool
            If true, the plugin is assumed to already exist and no
            action is taken.
        name : str
            A name for the plugin, if None, the name attribute is
            checked on the Plugin instance and generated if not
            discovered.
        **kwargs
            Deprecated; additional arguments passed to the `plugin` class if it is
            not already an instance
        """
        if isinstance(plugin, type):
            warnings.warn(
                "Adding plugins by class is deprecated and will be disabled in a "
                "future release. Please add plugins by instance instead.",
                category=FutureWarning,
            )
            plugin = plugin(self, **kwargs)  # type: ignore
        elif kwargs:
            raise ValueError("kwargs provided but plugin is already an instance")

        if name is None:
            name = _get_plugin_name(plugin)

        if name in self.plugins:
            if idempotent:
                return
            warnings.warn(
                f"Scheduler already contains a plugin with name {name}; overwriting.",
                category=UserWarning,
            )

        self.plugins[name] = plugin

    def remove_plugin(
        self,
        name: "str | None" = None,
        plugin: "SchedulerPlugin | None" = None,
    ) -> None:
        """Remove external plugin from scheduler

        Parameters
        ----------
        name : str
            Name of the plugin to remove
        plugin : SchedulerPlugin
            Deprecated; use `name` argument instead. Instance of a
            SchedulerPlugin class to remove;
        """
        # TODO: Remove this block of code once removing plugins by value is disabled
        if bool(name) == bool(plugin):
            raise ValueError("Must provide plugin or name (mutually exclusive)")
        if isinstance(name, SchedulerPlugin):
            # Backwards compatibility - the sig used to be (plugin, name)
            plugin = name
            name = None
        if plugin is not None:
            warnings.warn(
                "Removing scheduler plugins by value is deprecated and will be disabled "
                "in a future release. Please remove scheduler plugins by name instead.",
                category=FutureWarning,
            )
            if hasattr(plugin, "name"):
                name = plugin.name  # type: ignore
            else:
                names = [k for k, v in self.plugins.items() if v is plugin]
                if not names:
                    raise ValueError(
                        f"Could not find {plugin} among the current scheduler plugins"
                    )
                if len(names) > 1:
                    raise ValueError(
                        f"Multiple instances of {plugin} were found in the current "
                        "scheduler plugins; we cannot remove this plugin."
                    )
                name = names[0]
        assert name is not None
        # End deprecated code

        try:
            del self.plugins[name]
        except KeyError:
            raise ValueError(
                f"Could not find plugin {name!r} among the current scheduler plugins"
            )

    async def register_scheduler_plugin(self, plugin, name=None, idempotent=None):
        """Register a plugin on the scheduler."""
        if not dask.config.get("distributed.scheduler.pickle"):
            raise ValueError(
                "Cannot register a scheduler plugin as the scheduler "
                "has been explicitly disallowed from deserializing "
                "arbitrary bytestrings using pickle via the "
                "'distributed.scheduler.pickle' configuration setting."
            )
        plugin = loads(plugin)

        if name is None:
            name = _get_plugin_name(plugin)

        if name in self.plugins and idempotent:
            return

        if hasattr(plugin, "start"):
            result = plugin.start(self)
            if inspect.isawaitable(result):
                await result

        self.add_plugin(plugin, name=name, idempotent=idempotent)

    def worker_send(self, worker, msg):
        """Send message to worker

        This also handles connection failures by adding a callback to remove
        the worker on the next cycle.
        """
        stream_comms: dict = self.stream_comms
        try:
            stream_comms[worker].send(msg)
        except (CommClosedError, AttributeError):
            self.loop.add_callback(self.remove_worker, address=worker)

    def client_send(self, client, msg):
        """Send message to client"""
        client_comms: dict = self.client_comms
        c = client_comms.get(client)
        if c is None:
            return
        try:
            c.send(msg)
        except CommClosedError:
            if self.status == Status.running:
                logger.critical(
                    "Closed comm %r while trying to write %s", c, msg, exc_info=True
                )

    def send_all(self, client_msgs: dict, worker_msgs: dict):
        """Send messages to client and workers"""
        client_comms: dict = self.client_comms
        stream_comms: dict = self.stream_comms
        msgs: list

        for client, msgs in client_msgs.items():
            c = client_comms.get(client)
            if c is None:
                continue
            try:
                c.send(*msgs)
            except CommClosedError:
                if self.status == Status.running:
                    logger.critical(
                        "Closed comm %r while trying to write %s",
                        c,
                        msgs,
                        exc_info=True,
                    )

        for worker, msgs in worker_msgs.items():
            try:
                w = stream_comms[worker]
                w.send(*msgs)
            except KeyError:
                # worker already gone
                pass
            except (CommClosedError, AttributeError):
                self.loop.add_callback(self.remove_worker, address=worker)

    ############################
    # Less common interactions #
    ############################

    async def scatter(
        self,
        comm=None,
        data=None,
        workers=None,
        client=None,
        broadcast=False,
        timeout=2,
    ):
        """Send data out to workers

        See also
        --------
        Scheduler.broadcast:
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState

        start = time()
        while True:
            if workers is None:
                wss = parent._running
            else:
                workers = [self.coerce_address(w) for w in workers]
                wss = {parent._workers_dv[w] for w in workers}
                wss = {ws for ws in wss if ws._status == Status.running}

            if wss:
                break
            if time() > start + timeout:
                raise TimeoutError("No valid workers found")
            await asyncio.sleep(0.1)

        nthreads = {ws._address: ws.nthreads for ws in wss}

        assert isinstance(data, dict)

        keys, who_has, nbytes = await scatter_to_workers(
            nthreads, data, rpc=self.rpc, report=False
        )

        self.update_data(who_has=who_has, nbytes=nbytes, client=client)

        if broadcast:
            n = len(nthreads) if broadcast is True else broadcast
            await self.replicate(keys=keys, workers=workers, n=n)

        self.log_event(
            [client, "all"], {"action": "scatter", "client": client, "count": len(data)}
        )
        return keys

    async def gather(self, keys, serializers=None):
        """Collect data from workers to the scheduler"""
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        keys = list(keys)
        who_has = {}
        for key in keys:
            ts: TaskState = parent._tasks.get(key)
            if ts is not None:
                who_has[key] = [ws._address for ws in ts._who_has]
            else:
                who_has[key] = []

        data, missing_keys, missing_workers = await gather_from_workers(
            who_has, rpc=self.rpc, close=False, serializers=serializers
        )
        if not missing_keys:
            result = {"status": "OK", "data": data}
        else:
            missing_states = [
                (parent._tasks[key].state if key in parent._tasks else None)
                for key in missing_keys
            ]
            logger.exception(
                "Couldn't gather keys %s state: %s workers: %s",
                missing_keys,
                missing_states,
                missing_workers,
            )
            result = {"status": "error", "keys": missing_keys}
            with log_errors():
                # Remove suspicious workers from the scheduler but allow them to
                # reconnect.
                await asyncio.gather(
                    *(
                        self.remove_worker(address=worker, close=False)
                        for worker in missing_workers
                    )
                )
                recommendations: dict
                client_msgs: dict = {}
                worker_msgs: dict = {}
                for key, workers in missing_keys.items():
                    # Task may already be gone if it was held by a
                    # `missing_worker`
                    ts: TaskState = parent._tasks.get(key)
                    logger.exception(
                        "Workers don't have promised key: %s, %s",
                        str(workers),
                        str(key),
                    )
                    if not workers or ts is None:
                        continue
                    recommendations: dict = {key: "released"}
                    for worker in workers:
                        ws = parent._workers_dv.get(worker)
                        if ws is not None and ws in ts._who_has:
                            parent.remove_replica(ts, ws)
                            parent._transitions(
                                recommendations, client_msgs, worker_msgs
                            )
                self.send_all(client_msgs, worker_msgs)

        self.log_event("all", {"action": "gather", "count": len(keys)})
        return result

    def clear_task_state(self):
        # XXX what about nested state such as ClientState.wants_what
        # (see also fire-and-forget...)
        logger.info("Clear task state")
        for collection in self._task_state_collections:
            collection.clear()

    async def restart(self, client=None, timeout=30):
        """Restart all workers. Reset local state."""
        parent: SchedulerState = cast(SchedulerState, self)
        with log_errors():

            n_workers = len(parent._workers_dv)

            logger.info("Send lost future signal to clients")
            cs: ClientState
            ts: TaskState
            for cs in parent._clients.values():
                self.client_releases_keys(
                    keys=[ts._key for ts in cs._wants_what], client=cs._client_key
                )

            ws: WorkerState
            nannies = {addr: ws._nanny for addr, ws in parent._workers_dv.items()}

            for addr in list(parent._workers_dv):
                try:
                    # Ask the worker to close if it doesn't have a nanny,
                    # otherwise the nanny will kill it anyway
                    await self.remove_worker(address=addr, close=addr not in nannies)
                except Exception:
                    logger.info(
                        "Exception while restarting.  This is normal", exc_info=True
                    )

            self.clear_task_state()

            for plugin in list(self.plugins.values()):
                try:
                    plugin.restart(self)
                except Exception as e:
                    logger.exception(e)

            logger.debug("Send kill signal to nannies: %s", nannies)

            nannies = [
                rpc(nanny_address, connection_args=self.connection_args)
                for nanny_address in nannies.values()
                if nanny_address is not None
            ]

            resps = All(
                [
                    nanny.restart(
                        close=True, timeout=timeout * 0.8, executor_wait=False
                    )
                    for nanny in nannies
                ]
            )
            try:
                resps = await asyncio.wait_for(resps, timeout)
            except TimeoutError:
                logger.error(
                    "Nannies didn't report back restarted within "
                    "timeout.  Continuuing with restart process"
                )
            else:
                if not all(resp == "OK" for resp in resps):
                    logger.error(
                        "Not all workers responded positively: %s", resps, exc_info=True
                    )
            finally:
                await asyncio.gather(*[nanny.close_rpc() for nanny in nannies])

            self.clear_task_state()

            with suppress(AttributeError):
                for c in self._worker_coroutines:
                    c.cancel()

            self.log_event([client, "all"], {"action": "restart", "client": client})
            start = time()
            while time() < start + 10 and len(parent._workers_dv) < n_workers:
                await asyncio.sleep(0.01)

            self.report({"op": "restart"})

    async def broadcast(
        self,
        comm=None,
        *,
        msg: dict,
        workers: "list[str] | None" = None,
        hosts: "list[str] | None" = None,
        nanny: bool = False,
        serializers=None,
        on_error: "Literal['raise', 'return', 'return_pickle', 'ignore']" = "raise",
    ) -> dict:  # dict[str, Any]
        """Broadcast message to workers, return all results"""
        parent: SchedulerState = cast(SchedulerState, self)
        if workers is True:
            warnings.warn(
                "workers=True is deprecated; pass workers=None or omit instead",
                category=FutureWarning,
            )
            workers = None
        if workers is None:
            if hosts is None:
                workers = list(parent._workers_dv)
            else:
                workers = []
        if hosts is not None:
            for host in hosts:
                dh: dict = parent._host_info.get(host)  # type: ignore
                if dh is not None:
                    workers.extend(dh["addresses"])
        # TODO replace with worker_list

        if nanny:
            addresses = [parent._workers_dv[w].nanny for w in workers]
        else:
            addresses = workers

        ERROR = object()

        async def send_message(addr):
            try:
                comm = await self.rpc.connect(addr)
                comm.name = "Scheduler Broadcast"
                try:
                    resp = await send_recv(
                        comm, close=True, serializers=serializers, **msg
                    )
                finally:
                    self.rpc.reuse(addr, comm)
                return resp
            except Exception as e:
                logger.error(f"broadcast to {addr} failed: {e.__class__.__name__}: {e}")
                if on_error == "raise":
                    raise
                elif on_error == "return":
                    return e
                elif on_error == "return_pickle":
                    return dumps(e, protocol=4)
                elif on_error == "ignore":
                    return ERROR
                else:
                    raise ValueError(
                        "on_error must be 'raise', 'return', 'return_pickle', "
                        f"or 'ignore'; got {on_error!r}"
                    )

        results = await All(
            [send_message(address) for address in addresses if address is not None]
        )

        return {k: v for k, v in zip(workers, results) if v is not ERROR}

    async def proxy(self, comm=None, msg=None, worker=None, serializers=None):
        """Proxy a communication through the scheduler to some other worker"""
        d = await self.broadcast(
            comm=comm, msg=msg, workers=[worker], serializers=serializers
        )
        return d[worker]

    async def gather_on_worker(
        self, worker_address: str, who_has: "dict[str, list[str]]"
    ) -> set:
        """Peer-to-peer copy of keys from multiple workers to a single worker

        Parameters
        ----------
        worker_address: str
            Recipient worker address to copy keys to
        who_has: dict[Hashable, list[str]]
            {key: [sender address, sender address, ...], key: ...}

        Returns
        -------
        returns:
            set of keys that failed to be copied
        """
        try:
            result = await retry_operation(
                self.rpc(addr=worker_address).gather, who_has=who_has
            )
        except OSError as e:
            # This can happen e.g. if the worker is going through controlled shutdown;
            # it doesn't necessarily mean that it went unexpectedly missing
            logger.warning(
                f"Communication with worker {worker_address} failed during "
                f"replication: {e.__class__.__name__}: {e}"
            )
            return set(who_has)

        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv.get(worker_address)  # type: ignore

        if ws is None:
            logger.warning(f"Worker {worker_address} lost during replication")
            return set(who_has)
        elif result["status"] == "OK":
            keys_failed = set()
            keys_ok: Set = who_has.keys()
        elif result["status"] == "partial-fail":
            keys_failed = set(result["keys"])
            keys_ok = who_has.keys() - keys_failed
            logger.warning(
                f"Worker {worker_address} failed to acquire keys: {result['keys']}"
            )
        else:  # pragma: nocover
            raise ValueError(f"Unexpected message from {worker_address}: {result}")

        for key in keys_ok:
            ts: TaskState = parent._tasks.get(key)  # type: ignore
            if ts is None or ts._state != "memory":
                logger.warning(f"Key lost during replication: {key}")
                continue
            if ws not in ts._who_has:
                parent.add_replica(ts, ws)

        return keys_failed

    async def delete_worker_data(
        self, worker_address: str, keys: "Collection[str]"
    ) -> None:
        """Delete data from a worker and update the corresponding worker/task states

        Parameters
        ----------
        worker_address: str
            Worker address to delete keys from
        keys: list[str]
            List of keys to delete on the specified worker
        """
        parent: SchedulerState = cast(SchedulerState, self)

        try:
            await retry_operation(
                self.rpc(addr=worker_address).free_keys,
                keys=list(keys),
                stimulus_id=f"delete-data-{time()}",
            )
        except OSError as e:
            # This can happen e.g. if the worker is going through controlled shutdown;
            # it doesn't necessarily mean that it went unexpectedly missing
            logger.warning(
                f"Communication with worker {worker_address} failed during "
                f"replication: {e.__class__.__name__}: {e}"
            )
            return

        ws: WorkerState = parent._workers_dv.get(worker_address)  # type: ignore
        if ws is None:
            return

        for key in keys:
            ts: TaskState = parent._tasks.get(key)  # type: ignore
            if ts is not None and ws in ts._who_has:
                assert ts._state == "memory"
                parent.remove_replica(ts, ws)
                if not ts._who_has:
                    # Last copy deleted
                    self.transitions({key: "released"})

        self.log_event(ws._address, {"action": "remove-worker-data", "keys": keys})

    async def rebalance(
        self,
        comm=None,
        keys: "Iterable[Hashable]" = None,
        workers: "Iterable[str]" = None,
    ) -> dict:
        """Rebalance keys so that each worker ends up with roughly the same process
        memory (managed+unmanaged).

        .. warning::
           This operation is generally not well tested against normal operation of the
           scheduler. It is not recommended to use it while waiting on computations.

        **Algorithm**

        #. Find the mean occupancy of the cluster, defined as data managed by dask +
           unmanaged process memory that has been there for at least 30 seconds
           (``distributed.worker.memory.recent-to-old-time``).
           This lets us ignore temporary spikes caused by task heap usage.

           Alternatively, you may change how memory is measured both for the individual
           workers as well as to calculate the mean through
           ``distributed.worker.memory.rebalance.measure``. Namely, this can be useful
           to disregard inaccurate OS memory measurements.

        #. Discard workers whose occupancy is within 5% of the mean cluster occupancy
           (``distributed.worker.memory.rebalance.sender-recipient-gap`` / 2).
           This helps avoid data from bouncing around the cluster repeatedly.
        #. Workers above the mean are senders; those below are recipients.
        #. Discard senders whose absolute occupancy is below 30%
           (``distributed.worker.memory.rebalance.sender-min``). In other words, no data
           is moved regardless of imbalancing as long as all workers are below 30%.
        #. Discard recipients whose absolute occupancy is above 60%
           (``distributed.worker.memory.rebalance.recipient-max``).
           Note that this threshold by default is the same as
           ``distributed.worker.memory.target`` to prevent workers from accepting data
           and immediately spilling it out to disk.
        #. Iteratively pick the sender and recipient that are farthest from the mean and
           move the *least recently inserted* key between the two, until either all
           senders or all recipients fall within 5% of the mean.

           A recipient will be skipped if it already has a copy of the data. In other
           words, this method does not degrade replication.
           A key will be skipped if there are no recipients available with enough memory
           to accept the key and that don't already hold a copy.

        The least recently insertd (LRI) policy is a greedy choice with the advantage of
        being O(1), trivial to implement (it relies on python dict insertion-sorting)
        and hopefully good enough in most cases. Discarded alternative policies were:

        - Largest first. O(n*log(n)) save for non-trivial additional data structures and
          risks causing the largest chunks of data to repeatedly move around the
          cluster like pinballs.
        - Least recently used (LRU). This information is currently available on the
          workers only and not trivial to replicate on the scheduler; transmitting it
          over the network would be very expensive. Also, note that dask will go out of
          its way to minimise the amount of time intermediate keys are held in memory,
          so in such a case LRI is a close approximation of LRU.

        Parameters
        ----------
        keys: optional
            allowlist of dask keys that should be considered for moving. All other keys
            will be ignored. Note that this offers no guarantee that a key will actually
            be moved (e.g. because it is unnecessary or because there are no viable
            recipient workers for it).
        workers: optional
            allowlist of workers addresses to be considered as senders or recipients.
            All other workers will be ignored. The mean cluster occupancy will be
            calculated only using the allowed workers.
        """
        parent: SchedulerState = cast(SchedulerState, self)

        with log_errors():
            wss: "Collection[WorkerState]"
            if workers is not None:
                wss = [parent._workers_dv[w] for w in workers]
            else:
                wss = parent._workers_dv.values()
            if not wss:
                return {"status": "OK"}

            if keys is not None:
                if not isinstance(keys, Set):
                    keys = set(keys)  # unless already a set-like
                if not keys:
                    return {"status": "OK"}
                missing_data = [
                    k
                    for k in keys
                    if k not in parent._tasks or not parent._tasks[k].who_has
                ]
                if missing_data:
                    return {"status": "partial-fail", "keys": missing_data}

            msgs = self._rebalance_find_msgs(keys, wss)
            if not msgs:
                return {"status": "OK"}

            async with self._lock:
                result = await self._rebalance_move_data(msgs)
                if result["status"] == "partial-fail" and keys is None:
                    # Only return failed keys if the client explicitly asked for them
                    result = {"status": "OK"}
                return result

    def _rebalance_find_msgs(
        self,
        keys: "Set[Hashable] | None",
        workers: "Iterable[WorkerState]",
    ) -> "list[tuple[WorkerState, WorkerState, TaskState]]":
        """Identify workers that need to lose keys and those that can receive them,
        together with how many bytes each needs to lose/receive. Then, pair a sender
        worker with a recipient worker for each key, until the cluster is rebalanced.

        This method only defines the work to be performed; it does not start any network
        transfers itself.

        The big-O complexity is O(wt + ke*log(we)), where

        - wt is the total number of workers on the cluster (or the number of allowed
          workers, if explicitly stated by the user)
        - we is the number of workers that are eligible to be senders or recipients
        - kt is the total number of keys on the cluster (or on the allowed workers)
        - ke is the number of keys that need to be moved in order to achieve a balanced
          cluster

        There is a degenerate edge case O(wt + kt*log(we)) when kt is much greater than
        the number of allowed keys, or when most keys are replicated or cannot be
        moved for some other reason.

        Returns list of tuples to feed into _rebalance_move_data:

        - sender worker
        - recipient worker
        - task to be transferred
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState
        ws: WorkerState

        # Heaps of workers, managed by the heapq module, that need to send/receive data,
        # with how many bytes each needs to send/receive.
        #
        # Each element of the heap is a tuple constructed as follows:
        # - snd_bytes_max/rec_bytes_max: maximum number of bytes to send or receive.
        #   This number is negative, so that the workers farthest from the cluster mean
        #   are at the top of the smallest-first heaps.
        # - snd_bytes_min/rec_bytes_min: minimum number of bytes after sending/receiving
        #   which the worker should not be considered anymore. This is also negative.
        # - arbitrary unique number, there just to to make sure that WorkerState objects
        #   are never used for sorting in the unlikely event that two processes have
        #   exactly the same number of bytes allocated.
        # - WorkerState
        # - iterator of all tasks in memory on the worker (senders only), insertion
        #   sorted (least recently inserted first).
        #   Note that this iterator will typically *not* be exhausted. It will only be
        #   exhausted if, after moving away from the worker all keys that can be moved,
        #   is insufficient to drop snd_bytes_min above 0.
        senders: "list[tuple[int, int, int, WorkerState, Iterator[TaskState]]]" = []
        recipients: "list[tuple[int, int, int, WorkerState]]" = []

        # Output: [(sender, recipient, task), ...]
        msgs: "list[tuple[WorkerState, WorkerState, TaskState]]" = []

        # By default, this is the optimistic memory, meaning total process memory minus
        # unmanaged memory that appeared over the last 30 seconds
        # (distributed.worker.memory.recent-to-old-time).
        # This lets us ignore temporary spikes caused by task heap usage.
        memory_by_worker = [
            (ws, getattr(ws.memory, parent.MEMORY_REBALANCE_MEASURE)) for ws in workers
        ]
        mean_memory = sum(m for _, m in memory_by_worker) // len(memory_by_worker)

        for ws, ws_memory in memory_by_worker:
            if ws.memory_limit:
                half_gap = int(parent.MEMORY_REBALANCE_HALF_GAP * ws.memory_limit)
                sender_min = parent.MEMORY_REBALANCE_SENDER_MIN * ws.memory_limit
                recipient_max = parent.MEMORY_REBALANCE_RECIPIENT_MAX * ws.memory_limit
            else:
                half_gap = 0
                sender_min = 0.0
                recipient_max = math.inf

            if (
                ws._has_what
                and ws_memory >= mean_memory + half_gap
                and ws_memory >= sender_min
            ):
                # This may send the worker below sender_min (by design)
                snd_bytes_max = mean_memory - ws_memory  # negative
                snd_bytes_min = snd_bytes_max + half_gap  # negative
                # See definition of senders above
                senders.append(
                    (snd_bytes_max, snd_bytes_min, id(ws), ws, iter(ws._has_what))
                )
            elif ws_memory < mean_memory - half_gap and ws_memory < recipient_max:
                # This may send the worker above recipient_max (by design)
                rec_bytes_max = ws_memory - mean_memory  # negative
                rec_bytes_min = rec_bytes_max + half_gap  # negative
                # See definition of recipients above
                recipients.append((rec_bytes_max, rec_bytes_min, id(ws), ws))

        # Fast exit in case no transfers are necessary or possible
        if not senders or not recipients:
            self.log_event(
                "all",
                {
                    "action": "rebalance",
                    "senders": len(senders),
                    "recipients": len(recipients),
                    "moved_keys": 0,
                },
            )
            return []

        heapq.heapify(senders)
        heapq.heapify(recipients)

        snd_ws: WorkerState
        rec_ws: WorkerState

        while senders and recipients:
            snd_bytes_max, snd_bytes_min, _, snd_ws, ts_iter = senders[0]

            # Iterate through tasks in memory, least recently inserted first
            for ts in ts_iter:
                if keys is not None and ts.key not in keys:
                    continue
                nbytes = ts.nbytes
                if nbytes + snd_bytes_max > 0:
                    # Moving this task would cause the sender to go below mean and
                    # potentially risk becoming a recipient, which would cause tasks to
                    # bounce around. Move on to the next task of the same sender.
                    continue

                # Find the recipient, farthest from the mean, which
                # 1. has enough available RAM for this task, and
                # 2. doesn't hold a copy of this task already
                # There may not be any that satisfies these conditions; in this case
                # this task won't be moved.
                skipped_recipients = []
                use_recipient = False
                while recipients and not use_recipient:
                    rec_bytes_max, rec_bytes_min, _, rec_ws = recipients[0]
                    if nbytes + rec_bytes_max > 0:
                        # recipients are sorted by rec_bytes_max.
                        # The next ones will be worse; no reason to continue iterating
                        break
                    use_recipient = ts not in rec_ws._has_what
                    if not use_recipient:
                        skipped_recipients.append(heapq.heappop(recipients))

                for recipient in skipped_recipients:
                    heapq.heappush(recipients, recipient)

                if not use_recipient:
                    # This task has no recipients available. Leave it on the sender and
                    # move on to the next task of the same sender.
                    continue

                # Schedule task for transfer from sender to recipient
                msgs.append((snd_ws, rec_ws, ts))

                # *_bytes_max/min are all negative for heap sorting
                snd_bytes_max += nbytes
                snd_bytes_min += nbytes
                rec_bytes_max += nbytes
                rec_bytes_min += nbytes

                # Stop iterating on the tasks of this sender for now and, if it still
                # has bytes to lose, push it back into the senders heap; it may or may
                # not come back on top again.
                if snd_bytes_min < 0:
                    # See definition of senders above
                    heapq.heapreplace(
                        senders,
                        (snd_bytes_max, snd_bytes_min, id(snd_ws), snd_ws, ts_iter),
                    )
                else:
                    heapq.heappop(senders)

                # If recipient still has bytes to gain, push it back into the recipients
                # heap; it may or may not come back on top again.
                if rec_bytes_min < 0:
                    # See definition of recipients above
                    heapq.heapreplace(
                        recipients,
                        (rec_bytes_max, rec_bytes_min, id(rec_ws), rec_ws),
                    )
                else:
                    heapq.heappop(recipients)

                # Move to next sender with the most data to lose.
                # It may or may not be the same sender again.
                break

            else:  # for ts in ts_iter
                # Exhausted tasks on this sender
                heapq.heappop(senders)

        return msgs

    async def _rebalance_move_data(
        self, msgs: "list[tuple[WorkerState, WorkerState, TaskState]]"
    ) -> dict:
        """Perform the actual transfer of data across the network in rebalance().
        Takes in input the output of _rebalance_find_msgs(), that is a list of tuples:

        - sender worker
        - recipient worker
        - task to be transferred

        FIXME this method is not robust when the cluster is not idle.
        """
        snd_ws: WorkerState
        rec_ws: WorkerState
        ts: TaskState

        to_recipients: "defaultdict[str, defaultdict[str, list[str]]]" = defaultdict(
            lambda: defaultdict(list)
        )
        for snd_ws, rec_ws, ts in msgs:
            to_recipients[rec_ws.address][ts._key].append(snd_ws.address)
        failed_keys_by_recipient = dict(
            zip(
                to_recipients,
                await asyncio.gather(
                    *(
                        # Note: this never raises exceptions
                        self.gather_on_worker(w, who_has)
                        for w, who_has in to_recipients.items()
                    )
                ),
            )
        )

        to_senders = defaultdict(list)
        for snd_ws, rec_ws, ts in msgs:
            if ts._key not in failed_keys_by_recipient[rec_ws.address]:
                to_senders[snd_ws.address].append(ts._key)

        # Note: this never raises exceptions
        await asyncio.gather(
            *(self.delete_worker_data(r, v) for r, v in to_senders.items())
        )

        for r, v in to_recipients.items():
            self.log_event(r, {"action": "rebalance", "who_has": v})
        self.log_event(
            "all",
            {
                "action": "rebalance",
                "senders": valmap(len, to_senders),
                "recipients": valmap(len, to_recipients),
                "moved_keys": len(msgs),
            },
        )

        missing_keys = {k for r in failed_keys_by_recipient.values() for k in r}
        if missing_keys:
            return {"status": "partial-fail", "keys": list(missing_keys)}
        else:
            return {"status": "OK"}

    async def replicate(
        self,
        comm=None,
        keys=None,
        n=None,
        workers=None,
        branching_factor=2,
        delete=True,
        lock=True,
    ):
        """Replicate data throughout cluster

        This performs a tree copy of the data throughout the network
        individually on each piece of data.

        Parameters
        ----------
        keys: Iterable
            list of keys to replicate
        n: int
            Number of replications we expect to see within the cluster
        branching_factor: int, optional
            The number of workers that can copy data in each generation.
            The larger the branching factor, the more data we copy in
            a single step, but the more a given worker risks being
            swamped by data requests.

        See also
        --------
        Scheduler.rebalance
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        wws: WorkerState
        ts: TaskState

        assert branching_factor > 0
        async with self._lock if lock else empty_context:
            if workers is not None:
                workers = {parent._workers_dv[w] for w in self.workers_list(workers)}
                workers = {ws for ws in workers if ws._status == Status.running}
            else:
                workers = parent._running

            if n is None:
                n = len(workers)
            else:
                n = min(n, len(workers))
            if n == 0:
                raise ValueError("Can not use replicate to delete data")

            tasks = {parent._tasks[k] for k in keys}
            missing_data = [ts._key for ts in tasks if not ts._who_has]
            if missing_data:
                return {"status": "partial-fail", "keys": missing_data}

            # Delete extraneous data
            if delete:
                del_worker_tasks = defaultdict(set)
                for ts in tasks:
                    del_candidates = tuple(ts._who_has & workers)
                    if len(del_candidates) > n:
                        for ws in random.sample(
                            del_candidates, len(del_candidates) - n
                        ):
                            del_worker_tasks[ws].add(ts)

                # Note: this never raises exceptions
                await asyncio.gather(
                    *[
                        self.delete_worker_data(ws._address, [t.key for t in tasks])
                        for ws, tasks in del_worker_tasks.items()
                    ]
                )

            # Copy not-yet-filled data
            while tasks:
                gathers = defaultdict(dict)
                for ts in list(tasks):
                    if ts._state == "forgotten":
                        # task is no longer needed by any client or dependant task
                        tasks.remove(ts)
                        continue
                    n_missing = n - len(ts._who_has & workers)
                    if n_missing <= 0:
                        # Already replicated enough
                        tasks.remove(ts)
                        continue

                    count = min(n_missing, branching_factor * len(ts._who_has))
                    assert count > 0

                    for ws in random.sample(tuple(workers - ts._who_has), count):
                        gathers[ws._address][ts._key] = [
                            wws._address for wws in ts._who_has
                        ]

                await asyncio.gather(
                    *(
                        # Note: this never raises exceptions
                        self.gather_on_worker(w, who_has)
                        for w, who_has in gathers.items()
                    )
                )
                for r, v in gathers.items():
                    self.log_event(r, {"action": "replicate-add", "who_has": v})

            self.log_event(
                "all",
                {
                    "action": "replicate",
                    "workers": list(workers),
                    "key-count": len(keys),
                    "branching-factor": branching_factor,
                },
            )

    def workers_to_close(
        self,
        comm=None,
        memory_ratio: "int | float | None" = None,
        n: "int | None" = None,
        key: "Callable[[WorkerState], Hashable] | None" = None,
        minimum: "int | None" = None,
        target: "int | None" = None,
        attribute: str = "address",
    ) -> "list[str]":
        """
        Find workers that we can close with low cost

        This returns a list of workers that are good candidates to retire.
        These workers are not running anything and are storing
        relatively little data relative to their peers.  If all workers are
        idle then we still maintain enough workers to have enough RAM to store
        our data, with a comfortable buffer.

        This is for use with systems like ``distributed.deploy.adaptive``.

        Parameters
        ----------
        memory_ratio : Number
            Amount of extra space we want to have for our stored data.
            Defaults to 2, or that we want to have twice as much memory as we
            currently have data.
        n : int
            Number of workers to close
        minimum : int
            Minimum number of workers to keep around
        key : Callable(WorkerState)
            An optional callable mapping a WorkerState object to a group
            affiliation. Groups will be closed together. This is useful when
            closing workers must be done collectively, such as by hostname.
        target : int
            Target number of workers to have after we close
        attribute : str
            The attribute of the WorkerState object to return, like "address"
            or "name".  Defaults to "address".

        Examples
        --------
        >>> scheduler.workers_to_close()
        ['tcp://192.168.0.1:1234', 'tcp://192.168.0.2:1234']

        Group workers by hostname prior to closing

        >>> scheduler.workers_to_close(key=lambda ws: ws.host)
        ['tcp://192.168.0.1:1234', 'tcp://192.168.0.1:4567']

        Remove two workers

        >>> scheduler.workers_to_close(n=2)

        Keep enough workers to have twice as much memory as we we need.

        >>> scheduler.workers_to_close(memory_ratio=2)

        Returns
        -------
        to_close: list of worker addresses that are OK to close

        See Also
        --------
        Scheduler.retire_workers
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if target is not None and n is None:
            n = len(parent._workers_dv) - target
        if n is not None:
            if n < 0:
                n = 0
            target = len(parent._workers_dv) - n

        if n is None and memory_ratio is None:
            memory_ratio = 2

        ws: WorkerState
        with log_errors():
            if not n and all([ws._processing for ws in parent._workers_dv.values()]):
                return []

            if key is None:
                key = operator.attrgetter("address")
            if isinstance(key, bytes) and dask.config.get(
                "distributed.scheduler.pickle"
            ):
                key = pickle.loads(key)

            groups = groupby(key, parent._workers.values())

            limit_bytes = {
                k: sum([ws._memory_limit for ws in v]) for k, v in groups.items()
            }
            group_bytes = {k: sum([ws._nbytes for ws in v]) for k, v in groups.items()}

            limit = sum(limit_bytes.values())
            total = sum(group_bytes.values())

            def _key(group):
                wws: WorkerState
                is_idle = not any([wws._processing for wws in groups[group]])
                bytes = -group_bytes[group]
                return (is_idle, bytes)

            idle = sorted(groups, key=_key)

            to_close = []
            n_remain = len(parent._workers_dv)

            while idle:
                group = idle.pop()
                if n is None and any([ws._processing for ws in groups[group]]):
                    break

                if minimum and n_remain - len(groups[group]) < minimum:
                    break

                limit -= limit_bytes[group]

                if (
                    n is not None and n_remain - len(groups[group]) >= cast(int, target)
                ) or (memory_ratio is not None and limit >= memory_ratio * total):
                    to_close.append(group)
                    n_remain -= len(groups[group])

                else:
                    break

            result = [getattr(ws, attribute) for g in to_close for ws in groups[g]]
            if result:
                logger.debug("Suggest closing workers: %s", result)

            return result

    async def retire_workers(
        self,
        comm=None,
        *,
        workers: "list[str] | None" = None,
        names: "list | None" = None,
        close_workers: bool = False,
        remove: bool = True,
        **kwargs,
    ) -> dict:
        """Gracefully retire workers from cluster

        Parameters
        ----------
        workers: list[str] (optional)
            List of worker addresses to retire.
        names: list (optional)
            List of worker names to retire.
            Mutually exclusive with ``workers``.
            If neither ``workers`` nor ``names`` are provided, we call
            ``workers_to_close`` which finds a good set.
        close_workers: bool (defaults to False)
            Whether or not to actually close the worker explicitly from here.
            Otherwise we expect some external job scheduler to finish off the
            worker.
        remove: bool (defaults to True)
            Whether or not to remove the worker metadata immediately or else
            wait for the worker to contact us
        **kwargs: dict
            Extra options to pass to workers_to_close to determine which
            workers we should drop

        Returns
        -------
        Dictionary mapping worker ID/address to dictionary of information about
        that worker for each retired worker.

        See Also
        --------
        Scheduler.workers_to_close
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        ts: TaskState
        with log_errors():
            # This lock makes retire_workers, rebalance, and replicate mutually
            # exclusive and will no longer be necessary once rebalance and replicate are
            # migrated to the Active Memory Manager.
            # Note that, incidentally, it also prevents multiple calls to retire_workers
            # from running in parallel - this is unnecessary.
            async with self._lock:
                if names is not None:
                    if workers is not None:
                        raise TypeError("names and workers are mutually exclusive")
                    if names:
                        logger.info("Retire worker names %s", names)
                    # Support cases where names are passed through a CLI and become
                    # strings
                    names_set = {str(name) for name in names}
                    wss = {
                        ws
                        for ws in parent._workers_dv.values()
                        if str(ws._name) in names_set
                    }
                elif workers is not None:
                    wss = {
                        parent._workers_dv[address]
                        for address in workers
                        if address in parent._workers_dv
                    }
                else:
                    wss = {
                        parent._workers_dv[address]
                        for address in self.workers_to_close(**kwargs)
                    }
                if not wss:
                    return {}

                stop_amm = False
                amm: ActiveMemoryManagerExtension = self.extensions["amm"]
                if not amm.running:
                    amm = ActiveMemoryManagerExtension(
                        self, policies=set(), register=False, start=True, interval=2.0
                    )
                    stop_amm = True

                try:
                    coros = []
                    for ws in wss:
                        logger.info("Retiring worker %s", ws._address)

                        policy = RetireWorker(ws._address)
                        amm.add_policy(policy)

                        # Change Worker.status to closing_gracefully. Immediately set
                        # the same on the scheduler to prevent race conditions.
                        prev_status = ws.status
                        ws.status = Status.closing_gracefully
                        self.running.discard(ws)
                        self.stream_comms[ws.address].send(
                            {"op": "worker-status-change", "status": ws.status.name}
                        )

                        coros.append(
                            self._track_retire_worker(
                                ws,
                                policy,
                                prev_status=prev_status,
                                close_workers=close_workers,
                                remove=remove,
                            )
                        )

                    # Give the AMM a kick, in addition to its periodic running. This is
                    # to avoid unnecessarily waiting for a potentially arbitrarily long
                    # time (depending on interval settings)
                    amm.run_once()

                    workers_info = dict(await asyncio.gather(*coros))
                    workers_info.pop(None, None)
                finally:
                    if stop_amm:
                        amm.stop()

            self.log_event("all", {"action": "retire-workers", "workers": workers_info})
            self.log_event(list(workers_info), {"action": "retired"})

            return workers_info

    async def _track_retire_worker(
        self,
        ws: WorkerState,
        policy: RetireWorker,
        prev_status: Status,
        close_workers: bool,
        remove: bool,
    ) -> tuple:  # tuple[str | None, dict]
        parent: SchedulerState = cast(SchedulerState, self)

        while not policy.done():
            if policy.no_recipients:
                # Abort retirement. This time we don't need to worry about race
                # conditions and we can wait for a scheduler->worker->scheduler
                # round-trip.
                self.stream_comms[ws.address].send(
                    {"op": "worker-status-change", "status": prev_status.name}
                )
                return None, {}

            # Sleep 0.01s when there are 4 tasks or less
            # Sleep 0.5s when there are 200 or more
            poll_interval = max(0.01, min(0.5, len(ws.has_what) / 400))
            await asyncio.sleep(poll_interval)

        logger.debug(
            "All unique keys on worker %s have been replicated elsewhere", ws._address
        )

        if close_workers and ws._address in parent._workers_dv:
            await self.close_worker(worker=ws._address, safe=True)
        if remove:
            await self.remove_worker(address=ws._address, safe=True)

        logger.info("Retired worker %s", ws._address)
        return ws._address, ws.identity()

    def add_keys(self, worker=None, keys=(), stimulus_id=None):
        """
        Learn that a worker has certain keys

        This should not be used in practice and is mostly here for legacy
        reasons.  However, it is sent by workers from time to time.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if worker not in parent._workers_dv:
            return "not found"
        ws: WorkerState = parent._workers_dv[worker]
        redundant_replicas = []
        for key in keys:
            ts: TaskState = parent._tasks.get(key)
            if ts is not None and ts._state == "memory":
                if ws not in ts._who_has:
                    parent.add_replica(ts, ws)
            else:
                redundant_replicas.append(key)

        if redundant_replicas:
            if not stimulus_id:
                stimulus_id = f"redundant-replicas-{time()}"
            self.worker_send(
                worker,
                {
                    "op": "remove-replicas",
                    "keys": redundant_replicas,
                    "stimulus_id": stimulus_id,
                },
            )

        return "OK"

    def update_data(
        self,
        *,
        who_has: dict,
        nbytes: dict,
        client=None,
    ):
        """
        Learn that new data has entered the network from an external source

        See Also
        --------
        Scheduler.mark_key_in_memory
        """
        parent: SchedulerState = cast(SchedulerState, self)
        with log_errors():
            who_has = {
                k: [self.coerce_address(vv) for vv in v] for k, v in who_has.items()
            }
            logger.debug("Update data %s", who_has)

            for key, workers in who_has.items():
                ts: TaskState = parent._tasks.get(key)  # type: ignore
                if ts is None:
                    ts = parent.new_task(key, None, "memory")
                ts.state = "memory"
                ts_nbytes = nbytes.get(key, -1)
                if ts_nbytes >= 0:
                    ts.set_nbytes(ts_nbytes)

                for w in workers:
                    ws: WorkerState = parent._workers_dv[w]
                    if ws not in ts._who_has:
                        parent.add_replica(ts, ws)
                self.report(
                    {"op": "key-in-memory", "key": key, "workers": list(workers)}
                )

            if client:
                self.client_desires_keys(keys=list(who_has), client=client)

    def report_on_key(self, key: str = None, ts: TaskState = None, client: str = None):
        parent: SchedulerState = cast(SchedulerState, self)
        if ts is None:
            ts = parent._tasks.get(key)
        elif key is None:
            key = ts._key
        else:
            assert False, (key, ts)
            return

        report_msg: dict
        if ts is None:
            report_msg = {"op": "cancelled-key", "key": key}
        else:
            report_msg = _task_to_report_msg(parent, ts)
        if report_msg is not None:
            self.report(report_msg, ts=ts, client=client)

    async def feed(
        self, comm, function=None, setup=None, teardown=None, interval="1s", **kwargs
    ):
        """
        Provides a data Comm to external requester

        Caution: this runs arbitrary Python code on the scheduler.  This should
        eventually be phased out.  It is mostly used by diagnostics.
        """
        if not dask.config.get("distributed.scheduler.pickle"):
            logger.warn(
                "Tried to call 'feed' route with custom functions, but "
                "pickle is disallowed.  Set the 'distributed.scheduler.pickle'"
                "config value to True to use the 'feed' route (this is mostly "
                "commonly used with progress bars)"
            )
            return

        interval = parse_timedelta(interval)
        with log_errors():
            if function:
                function = pickle.loads(function)
            if setup:
                setup = pickle.loads(setup)
            if teardown:
                teardown = pickle.loads(teardown)
            state = setup(self) if setup else None
            if inspect.isawaitable(state):
                state = await state
            try:
                while self.status == Status.running:
                    if state is None:
                        response = function(self)
                    else:
                        response = function(self, state)
                    await comm.write(response)
                    await asyncio.sleep(interval)
            except OSError:
                pass
            finally:
                if teardown:
                    teardown(self, state)

    def log_worker_event(self, worker=None, topic=None, msg=None):
        self.log_event(topic, msg)

    def subscribe_worker_status(self, comm=None):
        WorkerStatusPlugin(self, comm)
        ident = self.identity()
        for v in ident["workers"].values():
            del v["metrics"]
            del v["last_seen"]
        return ident

    def get_processing(self, workers=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        ts: TaskState
        if workers is not None:
            workers = set(map(self.coerce_address, workers))
            return {
                w: [ts._key for ts in parent._workers_dv[w].processing] for w in workers
            }
        else:
            return {
                w: [ts._key for ts in ws._processing]
                for w, ws in parent._workers_dv.items()
            }

    def get_who_has(self, keys=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        ts: TaskState
        if keys is not None:
            return {
                k: [ws._address for ws in parent._tasks[k].who_has]
                if k in parent._tasks
                else []
                for k in keys
            }
        else:
            return {
                key: [ws._address for ws in ts._who_has]
                for key, ts in parent._tasks.items()
            }

    def get_has_what(self, workers=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        ts: TaskState
        if workers is not None:
            workers = map(self.coerce_address, workers)
            return {
                w: [ts._key for ts in parent._workers_dv[w].has_what]
                if w in parent._workers_dv
                else []
                for w in workers
            }
        else:
            return {
                w: [ts._key for ts in ws.has_what]
                for w, ws in parent._workers_dv.items()
            }

    def get_ncores(self, workers=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        if workers is not None:
            workers = map(self.coerce_address, workers)
            return {
                w: parent._workers_dv[w].nthreads
                for w in workers
                if w in parent._workers_dv
            }
        else:
            return {w: ws._nthreads for w, ws in parent._workers_dv.items()}

    def get_ncores_running(self, workers=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ncores = self.get_ncores(workers=workers)
        return {
            w: n
            for w, n in ncores.items()
            if parent._workers_dv[w].status == Status.running
        }

    async def get_call_stack(self, keys=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState
        dts: TaskState
        if keys is not None:
            stack = list(keys)
            processing = set()
            while stack:
                key = stack.pop()
                ts = parent._tasks[key]
                if ts._state == "waiting":
                    stack.extend([dts._key for dts in ts._dependencies])
                elif ts._state == "processing":
                    processing.add(ts)

            workers = defaultdict(list)
            for ts in processing:
                if ts._processing_on:
                    workers[ts._processing_on.address].append(ts._key)
        else:
            workers = {w: None for w in parent._workers_dv}

        if not workers:
            return {}

        results = await asyncio.gather(
            *(self.rpc(w).call_stack(keys=v) for w, v in workers.items())
        )
        response = {w: r for w, r in zip(workers, results) if r}
        return response

    async def benchmark_hardware(self) -> "dict[str, dict[str, float]]":
        """
        Run a benchmark on the workers for memory, disk, and network bandwidths

        Returns
        -------
        result: dict
            A dictionary mapping the names "disk", "memory", and "network" to
            dictionaries mapping sizes to bandwidths.  These bandwidths are
            averaged over many workers running computations across the cluster.
        """
        out: "dict[str, defaultdict[str, list[float]]]" = {
            name: defaultdict(list) for name in ["disk", "memory", "network"]
        }

        # disk
        result = await self.broadcast(msg={"op": "benchmark_disk"})
        for d in result.values():
            for size, duration in d.items():
                out["disk"][size].append(duration)

        # memory
        result = await self.broadcast(msg={"op": "benchmark_memory"})
        for d in result.values():
            for size, duration in d.items():
                out["memory"][size].append(duration)

        # network
        workers = list(self.workers)
        # On an adaptive cluster, if multiple workers are started on the same physical host,
        # they are more likely to connect to the Scheduler in sequence, ending up next to
        # each other in this list.
        # The transfer speed within such clusters of workers will be effectively that of
        # localhost. This could happen across different VMs and/or docker images, so
        # implementing logic based on IP addresses would not necessarily help.
        # Randomize the connections to even out the mean measures.
        random.shuffle(workers)
        futures = [
            self.rpc(a).benchmark_network(address=b) for a, b in partition(2, workers)
        ]
        responses = await asyncio.gather(*futures)

        for d in responses:
            for size, duration in d.items():
                out["network"][size].append(duration)

        result = {}
        for mode in out:
            result[mode] = {
                size: sum(durations) / len(durations)
                for size, durations in out[mode].items()
            }

        return result

    def get_nbytes(self, keys=None, summary=True):
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState
        with log_errors():
            if keys is not None:
                result = {k: parent._tasks[k].nbytes for k in keys}
            else:
                result = {
                    k: ts._nbytes for k, ts in parent._tasks.items() if ts._nbytes >= 0
                }

            if summary:
                out = defaultdict(lambda: 0)
                for k, v in result.items():
                    out[key_split(k)] += v
                result = dict(out)

            return result

    def run_function(self, comm, function, args=(), kwargs=None, wait=True):
        """Run a function within this process

        See Also
        --------
        Client.run_on_scheduler
        """
        from distributed.worker import run

        if not dask.config.get("distributed.scheduler.pickle"):
            raise ValueError(
                "Cannot run function as the scheduler has been explicitly disallowed from "
                "deserializing arbitrary bytestrings using pickle via the "
                "'distributed.scheduler.pickle' configuration setting."
            )
        kwargs = kwargs or {}
        self.log_event("all", {"action": "run-function", "function": function})
        return run(self, comm, function=function, args=args, kwargs=kwargs, wait=wait)

    def set_metadata(self, keys=None, value=None):
        parent: SchedulerState = cast(SchedulerState, self)
        metadata = parent._task_metadata
        for key in keys[:-1]:
            if key not in metadata or not isinstance(metadata[key], (dict, list)):
                metadata[key] = {}
            metadata = metadata[key]
        metadata[keys[-1]] = value

    def get_metadata(self, keys, default=no_default):
        parent: SchedulerState = cast(SchedulerState, self)
        metadata = parent._task_metadata
        for key in keys[:-1]:
            metadata = metadata[key]
        try:
            return metadata[keys[-1]]
        except KeyError:
            if default != no_default:
                return default
            else:
                raise

    def set_restrictions(self, worker: "dict[str, Collection[str] | str]"):
        ts: TaskState
        for key, restrictions in worker.items():
            ts = self.tasks[key]
            if isinstance(restrictions, str):
                restrictions = {restrictions}
            ts._worker_restrictions = set(restrictions)

    def get_task_prefix_states(self):
        with log_errors():
            state = {}

            for tp in self.task_prefixes.values():
                active_states = tp.active_states
                if any(
                    active_states.get(s)
                    for s in {"memory", "erred", "released", "processing", "waiting"}
                ):
                    state[tp.name] = {
                        "memory": active_states["memory"],
                        "erred": active_states["erred"],
                        "released": active_states["released"],
                        "processing": active_states["processing"],
                        "waiting": active_states["waiting"],
                    }

        return state

    def get_task_status(self, keys=None):
        parent: SchedulerState = cast(SchedulerState, self)
        return {
            key: (parent._tasks[key].state if key in parent._tasks else None)
            for key in keys
        }

    def get_task_stream(self, start=None, stop=None, count=None):
        from distributed.diagnostics.task_stream import TaskStreamPlugin

        if TaskStreamPlugin.name not in self.plugins:
            self.add_plugin(TaskStreamPlugin(self))

        plugin = self.plugins[TaskStreamPlugin.name]

        return plugin.collect(start=start, stop=stop, count=count)

    def start_task_metadata(self, name=None):
        plugin = CollectTaskMetaDataPlugin(scheduler=self, name=name)
        self.add_plugin(plugin)

    def stop_task_metadata(self, name=None):
        plugins = [
            p
            for p in list(self.plugins.values())
            if isinstance(p, CollectTaskMetaDataPlugin) and p.name == name
        ]
        if len(plugins) != 1:
            raise ValueError(
                "Expected to find exactly one CollectTaskMetaDataPlugin "
                f"with name {name} but found {len(plugins)}."
            )

        plugin = plugins[0]
        self.remove_plugin(name=plugin.name)
        return {"metadata": plugin.metadata, "state": plugin.state}

    async def register_worker_plugin(self, comm, plugin, name=None):
        """Registers a worker plugin on all running and future workers"""
        self.worker_plugins[name] = plugin

        responses = await self.broadcast(
            msg=dict(op="plugin-add", plugin=plugin, name=name)
        )
        return responses

    async def unregister_worker_plugin(self, comm, name):
        """Unregisters a worker plugin"""
        try:
            self.worker_plugins.pop(name)
        except KeyError:
            raise ValueError(f"The worker plugin {name} does not exists")

        responses = await self.broadcast(msg=dict(op="plugin-remove", name=name))
        return responses

    async def register_nanny_plugin(self, comm, plugin, name=None):
        """Registers a setup function, and call it on every worker"""
        self.nanny_plugins[name] = plugin

        responses = await self.broadcast(
            msg=dict(op="plugin_add", plugin=plugin, name=name),
            nanny=True,
        )
        return responses

    async def unregister_nanny_plugin(self, comm, name):
        """Unregisters a worker plugin"""
        try:
            self.nanny_plugins.pop(name)
        except KeyError:
            raise ValueError(f"The nanny plugin {name} does not exists")

        responses = await self.broadcast(
            msg=dict(op="plugin_remove", name=name), nanny=True
        )
        return responses

    def transition(self, key, finish: str, *args, **kwargs):
        """Transition a key from its current state to the finish state

        Examples
        --------
        >>> self.transition('x', 'waiting')
        {'x': 'processing'}

        Returns
        -------
        Dictionary of recommendations for future transitions

        See Also
        --------
        Scheduler.transitions: transitive version of this function
        """
        parent: SchedulerState = cast(SchedulerState, self)
        recommendations: dict
        worker_msgs: dict
        client_msgs: dict
        a: tuple = parent._transition(key, finish, *args, **kwargs)
        recommendations, client_msgs, worker_msgs = a
        self.send_all(client_msgs, worker_msgs)
        return recommendations

    def transitions(self, recommendations: dict):
        """Process transitions until none are left

        This includes feedback from previous transitions and continues until we
        reach a steady state
        """
        parent: SchedulerState = cast(SchedulerState, self)
        client_msgs: dict = {}
        worker_msgs: dict = {}
        parent._transitions(recommendations, client_msgs, worker_msgs)
        self.send_all(client_msgs, worker_msgs)

    def story(self, *keys):
        """Get all transitions that touch one of the input keys"""
        keys = {key.key if isinstance(key, TaskState) else key for key in keys}
        return scheduler_story(keys, self.transition_log)

    transition_story = story

    def reschedule(self, key=None, worker=None):
        """Reschedule a task

        Things may have shifted and this task may now be better suited to run
        elsewhere
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ts: TaskState
        try:
            ts = parent._tasks[key]
        except KeyError:
            logger.warning(
                "Attempting to reschedule task {}, which was not "
                "found on the scheduler. Aborting reschedule.".format(key)
            )
            return
        if ts._state != "processing":
            return
        if worker and ts._processing_on.address != worker:
            return
        self.transitions({key: "released"})

    #####################
    # Utility functions #
    #####################

    def add_resources(self, worker: str, resources=None):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv[worker]
        if resources:
            ws._resources.update(resources)
        ws._used_resources = {}
        for resource, quantity in ws._resources.items():
            ws._used_resources[resource] = 0
            dr: dict = parent._resources.get(resource, None)
            if dr is None:
                parent._resources[resource] = dr = {}
            dr[worker] = quantity
        return "OK"

    def remove_resources(self, worker):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv[worker]
        for resource, quantity in ws._resources.items():
            dr: dict = parent._resources.get(resource, None)
            if dr is None:
                parent._resources[resource] = dr = {}
            del dr[worker]

    def coerce_address(self, addr, resolve=True):
        """
        Coerce possible input addresses to canonical form.
        *resolve* can be disabled for testing with fake hostnames.

        Handles strings, tuples, or aliases.
        """
        # XXX how many address-parsing routines do we have?
        parent: SchedulerState = cast(SchedulerState, self)
        if addr in parent._aliases:
            addr = parent._aliases[addr]
        if isinstance(addr, tuple):
            addr = unparse_host_port(*addr)
        if not isinstance(addr, str):
            raise TypeError(f"addresses should be strings or tuples, got {addr!r}")

        if resolve:
            addr = resolve_address(addr)
        else:
            addr = normalize_address(addr)

        return addr

    def workers_list(self, workers):
        """
        List of qualifying workers

        Takes a list of worker addresses or hostnames.
        Returns a list of all worker addresses that match
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if workers is None:
            return list(parent._workers)

        out = set()
        for w in workers:
            if ":" in w:
                out.add(w)
            else:
                out.update({ww for ww in parent._workers if w in ww})  # TODO: quadratic
        return list(out)

    def start_ipython(self):
        """Start an IPython kernel

        Returns Jupyter connection info dictionary.
        """
        from distributed._ipython_utils import start_ipython

        if self._ipython_kernel is None:
            self._ipython_kernel = start_ipython(
                ip=self.ip, ns={"scheduler": self}, log=logger
            )
        return self._ipython_kernel.get_connection_info()

    async def get_profile(
        self,
        comm=None,
        workers=None,
        scheduler=False,
        server=False,
        merge_workers=True,
        start=None,
        stop=None,
        key=None,
    ):
        parent: SchedulerState = cast(SchedulerState, self)
        if workers is None:
            workers = parent._workers_dv
        else:
            workers = set(parent._workers_dv) & set(workers)

        if scheduler:
            return profile.get_profile(self.io_loop.profile, start=start, stop=stop)

        results = await asyncio.gather(
            *(
                self.rpc(w).profile(start=start, stop=stop, key=key, server=server)
                for w in workers
            ),
            return_exceptions=True,
        )

        results = [r for r in results if not isinstance(r, Exception)]

        if merge_workers:
            response = profile.merge(*results)
        else:
            response = dict(zip(workers, results))
        return response

    async def get_profile_metadata(
        self,
        workers: "Iterable[str] | None" = None,
        start: float = 0,
        stop: "float | None" = None,
        profile_cycle_interval: "str | float | None" = None,
    ):
        parent: SchedulerState = cast(SchedulerState, self)
        dt = profile_cycle_interval or dask.config.get(
            "distributed.worker.profile.cycle"
        )
        dt = parse_timedelta(dt, default="ms")

        if workers is None:
            workers = parent._workers_dv
        else:
            workers = set(parent._workers_dv) & set(workers)
        results = await asyncio.gather(
            *(self.rpc(w).profile_metadata(start=start, stop=stop) for w in workers),
            return_exceptions=True,
        )

        results = [r for r in results if not isinstance(r, Exception)]
        counts = [
            (time, sum(pluck(1, group)))
            for time, group in itertools.groupby(
                merge_sorted(
                    *(v["counts"] for v in results),
                ),
                lambda t: t[0] // dt * dt,
            )
        ]

        keys: dict[str, list[list]] = {
            k: [] for v in results for t, d in v["keys"] for k in d
        }

        groups1 = [v["keys"] for v in results]
        groups2 = list(merge_sorted(*groups1, key=first))

        last = 0
        for t, d in groups2:
            tt = t // dt * dt
            if tt > last:
                last = tt
                for k, v in keys.items():
                    v.append([tt, 0])
            for k, v in d.items():
                keys[k][-1][1] += v

        return {"counts": counts, "keys": keys}

    async def performance_report(
        self, start: float, last_count: int, code="", mode=None
    ):
        parent: SchedulerState = cast(SchedulerState, self)
        stop = time()
        # Profiles
        compute, scheduler, workers = await asyncio.gather(
            *[
                self.get_profile(start=start),
                self.get_profile(scheduler=True, start=start),
                self.get_profile(server=True, start=start),
            ]
        )
        from distributed import profile

        def profile_to_figure(state):
            data = profile.plot_data(state)
            figure, source = profile.plot_figure(data, sizing_mode="stretch_both")
            return figure

        compute, scheduler, workers = map(
            profile_to_figure, (compute, scheduler, workers)
        )

        # Task stream
        task_stream = self.get_task_stream(start=start)
        total_tasks = len(task_stream)
        timespent: "defaultdict[str, float]" = defaultdict(float)
        for d in task_stream:
            for x in d["startstops"]:
                timespent[x["action"]] += x["stop"] - x["start"]
        tasks_timings = ""
        for k in sorted(timespent.keys()):
            tasks_timings += f"\n<li> {k} time: {format_time(timespent[k])} </li>"

        from distributed.dashboard.components.scheduler import task_stream_figure
        from distributed.diagnostics.task_stream import rectangles

        rects = rectangles(task_stream)
        source, task_stream = task_stream_figure(sizing_mode="stretch_both")
        source.data.update(rects)

        # Bandwidth
        from distributed.dashboard.components.scheduler import (
            BandwidthTypes,
            BandwidthWorkers,
        )

        bandwidth_workers = BandwidthWorkers(self, sizing_mode="stretch_both")
        bandwidth_workers.update()
        bandwidth_types = BandwidthTypes(self, sizing_mode="stretch_both")
        bandwidth_types.update()

        # System monitor
        from distributed.dashboard.components.shared import SystemMonitor

        sysmon = SystemMonitor(self, last_count=last_count, sizing_mode="stretch_both")
        sysmon.update()

        # Scheduler logs
        from distributed.dashboard.components.scheduler import SchedulerLogs

        logs = SchedulerLogs(self, start=start)

        from bokeh.models import Div, Panel, Tabs

        import distributed

        # HTML
        ws: WorkerState
        html = """
        <h1> Dask Performance Report </h1>

        <i> Select different tabs on the top for additional information </i>

        <h2> Duration: {time} </h2>
        <h2> Tasks Information </h2>
        <ul>
         <li> number of tasks: {ntasks} </li>
         {tasks_timings}
        </ul>

        <h2> Scheduler Information </h2>
        <ul>
          <li> Address: {address} </li>
          <li> Workers: {nworkers} </li>
          <li> Threads: {threads} </li>
          <li> Memory: {memory} </li>
          <li> Dask Version: {dask_version} </li>
          <li> Dask.Distributed Version: {distributed_version} </li>
        </ul>

        <h2> Calling Code </h2>
        <pre>
{code}
        </pre>
        """.format(
            time=format_time(stop - start),
            ntasks=total_tasks,
            tasks_timings=tasks_timings,
            address=self.address,
            nworkers=len(parent._workers_dv),
            threads=sum([ws._nthreads for ws in parent._workers_dv.values()]),
            memory=format_bytes(
                sum([ws._memory_limit for ws in parent._workers_dv.values()])
            ),
            code=code,
            dask_version=dask.__version__,
            distributed_version=distributed.__version__,
        )
        html = Div(
            text=html,
            style={
                "width": "100%",
                "height": "100%",
                "max-width": "1920px",
                "max-height": "1080px",
                "padding": "12px",
                "border": "1px solid lightgray",
                "box-shadow": "inset 1px 0 8px 0 lightgray",
                "overflow": "auto",
            },
        )

        html = Panel(child=html, title="Summary")
        compute = Panel(child=compute, title="Worker Profile (compute)")
        workers = Panel(child=workers, title="Worker Profile (administrative)")
        scheduler = Panel(child=scheduler, title="Scheduler Profile (administrative)")
        task_stream = Panel(child=task_stream, title="Task Stream")
        bandwidth_workers = Panel(
            child=bandwidth_workers.root, title="Bandwidth (Workers)"
        )
        bandwidth_types = Panel(child=bandwidth_types.root, title="Bandwidth (Types)")
        system = Panel(child=sysmon.root, title="System")
        logs = Panel(child=logs.root, title="Scheduler Logs")

        tabs = Tabs(
            tabs=[
                html,
                task_stream,
                system,
                logs,
                compute,
                workers,
                scheduler,
                bandwidth_workers,
                bandwidth_types,
            ]
        )

        from bokeh.core.templates import get_env
        from bokeh.plotting import output_file, save

        with tmpfile(extension=".html") as fn:
            output_file(filename=fn, title="Dask Performance Report", mode=mode)
            template_directory = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "dashboard", "templates"
            )
            template_environment = get_env()
            template_environment.loader.searchpath.append(template_directory)
            template = template_environment.get_template("performance_report.html")
            save(tabs, filename=fn, template=template)

            with open(fn) as f:
                data = f.read()

        return data

    async def get_worker_logs(self, n=None, workers=None, nanny=False):
        results = await self.broadcast(
            msg={"op": "get_logs", "n": n}, workers=workers, nanny=nanny
        )
        return results

    def log_event(self, name, msg):
        event = (time(), msg)
        if isinstance(name, (list, tuple)):
            for n in name:
                self.events[n].append(event)
                self.event_counts[n] += 1
                self._report_event(n, event)
        else:
            self.events[name].append(event)
            self.event_counts[name] += 1
            self._report_event(name, event)

    def _report_event(self, name, event):
        for client in self.event_subscriber[name]:
            self.report(
                {
                    "op": "event",
                    "topic": name,
                    "event": event,
                },
                client=client,
            )

    def subscribe_topic(self, topic, client):
        self.event_subscriber[topic].add(client)

    def unsubscribe_topic(self, topic, client):
        self.event_subscriber[topic].discard(client)

    def get_events(self, topic=None):
        if topic is not None:
            return tuple(self.events[topic])
        else:
            return valmap(tuple, self.events)

    async def get_worker_monitor_info(self, recent=False, starts=None):
        parent: SchedulerState = cast(SchedulerState, self)
        if starts is None:
            starts = {}
        results = await asyncio.gather(
            *(
                self.rpc(w).get_monitor_info(recent=recent, start=starts.get(w, 0))
                for w in parent._workers_dv
            )
        )
        return dict(zip(parent._workers_dv, results))

    ###########
    # Cleanup #
    ###########

    def reevaluate_occupancy(self, worker_index: Py_ssize_t = 0):
        """Periodically reassess task duration time

        The expected duration of a task can change over time.  Unfortunately we
        don't have a good constant-time way to propagate the effects of these
        changes out to the summaries that they affect, like the total expected
        runtime of each of the workers, or what tasks are stealable.

        In this coroutine we walk through all of the workers and re-align their
        estimates with the current state of tasks.  We do this periodically
        rather than at every transition, and we only do it if the scheduler
        process isn't under load (using psutil.Process.cpu_percent()).  This
        lets us avoid this fringe optimization when we have better things to
        think about.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        try:
            if self.status == Status.closed:
                return
            last = time()
            next_time = timedelta(seconds=0.1)

            if self.proc.cpu_percent() < 50:
                workers: list = list(parent._workers.values())
                nworkers: Py_ssize_t = len(workers)
                i: Py_ssize_t
                for i in range(nworkers):
                    ws: WorkerState = workers[worker_index % nworkers]
                    worker_index += 1
                    try:
                        if ws is None or not ws._processing:
                            continue
                        parent._reevaluate_occupancy_worker(ws)
                    finally:
                        del ws  # lose ref

                    duration = time() - last
                    if duration > 0.005:  # 5ms since last release
                        next_time = timedelta(seconds=duration * 5)  # 25ms gap
                        break

            self.loop.add_timeout(
                next_time, self.reevaluate_occupancy, worker_index=worker_index
            )

        except Exception:
            logger.error("Error in reevaluate occupancy", exc_info=True)
            raise

    async def check_worker_ttl(self):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        now = time()
        for ws in parent._workers_dv.values():
            if (ws._last_seen < now - self.worker_ttl) and (
                ws._last_seen < now - 10 * heartbeat_interval(len(parent._workers_dv))
            ):
                logger.warning(
                    "Worker failed to heartbeat within %s seconds. Closing: %s",
                    self.worker_ttl,
                    ws,
                )
                await self.remove_worker(address=ws._address)

    def check_idle(self):
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        if (
            any([ws._processing for ws in parent._workers_dv.values()])
            or parent._unrunnable
        ):
            self.idle_since = None
            return
        elif not self.idle_since:
            self.idle_since = time()

        if time() > self.idle_since + self.idle_timeout:
            logger.info(
                "Scheduler closing after being idle for %s",
                format_time(self.idle_timeout),
            )
            self.loop.add_callback(self.close)

    def adaptive_target(self, target_duration=None):
        """Desired number of workers based on the current workload

        This looks at the current running tasks and memory use, and returns a
        number of desired workers.  This is often used by adaptive scheduling.

        Parameters
        ----------
        target_duration : str
            A desired duration of time for computations to take.  This affects
            how rapidly the scheduler will ask to scale.

        See Also
        --------
        distributed.deploy.Adaptive
        """
        parent: SchedulerState = cast(SchedulerState, self)
        if target_duration is None:
            target_duration = dask.config.get("distributed.adaptive.target-duration")
        target_duration = parse_timedelta(target_duration)

        # CPU
        cpu = math.ceil(
            parent._total_occupancy / target_duration
        )  # TODO: threads per worker

        # Avoid a few long tasks from asking for many cores
        ws: WorkerState
        tasks_processing = 0
        for ws in parent._workers_dv.values():
            tasks_processing += len(ws._processing)

            if tasks_processing > cpu:
                break
        else:
            cpu = min(tasks_processing, cpu)

        if parent._unrunnable and not parent._workers_dv:
            cpu = max(1, cpu)

        # add more workers if more than 60% of memory is used
        limit = sum([ws._memory_limit for ws in parent._workers_dv.values()])
        used = sum([ws._nbytes for ws in parent._workers_dv.values()])
        memory = 0
        if used > 0.6 * limit and limit > 0:
            memory = 2 * len(parent._workers_dv)

        target = max(memory, cpu)
        if target >= len(parent._workers_dv):
            return target
        else:  # Scale down?
            to_close = self.workers_to_close()
            return len(parent._workers_dv) - len(to_close)

    def request_acquire_replicas(self, addr: str, keys: list, *, stimulus_id: str):
        """Asynchronously ask a worker to acquire a replica of the listed keys from
        other workers. This is a fire-and-forget operation which offers no feedback for
        success or failure, and is intended for housekeeping and not for computation.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState
        ts: TaskState

        who_has = {}
        for key in keys:
            ts = parent._tasks[key]
            who_has[key] = {ws._address for ws in ts._who_has}

        self.stream_comms[addr].send(
            {
                "op": "acquire-replicas",
                "keys": keys,
                "who_has": who_has,
                "stimulus_id": stimulus_id,
            },
        )

    def request_remove_replicas(self, addr: str, keys: list, *, stimulus_id: str):
        """Asynchronously ask a worker to discard its replica of the listed keys.
        This must never be used to destroy the last replica of a key. This is a
        fire-and-forget operation, intended for housekeeping and not for computation.

        The replica disappears immediately from TaskState.who_has on the Scheduler side;
        if the worker refuses to delete, e.g. because the task is a dependency of
        another task running on it, it will (also asynchronously) inform the scheduler
        to re-add itself to who_has. If the worker agrees to discard the task, there is
        no feedback.
        """
        parent: SchedulerState = cast(SchedulerState, self)
        ws: WorkerState = parent._workers_dv[addr]
        validate = self.validate

        # The scheduler immediately forgets about the replica and suggests the worker to
        # drop it. The worker may refuse, at which point it will send back an add-keys
        # message to reinstate it.
        for key in keys:
            ts: TaskState = parent._tasks[key]
            if validate:
                # Do not destroy the last copy
                assert len(ts._who_has) > 1
            self.remove_replica(ts, ws)

        self.stream_comms[addr].send(
            {
                "op": "remove-replicas",
                "keys": keys,
                "stimulus_id": stimulus_id,
            }
        )


@cfunc
@exceptval(check=False)
def _remove_from_processing(
    state: SchedulerState, ts: TaskState
) -> str:  # -> str | None
    """
    Remove *ts* from the set of processing tasks.

    See also ``Scheduler.set_duration_estimate``
    """
    ws: WorkerState = ts._processing_on
    ts._processing_on = None  # type: ignore
    w: str = ws._address

    if w not in state._workers_dv:  # may have been removed
        return None  # type: ignore

    duration: double = ws._processing.pop(ts)
    if not ws._processing:
        state._total_occupancy -= ws._occupancy
        ws._occupancy = 0
    else:
        state._total_occupancy -= duration
        ws._occupancy -= duration

    state.check_idle_saturated(ws)
    state.release_resources(ts, ws)

    return w


@cfunc
@exceptval(check=False)
def _add_to_memory(
    state: SchedulerState,
    ts: TaskState,
    ws: WorkerState,
    recommendations: dict,
    client_msgs: dict,
    type=None,
    typename: str = None,
):
    """
    Add *ts* to the set of in-memory tasks.
    """
    if state._validate:
        assert ts not in ws._has_what

    state.add_replica(ts, ws)

    deps: list = list(ts._dependents)
    if len(deps) > 1:
        deps.sort(key=operator.attrgetter("priority"), reverse=True)

    dts: TaskState
    s: set
    for dts in deps:
        s = dts._waiting_on
        if ts in s:
            s.discard(ts)
            if not s:  # new task ready to run
                recommendations[dts._key] = "processing"

    for dts in ts._dependencies:
        s = dts._waiters
        s.discard(ts)
        if not s and not dts._who_wants:
            recommendations[dts._key] = "released"

    report_msg: dict = {}
    cs: ClientState
    if not ts._waiters and not ts._who_wants:
        recommendations[ts._key] = "released"
    else:
        report_msg["op"] = "key-in-memory"
        report_msg["key"] = ts._key
        if type is not None:
            report_msg["type"] = type

        for cs in ts._who_wants:
            client_msgs[cs._client_key] = [report_msg]

    ts.state = "memory"
    ts._type = typename  # type: ignore
    ts._group._types.add(typename)

    cs = state._clients["fire-and-forget"]
    if ts in cs._wants_what:
        _client_releases_keys(
            state,
            cs=cs,
            keys=[ts._key],
            recommendations=recommendations,
        )


@cfunc
@exceptval(check=False)
def _propagate_forgotten(
    state: SchedulerState, ts: TaskState, recommendations: dict, worker_msgs: dict
):
    ts.state = "forgotten"
    key: str = ts._key
    dts: TaskState
    for dts in ts._dependents:
        dts._has_lost_dependencies = True
        dts._dependencies.remove(ts)
        dts._waiting_on.discard(ts)
        if dts._state not in ("memory", "erred"):
            # Cannot compute task anymore
            recommendations[dts._key] = "forgotten"
    ts._dependents.clear()
    ts._waiters.clear()

    for dts in ts._dependencies:
        dts._dependents.remove(ts)
        dts._waiters.discard(ts)
        if not dts._dependents and not dts._who_wants:
            # Task not needed anymore
            assert dts is not ts
            recommendations[dts._key] = "forgotten"
    ts._dependencies.clear()
    ts._waiting_on.clear()

    ws: WorkerState
    for ws in ts._who_has:
        w: str = ws._address
        if w in state._workers_dv:  # in case worker has died
            worker_msgs[w] = [
                {
                    "op": "free-keys",
                    "keys": [key],
                    "stimulus_id": f"propagate-forgotten-{time()}",
                }
            ]
    state.remove_all_replicas(ts)


@cfunc
@exceptval(check=False)
def _client_releases_keys(
    state: SchedulerState, keys: list, cs: ClientState, recommendations: dict
):
    """Remove keys from client desired list"""
    logger.debug("Client %s releases keys: %s", cs._client_key, keys)
    ts: TaskState
    for key in keys:
        ts = state._tasks.get(key)  # type: ignore
        if ts is not None and ts in cs._wants_what:
            cs._wants_what.remove(ts)
            ts._who_wants.remove(cs)
            if not ts._who_wants:
                if not ts._dependents:
                    # No live dependents, can forget
                    recommendations[ts._key] = "forgotten"
                elif ts._state != "erred" and not ts._waiters:
                    recommendations[ts._key] = "released"


@cfunc
@exceptval(check=False)
def _task_to_msg(state: SchedulerState, ts: TaskState, duration: double = -1) -> dict:
    """Convert a single computational task to a message"""
    ws: WorkerState
    dts: TaskState

    # FIXME: The duration attribute is not used on worker. We could safe ourselves the time to compute and submit this
    if duration < 0:
        duration = state.get_task_duration(ts)

    msg: dict = {
        "op": "compute-task",
        "key": ts._key,
        "priority": ts._priority,
        "duration": duration,
        "stimulus_id": f"compute-task-{time()}",
        "who_has": {},
    }
    if ts._resource_restrictions:
        msg["resource_restrictions"] = ts._resource_restrictions
    if ts._actor:
        msg["actor"] = True

    deps: set = ts._dependencies
    if deps:
        msg["who_has"] = {
            dts._key: [ws._address for ws in dts._who_has] for dts in deps
        }
        msg["nbytes"] = {dts._key: dts._nbytes for dts in deps}

        if state._validate:
            assert all(msg["who_has"].values())

    task = ts._run_spec
    if type(task) is dict:
        msg.update(task)
    else:
        msg["task"] = task

    if ts._annotations:
        msg["annotations"] = ts._annotations

    return msg


@cfunc
@exceptval(check=False)
def _task_to_report_msg(state: SchedulerState, ts: TaskState) -> dict:  # -> dict | None
    if ts._state == "forgotten":
        return {"op": "cancelled-key", "key": ts._key}
    elif ts._state == "memory":
        return {"op": "key-in-memory", "key": ts._key}
    elif ts._state == "erred":
        failing_ts: TaskState = ts._exception_blame
        return {
            "op": "task-erred",
            "key": ts._key,
            "exception": failing_ts._exception,
            "traceback": failing_ts._traceback,
        }
    else:
        return None  # type: ignore


@cfunc
@exceptval(check=False)
def _task_to_client_msgs(state: SchedulerState, ts: TaskState) -> dict:
    if ts._who_wants:
        report_msg: dict = _task_to_report_msg(state, ts)
        if report_msg is not None:
            cs: ClientState
            return {cs._client_key: [report_msg] for cs in ts._who_wants}
    return {}


@cfunc
@exceptval(check=False)
def decide_worker(
    ts: TaskState, all_workers, valid_workers: set, objective
) -> WorkerState:  # -> WorkerState | None
    """
    Decide which worker should take task *ts*.

    We choose the worker that has the data on which *ts* depends.

    If several workers have dependencies then we choose the less-busy worker.

    Optionally provide *valid_workers* of where jobs are allowed to occur
    (if all workers are allowed to take the task, pass None instead).

    If the task requires data communication because no eligible worker has
    all the dependencies already, then we choose to minimize the number
    of bytes sent between workers.  This is determined by calling the
    *objective* function.
    """
    ws: WorkerState = None  # type: ignore
    wws: WorkerState
    dts: TaskState
    deps: set = ts._dependencies
    candidates: set
    assert all([dts._who_has for dts in deps])
    if ts._actor:
        candidates = set(all_workers)
    else:
        candidates = {wws for dts in deps for wws in dts._who_has}
    if valid_workers is None:
        if not candidates:
            candidates = set(all_workers)
    else:
        candidates &= valid_workers
        if not candidates:
            candidates = valid_workers
            if not candidates:
                if ts._loose_restrictions:
                    ws = decide_worker(ts, all_workers, None, objective)
                return ws

    ncandidates: Py_ssize_t = len(candidates)
    if ncandidates == 0:
        pass
    elif ncandidates == 1:
        for ws in candidates:
            break
    else:
        ws = min(candidates, key=objective)
    return ws


def validate_task_state(ts: TaskState):
    """
    Validate the given TaskState.
    """
    ws: WorkerState
    dts: TaskState

    assert ts._state in ALL_TASK_STATES or ts._state == "forgotten", ts

    if ts._waiting_on:
        assert ts._waiting_on.issubset(ts._dependencies), (
            "waiting not subset of dependencies",
            str(ts._waiting_on),
            str(ts._dependencies),
        )
    if ts._waiters:
        assert ts._waiters.issubset(ts._dependents), (
            "waiters not subset of dependents",
            str(ts._waiters),
            str(ts._dependents),
        )

    for dts in ts._waiting_on:
        assert not dts._who_has, ("waiting on in-memory dep", str(ts), str(dts))
        assert dts._state != "released", ("waiting on released dep", str(ts), str(dts))
    for dts in ts._dependencies:
        assert ts in dts._dependents, (
            "not in dependency's dependents",
            str(ts),
            str(dts),
            str(dts._dependents),
        )
        if ts._state in ("waiting", "processing"):
            assert dts in ts._waiting_on or dts._who_has, (
                "dep missing",
                str(ts),
                str(dts),
            )
        assert dts._state != "forgotten"

    for dts in ts._waiters:
        assert dts._state in ("waiting", "processing"), (
            "waiter not in play",
            str(ts),
            str(dts),
        )
    for dts in ts._dependents:
        assert ts in dts._dependencies, (
            "not in dependent's dependencies",
            str(ts),
            str(dts),
            str(dts._dependencies),
        )
        assert dts._state != "forgotten"

    assert (ts._processing_on is not None) == (ts._state == "processing")
    assert bool(ts._who_has) == (ts._state == "memory"), (ts, ts._who_has, ts._state)

    if ts._state == "processing":
        assert all([dts._who_has for dts in ts._dependencies]), (
            "task processing without all deps",
            str(ts),
            str(ts._dependencies),
        )
        assert not ts._waiting_on

    if ts._who_has:
        assert ts._waiters or ts._who_wants, (
            "unneeded task in memory",
            str(ts),
            str(ts._who_has),
        )
        if ts._run_spec:  # was computed
            assert ts._type
            assert isinstance(ts._type, str)
        assert not any([ts in dts._waiting_on for dts in ts._dependents])
        for ws in ts._who_has:
            assert ts in ws._has_what, (
                "not in who_has' has_what",
                str(ts),
                str(ws),
                str(ws._has_what),
            )

    if ts._who_wants:
        cs: ClientState
        for cs in ts._who_wants:
            assert ts in cs._wants_what, (
                "not in who_wants' wants_what",
                str(ts),
                str(cs),
                str(cs._wants_what),
            )

    if ts._actor:
        if ts._state == "memory":
            assert sum([ts in ws._actors for ws in ts._who_has]) == 1
        if ts._state == "processing":
            assert ts in ts._processing_on.actors


def validate_worker_state(ws: WorkerState):
    ts: TaskState
    for ts in ws._has_what:
        assert ws in ts._who_has, (
            "not in has_what' who_has",
            str(ws),
            str(ts),
            str(ts._who_has),
        )

    for ts in ws._actors:
        assert ts._state in ("memory", "processing")


def validate_state(tasks, workers, clients):
    """
    Validate a current runtime state

    This performs a sequence of checks on the entire graph, running in about
    linear time.  This raises assert errors if anything doesn't check out.
    """
    ts: TaskState
    for ts in tasks.values():
        validate_task_state(ts)

    ws: WorkerState
    for ws in workers.values():
        validate_worker_state(ws)

    cs: ClientState
    for cs in clients.values():
        for ts in cs._wants_what:
            assert cs in ts._who_wants, (
                "not in wants_what' who_wants",
                str(cs),
                str(ts),
                str(ts._who_wants),
            )


def heartbeat_interval(n):
    """
    Interval in seconds that we desire heartbeats based on number of workers
    """
    if n <= 10:
        return 0.5
    elif n < 50:
        return 1
    elif n < 200:
        return 2
    else:
        # no more than 200 hearbeats a second scaled by workers
        return n / 200 + 1


class KilledWorker(Exception):
    def __init__(self, task, last_worker):
        super().__init__(task, last_worker)
        self.task = task
        self.last_worker = last_worker


class WorkerStatusPlugin(SchedulerPlugin):
    """
    An plugin to share worker status with a remote observer

    This is used in cluster managers to keep updated about the status of the
    scheduler.
    """

    name = "worker-status"

    def __init__(self, scheduler, comm):
        self.bcomm = BatchedSend(interval="5ms")
        self.bcomm.start(comm)

        self.scheduler = scheduler
        self.scheduler.add_plugin(self)

    def add_worker(self, worker=None, **kwargs):
        ident = self.scheduler.workers[worker].identity()
        del ident["metrics"]
        del ident["last_seen"]
        try:
            self.bcomm.send(["add", {"workers": {worker: ident}}])
        except CommClosedError:
            self.scheduler.remove_plugin(name=self.name)

    def remove_worker(self, worker=None, **kwargs):
        try:
            self.bcomm.send(["remove", worker])
        except CommClosedError:
            self.scheduler.remove_plugin(name=self.name)

    def teardown(self):
        self.bcomm.close()


class CollectTaskMetaDataPlugin(SchedulerPlugin):
    def __init__(self, scheduler, name):
        self.scheduler = scheduler
        self.name = name
        self.keys = set()
        self.metadata = {}
        self.state = {}

    def update_graph(self, scheduler, dsk=None, keys=None, restrictions=None, **kwargs):
        self.keys.update(keys)

    def transition(self, key, start, finish, *args, **kwargs):
        if finish == "memory" or finish == "erred":
            ts: TaskState = self.scheduler.tasks.get(key)
            if ts is not None and ts._key in self.keys:
                self.metadata[key] = ts._metadata
                self.state[key] = finish
                self.keys.discard(key)
