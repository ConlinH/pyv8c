"""堆上限与外部内存记账；上限行为在独立进程里验证，越界终止脚本而不是进程。"""

import gc
import ctypes
import subprocess
import sys
import textwrap
import unittest

from pyv8c import pyv8

#: 必须返回数组并由 Python 持有：返回数字会被 V8 当作无用分配消掉，测不到堆上限。
HOLD_STRINGS = (
    "(()=>{const c=[];for(let i=0;i<%(count)d;i++)"
    "c.push(String.fromCharCode(120).repeat(1024));return c;})()"
)


class TestHeapLimits(unittest.TestCase):
    """验证有界堆增长与外部内存记账；上限用 16MB，默认上限撞不到。"""

    def test_the_context_reports_its_isolate_heap_statistics(self):
        """堆统计：三个字段都是字节，上限与提交量大于 0，使用量不超过提交量。"""
        context = pyv8.Context(object)
        statistics = context.heap_statistics()
        self.assertEqual(sorted(statistics), ["limit", "total", "used"])
        self.assertGreater(statistics["limit"], 0)
        self.assertGreater(statistics["total"], 0)
        self.assertGreaterEqual(statistics["total"], statistics["used"])

    def run_code(self, code, timeout=120):
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @unittest.skipUnless(sys.platform == "win32" and sys.maxsize > 2**32,
                         "Windows x64 host memory configuration")
    def test_default_heap_scales_with_host_memory_and_explicit_flag_wins(self):
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                (name, ctypes.c_uint64) for name in (
                    "total_phys", "avail_phys", "total_page_file", "avail_page_file",
                    "total_virtual", "avail_virtual", "avail_extended_virtual")]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            self.skipTest("Windows memory information unavailable")
        if status.total_phys < 8 * 1024**3:
            self.skipTest("high-memory host required for this comparison")
        default = self.run_code("""
            from pyv8c import pyv8
            print(pyv8.Context(object).heap_statistics()['limit'])
        """)
        limited = self.run_code("""
            from pyv8c import pyv8
            pyv8.configure(params=['--max-old-space-size=64'])
            print(pyv8.Context(object).heap_statistics()['limit'])
        """)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(limited.returncode, 0, limited.stderr)
        self.assertGreater(int(default.stdout), 3 * 1024**3)
        self.assertLess(int(limited.stdout), 256 * 1024**2)

    def test_a_script_that_stays_inside_the_budget_runs(self):
        """预算之内照常：64MB 预算下持有 32MB 存活对象仍然成功。"""

        result = self.run_code(
            """
            from pyv8c import pyv8

            pyv8.configure(params=["--max-old-space-size=64"])
            context = pyv8.Context(object)
            held = context.exec_js(%(hold)r)
            print(round(held.length * 1024 / (1024 * 1024)))
            context.close()
            """
            % {"hold": HOLD_STRINGS % {"count": 32 * 1024}}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["32"])

    def test_a_script_that_outgrows_the_budget_is_stopped(self):
        """预算用尽即终止脚本：64MB 预算下无限申请抛 JavaScriptTerminated，
        进程正常退出，随后同一个上下文照常执行。"""

        result = self.run_code(
            """
            from pyv8c import pyv8

            pyv8.configure(params=["--max-old-space-size=64"])
            context = pyv8.Context(object)
            try:
                context.exec_js(
                    "var a = []; for (let i = 0; i < 200000000; i++) a.push(i); 1")
                print("no termination")
            except pyv8.JavaScriptTerminated:
                print("terminated", context.heap_terminations)
            print("next", context.exec_js("1 + 1"))
            context.close()
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["terminated", "1", "next", "2"])

    def test_sources_and_host_objects_are_reported_and_given_back(self):
        """持有多少就报多少，释放后回到原值。"""

        isolate = pyv8.Isolate()
        context = isolate.context(object)
        try:
            # The global host object is already one.
            baseline = isolate.external_memory
            self.assertGreater(baseline, 0)

            source = "var a = 1;" + "x" * 500
            script = pyv8.Script(context, source)
            self.assertEqual(isolate.external_memory - baseline, len(source))

            del script
            gc.collect()
            self.assertEqual(isolate.external_memory, baseline)
        finally:
            context.close()
            isolate.close()

    def test_a_closed_isolate_reports_nothing(self):
        """关闭之后不再报告。"""

        isolate = pyv8.Isolate()
        context = isolate.context(object)
        script = pyv8.Script(context, "1 + 1")
        del script
        gc.collect()
        context.close()
        isolate.close()
        self.assertEqual(isolate.external_memory, 0)


if __name__ == "__main__":
    unittest.main()
