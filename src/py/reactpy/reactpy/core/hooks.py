from __future__ import annotations

import inspect
import warnings
from asyncio import CancelledError, Event, create_task
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Coroutine,
    Generator,
    Sequence,
)
from logging import getLogger
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from typing_extensions import TypeAlias

from reactpy.config import REACTPY_DEBUG_MODE
from reactpy.core._life_cycle_hook import StopEffect, current_hook
from reactpy.core.types import Context, Key, State, VdomDict
from reactpy.utils import Ref

if not TYPE_CHECKING:
    # make flake8 think that this variable exists
    ellipsis = type(...)

__all__ = [
    "use_state",
    "use_effect",
    "use_reducer",
    "use_callback",
    "use_ref",
    "use_memo",
]

logger = getLogger(__name__)

_Type = TypeVar("_Type")


@overload
def use_state(initial_value: Callable[[], _Type]) -> State[_Type]:
    ...


@overload
def use_state(initial_value: _Type) -> State[_Type]:
    ...


def use_state(initial_value: _Type | Callable[[], _Type]) -> State[_Type]:
    """See the full :ref:`Use State` docs for details

    Parameters:
        initial_value:
            Defines the initial value of the state. A callable (accepting no arguments)
            can be used as a constructor function to avoid re-creating the initial value
            on each render.

    Returns:
        A tuple containing the current state and a function to update it.
    """
    current_state = _use_const(lambda: _CurrentState(initial_value))
    return State(current_state.value, current_state.dispatch)


class _CurrentState(Generic[_Type]):
    __slots__ = "value", "dispatch"

    def __init__(
        self,
        initial_value: _Type | Callable[[], _Type],
    ) -> None:
        if callable(initial_value):
            self.value = initial_value()
        else:
            self.value = initial_value

        hook = current_hook()

        def dispatch(new: _Type | Callable[[_Type], _Type]) -> None:
            if callable(new):
                next_value = new(self.value)
            else:
                next_value = new
            if not strictly_equal(next_value, self.value):
                self.value = next_value
                hook.schedule_render()

        self.dispatch = dispatch


_SyncGeneratorEffect: TypeAlias = Callable[[], Generator[None, None, None]]
_SyncFunctionEffect: TypeAlias = Callable[[], Callable[[], None] | None]
_SyncEffect: TypeAlias = _SyncGeneratorEffect | _SyncFunctionEffect

_AsyncGeneratorEffect: TypeAlias = Callable[[], AsyncGenerator[None, None]]
_AsyncFunctionEffect: TypeAlias = Callable[
    [], Coroutine[None, None, Callable[[], None] | None]
]
_AsyncEffect: TypeAlias = _AsyncGeneratorEffect | _AsyncFunctionEffect
_Effect: TypeAlias = _SyncEffect | _AsyncEffect


