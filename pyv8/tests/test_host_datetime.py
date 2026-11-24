import unittest

from pyv8c import pyv8


_WINTER = "new Date(Date.UTC(2026, 0, 15, 12))"
_SUMMER = "new Date(Date.UTC(2026, 6, 15, 12))"


class TestHostDatetime(unittest.TestCase):
    """每个 Isolate 持有自己的时区和语言，离开后回到本机默认。"""

    @staticmethod
    def _close_isolates(*isolates):
        for isolate in isolates:
            if isolate is not None and not isolate.closed:
                isolate.close()

    def test_isolates_keep_independent_zones_and_languages(self):
        isolate_a = pyv8.Isolate()
        isolate_b = pyv8.Isolate()
        context_a = context_b = None
        try:
            self.assertEqual(
                isolate_a.set_host_datetime("Asia/Shanghai", "zh-CN"),
                ("Asia/Shanghai", "zh-CN"),
            )
            self.assertEqual(
                isolate_b.set_host_datetime("America/New_York", "en-US"),
                ("America/New_York", "en-US"),
            )
            context_a = isolate_a.context(object)
            context_b = isolate_b.context(object)
            self.assertEqual(context_a.exec_js(f"{_WINTER}.getTimezoneOffset()"), -480)
            self.assertEqual(
                context_a.exec_js(f"{_WINTER}.toString()"),
                "Thu Jan 15 2026 20:00:00 GMT+0800 (中国标准时间)",
            )
            self.assertEqual(context_b.exec_js(f"{_WINTER}.getTimezoneOffset()"), 300)
            self.assertEqual(
                context_b.exec_js(f"{_WINTER}.toString()"),
                "Thu Jan 15 2026 07:00:00 GMT-0500 (Eastern Standard Time)",
            )
            self.assertEqual(context_b.exec_js(f"{_SUMMER}.getTimezoneOffset()"), 240)
            self.assertEqual(
                context_b.exec_js(f"{_SUMMER}.toString()"),
                "Wed Jul 15 2026 08:00:00 GMT-0400 (Eastern Daylight Time)",
            )
            self.assertEqual(
                context_a.exec_js(
                    "JSON.stringify([Intl.DateTimeFormat().resolvedOptions().timeZone,"
                    " Intl.DateTimeFormat().resolvedOptions().locale])"
                ),
                '["Asia/Shanghai","zh-CN"]',
            )
            self.assertEqual(context_a.exec_js(f"{_WINTER}.getTimezoneOffset()"), -480)
            self.assertEqual(
                isolate_a.set_host_datetime("Asia/Shanghai", "zh-CN"),
                ("Asia/Shanghai", "zh-CN"),
            )
            self.assertEqual(context_a.exec_js(f"{_WINTER}.toString()"),
                             "Thu Jan 15 2026 20:00:00 GMT+0800 (中国标准时间)")
        finally:
            for context in (context_a, context_b):
                if context is not None:
                    context.close()
            self._close_isolates(isolate_a, isolate_b)

    def test_aliases_canonicalize_and_invalid_values_keep_the_previous_zone(self):
        isolate = pyv8.Isolate()
        context = None
        try:
            self.assertEqual(
                isolate.set_host_datetime("US/Eastern", "zh_CN"),
                ("America/New_York", "zh-CN"),
            )
            self.assertEqual(isolate.set_host_datetime("Etc/UTC", "en-US"), ("UTC", "en-US"))
            self.assertEqual(isolate.set_host_datetime("Etc/GMT", "in"), ("UTC", "id"))
            self.assertEqual(isolate.set_host_datetime("UTC", "ja-JP")[0], "UTC")
            isolate.set_host_datetime("Asia/Shanghai", "zh-CN")
            for zone in ("", "Not/AZone", "GMT+08:00"):
                with self.assertRaisesRegex(ValueError, "unknown time zone"):
                    isolate.set_host_datetime(zone, "en-US")
            for language in ("", "***"):
                with self.assertRaisesRegex(ValueError, "invalid language tag"):
                    isolate.set_host_datetime("Europe/Paris", language)
            context = isolate.context(object)
            self.assertEqual(context.exec_js(f"{_WINTER}.getTimezoneOffset()"), -480)
            self.assertEqual(
                context.exec_js("Intl.DateTimeFormat().resolvedOptions().locale"),
                "zh-CN",
            )
        finally:
            del context
            self._close_isolates(isolate)

    def test_unconfigured_isolate_keeps_the_host_zone(self):
        bare = pyv8.Context(object)
        unconfigured = pyv8.Isolate()
        configured = pyv8.Isolate()
        plain = configured_context = None
        try:
            host = bare.exec_js("Intl.DateTimeFormat().resolvedOptions().timeZone")
            other = "Europe/Paris" if host == "Pacific/Auckland" else "Pacific/Auckland"
            plain = unconfigured.context(object)
            self.assertEqual(
                plain.exec_js("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                host,
            )
            configured.set_host_datetime(other, "en-US")
            configured_context = configured.context(object)
            self.assertEqual(
                configured_context.exec_js("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                other,
            )
            self.assertEqual(
                bare.exec_js("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                host,
            )
            self.assertEqual(
                plain.exec_js("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                host,
            )
        finally:
            del plain, configured_context, bare
            self._close_isolates(unconfigured, configured)
