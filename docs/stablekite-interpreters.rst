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

Frames retained after owner-thread exit
--------------------------------------

An external Python frame returned by ``gr_frame`` must survive destruction of
the suspended greenlet after its native owner thread exits. Abandoned interpreter
frames now transfer their references into the materialized Python frame objects
before their native stack chunks are released. Deferred stack references become
owning heap references. Frame critical sections protect the ownership transition
from concurrent readers, and the top traversal root remains visible during it.
The regression check reads code, back frames and locals concurrently in main and
real child interpreters, then verifies collection after the last frame is freed.

For older eval loops, the Python FASTCALL boundary publishes the actual caller
operand stack while its C call is suspended. Dead-thread draining occurs at the
explicit public getcurrent boundary, rather than inside internal TLS lookup where
arbitrary finalizers could invalidate an in-progress switch. The twelve-slot
capsule ABI continues to accept the original borrowed tuple/dictionary arguments.

The original 172 test identities and their assertions remain. Historical
expected-leak decorators on the affected cleanup and dead-thread-throw cases are
removed because the corrected ownership must collect them; these cases now require
no leak. Separate complete suites on CPython 3.10, 3.11 and 3.12 qualify backward
compatibility, independently of CPython 3.15t acceptance. Original failing CI runs
and native retained-frame crash controls are retained in the evidence archive.

CPython 3.14 free-threaded builds do not export the collector's frame-stack
visitors used by 3.15. While a 3.14 stack is suspended, deferred frame references
therefore receive explicit owning pins and already owning frame references are
visited directly. These pins move back to the running-thread domain on resume
and are released after frame ownership is transferred on abandonment. This
compatibility path is qualified separately from the newest-runtime policy.

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
and raw logs. Linux wheels receive measured auditwheel ABI tags, keeping the raw
originals. A separate fresh environment installs each final distributable and
checks all original tests, RECORD, real SI and parallel retained-frame readers.
The qualified wheel and canonical source archive are recorded with SHA256 in
PUBLISH_ARTIFACTS.json; this manifest does not claim a completed upload.
The workflow selects the newest stable free-threaded CPython, requests JIT,
checks JIT when available and verifies natural GIL OFF with flags.gil=None.
Five remote results, the release/publish job, protected-index readback and
ordinary default-branch pip delivery are required before release acceptance.

The sixth job depends on all five successful platform validations. Its canonical
version gate produces a successful validation-only marker for an unchanged-version
push, without devpi authentication, artifact download for publication, upload or
readback. Version changes and manual publication are restricted to the exact
default branch. Publication verifies five distinct ABI-qualified wheels, one
canonical source archive, their RECORD checksums and provenance, then installs
the glibc wheel in a fresh consumer with the original-header C client and both
real interpreter import orders.

Before any devpi credentials are made available, the original ``tests.yml``
push workflow must succeed on the same repository, default branch and commit.
The publisher waits at most 30 minutes for that workflow. A failed, missing or
still-pending result withholds publication; an older successful attempt cannot
replace a newer failed or pending attempt. The checked run and attempt are
recorded in ``ORIGINAL_COMPATIBILITY.json``, which every later publication phase
requires. Unchanged-version validation skips this publication-only gate too.

The canonical endpoint is ``https://devpi.stablekite.com/StableKite/index/``.
``DEVPI_READ_TOKEN`` must be a separately authorized credential for authenticated
collision checks and post-upload reads. ``DEVPI_TOKEN`` is confined to the upload
step. Missing credentials, unconfirmed identity, version collisions, redirects,
TLS failures or foreign URLs fail the job. Each upload receives one attempt;
post-upload confirmation downloads all six files and compares the exact bytes.
Offline failure controls never contact devpi. Implemented automation alone does
not qualify a release: successful exact-head CI, actual protected-index readback
and default-branch installation evidence remain required.


Unified shutdown and sanitizer integration
-----------------------------------------

The unified candidate includes the separate shutdown and ASan fiber work.
Native deletion queues already transfer ownership under their mutex and run
callbacks after releasing it. Per-interpreter ContextRoot.close() invalidates
all native handles and clears states attached to the owner before teardown;
the legacy process-wide cleanup capsule is superseded by this existing owner.
The fallback shutdown destructor clears a current main greenlet directly.
Final frame disposal also releases the native snapshot buffer capacity, while
the 3.14 suspended-frame owning pins keep their previous lifetime guard.

On Linux, GREENLET_ASAN_FIBERS=1 enables sanitizer fiber notifications for
an instrumented build. Each logical stack retains its own fake-stack handle;
the physical native stack bounds are thread-local C data. The hook uses the
const current-greenlet getter: queue maintenance cannot run after PythonState
has been saved for the low-level stack switch. Normal builds omit the hooks.

The original 172 test identities remain, with a finished-greenlet shutdown
regression and four additional public fiber scenarios. These source changes
require fresh native and sanitizer qualification. They do not close the
separate stock-musl CPython errno defect or imply a completed publication.


The context anchor releases its native shared-owner holder during tp_clear
and explicit ContextRoot shutdown. The holder is detached before callbacks,
then retained locally until close() completes. A heap type may retain its
Python anchor beyond interpreter cleanup, so native ownership cannot depend
solely on the anchor's tp_dealloc. The fully instrumented predecessor showed
a 16-byte holder leak after ordinary import; this correction requires fresh
strict ASan/UBSan/LSan and native lifetime qualification.
