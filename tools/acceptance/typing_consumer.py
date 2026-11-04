"""Check the installed public types used by ordinary greenlet consumers."""

from contextvars import copy_context
from types import FrameType
from typing import Literal, assert_type

from greenlet import (
    GreenletExit,
    error,
    get_clocks_used_doing_optional_cleanup,
    getcurrent,
    gettrace,
    greenlet,
    settrace,
)

def record(event: Literal["switch", "throw"], pair: tuple[greenlet, greenlet]) -> None:
    assert_type(event, Literal["switch", "throw"])
    assert_type(pair[0], greenlet)

current = getcurrent()
assert_type(current, greenlet)
assert_type(current.parent, greenlet | None)
assert_type(current.gr_frame, FrameType | None)
assert_type(current.dead, bool)
assert_type(get_clocks_used_doing_optional_cleanup(), int | None)
def echo(value: int) -> int:
    return value

created = greenlet(echo, current)
created.gr_context = copy_context()
settrace(record)
previous = gettrace()
if previous is not None:
    previous("switch", (current, created))
settrace(None)
assert_type(GreenletExit(), GreenletExit)
assert_type(error(), error)
