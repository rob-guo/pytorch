# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import logging
import os
import pickle
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Optional, Set, Tuple

from torch.distributed import Store

from .api import (
    RendezvousClosedError,
    RendezvousHandler,
    RendezvousParameters,
    RendezvousStateError,
    RendezvousTimeoutError,
)

from .utils import _delay


log = logging.getLogger(__name__)


Token = Any
"""Represents an opaque fencing token used by the rendezvous backend."""


class RendezvousBackend(ABC):
    """Represents a backend that holds the rendezvous state."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Gets the name of the backend."""

    @abstractmethod
    def get_state(self) -> Optional[Tuple[bytes, Token]]:
        """Gets the rendezvous state.

        Returns:
            A tuple of the encoded rendezvous state and its fencing token or
            ``None`` if no state is found in the backend.

        Raises:
            RendezvousConnectionError:
                The connection to the backend has failed.
            RendezvousStateError:
                The rendezvous state is corrupt.
        """

    @abstractmethod
    def set_state(
        self, state: bytes, token: Optional[Token] = None
    ) -> Optional[Tuple[bytes, Token, bool]]:
        """Sets the rendezvous state.

        The new rendezvous state is set conditionally:

          - If the specified ``token`` matches the fencing token stored in the
            backend, the state will be updated. The new state will be returned
            to the caller along with its fencing token.
          - If the specified ``token`` does not match the fencing token stored
            in the backend, the state won't be updated; instead the existing
            state along with its fencing token will be returned to the caller.
          - If the specified ``token`` is ``None``, the new state will be set
            only if there is no existing state in the backend. Either the new
            state or the existing state along with its fencing token will be
            returned to the caller.

        Args:
            state:
                The encoded rendezvous state.
            token:
                An optional fencing token that was retrieved by a previous call
                to :py:meth:`get_state` or ``set_state()``.

        Returns:
            A tuple of the serialized rendezvous state, its fencing token, and
            a boolean value indicating whether our set attempt succeeded.

        Raises:
            RendezvousConnectionError:
                The connection to the backend has failed.
            RendezvousStateError:
                The rendezvous state is corrupt.
        """


class RendezvousTimeout:
    """Holds the timeout configuration of a rendezvous.

    Args:
        join:
            The time within which the rendezvous is expected to complete.
        last_call:
            An additional wait amount before completing the rendezvous once the
            rendezvous has the minimum number of required participants.
        close:
            The time within which the rendezvous is expected to close after a
            call to :py:meth:`RendezvousHandler.set_closed` or
            :py:meth:`RendezvousHandler.shutdown`.
        keep_alive:
            The time within which a keep-alive heartbeat is expected to
            complete.
    """

    _ZERO = timedelta(0)

    _DEFAULT_TIMEOUTS = {
        "join": timedelta(seconds=600),
        "last_call": timedelta(seconds=30),
        "close": timedelta(seconds=30),
        "heartbeat": timedelta(seconds=5),
    }

    _join: timedelta
    _last_call: timedelta
    _close: timedelta
    _heartbeat: timedelta

    def __init__(
        self,
        join: Optional[timedelta] = None,
        last_call: Optional[timedelta] = None,
        close: Optional[timedelta] = None,
        heartbeat: Optional[timedelta] = None,
    ) -> None:
        self._set_timeouts(join=join, last_call=last_call, close=close, heartbeat=heartbeat)

    @property
    def join(self) -> timedelta:
        """Gets the join timeout."""
        return self._join

    @property
    def last_call(self) -> timedelta:
        """Gets the last call timeout."""
        return self._last_call

    @property
    def close(self) -> timedelta:
        """Gets the close timeout."""
        return self._close

    @property
    def heartbeat(self) -> timedelta:
        """Gets the keep-alive heartbeat timeout."""
        return self._heartbeat

    def _set_timeouts(self, **timeouts: Optional[timedelta]):
        for name, timeout in timeouts.items():
            if timeout is None:
                timeout = self._DEFAULT_TIMEOUTS[name]
            if timeout <= self._ZERO:
                raise ValueError(f"The {name} timeout ({timeout}) must be positive.")
            setattr(self, "_" + name, timeout)


