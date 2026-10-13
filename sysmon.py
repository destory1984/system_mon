"""시스템 사용량을 글자로 보여준다: CPU, 메모리, GPU, 디스크, 네트워크, 그리고 많이 쓰는 프로그램.

    python sysmon.py                 # 2초마다 화면을 고친다. Ctrl+C 로 끈다
    python sysmon.py --interval 5    # 5초마다
    python sysmon.py --sort mem      # 메모리 많이 쓰는 차례
    python sysmon.py --top 30        # 30줄
    python sysmon.py --once          # 한 번 재고 꾸밈 없는 글로 찍고 끝낸다
"""
import argparse
import ctypes
import subprocess
import sys
import threading
import time
import unicodedata
from collections import namedtuple

import psutil

Row = namedtuple("Row", "name count cpu mem")            # 같은 이름을 묶은 한 줄. cpu 는 한 코어 기준 %
Gpu = namedtuple("Gpu", "name load mem_used mem_total")
Snap = namedtuple("Snap", "cpu mem_used mem_total gpus disk_read disk_write net_down net_up rows")

MB, GB = 1024 ** 2, 1024 ** 3
SKIP = {"System Idle Process"}   # 쉬는 시간을 CPU 사용으로 세는 가짜 프로세스


def size(n):
    """바이트를 'NNN MB' 나 'N.N GB' 로."""
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / MB:.0f} MB"


def rate(n):
    """초당 바이트를 'NNN KB/s' 나 'N.N MB/s' 로."""
    return f"{n / MB:.1f} MB/s" if n >= MB else f"{n / 1024:.0f} KB/s"


def bar(pct, width=16):
    full = min(width, max(0, round(pct / 100 * width)))
    return "█" * full + "░" * (width - full)


def core_pct(before, now, seconds):
    """{pid: 쓴 CPU 초} 둘에서 {pid: 한 코어 기준 %}. 새로 뜬 것은 0, 사라진 것은 뺀다."""
    return {pid: max(0.0, (t - before[pid]) / seconds * 100) if pid in before else 0.0
            for pid, t in now.items()}


def group(procs, cpu, by="cpu", top=None):
    """[(pid, 이름, 메모리)] 를 이름으로 묶어 더한다. by 가 'mem' 이면 메모리, 아니면 CPU 많은 차례."""
    sums = {}
    for pid, name, mem in procs:
        key = name[:-4] if name.lower().endswith(".exe") else name
        count, c, m = sums.get(key, (0, 0.0, 0))
        sums[key] = (count + 1, c + cpu.get(pid, 0.0), m + mem)
    rows = [Row(name, *v) for name, v in sums.items()]
    rows.sort(key=(lambda r: (-r.mem, -r.cpu)) if by == "mem" else (lambda r: (-r.cpu, -r.mem)))
    return rows[:top]


def parse_gpu(text):
    """nvidia-smi 의 'name, utilization, memory.used, memory.total' 줄들 (MiB)."""
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        try:
            out.append(Gpu(parts[0], float(parts[1]), int(parts[2]) * MB, int(parts[3]) * MB))
        except (IndexError, ValueError):
            continue
    return out


def read_gpu():
    """NVIDIA 그래픽카드들. nvidia-smi 가 없거나 실패하면 빈 목록."""
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_gpu(done.stdout) if done.returncode == 0 else []


