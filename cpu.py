"""10초마다 CPU 사용량을 한 줄씩 찍는다. Ctrl+C 로 끈다.

    python cpu.py        # 10초마다
    python cpu.py 5      # 5초마다
"""
import sys
import time

import psutil

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10
try:
    while True:
        used = psutil.cpu_percent(interval=seconds)   # 그 사이 모든 코어의 평균
        print(f"{time.strftime('%H:%M:%S')}  CPU {used:3.0f}%", flush=True)
except KeyboardInterrupt:
    pass
