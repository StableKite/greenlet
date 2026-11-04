#ifndef T_THREADSTATE_DESTROY
#define T_THREADSTATE_DESTROY

#include "TGreenlet.hpp"
#include "TGreenletGlobals.cpp"
#include "TThreadState.hpp"
#include "TThreadStateCreator.hpp"

namespace greenlet {
static int PendingCallback_DestroyQueue(void* argument)
{
    auto* owner = static_cast<GreenletGlobals*>(argument);
    // Py_AddPendingCall runs in main only. Never route child references here.
    if (!owner->closing.load()) {
        DrainDeadThreadStates(owner);
    }
    return 0;
}

void MarkThreadStateDead(const std::shared_ptr<GreenletGlobals>& owner,
                         const std::shared_ptr<ThreadStateHandle>& handle) noexcept
{
    LockGuard lock(owner->cleanup_mutex);
    ThreadState* state = handle->state.load();
    if (owner->closing.load() || !state || handle->dead) {
        return;
    }
    state->mark_main_greenlet_dead();
    handle->dead = true;
    ++owner->pending_count;
    if (owner->interpreter_id == 0 && !owner->pending_call && !Py_IsFinalizing()) {
        owner->pending_call = true;
        if (Py_AddPendingCall(PendingCallback_DestroyQueue, owner.get()) < 0) {
            owner->pending_call = false;
        }
    }
}

void DrainDeadThreadStates(GreenletGlobals* owner)
{
    for (;;) {
        ThreadState* state = nullptr;
        {
            LockGuard lock(owner->cleanup_mutex);
            if (owner->closing.load()) {
                return;
            }
            for (auto it = owner->handles.begin(); it != owner->handles.end(); ++it) {
                if ((*it)->dead || (!(*it)->constructing.load() && !(*it)->state.load())) {
                    state = (*it)->state.exchange(nullptr);
                    owner->handles.erase(it);
                    if (state) {
                        --owner->pending_count;
                    }
                    break;
                }
            }
            if (!state) {
                owner->pending_call = false;
                return;
            }
        }
        delete state;
    }
}

void GreenletGlobals::close() noexcept
{
    std::vector<std::shared_ptr<ThreadStateHandle>> closing_handles;
    {
        LockGuard lock(cleanup_mutex);
        if (closing.exchange(true)) {
            return;
        }
        handles.swap(closing_handles);
        pending_count = 0;
    }
    GreenletGlobals* previous = closing_context;
    closing_context = this;
    // This runs attached to the owner interpreter before its last GC. Native
    // TLS handles are invalidated before Python decrefs can invoke callbacks.
    for (auto& handle : closing_handles) {
        ThreadState* state = handle->state.exchange(nullptr);
        if (state) {
            state->close_interpreter_state();
            delete state;
        }
    }
    greenlet_type.CLEAR();
    unswitchable_type.CLEAR();
    event_switch.CLEAR();
    event_throw.CLEAR();
    PyExc_GreenletError.CLEAR();
    PyExc_GreenletExit.CLEAR();
    empty_tuple.CLEAR();
    empty_dict.CLEAR();
    str_run.CLEAR();
    closing_context = previous;
}
} // namespace greenlet

static thread_local greenlet::ThreadStateCreator g_thread_state_global;
#define GET_THREAD_STATE() g_thread_state_global
#endif