class _JobLimits(ctypes.Structure):
    """윈도우 JOBOBJECT_EXTENDED_LIMIT_INFORMATION. LimitFlags 만 쓴다."""
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong), ("IoCounters", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def die_with_me(child):
    """이 프로그램이 어떻게 끝나든(강제 종료 포함) child 도 같이 끝나게 윈도우 작업 개체에 넣는다.
    돌려준 손잡이를 들고 있어야 한다 — 이 프로그램이 죽어 손잡이가 닫히면 윈도우가 child 를 끝낸다."""
    if sys.platform != "win32":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = k32.CreateJobObjectW(None, None)
    limits = _JobLimits(LimitFlags=0x2000)   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        return None
    k32.AssignProcessToJobObject(job, int(child._handle))
    return job


class GpuWatch:
    """nvidia-smi 하나를 띄워 두고 그것이 간격마다 찍는 줄을 읽는다. 잴 때마다 새로 띄우면 한 번에 0.3초가 걸린다."""

    def __init__(self, interval):
        self.latest, self.child = {}, None
        try:
            # 같은 콘솔에 붙여 둔다 (CREATE_NO_WINDOW 를 주지 않는다). 그래야 Ctrl+C 나 창 닫기에 같이 꺼진다
            self.child = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits", "-lms", str(int(interval * 1000))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        self.job = die_with_me(self.child)
        threading.Thread(target=self._follow, daemon=True).start()

    def _follow(self):
        for line in self.child.stdout:
            index, _, rest = line.partition(",")
            for g in parse_gpu(rest):
                self.latest[index.strip()] = g

    def read(self):
        return [self.latest[i] for i in sorted(self.latest)]

    def stop(self):
        if self.child:
            self.child.terminate()


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    """윈도우 SYSTEM_PROCESS_INFORMATION 의 앞부분. 시간은 100나노초 단위."""
    _fields_ = [("NextEntryOffset", ctypes.c_ulong), ("NumberOfThreads", ctypes.c_ulong),
                ("WorkingSetPrivateSize", ctypes.c_longlong), ("HardFaultCount", ctypes.c_ulong),
                ("NumberOfThreadsHighWatermark", ctypes.c_ulong), ("CycleTime", ctypes.c_ulonglong),
                ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong),
                ("KernelTime", ctypes.c_longlong), ("ImageName", _UnicodeString),
                ("BasePriority", ctypes.c_long), ("UniqueProcessId", ctypes.c_void_p)]


_buffer_size = 1 << 20


