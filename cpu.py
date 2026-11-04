"""10초마다 CPU, RAM, VRAM 사용량을 한 줄씩 찍는다. Ctrl+C 로 끈다.

    python cpu.py        # 10초마다
    python cpu.py 5      # 5초마다
"""
import subprocess
import sys
import time

import psutil



def vram_gb():
    """NVIDIA GPU 들의 VRAM 사용량 합 (GB). nvidia-smi 가 없거나 실패하면 None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout
        return sum(float(x) for x in out.split()) / 1024   # MiB -> GiB
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10
try:
    while True:
        used = psutil.cpu_percent(interval=seconds)   # 그 사이 모든 코어의 평균
        ram = psutil.virtual_memory()
        vram = vram_gb()
        vram_text = f"{vram:.1f}GB" if vram is not None else "-"
        print(f"{time.strftime('%H:%M:%S')} CPU {used:3.0f}%  "
              f"RAM {(ram.total - ram.available) / 2 ** 30:.1f}GB VRAM {vram_text}", flush=True)
except KeyboardInterrupt:
    pass
