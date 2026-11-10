/* Interpreter-owned Python references and native cleanup control. */
#ifndef T_GREENLET_GLOBALS
#define T_GREENLET_GLOBALS

#include <algorithm>
#include <atomic>
#include <memory>
#include <cstdint>
#include <ctime>
#include "greenlet_refs.hpp"
#include "greenlet_exceptions.hpp"
#include "greenlet_thread_support.hpp"
#include "greenlet_internal.hpp"

namespace greenlet {

class GreenletGlobals;
struct ThreadStateHandle {
    std::atomic<ThreadState*> state{nullptr};
    std::atomic<bool> constructing{true};
    bool dead = false;  // Protected by the owning cleanup mutex.
};

// Pure native serial, not a Python reference or an interpreter address cache.
static std::atomic<uint64_t> next_context_generation{1};

class GreenletGlobals : public std::enable_shared_from_this<GreenletGlobals> {
public:
    const int64_t interpreter_id;
    const uint64_t generation;
    std::atomic<bool> closing{false};
    bool ready = false;
    refs::OwnedObject greenlet_type;
    refs::OwnedObject unswitchable_type;
    refs::ImmortalEventName event_switch;
    refs::ImmortalEventName event_throw;
    refs::ImmortalException PyExc_GreenletError;
    refs::ImmortalException PyExc_GreenletExit;
    refs::ImmortalObject empty_tuple;
    refs::ImmortalObject empty_dict;
    refs::ImmortalString str_run;
    std::atomic<std::clock_t> clocks_used_doing_gc{0};
    std::atomic<Py_ssize_t> total_main_greenlets{0};
    void* c_api[PyGreenlet_API_pointers]{};
    Mutex cleanup_mutex;
    Mutex* const thread_states_to_destroy_lock;
    std::vector<std::shared_ptr<ThreadStateHandle>> handles;
    size_t pending_count = 0;
    bool pending_call = false;

    explicit GreenletGlobals(int64_t id) :
        interpreter_id(id), generation(next_context_generation.fetch_add(1)),
        event_switch("switch"), event_throw("throw"),
        PyExc_GreenletError("greenlet.error"),
        PyExc_GreenletExit("greenlet.GreenletExit", PyExc_BaseException),
        empty_tuple(Require(PyTuple_New(0))), empty_dict(Require(PyDict_New())),
        str_run("run"),
        thread_states_to_destroy_lock(&cleanup_mutex)
    {}