def read_procs_windows():
    """윈도우에 한 번 물어 모든 프로세스를 받는다 (작업 관리자가 쓰는 길). 프로세스를 하나씩 여는 psutil 방식은
    보안 프로그램이 끼어 있는 PC 에서 463개에 20초가 걸렸다. 메모리는 작업 관리자와 같은 「개인 작업 집합」."""
    global _buffer_size
    query = ctypes.windll.ntdll.NtQuerySystemInformation
    while True:
        buf = ctypes.create_string_buffer(_buffer_size)
        need = ctypes.c_ulong(0)
        status = query(5, buf, _buffer_size, ctypes.byref(need)) & 0xFFFFFFFF   # 5 = SystemProcessInformation
        if status == 0:
            break
        if status != 0xC0000004:   # STATUS_INFO_LENGTH_MISMATCH 가 아니면 진짜 실패
            raise OSError(f"NtQuerySystemInformation 0x{status:08X}")
        _buffer_size = max(_buffer_size * 2, need.value + (1 << 16))
    procs, times = [], {}
    base, offset = ctypes.addressof(buf), 0
    while True:
        p = _ProcessInfo.from_address(base + offset)
        pid = p.UniqueProcessId or 0
        if pid and p.ImageName.Buffer:   # pid 0 은 쉬는 시간을 세는 가짜 프로세스
            name = ctypes.wstring_at(p.ImageName.Buffer, p.ImageName.Length // 2)
            procs.append((pid, name, p.WorkingSetPrivateSize))
            times[pid] = (p.UserTime + p.KernelTime) / 1e7
        if not p.NextEntryOffset:
            return procs, times
        offset += p.NextEntryOffset


def read_procs():
    """([(pid, 이름, 메모리)], {pid: 쓴 CPU 초}). 권한이 없어 못 읽는 프로세스는 건너뛴다."""
    if sys.platform == "win32":
        return read_procs_windows()
    procs, times = [], {}
    for p in psutil.process_iter(["pid", "name", "cpu_times", "memory_info"]):
        i = p.info
        if not i["name"] or i["name"] in SKIP or i["cpu_times"] is None or i["memory_info"] is None:
            continue
        procs.append((i["pid"], i["name"], i["memory_info"].rss))
        times[i["pid"]] = i["cpu_times"].user + i["cpu_times"].system
    return procs, times


class Meter:
    """잴 때마다 앞에 잰 것과의 차이로 사용량을 낸다."""

    def __init__(self, gpu=None):
        self.gpu = gpu   # GPU 목록을 돌려주는 함수. None 이면 재지 않는다
        psutil.cpu_percent()   # 첫 값은 뜻이 없다. 다음부터 그 사이 평균이 나온다
        self.at = time.monotonic()
        _, self.times = read_procs()
        self.disk, self.net = psutil.disk_io_counters(), psutil.net_io_counters()

    def read(self, by="cpu", top=15):
        now = time.monotonic()
        seconds = max(now - self.at, 0.001)
        procs, times = read_procs()
        disk, net = psutil.disk_io_counters(), psutil.net_io_counters()
        mem = psutil.virtual_memory()
        snap = Snap(
            cpu=psutil.cpu_percent(), mem_used=mem.total - mem.available, mem_total=mem.total,
            gpus=self.gpu() if self.gpu else [],
            disk_read=(disk.read_bytes - self.disk.read_bytes) / seconds if disk and self.disk else 0,
            disk_write=(disk.write_bytes - self.disk.write_bytes) / seconds if disk and self.disk else 0,
            net_down=(net.bytes_recv - self.net.bytes_recv) / seconds,
            net_up=(net.bytes_sent - self.net.bytes_sent) / seconds,
            rows=group(procs, core_pct(self.times, times, seconds), by, top))
        self.at, self.times, self.disk, self.net = now, times, disk, net
        return snap


def head(s):
    """맨 위 요약 줄들."""
    lines = [f"CPU {s.cpu:.0f}% {bar(s.cpu)}   메모리 {s.mem_used / GB:.1f} / {s.mem_total / GB:.1f} GB "
             f"({s.mem_used / s.mem_total * 100:.0f}%)"]
    for g in s.gpus:
        lines.append(f"GPU {g.name} {g.load:.0f}% {bar(g.load)}   VRAM {g.mem_used / GB:.1f} / {g.mem_total / GB:.1f} GB")
    lines.append(f"디스크  읽기 {rate(s.disk_read)}  쓰기 {rate(s.disk_write)}    "
                 f"네트워크  받기 {rate(s.net_down)}  보내기 {rate(s.net_up)}")
    return lines


def pad(text, width, right=False):
    """글자판 칸 수에 맞춰 채운다. 한글은 두 칸을 차지한다."""
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    fill = " " * max(0, width - used)
    return fill + text if right else text + fill


def plain(s):
    """꾸밈 없는 글 (--once)."""
    cols = lambda name, count, cpu, mem: (pad(name[:30], 32) + pad(count, 5, True) + pad(cpu, 14, True)
                                          + pad(mem, 11, True))
    lines = head(s) + ["", cols("프로그램", "개수", "CPU(한 코어)", "메모리")]
    lines += [cols(r.name, str(r.count), f"{r.cpu:.0f}%", size(r.mem)) for r in s.rows]
    return "\n".join(lines)


def screen(s, by):
    """rich 로 그린 화면."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("프로그램", no_wrap=True, max_width=32)
    table.add_column("개수", justify="right")
    table.add_column("CPU(한 코어)" + (" ▼" if by == "cpu" else ""), justify="right")
    table.add_column("메모리" + (" ▼" if by == "mem" else ""), justify="right")
    for r in s.rows:
        style = "bold red" if r.cpu >= 100 else "yellow" if r.cpu >= 25 else ""
        table.add_row(r.name, str(r.count), Text(f"{r.cpu:.0f}%", style=style), size(r.mem))
    return Group(*[Text(line) for line in head(s)], Text(""), table)


def main():
    ap = argparse.ArgumentParser(description="시스템 사용량을 글자로 보여준다.")
    ap.add_argument("--interval", type=float, default=2.0, help="화면을 고치는 간격 (초, 기본 2)")
    ap.add_argument("--top", type=int, default=15, help="보일 프로그램 줄 수 (기본 15)")
    ap.add_argument("--sort", choices=["cpu", "mem"], default="cpu", help="차례 (기본 cpu)")
    ap.add_argument("--once", action="store_true", help="한 번 재고 꾸밈 없는 글로 찍고 끝낸다")
    ap.add_argument("--no-gpu", action="store_true", help="GPU 를 재지 않는다")
    args = ap.parse_args()
    interval = max(args.interval, 0.5)

    for stream in (sys.stdout, sys.stderr):   # 파이프로 붙으면 cp949 로 찍혀 막대가 깨진다
        if stream is not None:
            stream.reconfigure(encoding="utf-8")

    if args.once:
        meter = Meter(gpu=None if args.no_gpu else read_gpu)
        time.sleep(interval)
        print(plain(meter.read(args.sort, args.top)))
        return

    from rich.live import Live
    watch = None if args.no_gpu else GpuWatch(interval)
    meter = Meter(gpu=watch and watch.read)
    try:
        with Live(screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(interval)
                live.update(screen(meter.read(args.sort, args.top), args.sort), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        if watch:
            watch.stop()


if __name__ == "__main__":
    main()