@dataclass(repr=False, eq=False, frozen=True)
class RendezvousSettings:
    """Holds the settings of the rendezvous.

    Attributes:
        run_id:
            The run id of the rendezvous.
        min_nodes:
            The minimum number of nodes to admit to the rendezvous.
        max_nodes:
            The maximum number of nodes to admit to the rendezvous.
        timeout:
            The timeout configuration of the rendezvous.
        keep_alive_interval:
            The amount of time a node waits before sending a heartbeat to keep
            it alive in the rendezvous.
        keep_alive_max_attempt:
            The maximum number of failed heartbeat attempts after which a node
            is considered dead.
    """

    run_id: str
    min_nodes: int
    max_nodes: int
    timeout: RendezvousTimeout
    keep_alive_interval: timedelta
    keep_alive_max_attempt: int


@dataclass(eq=True, order=True, frozen=True)
class _NodeDesc:
    """Describes a node in the rendezvous.

    Attributes:
        fqdn:
            The FQDN of the node.
        pid:
            The id of the process in which the rendezvous handler runs.
        local_id:
            A process-wide unique id.
    """

    fqdn: str
    pid: int
    local_id: int

    def __repr__(self) -> str:
        return f"{self.fqdn}_{self.pid}_{self.local_id}"


class _NodeDescGenerator:
    """Generates node descriptors.

    A node descriptor is a combination of an FQDN, a process id, and an auto-
    incremented integer that uniquely identifies a node in the rendezvous.
    """

    _lock: threading.Lock
    _local_id: int

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # An integer that is incremented with each call to generate().
        self._local_id = 0

    def generate(self) -> _NodeDesc:
        # This method can be called by multiple threads concurrently; therefore,
        # we must increment the integer atomically.
        with self._lock:
            local_id = self._local_id

            self._local_id += 1

        return _NodeDesc(socket.getfqdn(), os.getpid(), local_id)


class _RendezvousState:
    """Holds the state of a rendezvous.

    Attributes:
        round:
            The current round of the rendezvous.
        complete:
            A boolean value indicating whether the current round of the
            rendezvous is complete.
        deadline:
            The time at which the current round of the rendezvous will be
            considered complete if it is still waiting for nodes to join.
        closed:
            A boolean value indicating whether the rendezvous is closed.
        participants:
            A dictionary of the participants and their corresponding ranks.
        wait_list:
            A set of nodes that are waiting to participate in the next round of
            the rendezvous.
        last_heartbeats:
            A dictionary containing each node's last heartbeat time.
    """

    round: int
    complete: bool
    deadline: Optional[datetime]
    closed: bool
    participants: Dict[_NodeDesc, int]
    wait_list: Set[_NodeDesc]
    last_heartbeats: Dict[_NodeDesc, datetime]

    def __init__(self) -> None:
        self.round = 0
        self.complete = False
        self.deadline = None
        self.closed = False
        self.participants = {}
        self.wait_list = set()
        self.last_heartbeats = {}


class _RendezvousStateHolder(ABC):
    """Holds the shared rendezvous state synced with other nodes."""

    @property
    @abstractmethod
    def state(self) -> _RendezvousState:
        """Gets the local state."""

    @abstractmethod
    def sync(self) -> Optional[bool]:
        """Reads or writes the latest state.

        Returns:
            A boolean value indicating whether the local state, in case marked
            as dirty, was successfully synced with other nodes.
        """

    @abstractmethod
    def mark_dirty(self) -> None:
        """Marks the local state as dirty."""


