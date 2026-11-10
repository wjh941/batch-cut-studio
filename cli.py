#!/usr/bin/env python3
"""BatchCut Studio · 命令行模式(兼容原 batch_cut_clips.py 用法)。

    python cli.py --csv D:\\clips\\list.csv --raw-dir D:\\origin --output-dir D:\\clips
    python cli.py --dry-run          # 只校验不剪切

引擎与 Web UI 共用 core.py;此处只做终端渲染。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core import CutConfig, CutEngine


def main() -> int:
    if os.name == "nt":
        os.system("")  # 启用 Windows 终端 ANSI 颜色

    parser = argparse.ArgumentParser(description="批量剪辑工场 · 命令行模式")
    parser.add_argument("--raw-dir", default=r"D:\1105\origin")
    parser.add_argument("--output-dir", default=r"D:\1105\cam02_new_videos")
    parser.add_argument("--csv", default=r"D:\1105\1105_cam02_clips_sample_副本.csv")
    parser.add_argument("--ffmpeg", default=r"D:\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe")
    parser.add_argument("--mode", choices=["encode", "copy", "smart"], default="smart")
    parser.add_argument("--overwrite", action="store_true", default=False)
    parser.add_argument("--hw-encoder", default="auto")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--timeout", type=float, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="只校验清单,不执行切割")
    args = parser.parse_args()

    cfg = CutConfig(
        raw_dir=args.raw_dir, output_dir=args.output_dir, csv_path=args.csv,
        ffmpeg=args.ffmpeg, mode=args.mode, overwrite=args.overwrite,
        hw_encoder=args.hw_encoder, crf=args.crf, workers=args.workers,
        timeout=args.timeout,
    )

    def render(event: dict) -> None:
        kind = event["type"]
        if kind == "log":
            print(event["message"])
        elif kind == "task":
            patch = event["patch"]
            if patch.get("status") == "running":
                sys.stdout.write(f"\r  进度 {patch.get('progress', 0) * 100:5.1f}% {patch.get('speed', '')}   ")
                sys.stdout.flush()
        elif kind == "done":
            s = event["summary"]
            print(f"\n完成:成功 {s['done']} / 跳过 {s['skip']} / 失败 {s['fail']} / "
                  f"取消 {s['cancel']} · 用时 {s['elapsed']}s")
            if s.get("failed_report"):
                print(f"失败清单: {s['failed_report']}")

    engine = CutEngine(cfg, on_event=render)
    try:
        total = engine.load_csv()
    except Exception as exc:
        print(f"装载失败: {exc}", file=sys.stderr)
        return 2

    problems = engine.validate()
    bad = [t for t in engine.tasks if t.problem]
    print(f"共 {total} 个任务,校验通过 {total - len(problems)} 个,问题 {len(problems)} 个")
    for t in bad:
        print(f"  第 {t.index} 行: {t.problem}")
    if args.dry_run:
        return 1 if problems else 0
    if not sys.stdin.isatty() and problems:
        print("(非交互环境,问题行已跳过;全部行仍会尝试执行)")

    engine.start()
    return 1 if engine._counts()["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
