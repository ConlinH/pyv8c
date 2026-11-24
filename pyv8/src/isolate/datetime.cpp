#include "datetime.h"
#include "isolate_state.h"

namespace {

constexpr int kDatetimeStackLimit = 8;

thread_local IsolateState* datetime_stack[kDatetimeStackLimit] = {};
thread_local int datetime_depth = 0;

void clear_thread_datetime() {
    pyv8_icu_set_thread_timezone(nullptr);
    pyv8_icu_set_thread_locale(nullptr);
}

void apply_thread_datetime(IsolateState* state) {
    if (state == nullptr || !state->host_datetime_configured()) {
        clear_thread_datetime();
        return;
    }
    if (pyv8_icu_set_thread_timezone(state->host_timezone().c_str()) != 0) {
        clear_thread_datetime();
        return;
    }
    if (pyv8_icu_set_thread_locale(state->host_locale().c_str()) != 0)
        clear_thread_datetime();
}

}  // namespace

bool pyv8_push_thread_datetime(IsolateState* state) {
    if (datetime_depth >= kDatetimeStackLimit)
        return false;
    datetime_stack[datetime_depth++] = state;
    apply_thread_datetime(state);
    return true;
}

void pyv8_pop_thread_datetime() {
    if (datetime_depth <= 0)
        return;
    --datetime_depth;
    if (datetime_depth == 0) {
        clear_thread_datetime();
        return;
    }
    apply_thread_datetime(datetime_stack[datetime_depth - 1]);
}

void pyv8_refresh_thread_datetime(IsolateState* state) {
    if (datetime_depth <= 0 || datetime_stack[datetime_depth - 1] != state)
        return;
    apply_thread_datetime(state);
}