class _BackendRendezvousStateHolder(_RendezvousStateHolder):
    """Holds the rendezvous state synced with other nodes via a backend.

    Args:
        backend:
            The rendezvous backend to use.
        settings:
            The rendezvous settings.
        cache_duration:
            The amount of time, in seconds, to cache the last rendezvous state
            before requesting it from the backend again.
    """

    _backend: RendezvousBackend
    _state: _RendezvousState
    _settings: RendezvousSettings
    _cache_duration: int
    _token: Token
    _dirty: bool
    _last_sync_time: float

    def __init__(
        self, backend: RendezvousBackend, settings: RendezvousSettings, cache_duration: int = 1
    ) -> None:
        self._backend = backend
        self._state = _RendezvousState()
        self._settings = settings
        self._cache_duration = cache_duration
        self._token = None
        self._dirty = False
        self._last_sync_time = -1

    @property
    def state(self) -> _RendezvousState:
        """See base class."""
        return self._state

    def sync(self) -> Optional[bool]:
        """See base class."""
        state_bits: Optional[bytes] = None

        token = None

        has_set: Optional[bool]

        if self._dirty:
            has_set = False

            state_bits = pickle.dumps(self._state)

            set_response = self._backend.set_state(state_bits, self._token)
            if set_response is not None:
                state_bits, token, has_set = set_response
        else:
            has_set = None

            if self._cache_duration > 0:
                # Avoid overloading the backend if we are asked to retrieve the
                # state repeatedly. Try to serve the cached state.
                if self._last_sync_time >= max(time.monotonic() - self._cache_duration, 0):
                    return None

            get_response = self._backend.get_state()
            if get_response is not None:
                state_bits, token = get_response

        if state_bits is not None:
            try:
                self._state = pickle.loads(state_bits)
            except pickle.PickleError as exc:
                raise RendezvousStateError(
                    "The rendezvous state is corrupt. See inner exception for details."
                ) from exc
        else:
            self._state = _RendezvousState()

        self._token = token

        self._dirty = False

        self._last_sync_time = time.monotonic()

        self._sanitize()

        return has_set

    def _sanitize(self) -> None:
        expire_time = datetime.utcnow() - (
            self._settings.keep_alive_interval * self._settings.keep_alive_max_attempt
        )

        # Filter out the dead nodes.
        dead_nodes = [
            node
            for node, last_heartbeat in self._state.last_heartbeats.items()
            if last_heartbeat < expire_time
        ]

        for dead_node in dead_nodes:
            del self._state.last_heartbeats[dead_node]

            try:
                del self._state.participants[dead_node]
            except KeyError:
                pass

            try:
                self._state.wait_list.remove(dead_node)
            except KeyError:
                pass

    def mark_dirty(self) -> None:
        """See base class.

        If the local rendezvous state is dirty, the next sync call will try to
        write the changes back to the backend. However this attempt might fail
        if another node, which had the same state, also made changes and wrote
        them before us.
        """
        self._dirty = True


class _Action(Enum):
    """Specifies the possible actions based on the state of the rendezvous."""

    KEEP_ALIVE = 1
    ADD_TO_PARTICIPANTS = 2
    ADD_TO_WAIT_LIST = 3
    REMOVE_FROM_PARTICIPANTS = 4
    REMOVE_FROM_WAIT_LIST = 5
    MARK_RENDEZVOUS_COMPLETE = 6
    MARK_RENDEZVOUS_CLOSED = 7
    SYNC = 8
    ERROR_CLOSED = 9
    ERROR_TIMEOUT = 10
    FINISH = 11


class _RendezvousContext:
    """Holds the context of the rendezvous.

    Attributes:
        node:
            The node descriptor associated with the current rendezvous handler
            instance.
        state:
            The current state of the rendezvous.
        settings:
            The rendezvous settings.
    """

    node: _NodeDesc
    state: _RendezvousState
    settings: RendezvousSettings

    def __init__(
        self, node: _NodeDesc, state: _RendezvousState, settings: RendezvousSettings
    ) -> None:
        self.node = node
        self.state = state
        self.settings = settings


