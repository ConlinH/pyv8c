#include "datetime.h"
#include "isolate_state.h"

static void clear_thread_datetime() {
    pyv8_icu_set_thread_timezone(nullptr);
    pyv8_icu_set_thread_locale(nullptr);
}

void pyv8_apply_thread_datetime(IsolateState* state) {
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
