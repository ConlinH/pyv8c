#ifndef PYV8_ISOLATE_DATETIME_H
#define PYV8_ISOLATE_DATETIME_H

// Thread-local ICU overrides compiled into the V8 monolith. A clean V8
// checkout does not contain them; replay pyv8/patches/icu-thread-datetime.patch
// before rebuilding v8_monolith. The extension never includes an ICU header.

class IsolateState;

extern "C" {
int pyv8_icu_canonicalize_timezone(const char* input, char* output, int output_size);
int pyv8_icu_set_thread_timezone(const char* iana);
int pyv8_icu_canonicalize_locale(const char* input, char* output, int output_size);
int pyv8_icu_set_thread_locale(const char* tag);
}

// Fixed depth so a push after Isolate::Enter cannot throw and leak the lock.
// A full stack returns false and leaves the current override in place.
bool pyv8_push_thread_datetime(IsolateState* state);
void pyv8_pop_thread_datetime();
void pyv8_refresh_thread_datetime(IsolateState* state);

#endif