class _RendezvousOpExecutor(ABC):
    """Executes rendezvous operations."""

    @abstractmethod
    def run(
        self, state_handler: Callable[[_RendezvousContext, float], _Action], deadline: float
    ) -> None:
        """Executes a rendezvous operation.

        An operation is run inside a state machine and is expected to transition
        the rendezvous from one state to another.

        Args:
            state_handler:
                A callable that is expected to return the next state transition
                action based on the current state of the rendezvous.
            deadline:
                The time, in seconds, at which the operation will be considered
                timed-out.
        """


class _DistributedRendezvousOpExecutor(_RendezvousOpExecutor):
    """Executes rendezvous operations using a shared state.

    Args:
        node:
            The node descriptor associated with the current rendezvous handler
            instance.
        state_holder:
            The ``RendezvousStateHolder`` to use to sync the rendezvous state
            with other nodes.
        settings:
            The rendezvous settings.
    """

    _node: _NodeDesc
    _state: _RendezvousState
    _state_holder: _RendezvousStateHolder
    _settings: RendezvousSettings

    def __init__(
        self,
        node: _NodeDesc,
        state_holder: _RendezvousStateHolder,
        settings: RendezvousSettings,
    ) -> None:
        self._node = node
        self._state_holder = state_holder
        self._settings = settings

    def run(
        self, state_handler: Callable[[_RendezvousContext, float], _Action], deadline: float
    ) -> None:
        """See base class."""
        action = None

        while action != _Action.FINISH:
            # Reads or writes the latest rendezvous state shared by all nodes in
            # the rendezvous. Note that our local changes might get overridden
            # by another node if that node synced its changes before us.
            has_set = self._state_holder.sync()
            if has_set is not None:
                if has_set:
                    log.debug(
                        f"The node '{self._node}' has successfully synced its local changes with "
                        f"other nodes in the rendezvous '{self._settings.run_id}'."
                    )
                else:
                    log.debug(
                        f"The node '{self._node}' has a stale state and failed to sync its local "
                        f"changes with other nodes in the rendezvous '{self._settings.run_id}'."
                    )

            self._state = self._state_holder.state

            ctx = _RendezvousContext(self._node, self._state, self._settings)

            # Determine the next action to take based on the current state of
            # the rendezvous.
            action = state_handler(ctx, deadline)

            if action == _Action.FINISH:
                continue

            if action == _Action.ERROR_CLOSED:
                raise RendezvousClosedError()

            if action == _Action.ERROR_TIMEOUT:
                raise RendezvousTimeoutError()

            if action == _Action.SYNC:
                # Delay the execution by one second to avoid overloading the
                # backend if we are asked to poll for state changes.
                _delay(seconds=1)
            else:
                if action == _Action.KEEP_ALIVE:
                    self._keep_alive()
                elif action == _Action.ADD_TO_PARTICIPANTS:
                    self._add_to_participants()
                elif action == _Action.ADD_TO_WAIT_LIST:
                    self._add_to_wait_list()
                elif action == _Action.REMOVE_FROM_PARTICIPANTS:
                    self._remove_from_participants()
                elif action == _Action.REMOVE_FROM_WAIT_LIST:
                    self._remove_from_wait_list()
                elif action == _Action.MARK_RENDEZVOUS_COMPLETE:
                    self._mark_rendezvous_complete()
                elif action == _Action.MARK_RENDEZVOUS_CLOSED:
                    self._mark_rendezvous_closed()

                # Attempt to sync our changes back to other nodes.
                self._state_holder.mark_dirty()

    def _keep_alive(self) -> None:
        log.debug(
            f"The node '{self._node}' updated its keep-alive heartbeat time for the rendezvous "
            f"'{self._settings.run_id}'. Pending sync."
        )

        self._state.last_heartbeats[self._node] = datetime.utcnow()

    def _add_to_participants(self) -> None:
        log.debug(
            f"The node '{self._node}' added itself to the participants of round "
            f"{self._state.round} of the rendezvous '{self._settings.run_id}'. Pending sync."
        )

        state = self._state

        try:
            state.wait_list.remove(self._node)
        except KeyError:
            pass

        # The ranks of the participants will be set once the rendezvous is
        # complete.
        state.participants[self._node] = 0

        self._keep_alive()

        if len(state.participants) == self._settings.min_nodes:
            state.deadline = datetime.utcnow() + self._settings.timeout.last_call

        if len(state.participants) == self._settings.max_nodes:
            self._mark_rendezvous_complete()

    def _add_to_wait_list(self) -> None:
        log.debug(
            f"The '{self._node}' added itself to the wait list of round {self._state.round + 1} "
            f"of the rendezvous '{self._settings.run_id}'. Pending sync."
        )

        self._state.wait_list.add(self._node)

        self._keep_alive()

    def _remove_from_participants(self) -> None:
        log.debug(
            f"The node '{self._node}' removed itself from the participants of round "
            f"{self._state.round} of the rendezvous '{self._settings.run_id}'. Pending sync."
        )

        state = self._state

        del state.participants[self._node]

        del state.last_heartbeats[self._node]

        if state.complete:
            # If we do not have any participants left, move to the next round.
            if not state.participants:
                state.complete = False

                state.round += 1
        else:
            if len(state.participants) < self._settings.min_nodes:
                state.deadline = None

    def _remove_from_wait_list(self) -> None:
        log.debug(
            f"The node '{self._node}' removed itself from the wait list of round "
            f"{self._state.round + 1} of the rendezvous '{self._settings.run_id}'. Pending sync."
        )

        self._state.wait_list.remove(self._node)

        del self._state.last_heartbeats[self._node]

    def _mark_rendezvous_complete(self) -> None:
        log.debug(
            f"The node '{self._node}' marked round {self._state.round} of the rendezvous "
            f"'{self._settings.run_id}' as complete. Pending sync."
        )

        state = self._state

        state.complete = True
        state.deadline = None

        # Assign the ranks.
        for rank, node in enumerate(sorted(state.participants)):
            state.participants[node] = rank

    def _mark_rendezvous_closed(self) -> None:
        log.debug(
            f"The node '{self._node}' marked the rendezvous '{self._settings.run_id}' as closed. "
            "Pending sync."
        )

        self._state.closed = True


