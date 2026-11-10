# -*- coding: utf-8 -*-
"""
The root of the greenlet package.
"""

__all__ = [
    '__version__',
    '_C_API',

    'GreenletExit',
    'error',

    'getcurrent',
    'greenlet',

    'gettrace',
    'settrace',
]

# pylint:disable=no-name-in-module

###
# Metadata
###
__version__ = '3.5.7.dev1+stablekite.3'
from ._greenlet import _C_API # pylint:disable=no-name-in-module

###
# Exceptions
###
from ._greenlet import GreenletExit
from ._greenlet import error

###
# greenlets
###
from ._greenlet import getcurrent
from ._greenlet import greenlet

###
# tracing
###
from ._greenlet import gettrace
from ._greenlet import settrace

###
# Constants
# These constants aren't documented and aren't recommended.
# In 1.0, USE_GC and USE_TRACING are always true, and USE_CONTEXT_VARS
# is the same as ``sys.version_info[:2] >= 3.7``
###
from ._greenlet import GREENLET_USE_CONTEXT_VARS as _use_context_vars
from ._greenlet import GREENLET_USE_GC as _use_gc
from ._greenlet import GREENLET_USE_TRACING as _use_tracing
GREENLET_USE_CONTEXT_VARS = _use_context_vars
GREENLET_USE_GC = _use_gc
GREENLET_USE_TRACING = _use_tracing
del _use_context_vars, _use_gc, _use_tracing

# Controlling the use of the gc module. Provisional API for this greenlet
# implementation in 2.0.
from ._greenlet import CLOCKS_PER_SEC as _clocks_per_sec
from ._greenlet import enable_optional_cleanup as _enable_cleanup
from ._greenlet import get_clocks_used_doing_optional_cleanup as _cleanup_clocks
CLOCKS_PER_SEC = _clocks_per_sec
enable_optional_cleanup = _enable_cleanup
get_clocks_used_doing_optional_cleanup = _cleanup_clocks
del _clocks_per_sec, _enable_cleanup, _cleanup_clocks

# Other APIS in the _greenlet module are for test support.
