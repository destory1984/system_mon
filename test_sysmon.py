import unittest

import sysmon as m


class UnitsTest(unittest.TestCase):
    def test_size(self):
        self.assertEqual(m.size(0), "0 MB")
        self.assertEqual(m.size(300 * 1024 ** 2), "300 MB")
        self.assertEqual(m.size(1536 * 1024 ** 2), "1.5 GB")
        self.assertEqual(m.size(24 * 1024 ** 3), "24.0 GB")

    def test_rate(self):
        self.assertEqual(m.rate(0), "0 KB/s")
        self.assertEqual(m.rate(512 * 1024), "512 KB/s")
        self.assertEqual(m.rate(3.1 * 1024 ** 2), "3.1 MB/s")

    def test_bar(self):
        self.assertEqual(m.bar(50, 10), "█████░░░░░")
        self.assertEqual(m.bar(0, 4), "░░░░")
        self.assertEqual(m.bar(250, 4), "████")       # 100 을 넘어도 칸을 넘지 않는다


class PadTest(unittest.TestCase):
    def test_korean_takes_two_cells(self):
        self.assertEqual(m.pad("메모리", 8), "메모리  ")
        self.assertEqual(m.pad("ab", 5, right=True), "   ab")
        self.assertEqual(m.pad("toolong", 3), "toolong")


class CpuTest(unittest.TestCase):
    def test_core_pct(self):
        # 2초 동안 CPU 를 1초 썼으면 한 코어의 50%
        self.assertEqual(m.core_pct({1: 10.0}, {1: 11.0}, 2.0), {1: 50.0})

    def test_new_and_gone_processes(self):
        got = m.core_pct({1: 10.0, 2: 5.0}, {1: 10.5, 3: 9.0}, 1.0)
        self.assertEqual(got, {1: 50.0, 3: 0.0})      # 새로 뜬 것은 0, 사라진 것은 뺀다

    def test_pid_reused(self):
        # 같은 번호를 새 프로세스가 받으면 CPU 시간이 줄어 보인다. 음수로 내지 않는다
        self.assertEqual(m.core_pct({1: 100.0}, {1: 0.2}, 1.0), {1: 0.0})


class GroupTest(unittest.TestCase):
    PROCS = [(1, "chrome.exe", 100), (2, "chrome.exe", 300), (3, "claude.exe", 900), (4, "Python.EXE", 50)]

    def test_same_name_is_summed(self):
        rows = m.group(self.PROCS, {1: 10.0, 2: 30.0, 3: 20.0, 4: 0.0})
        self.assertEqual(rows[0], m.Row("chrome", 2, 40.0, 400))
        self.assertEqual([r.name for r in rows], ["chrome", "claude", "Python"])   # CPU 많은 차례, .exe 는 뗀다

    def test_sort_by_memory(self):
        rows = m.group(self.PROCS, {1: 10.0, 2: 30.0, 3: 20.0, 4: 0.0}, by="mem")
        self.assertEqual([r.name for r in rows], ["claude", "chrome", "Python"])

    def test_top(self):
        self.assertEqual(len(m.group(self.PROCS, {}, top=2)), 2)


class GpuTest(unittest.TestCase):
    def test_parse(self):
        got = m.parse_gpu("NVIDIA GeForce RTX 3090, 53, 2817, 24576\n")
        self.assertEqual(got, [m.Gpu("NVIDIA GeForce RTX 3090", 53.0, 2817 * 1024 ** 2, 24576 * 1024 ** 2)])

    def test_parse_garbage(self):
        self.assertEqual(m.parse_gpu(""), [])
        self.assertEqual(m.parse_gpu("no devices were found\n"), [])
        self.assertEqual(m.parse_gpu("Some GPU, [N/A], [N/A], 8192\n"), [])


class ProcsTest(unittest.TestCase):
    def test_reads_this_process(self):
        import os
        procs, times = m.read_procs()
        mine = [p for p in procs if p[0] == os.getpid()]
        self.assertEqual(len(mine), 1)
        self.assertIn("python", mine[0][1].lower())
        self.assertGreater(mine[0][2], 1024 ** 2)        # 메모리를 1MB 넘게 쓴다
        self.assertGreater(times[os.getpid()], 0)
        self.assertGreater(len(procs), 10)


class TextTest(unittest.TestCase):
    def test_plain_text(self):
        snap = m.Snap(cpu=49.0, mem_used=21 * 1024 ** 3, mem_total=32 * 1024 ** 3,
                      gpus=[m.Gpu("RTX 3090", 53.0, 2 * 1024 ** 3, 24 * 1024 ** 3)],
                      disk_read=1024 ** 2, disk_write=0, net_down=3 * 1024 ** 2, net_up=0,
                      rows=[m.Row("claude", 4, 120.0, 3 * 1024 ** 3)])
        text = m.plain(snap)
        self.assertIn("CPU 49%", text)
        self.assertIn("21.0 / 32.0 GB (66%)", text)
        self.assertIn("RTX 3090 53%", text)
        self.assertIn("claude", text)
        self.assertIn("120%", text)

    def test_plain_text_without_gpu(self):
        snap = m.Snap(cpu=1.0, mem_used=1, mem_total=2, gpus=[], disk_read=0, disk_write=0,
                      net_down=0, net_up=0, rows=[])
        self.assertNotIn("GPU", m.plain(snap))


if __name__ == "__main__":
    unittest.main()