    // The interpreter capsule clears every Python field before the last native
    // TLS reference can disappear. Late native destruction sees null fields.
    void close() noexcept;
};

// One GC anchor owns the Python references even when multiple module views
// share the same native execution domain. Module traversals visit this anchor,
// never the same native fields a second time. The interpreter capsule supplies
// a finalization root and closes references before native TLS can outlive them.
struct ContextAnchor {
    PyObject_HEAD
    std::shared_ptr<GreenletGlobals>* owner;
};

static int context_anchor_traverse(ContextAnchor* self, visitproc visit, void* arg)
{
    Py_VISIT(Py_TYPE(self));
    if (!self->owner) {
        return 0;
    }
    auto* context = self->owner->get();
    Py_VISIT(context->greenlet_type.borrow());
    Py_VISIT(context->unswitchable_type.borrow());
    Py_VISIT(context->event_switch.borrow());
    Py_VISIT(context->event_throw.borrow());
    Py_VISIT(context->PyExc_GreenletError.borrow());
    Py_VISIT(context->PyExc_GreenletExit.borrow());
    Py_VISIT(context->empty_tuple.borrow());
    Py_VISIT(context->empty_dict.borrow());
    Py_VISIT(context->str_run.borrow());
    return 0;
}

static int context_anchor_clear(ContextAnchor* self)
{
    // Detach before close() can release types and re-enter Python GC. The
    // local holder keeps the native domain alive throughout those callbacks.
    std::unique_ptr<std::shared_ptr<GreenletGlobals>> owner(self->owner);
    self->owner = nullptr;
    if (owner) {
        (*owner)->close();
    }
    return 0;
}

static void context_anchor_dealloc(ContextAnchor* self)
{
    PyObject_GC_UnTrack(self);
    context_anchor_clear(self);
    PyTypeObject* type = Py_TYPE(self);
    type->tp_free(reinterpret_cast<PyObject*>(self));
    Py_DECREF(type);
}

static PyObject* create_context_anchor(const std::shared_ptr<GreenletGlobals>& owner)
{
    static PyType_Slot slots[] = {
        {Py_tp_dealloc, (void*)context_anchor_dealloc},
        {Py_tp_traverse, (void*)context_anchor_traverse},
        {Py_tp_clear, (void*)context_anchor_clear},
        {0, nullptr}
    };
    PyType_Spec spec = {"greenlet._greenlet._InterpreterContext", sizeof(ContextAnchor), 0,
        Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_IMMUTABLETYPE, slots};
    refs::NewReference type(Require(PyType_FromSpec(&spec)));
    auto* anchor = reinterpret_cast<ContextAnchor*>(Require(
        reinterpret_cast<PyTypeObject*>(type.borrow())->tp_alloc(
            reinterpret_cast<PyTypeObject*>(type.borrow()), 0)));
    try {
        anchor->owner = new std::shared_ptr<GreenletGlobals>(owner);
    }
    catch (...) {
        Py_DECREF(anchor);
        throw;
    }
    return reinterpret_cast<PyObject*>(anchor);
}

struct ContextRoot {
    std::shared_ptr<GreenletGlobals> owner;
    PyObject* anchor = nullptr;
    explicit ContextRoot(std::shared_ptr<GreenletGlobals> value) : owner(std::move(value)) {}
    ~ContextRoot() {
        owner->close();
        if (anchor) {
            // A surviving/immortalized heap type can retain the Python
            // anchor after interpreter shutdown. Release its native holder
            // at the actual owner lifetime boundary, not only tp_dealloc.
            context_anchor_clear(reinterpret_cast<ContextAnchor*>(anchor));
        }
        Py_XDECREF(anchor);
    }
};

static thread_local GreenletGlobals* closing_context = nullptr;
struct GlobalsCache {
    PyInterpreterState* interpreter = nullptr;
    int64_t interpreter_id = -1;
    std::shared_ptr<GreenletGlobals> owner;
};
static thread_local GlobalsCache globals_cache;
static const char* const globals_key = "greenlet.interpreter_context.315t.v1";
static const char* const globals_capsule_name = "greenlet.interpreter_context";

static void globals_capsule_destroy(PyObject* capsule) noexcept
{
    auto* holder = static_cast<ContextRoot*>(
        PyCapsule_GetPointer(capsule, globals_capsule_name));
    if (!holder) {
        PyErr_Clear();
        return;
    }
    delete holder;
}

static GreenletGlobals* GetGlobals() noexcept
{
    if (closing_context) {
        return closing_context;
    }
    PyThreadState* thread = PyThreadState_GetUnchecked();
    if (!thread) {
        return nullptr;
    }
    PyInterpreterState* interpreter = PyThreadState_GetInterpreter(thread);
    const int64_t id = PyInterpreterState_GetID(interpreter);
    if (globals_cache.interpreter == interpreter && globals_cache.interpreter_id == id) {
        const auto& owner = globals_cache.owner;
        if (owner) {
            return owner.get();
        }
    }
    PyObject* dictionary = PyInterpreterState_GetDict(interpreter);
    if (!dictionary) {
        return nullptr;
    }
    PyObject* capsule = PyDict_GetItemString(dictionary, globals_key);
    if (!capsule || !PyCapsule_IsValid(capsule, globals_capsule_name)) {
        return nullptr;
    }
    auto* holder = static_cast<ContextRoot*>(
        PyCapsule_GetPointer(capsule, globals_capsule_name));
    if (!holder) {
        PyErr_Clear();
        return nullptr;
    }
    globals_cache.interpreter = interpreter;
    globals_cache.interpreter_id = id;
    globals_cache.owner = holder->owner;
    return holder->owner.get();
}

static std::shared_ptr<GreenletGlobals> GetOrCreateGlobals()
{
    if (GreenletGlobals* existing = GetGlobals()) {
        if (!existing->closing.load()) {
            return existing->shared_from_this();
        }
        // Py_Finalize/Py_Initialize can reuse main's interpreter address and
        // numeric ID. The closed native TLS cache belongs to the old lifetime.
        if (Py_IsFinalizing()) {
            throw PyErrOccurred(PyExc_RuntimeError, "greenlet is being finalized");
        }
        auto old_owner = existing->shared_from_this();
        PyInterpreterState* interpreter = PyThreadState_GetInterpreter(PyThreadState_Get());
        PyObject* dictionary = PyInterpreterState_GetDict(interpreter);
        PyObject* old_capsule = PyDict_GetItemString(dictionary, globals_key);
        if (old_capsule && PyCapsule_IsValid(old_capsule, globals_capsule_name)) {
            auto* holder = static_cast<ContextRoot*>(
                PyCapsule_GetPointer(old_capsule, globals_capsule_name));
            if (holder && holder->owner.get() == existing) {
                Require(PyDict_DelItemString(dictionary, globals_key));
            }
        }
        globals_cache.owner.reset();
    }
    PyInterpreterState* interpreter = PyThreadState_GetInterpreter(PyThreadState_Get());
    refs::NewReference key(Require(PyUnicode_FromString(globals_key)));
    auto owner = std::make_shared<GreenletGlobals>(PyInterpreterState_GetID(interpreter));
    std::unique_ptr<ContextRoot> holder(new ContextRoot(owner));
    refs::NewReference capsule(Require(PyCapsule_New(holder.get(), globals_capsule_name, globals_capsule_destroy)));
    holder.release();
    PyObject* installed = nullptr;
#if GREENLET_PY313
    int result = PyDict_SetDefaultRef(PyInterpreterState_GetDict(interpreter), key.borrow(), capsule.borrow(), &installed);
    Require(result);
#else
    installed = Require(PyDict_SetDefault(PyInterpreterState_GetDict(interpreter), key.borrow(), capsule.borrow()));
    Py_INCREF(installed);
#endif
    refs::NewReference installed_owner(installed);
    auto* installed_holder = static_cast<ContextRoot*>(
        PyCapsule_GetPointer(installed, globals_capsule_name));
    if (!installed_holder) {
        throw PyErrOccurred();
    }
    owner = installed_holder->owner;
    globals_cache.interpreter = interpreter;
    globals_cache.interpreter_id = owner->interpreter_id;
    globals_cache.owner = owner;
    return owner;
}

static PyObject* GetContextAnchor(const std::shared_ptr<GreenletGlobals>& owner)
{
    PyInterpreterState* interpreter = PyThreadState_GetInterpreter(PyThreadState_Get());
    PyObject* capsule = Require(PyDict_GetItemString(PyInterpreterState_GetDict(interpreter), globals_key));
    auto* root = static_cast<ContextRoot*>(PyCapsule_GetPointer(capsule, globals_capsule_name));
    if (!root) {
        throw PyErrOccurred();
    }
    if (!root->anchor) {
        root->anchor = create_context_anchor(owner);
    }
    return root->anchor;
}

bool IsCurrentInterpreterClosing() noexcept
{
    GreenletGlobals* globals = GetGlobals();
    return globals && globals->closing.load(std::memory_order_acquire);
}

PyTypeObject* GetGreenletType() noexcept
{
    GreenletGlobals* globals = GetGlobals();
    return globals ? reinterpret_cast<PyTypeObject*>(globals->greenlet_type.borrow()) : nullptr;
}

void MarkThreadStateDead(const std::shared_ptr<GreenletGlobals>& owner,
                         const std::shared_ptr<ThreadStateHandle>& handle) noexcept;
void DrainDeadThreadStates(GreenletGlobals* owner);

} // namespace greenlet

#define mod_globs (greenlet::GetGlobals())
#endif
