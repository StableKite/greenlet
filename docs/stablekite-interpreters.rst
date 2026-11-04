StableKite interpreter-state engineering candidate
================================================

This development version implements interpreter-local execution domains and
PEP 489 module initialization. It is a candidate for review, not a completed
project release. Platform CI, default-branch installation and publication have
their own acceptance records.

Ownership and compatibility
---------------------------

Each interpreter owns its constants, exceptions, native C API table, type
references, counters and native thread cleanup registry. A single GC anchor
traverses the Python references. Module instances reference that anchor rather
than traversing the same native-owned references twice. An interpreter lifetime
root clears Python state while its owner is still attached; later native TLS
destructors see invalidated handles and closed native control blocks.

Main-interpreter classes remain static to preserve existing compiled static
C subclasses. Child interpreters construct separate immutable heap classes.
The public header, twelve table slots, signatures and object layout are
unchanged. A consumer supporting multiple interpreters must keep its imported
C API table in its own module state. The historical header's default static
import variable does not automatically make an existing consumer safe for
concurrent interpreter use. The acceptance C client exercises the unchanged
header macros through a module-state alias; the older compiled static subclass
is tested separately in main.

Native TLS entries carry an execution-domain generation. Python references
from a child are never routed to the main interpreter's pending-call handler.
Normal Python atexit callbacks retain working APIs. After the interpreter
context has actually closed, API entry points translate unavailable-context
errors into Python exceptions. No GIL selection or loader-safety override is
used.

The optional cleanup API releases only references owned by the implementation
and invokes real cycle GC. It does not infer ownership from gc.get_referrers.
Legitimate native C references remain alive until their owners release them.

Acceptance boundaries
---------------------

Local Windows and Linux CPython 3.15.2t records include all 172 original test
IDs, separate real child-first/main-first imports, parallel child interpreters,
threads, tracing, context variables, subclass cycles, a module-state C client,
actual allocation-failure recovery and child teardown with suspended stacks.
The unchanged compiled static C subclass and repeated embedded runtime
initialization/finalization are main-interpreter checks. They do not replace
actual child-interpreter checks.

The runtime package and native API stub are checked with strict Pyright;
positive consumers and three required negative controls are separate gates.
Explicit Any occurs at the native variadic switch/run boundary, whose values
can be supplied by arbitrary cooperating greenlets. It is not an assertion
that switch always returns the callable's final return type.

The five-platform validation workflow preserves compiler outputs, original
wheels/source distributions, RECORD checks, source hashes, command exit codes
and raw logs. It selects the newest stable free-threaded CPython, requests JIT,
checks JIT when available and verifies natural GIL OFF with flags.gil=None.
Five remote results, the release/publish job, protected-index readback and
ordinary default-branch pip delivery are required before release acceptance.
The current workflow supplies the platform validation portion; release
automation remains an explicit open delivery task until implemented and run.
