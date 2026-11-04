#ifndef GREENLET_THREAD_STATE_CREATOR_HPP
#define GREENLET_THREAD_STATE_CREATOR_HPP

#include "TGreenletGlobals.cpp"
#include "TThreadState.hpp"

namespace greenlet {
class ThreadStateCreator {
    struct Entry {
        std::shared_ptr<GreenletGlobals> owner;
        std::shared_ptr<ThreadStateHandle> handle;
    };
    std::vector<Entry> entries;
    bool draining = false;
    G_NO_COPIES_OF_CLS(ThreadStateCreator);
public:
    ThreadStateCreator() = default;
    ~ThreadStateCreator()
    {
        for (auto& entry : entries) {
            MarkThreadStateDead(entry.owner, entry.handle);
        }
    }

    ThreadState& state()
    {
        GreenletGlobals* globals = GetGlobals();
        if (!globals || globals->closing.load() || !globals->ready) {
            throw PyErrOccurred(PyExc_RuntimeError, "greenlet interpreter context is unavailable");
        }
        Entry* found = nullptr;
        for (auto it = entries.begin(); it != entries.end();) {
            if (it->owner->closing.load()) {
                it = entries.erase(it);
                continue;
            }
            if (it->owner.get() == globals && it->owner->generation == globals->generation) {
                found = &*it;
            }
            ++it;
        }
        if (!found) {
            auto owner = globals->shared_from_this();
            auto handle = std::make_shared<ThreadStateHandle>();
            entries.push_back({owner, handle});
            try {
                {
                    LockGuard lock(owner->cleanup_mutex);
                    owner->handles.push_back(handle);
                }
                ThreadState* state = new ThreadState(owner);
                handle->state.store(state, std::memory_order_release);
                handle->constructing.store(false, std::memory_order_release);
            }
            catch (...) {
                handle->constructing.store(false, std::memory_order_release);
                {
                    LockGuard lock(owner->cleanup_mutex);
                    auto it = std::find(owner->handles.begin(), owner->handles.end(), handle);
                    if (it != owner->handles.end()) {
                        owner->handles.erase(it);
                    }
                }
                entries.pop_back();
                throw;
            }
            found = &entries.back();
        }
        ThreadState* state = found->handle->state.load(std::memory_order_acquire);
        if (!state) {
            throw PyErrOccurred(PyExc_RuntimeError, "recursive greenlet thread-state initialization");
        }
        return *state;
    }

    // Destruction may run GC, Python finalizers and arbitrary greenlet
    // switches. Only explicit public API boundaries may drain; state() is
    // also used between publishing switch arguments and saving the C stack.
    void drain_dead_states()
    {
        GreenletGlobals* globals = GetGlobals();
        if (globals && !globals->closing.load() && !draining) {
            draining = true;
            try {
                DrainDeadThreadStates(globals);
            }
            catch (...) {
                draining = false;
                throw;
            }
            draining = false;
        }
    }

    operator ThreadState&() { return state(); }
    operator ThreadState*() { return &state(); }
    int tp_traverse(visitproc visit, void* arg)
    {
        GreenletGlobals* globals = GetGlobals();
        for (auto& entry : entries) {
            if (entry.owner.get() == globals) {
                ThreadState* state = entry.handle->state.load();
                return state ? state->tp_traverse(visit, arg) : 0;
            }
        }
        return 0;
    }
};
} // namespace greenlet
#endif