@overload
def use_effect(
    function: None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> Callable[[_Effect], None]:
    ...


@overload
def use_effect(
    function: _Effect,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> None:
    ...


def use_effect(
    function: _Effect | None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> Callable[[_Effect], None] | None:
    """See the full :ref:`Use Effect` docs for details

    Parameters:
        function:
            Applies the effect and can return a clean-up function
        dependencies:
            Dependencies for the effect. The effect will only trigger if the identity
            of any value in the given sequence changes (i.e. their :func:`id` is
            different). By default these are inferred based on local variables that are
            referenced by the given function. If ``None``, then the effect runs on every
            render.

    Returns:
        If no function is provided, a decorator. Otherwise ``None``.
    """
    hook = current_hook()
    dependencies = _try_to_infer_closure_values(function, dependencies)
    memoize = use_memo(dependencies=dependencies)
    stop_last_effect: Ref[StopEffect | None] = use_ref(None)

    def add_effect(function: _Effect) -> None:
        effect_func = _cast_async_effect(function)

        async def start_effect() -> Callable:
            if stop_last_effect.current is not None:
                await stop_last_effect.current()

            stop = Event()

            async def run_effect() -> StopEffect:
                effect_gen = effect_func()
                # start running the effect
                effect_task = create_task(effect_gen.asend(None))
                # wait for re-render or unmount
                await stop.wait()
                # signal effect to stop (no-op if already complete)
                effect_task.cancel()
                # wait for effect to halt
                try:
                    await effect_task
                except CancelledError:
                    pass
                # wait for effect cleanup
                await effect_gen.aclose()

            effect_task = create_task(run_effect())

            async def stop_effect() -> None:
                stop.set()
                try:
                    await effect_task
                except Exception:
                    logger.exception("Error in effect")

            stop_last_effect.current = stop_effect

            return stop_effect

        return memoize(lambda: hook.add_effect(start_effect))

    if function is not None:
        add_effect(function)
        return None
    else:
        return add_effect


def _cast_async_effect(function: Callable[..., Any]) -> _AsyncGeneratorEffect:
    if inspect.isasyncgenfunction(function):
        async_generator_effect = function
    elif inspect.iscoroutinefunction(function):
        async_function_effect = cast(_AsyncFunctionEffect, function)

        async def async_generator_effect() -> AsyncIterator[None]:
            task = create_task(async_function_effect())
            try:
                yield
            finally:
                cleanup = await task
                if cleanup is not None:
                    warnings.warn(
                        "Async effect returned a cleanup function - use an async "
                        "generator instead by yielding inside a try/finally block. "
                        "This will be an error in a future version of ReactPy.",
                        DeprecationWarning,
                        stacklevel=3,
                    )
                    cleanup()

    elif inspect.isgeneratorfunction(function):
        sync_generator_effect = cast(_SyncGeneratorEffect, function)

        async def async_generator_effect() -> AsyncIterator[None]:
            gen = sync_generator_effect()
            gen.send(None)
            try:
                yield
            finally:
                gen.close()

    else:
        sync_function_effect = cast(_SyncFunctionEffect, function)

        async def async_generator_effect() -> AsyncIterator[None]:
            cleanup = sync_function_effect()
            try:
                yield
            finally:
                if cleanup is not None:
                    cleanup()

    return async_generator_effect


def use_debug_value(
    message: Any | Callable[[], Any],
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> None:
    """Log debug information when the given message changes.

    .. note::
        This hook only logs if :data:`~reactpy.config.REACTPY_DEBUG_MODE` is active.

    Unlike other hooks, a message is considered to have changed if the old and new
    values are ``!=``. Because this comparison is performed on every render of the
    component, it may be worth considering the performance cost in some situations.

    Parameters:
        message:
            The value to log or a memoized function for generating the value.
        dependencies:
            Dependencies for the memoized function. The message will only be recomputed
            if the identity of any value in the given sequence changes (i.e. their
            :func:`id` is different). By default these are inferred based on local
            variables that are referenced by the given function.
    """
    old: Ref[Any] = _use_const(lambda: Ref(object()))
    memo_func = message if callable(message) else lambda: message
    new = use_memo(memo_func, dependencies)

    if REACTPY_DEBUG_MODE.current and old.current != new:
        old.current = new
        logger.debug(f"{current_hook().component} {new}")


def create_context(default_value: _Type) -> Context[_Type]:
    """Return a new context type for use in :func:`use_context`"""

    def context(
        *children: Any,
        value: _Type = default_value,
        key: Key | None = None,
    ) -> _ContextProvider[_Type]:
        return _ContextProvider(
            *children,
            value=value,
            key=key,
            type=context,
        )

    context.__qualname__ = "context"

    return context


def use_context(context: Context[_Type]) -> _Type:
    """Get the current value for the given context type.

    See the full :ref:`Use Context` docs for more information.
    """
    hook = current_hook()
    provider = hook.get_context_provider(context)

    if provider is None:
        # same assertions but with normal exceptions
        if not isinstance(context, FunctionType):
            raise TypeError(f"{context} is not a Context")  # nocov
        if context.__kwdefaults__ is None:
            raise TypeError(f"{context} has no 'value' kwarg")  # nocov
        if "value" not in context.__kwdefaults__:
            raise TypeError(f"{context} has no 'value' kwarg")  # nocov
        return cast(_Type, context.__kwdefaults__["value"])

    return provider.value


class _ContextProvider(Generic[_Type]):
    def __init__(
        self,
        *children: Any,
        value: _Type,
        key: Key | None,
        type: Context[_Type],
    ) -> None:
        self.children = children
        self.key = key
        self.type = type
        self.value = value

    def render(self) -> VdomDict:
        current_hook().set_context_provider(self)
        return {"tagName": "", "children": self.children}

    def __repr__(self) -> str:
        return f"ContextProvider({self.type})"


_ActionType = TypeVar("_ActionType")


def use_reducer(
    reducer: Callable[[_Type, _ActionType], _Type],
    initial_value: _Type,
) -> tuple[_Type, Callable[[_ActionType], None]]:
    """See the full :ref:`Use Reducer` docs for details

    Parameters:
        reducer:
            A function which applies an action to the current state in order to
            produce the next state.
        initial_value:
            The initial state value (same as for :func:`use_state`)

    Returns:
        A tuple containing the current state and a function to change it with an action
    """
    state, set_state = use_state(initial_value)
    return state, _use_const(lambda: _create_dispatcher(reducer, set_state))


def _create_dispatcher(
    reducer: Callable[[_Type, _ActionType], _Type],
    set_state: Callable[[Callable[[_Type], _Type]], None],
) -> Callable[[_ActionType], None]:
    def dispatch(action: _ActionType) -> None:
        set_state(lambda last_state: reducer(last_state, action))

    return dispatch


_CallbackFunc = TypeVar("_CallbackFunc", bound=Callable[..., Any])


@overload
def use_callback(
    function: None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> Callable[[_CallbackFunc], _CallbackFunc]:
    ...


@overload
def use_callback(
    function: _CallbackFunc,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> _CallbackFunc:
    ...


def use_callback(
    function: _CallbackFunc | None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> _CallbackFunc | Callable[[_CallbackFunc], _CallbackFunc]:
    """See the full :ref:`Use Callback` docs for details

    Parameters:
        function:
            The function whose identity will be preserved
        dependencies:
            Dependencies of the callback. The identity the ``function`` will be updated
            if the identity of any value in the given sequence changes (i.e. their
            :func:`id` is different). By default these are inferred based on local
            variables that are referenced by the given function.

    Returns:
        The current function
    """
    dependencies = _try_to_infer_closure_values(function, dependencies)
    memoize = use_memo(dependencies=dependencies)

    def setup(function: _CallbackFunc) -> _CallbackFunc:
        return memoize(lambda: function)

    if function is not None:
        return setup(function)
    else:
        return setup


class _LambdaCaller(Protocol):
    """MyPy doesn't know how to deal with TypeVars only used in function return"""

    def __call__(self, func: Callable[[], _Type]) -> _Type:
        ...


@overload
def use_memo(
    function: None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> _LambdaCaller:
    ...


@overload
def use_memo(
    function: Callable[[], _Type],
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> _Type:
    ...


def use_memo(
    function: Callable[[], _Type] | None = None,
    dependencies: Sequence[Any] | ellipsis | None = ...,
) -> _Type | Callable[[Callable[[], _Type]], _Type]:
    """See the full :ref:`Use Memo` docs for details

    Parameters:
        function:
            The function to be memoized.
        dependencies:
            Dependencies for the memoized function. The memo will only be recomputed if
            the identity of any value in the given sequence changes (i.e. their
            :func:`id` is different). By default these are inferred based on local
            variables that are referenced by the given function.

    Returns:
        The current state
    """
    dependencies = _try_to_infer_closure_values(function, dependencies)

    memo: _Memo[_Type] = _use_const(_Memo)

    if memo.empty():
        # we need to initialize on the first run
        changed = True
        memo.deps = () if dependencies is None else dependencies
    elif dependencies is None:
        changed = True
        memo.deps = ()
    elif (
        len(memo.deps) != len(dependencies)
        # if deps are same length check identity for each item
        or not all(
            strictly_equal(current, new)
            for current, new in zip(memo.deps, dependencies)
        )
    ):
        memo.deps = dependencies
        changed = True
    else:
        changed = False

    setup: Callable[[Callable[[], _Type]], _Type]

    if changed:

        def setup(function: Callable[[], _Type]) -> _Type:
            current_value = memo.value = function()
            return current_value

    else:

        def setup(function: Callable[[], _Type]) -> _Type:
            return memo.value

    if function is not None:
        return setup(function)
    else:
        return setup


class _Memo(Generic[_Type]):
    """Simple object for storing memoization data"""

    __slots__ = "value", "deps"

    value: _Type
    deps: Sequence[Any]

    def empty(self) -> bool:
        try:
            self.value  # noqa: B018
        except AttributeError:
            return True
        else:
            return False


def use_ref(initial_value: _Type) -> Ref[_Type]:
    """See the full :ref:`Use State` docs for details

    Parameters:
        initial_value: The value initially assigned to the reference.

    Returns:
        A :class:`Ref` object.
    """
    return _use_const(lambda: Ref(initial_value))


def _use_const(function: Callable[[], _Type]) -> _Type:
    return current_hook().use_state(function)


def _try_to_infer_closure_values(
    func: Callable[..., Any] | None,
    values: Sequence[Any] | ellipsis | None,
) -> Sequence[Any] | None:
    if values is ...:
        if isinstance(func, FunctionType):
            return (
                [cell.cell_contents for cell in func.__closure__]
                if func.__closure__
                else []
            )
        else:
            return None
    else:
        return values


def strictly_equal(x: Any, y: Any) -> bool:
    """Check if two values are identical or, for a limited set or types, equal.

    Only the following types are checked for equality rather than identity:

    - ``int``
    - ``float``
    - ``complex``
    - ``str``
    - ``bytes``
    - ``bytearray``
    - ``memoryview``
    """
    return x is y or (type(x) in _NUMERIC_TEXT_BINARY_TYPES and x == y)


_NUMERIC_TEXT_BINARY_TYPES = {
    # numeric
    int,
    float,
    complex,
    # text
    str,
    # binary types
    bytes,
    bytearray,
    memoryview,
}
