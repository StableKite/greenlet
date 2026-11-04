"""Types for the native greenlet API, including its variadic call boundary."""

from collections.abc import Callable
from contextvars import Context
from types import FrameType, TracebackType
from typing import Any, ClassVar, Final, Literal, TypeAlias

__all__ = [
    "_C_API", "error", "GreenletExit", "greenlet", "UnswitchableGreenlet",
    "getcurrent", "gettrace", "settrace", "enable_optional_cleanup",
    "get_clocks_used_doing_optional_cleanup", "get_total_main_greenlets",
    "get_pending_cleanup_count", "set_thread_local", "CLOCKS_PER_SEC",
    "GREENLET_USE_CONTEXT_VARS", "GREENLET_USE_GC", "GREENLET_USE_TRACING",
    "GREENLET_USE_STANDARD_THREADING",
]

class error(Exception): ...
class GreenletExit(BaseException): ...

TraceCallback: TypeAlias = Callable[
    [Literal["switch", "throw"], tuple[greenlet, greenlet]], object
]

class greenlet:
    error: ClassVar[type[Exception]]
    GreenletExit: ClassVar[type[BaseException]]
    run: Callable[..., Any]
    parent: greenlet | None
    gr_context: Context | None
    __dict__: dict[str, Any]

    def __init__(
        self,
        run: Callable[..., Any] | None = None,
        parent: greenlet | None = None,
    ) -> None: ...
    def switch(self, *args: Any, **kwargs: Any) -> Any: ...
    def throw(
        self,
        typ: type[BaseException] | BaseException = ...,
        val: object = None,
        tb: TracebackType | None = None,
        /,
    ) -> Any: ...
    def __bool__(self) -> bool: ...
    @property
    def dead(self) -> bool: ...
    @property
    def gr_frame(self) -> FrameType | None: ...
    @property
    def _stack_saved(self) -> int: ...
    @staticmethod
    def getcurrent() -> greenlet: ...
    @staticmethod
    def gettrace() -> TraceCallback | None: ...
    @staticmethod
    def settrace(callback: TraceCallback | None) -> TraceCallback | None: ...

class UnswitchableGreenlet(greenlet):
    force_switch_error: bool
    force_slp_switch_error: bool

def getcurrent() -> greenlet: ...
def gettrace() -> TraceCallback | None: ...
def settrace(callback: TraceCallback | None) -> TraceCallback | None: ...
def enable_optional_cleanup(flag: object) -> None: ...
def get_clocks_used_doing_optional_cleanup() -> int | None: ...
def get_total_main_greenlets() -> int: ...
def get_pending_cleanup_count() -> int: ...
def set_thread_local(key: object, value: object) -> None: ...

_C_API: Final[object]
CLOCKS_PER_SEC: Final[int]
GREENLET_USE_CONTEXT_VARS: Final[Literal[1]]
GREENLET_USE_GC: Final[Literal[1]]
GREENLET_USE_TRACING: Final[Literal[1]]
GREENLET_USE_STANDARD_THREADING: Final[Literal[1]]