def _should_keep_alive(ctx: _RendezvousContext) -> bool:
    """Determines whether a keep-alive heartbeat should be sent."""
    try:
        last_heartbeat = ctx.state.last_heartbeats[ctx.node]
    except KeyError:
        return False

    return last_heartbeat <= datetime.utcnow() - ctx.settings.keep_alive_interval


class _RendezvousCloseOp:
    """Represents a rendezvous close operation."""

    def __call__(self, ctx: _RendezvousContext, deadline: float) -> _Action:
        if ctx.state.closed:
            return _Action.FINISH
        if time.monotonic() > deadline:
            return _Action.ERROR_TIMEOUT
        return _Action.MARK_RENDEZVOUS_CLOSED


class _RendezvousKeepAliveOp:
    """Represents a rendezvous keep-alive update operation."""

    def __call__(self, ctx: _RendezvousContext, deadline: float) -> _Action:
        if _should_keep_alive(ctx):
            if time.monotonic() > deadline:
                return _Action.ERROR_TIMEOUT
            return _Action.KEEP_ALIVE
        return _Action.FINISH


class DynamicRendezvousHandler(RendezvousHandler):
    """Represents the dynamic rendezvous handler.

    Args:
        run_id:
            The run id of the rendezvous.
        store:
            The C10d store to return as part of the rendezvous.
        backend:
            The backend to use to hold the rendezvous state.
        min_nodes:
            The minimum number of nodes to admit to the rendezvous.
        max_nodes:
            The maximum number of nodes to admit to the rendezvous.
        timeout:
            The timeout configuration of the rendezvous.
    """

    _run_id: str
    _settings: RendezvousSettings
    _store: Store
    _backend: RendezvousBackend

    def __init__(
        self,
        run_id: str,
        store: Store,
        backend: RendezvousBackend,
        min_nodes: int,
        max_nodes: int,
        timeout: Optional[RendezvousTimeout] = None,
    ) -> None:
        if not run_id:
            raise ValueError("The run id must be a non-empty string.")

        if min_nodes < 1:
            raise ValueError(
                f"The minimum number of nodes ({min_nodes}) must be greater than zero."
            )

        if max_nodes < min_nodes:
            raise ValueError(
                f"The maximum number of nodes ({max_nodes}) must be greater than or equal to the "
                f"minimum number of nodes ({min_nodes})."
            )

        self._settings = RendezvousSettings(
            run_id,
            min_nodes,
            max_nodes,
            timeout or RendezvousTimeout(),
            keep_alive_interval=timedelta(seconds=5),
            keep_alive_max_attempt=3,
        )

        self._store = store
        self._backend = backend

    @property
    def settings(self) -> RendezvousSettings:
        """Gets the settings of the rendezvous."""
        return self._settings

    @property
    def store(self) -> Store:
        """Gets the C10d store returned as part of the rendezvous."""
        return self._store

    @property
    def backend(self) -> RendezvousBackend:
        """Gets the backend used to hold the rendezvous state."""
        return self._backend

    def get_backend(self) -> str:
        """See base class."""
        return self._backend.name

    def next_rendezvous(self) -> Tuple[Store, int, int]:
        """See base class."""
        raise NotImplementedError()

    def is_closed(self) -> bool:
        """See base class."""
        raise NotImplementedError()

    def set_closed(self) -> None:
        """See base class."""
        raise NotImplementedError()

    def num_nodes_waiting(self) -> int:
        """See base class."""
        raise NotImplementedError()

    def get_run_id(self) -> str:
        """See base class."""
        return self.settings.run_id

    def shutdown(self) -> bool:
        """See base class."""
        raise NotImplementedError()


