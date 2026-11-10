#!/usr/bin/env python3
"""生成演示数据:3 个 30 秒测试视频 + 剪辑清单 CSV(带一个故意的问题行展示校验)。"""

import csv
import subprocess
import sys
from pathlib import Path

FFMPEG = r"D:\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "demo" / "origin"
OUT = ROOT / "demo" / "clips"
CSV = ROOT / "demo" / "clips_demo.csv"


def make_video(path: Path, seconds: int, color: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return
    subprocess.run([
        FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size=640x360:rate=24:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={440 if color == 'a' else 660}:duration={seconds}",
        "-vf", f"drawtext=text='{label}':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-preset", "veryfast", "-g", "48",  # GOP 2s,模拟监控关键帧间隔
        "-c:a", "aac", "-shortest",
        str(path),
    ], check=True)
    print(f"生成 {path.name}({seconds}s)")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    make_video(RAW / "cam01.mp4", 30, "a", "CAM 01")
    make_video(RAW / "cam02.mp4", 30, "b", "CAM 02")

    rows = [
        # 正常任务×4:两种模式混着测
        {"source": "cam01.mp4", "start": "00:00:05", "end": "00:00:12", "output": "cam01_event_a.mp4"},
        {"source": "cam01.mp4", "start": "00:00:18.5", "end": "00:00:26", "output": "cam01_event_b.mp4"},
        {"source": "cam02.mp4", "start": "3", "end": "9.5", "output": "cam02_event_a.mp4"},
        {"source": "cam02.mp4", "start": "00:00:15", "end": "00:00:22", "output": "cam02_event_b.mp4"},
        # 问题任务×2:dry-run 校验展示用
        {"source": "cam03.mp4", "start": "00:00:01", "end": "00:00:05", "output": "missing_source.mp4"},
        {"source": "cam02.mp4", "start": "00:00:28", "end": "00:00:40", "output": "over_end.mp4"},
    ]
    with CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "start", "end", "output"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"清单: {CSV}({len(rows)} 行,含 2 行故意的问题)")
    print(f"来源: {RAW}\n输出: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
