#!/usr/bin/env python3
"""性能压场:10 分钟无音轨长视频(大文件/无音轨双测)+ 大清单 CSV。"""
import csv
import subprocess
from pathlib import Path

FFMPEG = r"D:\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "demo" / "origin"

long_video = RAW / "longcam_noaudio.mp4"
if not long_video.is_file():
    print("生成 10 分钟无音轨长视频(约 30-60s)…")
    subprocess.run([
        FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=600",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48",
        str(long_video),
    ], check=True)
    print("生成完成")

csv_path = ROOT / "demo" / "biglist.csv"
rows = []
for i in range(1, 3001):
    src = ["cam01.mp4", "cam02.mp4", "longcam_noaudio.mp4"][i % 3]
    base = (i * 7) % 25
    rows.append({"source": src, "start": f"{base}", "end": f"{base + 4}.5",
                 "output": f"perf_{i:05d}.mp4"})
with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["source", "start", "end", "output"])
    w.writeheader()
    w.writerows(rows)
print(f"大清单: {csv_path}({len(rows)} 行)")