def _get_timeout(params: RendezvousParameters, key: str) -> Optional[timedelta]:
    timeout = params.get_as_int(key + "_timeout")
    if timeout is None:
        return None
    return timedelta(seconds=timeout)


def create_handler(
    store: Store, backend: RendezvousBackend, params: RendezvousParameters
) -> DynamicRendezvousHandler:
    """Create a new :py:class:`DynamicRendezvousHandler` from the specified
    parameters.

    +-------------------+------------------------------------------------------+
    | Parameter         | Description                                          |
    +===================+======================================================+
    | join_timeout      | The total time, in seconds, within which the         |
    |                   | rendezvous is expected to complete. Defaults to 600  |
    |                   | seconds.                                             |
    +-------------------+------------------------------------------------------+
    | last_call_timeout | An additional wait amount, in seconds, before        |
    |                   | completing the rendezvous once the minimum number of |
    |                   | nodes has been reached. Defaults to 30 seconds.      |
    +-------------------+------------------------------------------------------+
    | close_timeout     | The time, in seconds, within which the rendezvous is |
    |                   | expected to close after a call to                    |
    |                   | :py:meth:`RendezvousHandler.set_closed` or           |
    |                   | :py:meth:`RendezvousHandler.shutdown`. Defaults to   |
    |                   | 30 seconds.                                          |
    +-------------------+------------------------------------------------------+
    """
    timeout = RendezvousTimeout(
        _get_timeout(params, "join"),
        _get_timeout(params, "last_call"),
        _get_timeout(params, "close"),
    )

    return DynamicRendezvousHandler(
        params.run_id,
        store,
        backend,
        params.min_nodes,
        params.max_nodes,
        timeout,
    )
