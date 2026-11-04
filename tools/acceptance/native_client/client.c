
#include <Python.h>
#include "greenlet.h"
typedef struct {
    void** api;
    PyObject* capsule;
    PyObject* Client;
    Py_ssize_t allocations;
    int failed;
} State;
static State* state(PyObject* module) { return (State*)PyModule_GetState(module); }
// The historical header's process-static import variable cannot represent a
// multi-interpreter consumer. Its unchanged macros expand through this local
// module-state alias; the table layout and every public API signature remain.
#define _PyGreenlet_API (state(module)->api)

static PyObject* bind_api(PyObject* module, PyObject* unused) {
    State* s = state(module);
    if (s->api) Py_RETURN_NONE;
    PyObject* package = PyImport_ImportModule("greenlet");
    if (!package) return NULL;
    PyObject* capsule = PyObject_GetAttrString(package, "_C_API");
    Py_DECREF(package);
    if (!capsule) return NULL;
    void** api = (void**)PyCapsule_GetPointer(capsule, "greenlet._C_API");
    if (!api) { Py_DECREF(capsule); return NULL; }
    s->api = api;
    s->capsule = capsule;
    PyType_Slot slots[] = {{0, NULL}};
    PyType_Spec spec = {"_greenlet_si_client.Client", sizeof(PyGreenlet), 0,
        Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, slots};
    PyObject* bases = PyTuple_Pack(1, &PyGreenlet_Type);
    if (!bases) return NULL;
    PyObject* type = PyType_FromModuleAndSpec(module, &spec, bases);
    Py_DECREF(bases);
    if (!type) return NULL;
    s->Client = type;
    if (PyModule_AddObjectRef(module, "Client", type) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject* run_api(PyObject* module, PyObject* callable) {
    if (!state(module)->api) { PyErr_SetString(PyExc_RuntimeError, "bind API first"); return NULL; }
    PyGreenlet* main = PyGreenlet_GetCurrent();
    if (!main) return NULL;
    if (!PyGreenlet_MAIN(main) || !PyGreenlet_STARTED(main) || !PyGreenlet_ACTIVE(main)) {
        Py_DECREF(main); PyErr_SetString(PyExc_AssertionError, "main C API flags"); return NULL;
    }
    PyGreenlet* parent = PyGreenlet_GetParent(main);
    if (parent || PyErr_Occurred()) { Py_XDECREF(parent); Py_DECREF(main); return NULL; }
    PyGreenlet* child = PyGreenlet_New(callable, main);
    if (!child) { Py_DECREF(main); return NULL; }
    if (PyGreenlet_MAIN(child) || PyGreenlet_STARTED(child) || PyGreenlet_ACTIVE(child)) {
        Py_DECREF(child); Py_DECREF(main); PyErr_SetString(PyExc_AssertionError, "new child flags"); return NULL;
    }
    if (PyGreenlet_SetParent(child, main) < 0) { Py_DECREF(child); Py_DECREF(main); return NULL; }
    parent = PyGreenlet_GetParent(child);
    if (parent != main) { Py_XDECREF(parent); Py_DECREF(child); Py_DECREF(main);
        PyErr_SetString(PyExc_AssertionError, "parent C API"); return NULL; }
    Py_DECREF(parent);
    PyObject* result = PyGreenlet_Switch(child, NULL, NULL);
    if (result && (!PyGreenlet_STARTED(child) || PyGreenlet_ACTIVE(child))) {
        Py_DECREF(result); result = NULL; PyErr_SetString(PyExc_AssertionError, "finished flags");
    }
    Py_DECREF(child);
    Py_DECREF(main);
    return result;
}

static PyObject* throw_api(PyObject* module, PyObject* callable) {
    PyGreenlet* child = PyGreenlet_New(callable, NULL);
    if (!child) return NULL;
    PyObject* result = PyGreenlet_Throw(child, PyExc_ValueError, NULL, NULL);
    Py_DECREF(child);
    return result;
}

typedef struct { PyMemAllocatorEx previous; } Allocator;
typedef struct { Py_ssize_t fail_at, count; int hit, active; } Failure;
#if defined(_MSC_VER)
__declspec(thread) static Failure failure;
#else
static _Thread_local Failure failure;
#endif
static int fail_now(void) {
    if (!failure.active) return 0;
    ++failure.count;
    if (failure.count == failure.fail_at) { failure.hit = 1; return 1; }
    return 0;
}
static void* allocate(void* ctx, size_t size) {
    Allocator* a = (Allocator*)ctx;
    return fail_now() ? NULL : a->previous.malloc(a->previous.ctx, size);
}
static void* zero_allocate(void* ctx, size_t n, size_t size) {
    Allocator* a = (Allocator*)ctx;
    return fail_now() ? NULL : a->previous.calloc(a->previous.ctx, n, size);
}
static void* reallocate(void* ctx, void* ptr, size_t size) {
    Allocator* a = (Allocator*)ctx;
    return fail_now() ? NULL : a->previous.realloc(a->previous.ctx, ptr, size);
}
static void release(void* ctx, void* ptr) {
    Allocator* a = (Allocator*)ctx;
    a->previous.free(a->previous.ctx, ptr);
}

static PyObject* exec_with_failure(PyObject* module, PyObject* args) {
    PyObject* target;
    Py_ssize_t fail_at;
    if (!PyArg_ParseTuple(args, "On", &target, &fail_at)) return NULL;
    PyModuleDef* definition = PyModule_GetDef(target);
    if (!definition) return NULL;
    Allocator a[2];
    PyMemAllocatorDomain domains[2] = {PYMEM_DOMAIN_MEM, PYMEM_DOMAIN_OBJ};
    for (int i = 0; i < 2; ++i) {
        PyMem_GetAllocator(domains[i], &a[i].previous);
        PyMemAllocatorEx wrapper = {&a[i], allocate, zero_allocate, reallocate, release};
        PyMem_SetAllocator(domains[i], &wrapper);
    }
    failure = (Failure){fail_at, 0, 0, 1};
    int result = PyModule_ExecDef(target, definition);
    failure.active = 0;
    for (int i = 0; i < 2; ++i) PyMem_SetAllocator(domains[i], &a[i].previous);
    state(module)->allocations = failure.count;
    state(module)->failed = failure.hit;
    if (result < 0) return NULL;
    Py_RETURN_NONE;
}
static PyObject* failure_counts(PyObject* module, PyObject* unused) {
    return Py_BuildValue("ni", state(module)->allocations, state(module)->failed);
}
static int traverse(PyObject* module, visitproc visit, void* arg) {
    Py_VISIT(state(module)->capsule); Py_VISIT(state(module)->Client); return 0;
}
static int clear(PyObject* module) {
    state(module)->api = NULL; Py_CLEAR(state(module)->capsule); Py_CLEAR(state(module)->Client); return 0;
}
static void finish(void* module) { clear((PyObject*)module); }
static int execute(PyObject* module) { return 0; }
static PyMethodDef methods[] = {
    {"bind", bind_api, METH_NOARGS, NULL}, {"run", run_api, METH_O, NULL},
    {"throw", throw_api, METH_O, NULL}, {"exec_with_failure", exec_with_failure, METH_VARARGS, NULL},
    {"failure_counts", failure_counts, METH_NOARGS, NULL}, {NULL}};
static PyModuleDef_Slot slots[] = {{Py_mod_exec, execute},
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED}, {0}};
static PyModuleDef definition = {PyModuleDef_HEAD_INIT, "_greenlet_si_client", NULL,
    sizeof(State), methods, slots, traverse, clear, finish};
PyMODINIT_FUNC PyInit__greenlet_si_client(void) { return PyModuleDef_Init(&definition); }
