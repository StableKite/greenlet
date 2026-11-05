#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>

/* The probe only tests opaque POSIX directory handles and EOF, so it does not
 * depend on the build host's struct dirent layout or inspect its contents. */
typedef struct __dirstream ProbeDir;
extern ProbeDir *opendir(const char *);
extern void *readdir(ProbeDir *);
extern int closedir(ProbeDir *);

static PyObject *scan(PyObject *self, PyObject *args)
{
    (void)self;
    const char *path;
    int corrected;
    if (!PyArg_ParseTuple(args, "sp", &path, &corrected)) {
        return NULL;
    }
    ProbeDir *directory = opendir(path);
    if (!directory) {
        return PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    }
    Py_ssize_t count = 0;
    int detached_error = 0, operation_error = 0, attached_error = 0;
    for (;;) {
        errno = 0;
        PyThreadState *thread = PyEval_SaveThread();
        detached_error = errno;
        if (corrected) {
            errno = 0;
        }
        void *entry = readdir(directory);
        operation_error = errno;
        PyEval_RestoreThread(thread);
        attached_error = errno;
        if (!entry) {
            break;
        }
        ++count;
    }
    const int reported_error = corrected ? operation_error : attached_error;
    closedir(directory);
    return Py_BuildValue("niiii", count, detached_error, operation_error,
                         attached_error, reported_error);
}

static PyMethodDef methods[] = {
    {"scan", scan, METH_VARARGS, "Return real EOF errno observations around thread detach/attach."},
    {NULL, NULL, 0, NULL}
};
static PyModuleDef_Slot slots[] = {
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL}
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_listdir_errno_probe",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = slots
};
PyMODINIT_FUNC PyInit__listdir_errno_probe(void)
{
    return PyModuleDef_Init(&module);
}
