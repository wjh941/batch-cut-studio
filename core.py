#!/usr/bin/env python3
"""BatchCut Studio · 批量切片引擎。

从单文件脚本升级而来,保留原版全部硬功夫:
  - encode 模式「先粗后细」双 -ss 预寻址(快且准)
  - 硬件编码器真试编探测(NVENC / QSV / AMF,探不到回落 libx264)
  - 断点续跑(输出文件 >= MIN_VALID_OUTPUT_BYTES 视为已完成)
  - 双段式停止(第一次停止派新任务,第二次强杀全部 ffmpeg)
  - CSV 三编码兜底(utf-8-sig / utf-16 / gbk)

新增四个升级:
  - 单任务实时进度:ffmpeg -progress pipe:1 逐行解析,回调 on_event
  - ffprobe 时长探测:end 超出源时长提前告警,而不是让 ffmpeg 撞错
  - dry-run:只校验不剪切,输出逐行问题清单
  - 事件驱动架构:引擎不再直接 print,全部状态变化以事件回调抛出(UI/CLI 各自渲染)

零第三方依赖,Python >= 3.10。
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --- 默认配置(命令行/UI 未指定时使用)---
DEFAULT_FFMPEG = r"D:\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe"
MIN_VALID_OUTPUT_BYTES = 1024
HW_CANDIDATES = ["h264_nvenc", "h264_qsv", "h264_amf"]
HW_ENCODER_PARAMS: dict[str, list[str]] = {
    "h264_nvenc": ["-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
    "h264_qsv": ["-global_quality", "20"],
    "h264_amf": ["-quality", "balanced"],
}
CSV_ENCODINGS = ("utf-8-sig", "utf-16", "gbk")
REQUIRED_COLUMNS = {"source", "start", "end", "output"}


# ======================================================================
# 数据模型
# ======================================================================

@dataclass
class CutConfig:
    raw_dir: str = ""
    output_dir: str = ""
    csv_path: str = ""
    ffmpeg: str = DEFAULT_FFMPEG
    mode: str = "encode"          # encode | copy
    overwrite: bool = False
    hw_encoder: str = "auto"      # auto | off | 具体编码器名
    crf: int = 18
    workers: int = 3              # 0 = CPU 核数
    output_template: str = "{source}_{index}_{start}"  # output 为空时自动补名
    timeout: float = 0            # 单任务超时秒数,0 = 不限


@dataclass
class ClipTask:
    index: int                    # 1-based,对应 CSV 行号
    source: str
    start: str
    end: str
    output: str
    status: str = "pending"       # pending|running|done|skip|fail|cancel
    progress: float = 0.0         # 0~1,encode 模式由 -progress 解析驱动
    speed: str = ""               # ffmpeg 报告的 speed,如 "3.4x"
    error: str = ""
    output_mb: float = 0.0
    seconds: float = 0.0          # 剪辑耗时
    source_exists: bool | None = None
    source_duration: float | None = None
    has_audio: bool | None = None
    labels: str = ""              # 标注清单下行的行为标签(可用 + 连接)
    planned_duration: float = 0.0  # 计划时长(end-start),质检基准
    verify: str = ""               # ok|warn|bad 成品质检结论
    verify_reason: str = ""
    review_status: str = ""       # 标注清单的审核状态透传
    problem: str = ""             # dry-run 校验结论

    def public(self) -> dict:
        """序列化为 UI 可直接渲染的字典。"""
        return {
            "index": self.index, "source": self.source, "start": self.start,
            "end": self.end, "output": self.output, "status": self.status,
            "progress": round(self.progress, 4), "speed": self.speed,
            "error": self.error, "output_mb": round(self.output_mb, 2),
            "seconds": round(self.seconds, 2),
            "sourceExists": self.source_exists,
            "sourceDuration": self.source_duration,
            "hasAudio": self.has_audio,
            "plannedDuration": round(self.planned_duration, 3),
            "verify": self.verify,
            "verifyReason": self.verify_reason,
            "labels": self.labels,
            "reviewStatus": self.review_status,
            "problem": self.problem,
        }


# ======================================================================
# 纯函数(全部可单测)
# ======================================================================

def parse_time_to_seconds(value: str) -> float:
    """'01:02:03.5' / '62.5' -> 秒。空值抛 ValueError。"""
    value = (value or "").strip()
    if not value:
        raise ValueError("empty time")
    if ":" not in value:
        return float(value)
    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time: {value}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_seconds(value: float) -> str:
    """秒 -> 'HH:MM:SS.mmm'(ffmpeg 喜欢的输入格式)。"""
    value = max(0.0, value)
    return f"{int(value // 3600):02d}:{int((value % 3600) // 60):02d}:{value % 60:06.3f}"


def is_valid_output(path: Path, min_bytes: int = MIN_VALID_OUTPUT_BYTES) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def read_rows_and_fields(csv_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """同 read_rows,但额外返回归一化后的表头(UI 内编辑回写 CSV 需要保留全部列)。"""
    text = None
    for encoding in CSV_ENCODINGS:
        try:
            text = csv_path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        raise ValueError(
            f"无法解码 {csv_path}(尝试过 {' / '.join(CSV_ENCODINGS)})。"
            "请在 Excel 里另存为「CSV UTF-8」。"
        )
    reader = csv.DictReader(text.splitlines())
    reader.fieldnames = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise ValueError(f"CSV 缺少列: {', '.join(sorted(missing))}")
    rows = [row for row in reader if any(isinstance(v, str) and v.strip() for v in row.values())]
    return rows, list(reader.fieldnames)


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    return read_rows_and_fields(csv_path)[0]


def build_command(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    mode: str,
    overwrite: bool,
    crf: int,
    encoder: str | None,
    with_progress: bool = False,
    has_audio: bool | None = True,
) -> list[str]:
    """拼 ffmpeg 命令。

    encode 模式用「先粗后细」:先 -ss 到 start-10s(关键帧粗寻,极快),
    再在解码流上 -ss 精确偏移 + -t 时长;音轨统一转 aac。
    copy 模式保持输出端 -ss/-to(帧级准确,牺牲部分速度)。
    with_progress=True 时加 -progress pipe:1(机器可读进度,UI 模式用)。
    """
    overwrite_arg = "-y" if overwrite else "-n"
    head = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "error", overwrite_arg]
    if with_progress:
        head += ["-progress", "pipe:1"]

    if mode == "copy":
        # 输入端关键帧寻址(LosslessCut 同款做法):监控视频几小时长时,
        # 输出端寻址要白白解码到目标点,输入端寻址是秒级与小时级的差别。
        # 代价是切点对齐前一个关键帧(监控视频 GOP 短,误差通常 <2s);
        # -avoid_negative_ts make_zero 防止寻址重置时间戳后容器写负 ts。
        video_map = ["-map", "0:v"] if has_audio is False else ["-map", "0"]
        return [
            *head,
            "-ss", format_seconds(start_sec), "-i", str(input_path),
            "-t", f"{end_sec - start_sec:.3f}",
            *video_map, "-dn", "-sn", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output_path),
        ]

    if mode == "encode":
        duration = end_sec - start_sec
        pre_seek = max(0.0, start_sec - 10)
        offset = start_sec - pre_seek
        if encoder:
            video_args = ["-c:v", encoder, *HW_ENCODER_PARAMS.get(encoder, [])]
        else:
            video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
        return [
            *head,
            "-ss", format_seconds(pre_seek), "-i", str(input_path),
            "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
            "-map", "0", "-dn", "-sn", *video_args,
            *(["-c:a", "aac", "-b:a", "128k"] if has_audio is not False else []),
            str(output_path),
        ]

    raise ValueError(f"Unknown mode: {mode}")


def parse_progress_line(line: str, current: dict) -> dict:
    """逐行喂 ffmpeg -progress 输出(key=value),返回更新后的状态 dict。

    兼容三类时间键(不同 ffmpeg 版本行为不同):
      out_time_us  微秒(5.x+)
      out_time_ms  名字是 ms 实际也是微秒(历史 bug,按微秒处理)
      out_time     HH:MM:SS.mmmstr
    收到 progress=end 表示该任务编码结束。
    """
    line = line.strip()
    if not line or "=" not in line:
        return current
    key, _, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if key == "out_time_us":
        current["out_sec"] = float(value) / 1_000_000
    elif key == "out_time_ms":
        # 历史遗留:这个键名实际也是微秒
        current["out_sec"] = float(value) / 1_000_000
    elif key == "out_time":
        try:
            current["out_sec"] = parse_time_to_seconds(value)
        except ValueError:
            pass
    elif key == "speed":
        current["speed"] = value
    elif key == "progress":
        current["ended"] = value == "end"
    return current


# ---------- Smart Cut 与自动清单:关键帧/场景/黑场纯函数 ----------

def parse_keyframes_csv(text: str) -> list[float]:
    """ffprobe -show_packets -show_entries packet=pts_time,flags -of csv=p=0 输出 → 关键帧秒值。

    用 packets 的 K 标志而不是 -skip_frame nokey:后者按解码路径走,会漏 open-GOP
    访问点(监控 H.264 常见),且长视频上是解码限速;packets 只读容器,秒级。
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        pts, _, flags = line.partition(",")
        if flags.startswith("K"):
            try:
                out.append(float(pts))
            except ValueError:
                continue
    return sorted(out)


def pick_keyframes(keyframes: list[float], start: float, end: float) -> tuple[float | None, float | None]:
    """Smart Cut 吸附(方向沿用 LosslessCut 默认):起点取 <= start 的最后一个关键帧
    (不丢内容),终点取 >= end 的第一个关键帧(完整覆盖);找不到返回 None。"""
    kf_start = None
    kf_end = None
    for k in keyframes:
        if k <= start + 1e-6:
            kf_start = k
        if k >= end - 1e-6 and kf_end is None:
            kf_end = k
    return kf_start, kf_end


def parse_scene_points(text: str) -> list[float]:
    """ffmpeg metadata=print(stdout)→ 场景切换点(秒)。"""
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", text)]


def parse_blackdetect(text: str) -> list[tuple[float, float, float]]:
    """blackdetect 的 stderr 行 → [(black_start, black_end, duration)]。"""
    return [(float(a), float(b), float(c)) for a, b, c in re.findall(
        r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", text)]


def segments_from_points(
    points: list[float], duration: float, min_seg: float = 2.0, merge: float = 1.5,
) -> list[tuple[float, float]]:
    """场景点 → 全覆盖片段:相邻 <merge 视为噪声合并;尾段 <min_seg 并入前段;
    首段必须从 0 开始(监控要前因)。"""
    pts = sorted({p for p in points if 0 < p < duration})
    filtered: list[float] = []
    for p in pts:
        if filtered and p - filtered[-1] < merge:
            continue
        filtered.append(p)
    if filtered and duration - filtered[-1] < min_seg:
        filtered.pop()
    bounds = [0.0] + filtered + [duration]
    segments: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if segments and b - a < min_seg:
            segments[-1] = (segments[-1][0], b)  # 短段并入前段,保证全覆盖
        else:
            segments.append((round(a, 3), round(b, 3)))
    return segments


def build_verify_command(ffmpeg: str, clip_path: Path) -> list[str]:
    """成品质检:ffprobe 读时长/流 + blackdetect 查黑场。返回单条 ffprobe + ffmpeg 两条命令的元数据。"""
    return []  # 占位,实际质检由 verify_output 组装


def parse_black_pct(stderr_text: str, duration: float) -> float:
    """从 blackdetect 输出估算黑场占比(秒/总时长)。"""
    total = 0.0
    for _, _, dur in parse_blackdetect(stderr_text):
        total += dur
    return min(1.0, total / duration) if duration > 0 else 0.0


def step_reason(text: str) -> str:
    return text or '未知原因'


def judge_output(duration: float, expected: float, black_pct: float) -> tuple[str, str]:
    """成品质量判定:返回 (verdict, 原因)。verdict ∈ ok/warn/bad。"""
    if duration <= 0:
        return "bad", "成品时长读到 0,疑似文件损坏"
    delta = abs(duration - expected)
    tol = max(0.5, expected * 0.05)
    if delta > tol:
        return "bad", f"时长偏差 {delta:.2f}s 超出容差 {tol:.2f}s(期望 {expected:.2f}s,实际 {duration:.2f}s)"
    if black_pct >= 0.9:
        return "bad", f"成品 {black_pct*100:.0f}% 是黑场,疑似切片区间无有效画面"
    if black_pct >= 0.3:
        return "warn", f"成品 {black_pct*100:.0f}% 为黑场,建议人工复核"
    if delta > tol * 0.5:
        return "warn", f"时长偏差 {delta:.2f}s 接近容差上限"
    return "ok", ""


def parse_stream_info(json_text: str) -> dict:
    """ffprobe -show_entries stream=codec_type:format=duration -of json → {duration, has_audio}。"""
    import json as _json
    duration = None
    has_audio = False
    try:
        data = _json.loads(json_text or "{}")
        for stream in data.get("streams") or []:
            if stream.get("codec_type") == "audio":
                has_audio = True
        duration = float((data.get("format") or {}).get("duration"))
    except (ValueError, TypeError):
        pass
    return {"duration": duration, "has_audio": has_audio}


def count_probe_errors(stderr_text: str) -> int:
    """ffprobe -v error 的 stderr 非空行数 ≈ 解码/容器错误数。"""
    return sum(1 for line in (stderr_text or "").splitlines() if line.strip())


def expand_output(template: str, index: int, source: str, start: str, end: str) -> str:
    """输出命名模板:{source}=源文件名去扩展、{index}=三位行号、{start}/{end}=HHMMSS。"""
    def stamp(value: str) -> str:
        try:
            sec = parse_time_to_seconds(value)
        except ValueError:
            sec = 0.0
        return f"{int(sec // 3600):02d}{int((sec % 3600) // 60):02d}{int(sec % 60):02d}"
    name = Path(source).stem or "clip"
    rendered = (template
                .replace("{source}", name)
                .replace("{index}", f"{index:03d}")
                .replace("{start}", stamp(start))
                .replace("{end}", stamp(end)))
    # 模板没写扩展名时补 .mp4:ffmpeg 必须靠扩展名推断容器,否则直接报
    # "Unable to choose an output format"
    if not Path(rendered).suffix:
        rendered += ".mp4"
    return rendered


def resolve_ffmpeg(configured_path: str) -> str:
    configured_path = (configured_path or "").strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"配置的 ffmpeg 路径不存在: {path}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "找不到 ffmpeg。请在设置里指定 ffmpeg.exe 路径,或把它加进 PATH。"
    )


def ffprobe_path(ffmpeg: str) -> str | None:
    """ffprobe 通常与 ffmpeg 同目录;找不到返回 None(时长探测降级跳过)。"""
    candidate = Path(ffmpeg).with_name("ffprobe.exe")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ffprobe")
    if found:
        return found
    if Path(ffmpeg).with_name("ffprobe").is_file():
        return str(Path(ffmpeg).with_name("ffprobe"))
    return None


# ======================================================================
# 引擎
# ======================================================================

class CutEngine:
    """批量切片引擎。所有状态变化通过 on_event(event: dict) 抛出。

    事件类型:
      log       {message}                          日志行
      task      {index, patch}                     单任务状态/进度更新(patch)
      stats     {counts, total, started_at, eta}   全局计数
      stage     {stage}                            idle|probing|running|stopping|done
      done      {summary}                          整轮结束
    """

    def __init__(self, config: CutConfig, on_event: Callable[[dict], None]):
        self.cfg = config
        self.on_event = on_event
        self.tasks: list[ClipTask] = []
        self.encoder: str | None = None
        self._stop = threading.Event()
        self._force_stop = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self._duration_cache: dict[str, float | None] = {}
        self._kf_cache: dict = {}
        self._media_cache: dict = {}
        self._integrity_cache: dict = {}
        self._source_health: dict = {}
        self.fieldnames: list[str] = []
        self.raw_rows: list[dict[str, str]] = []
        self._started_at: float | None = None
        self.stage = "idle"

    # ---------- 事件与计数 ----------

    def _emit(self, event: dict) -> None:
        try:
            self.on_event(event)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        self._emit({"type": "log", "message": message})

    def _counts(self) -> dict[str, int]:
        c = {"pending": 0, "running": 0, "done": 0, "skip": 0, "fail": 0, "cancel": 0}
        with self._lock:
            for t in self.tasks:
                c[t.status] = c.get(t.status, 0) + 1
        return c

    def _emit_stats(self) -> None:
        counts = self._counts()
        eta = None
        if self._started_at and counts["running"]:
            done = counts["done"] + counts["skip"] + counts["fail"]
            total = len(self.tasks)
            if 0 < done < total:
                elapsed = time.monotonic() - self._started_at
                eta = round(elapsed / done * (total - done), 1)
        self._emit({"type": "stats", "counts": counts, "total": len(self.tasks),
                    "elapsed": round(time.monotonic() - self._started_at, 1) if self._started_at else 0,
                    "eta": eta})

    def _patch_task(self, task: ClipTask, **patch) -> None:
        with self._lock:
            for key, value in patch.items():
                setattr(task, key, value)
        self._emit({"type": "task", "index": task.index, "patch": task.public()})

    # ---------- 环境准备 ----------

    def _probe_duration(self, source: str) -> float | None:
        """源时长(秒),缓存;探测失败返回 None。"""
        if source in self._duration_cache:
            return self._duration_cache[source]
        duration = self._probe_media(source)["duration"]
        self._duration_cache[source] = duration
        return duration

    def _probe_media(self, source: str) -> dict:
        """一次 ffprobe 拿 duration + has_audio(带缓存)。"""
        if source in self._media_cache:
            return self._media_cache[source]
        probe = ffprobe_path(self.cfg.ffmpeg)
        info = {"duration": None, "has_audio": None}
        if probe:
            try:
                out = subprocess.run(
                    [probe, "-v", "error", "-show_entries",
                     "stream=codec_type:format=duration", "-of", "json",
                     str(Path(self.cfg.raw_dir) / source)],
                    capture_output=True, text=True, errors="replace", timeout=30)
                info = parse_stream_info(out.stdout)
            except Exception:
                pass
        self._media_cache[source] = info
        return info

    def probe_integrity(self, source: str) -> tuple[bool, int]:
        """容器级体检:ffprobe 能否完整 demux;返回 (是否健康, 错误行数)。"""
        if source in self._integrity_cache:
            return self._integrity_cache[source]
        probe = ffprobe_path(self.cfg.ffmpeg)
        ok, errors = True, 0
        if probe:
            try:
                out = subprocess.run(
                    [probe, "-v", "error", "-i", str(Path(self.cfg.raw_dir) / source),
                     "-show_entries", "format=duration", "-of", "csv=p=0"],
                    capture_output=True, text=True, errors="replace", timeout=120)
                errors = count_probe_errors(out.stderr)
                ok = out.returncode == 0 and errors == 0
            except Exception:
                ok, errors = False, 1
        else:
            ok, errors = True, 0  # 没有 ffprobe 时跳过体检,不误报
        self._integrity_cache[source] = (ok, errors)
        return ok, errors

    def _probe_window_ok(self, source: str, start: float, end: float) -> tuple[bool, str]:
        """切割前预检:目标区间能否读到视频包。读不到 = 区间损坏 → 提前报错。"""
        probe = ffprobe_path(self.cfg.ffmpeg)
        if not probe:
            return True, ""
        cmd = [
            probe, "-v", "error", "-select_streams", "v:0",
            "-read_intervals", f"{max(0.0, start):.3f}%{end:.3f}",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0",
            str(Path(self.cfg.raw_dir) / source),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 errors="replace", timeout=60)
        except Exception as exc:
            return True, ""  # 预检自身失败不阻断切割,交给 ffmpeg 兜底
        if out.returncode != 0:
            first = (out.stderr or "").strip().splitlines()
            detail = first[0] if first else f"ffprobe 退出码 {out.returncode}"
            return False, f"切割区间 [{start:.1f}s→{end:.1f}s] 读取失败,疑似源损坏: {detail}"
        packets = sum(1 for line in out.stdout.splitlines() if line.strip())
        if packets == 0:
            return False, f"切割区间 [{start:.1f}s→{end:.1f}s] 读不到任何视频包,疑似损坏或超出有效时长"
        return True, ""

    def resolve_encoder(self) -> str | None:
        """encode 模式选视频编码器:off=None(CPU),auto=逐个试编,具体名=单测。"""
        setting = (self.cfg.hw_encoder or "auto").strip()
        if setting.lower() == "off":
            self._log("编码器:CPU libx264(hw-encoder=off)")
            return None
        names = [setting] if setting.lower() != "auto" else HW_CANDIDATES
        for name in names:
            if self._probe_encoder(name):
                self._log(f"硬件编码器: {name}")
                return name
        if setting.lower() != "auto":
            raise RuntimeError(f"指定的编码器 {setting} 试编失败,请改用 auto 或 off")
        self._log("无可用硬件编码器,回落 CPU libx264")
        return None

    def _probe_encoder(self, name: str) -> bool:
        command = [
            self.cfg.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=black:s=128x128:d=0.1",
            "-c:v", name, *HW_ENCODER_PARAMS.get(name, []), "-f", "null", "-",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    errors="replace", timeout=60)
        except Exception:
            return False
        return result.returncode == 0

    # ---------- 任务装载与校验 ----------

    def load_csv(self) -> int:
        """读 CSV 建任务清单(含源存在性 + 时长探测),返回任务数。dry-run 也会先走这步。"""
        rows, self.fieldnames = read_rows_and_fields(Path(self.cfg.csv_path))
        self.raw_rows = rows
        for index, row in enumerate(rows, start=1):
            if not (row.get("output") or "").strip():
                row["output"] = expand_output(
                    self.cfg.output_template, index,
                    row.get("source", ""), row.get("start", ""), row.get("end", ""),
                )
        self.tasks = []
        for index, row in enumerate(rows, start=1):
            task = ClipTask(
                index=index,
                source=(row.get("source") or "").strip(),
                start=(row.get("start") or "").strip(),
                end=(row.get("end") or "").strip(),
                output=(row.get("output") or "").strip(),
            )
            self.tasks.append(task)
        self._probe_all_sources()
        return len(self.tasks)

    def _probe_all_sources(self) -> None:
        seen_sources: set[str] = set()
        for task in self.tasks:
            if not task.source:
                continue
            src = Path(self.cfg.raw_dir) / task.source
            task.source_exists = src.is_file()
            if task.source not in seen_sources and task.source_exists:
                seen_sources.add(task.source)
                media = self._probe_media(task.source)
                healthy, errors = self.probe_integrity(task.source)
                self._source_health[task.source] = (healthy, errors)
            if task.source_exists and task.source_duration is None:
                task.source_duration = self._probe_duration(task.source)
            if task.source in self._media_cache:
                task.has_audio = self._media_cache[task.source]["has_audio"]

    def validate(self) -> list[ClipTask]:
        """dry-run:不剪切,只把每个任务的问题写进 task.problem 并返回问题任务。"""
        output_dir = Path(self.cfg.output_dir)
        problems: list[ClipTask] = []
        for task in self.tasks:
            issue = ""
            try:
                if not (task.source and task.start and task.end and task.output):
                    issue = "source/start/end/output 存在空值"
                else:
                    start_sec = parse_time_to_seconds(task.start)
                    end_sec = parse_time_to_seconds(task.end)
                    if end_sec <= start_sec:
                        issue = f"end 必须大于 start({task.start} -> {task.end})"
                    elif not task.source_exists:
                        issue = "源视频不存在"
                    elif task.source_duration and end_sec > task.source_duration + 0.5:
                        issue = (f"end={task.end} 超出源时长"
                                 f" {task.source_duration:.1f}s,成片会提前截断")
                    elif task.source in self._source_health and not self._source_health[task.source][0]:
                        errors = self._source_health[task.source][1]
                        issue = f"源视频疑似损坏(ffprobe 报告 {errors} 个错误),无法可靠切割"
                    elif not self.cfg.overwrite:
                        out = output_dir / task.output
                        if out.exists() and not is_valid_output(out):
                            issue = "输出已存在但不像完整文件(疑似上次中断)"
            except ValueError as exc:
                issue = str(exc)
            if issue != task.problem:
                self._patch_task(task, problem=issue)
            if issue:
                problems.append(task)
        return problems

    # ---------- 执行 ----------

    def start(self, tasks: list[ClipTask] | None = None) -> None:
        """跑一轮。tasks=None 跑全部 pending;传入子集可做「只跑失败项」。"""
        if self.stage == "running":
            raise RuntimeError("引擎已在运行")
        run_list = tasks if tasks is not None else [
            t for t in self.tasks if t.status in ("pending", "fail")
        ]
        if not run_list:
            self._log("没有需要执行的任务")
            self._emit({"type": "done", "summary": self._summary([])})
            return

        self._stop.clear()
        self._force_stop.clear()
        self._started_at = time.monotonic()
        ffmpeg = resolve_ffmpeg(self.cfg.ffmpeg)
        self.cfg.ffmpeg = ffmpeg
        self.encoder = self.resolve_encoder() if self.cfg.mode in ("encode", "smart") else None
        workers = self.cfg.workers or (os.cpu_count() or 4)
        total = len(run_list)
        self._log(f"开始:{total} 个任务 · {self.cfg.mode} 模式 · {workers} 并发 · "
                  f"编码器 {self.encoder or 'libx264'}")

        def run_one(task: ClipTask) -> None:
            if self._stop.is_set():
                self._patch_task(task, status="cancel")
                return
            self._run_single(task, ffmpeg, total)

        self.stage = "running"
        self._emit({"type": "stage", "stage": "running"})
        self._emit_stats()
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            list(executor.map(run_one, run_list))
        finally:
            executor.shutdown(wait=True)
            self.stage = "done" if not self._stop.is_set() else "idle"
            self._emit({"type": "stage", "stage": self.stage})
            summary = self._summary(run_list)
            self._emit({"type": "done", "summary": summary})
            self._log(
                f"结束:{summary['done']} 成功 / {summary['skip']} 跳过 / "
                f"{summary['fail']} 失败 / {summary['cancel']} 取消 · 用时 {summary['elapsed']}s"
            )

    def _run_single(self, task: ClipTask, ffmpeg: str, total: int) -> None:
        try:
            start_sec = parse_time_to_seconds(task.start)
            end_sec = parse_time_to_seconds(task.end)
            if end_sec <= start_sec:
                raise ValueError(f"end 必须大于 start({task.start} -> {task.end})")
            input_path = Path(self.cfg.raw_dir) / task.source
            output_path = Path(self.cfg.output_dir) / task.output
            if not input_path.is_file():
                raise FileNotFoundError(f"源视频不存在: {task.source}")
            if output_path.exists() and not self.cfg.overwrite:
                if is_valid_output(output_path):
                    self._patch_task(task, status="skip", output_mb=output_path.stat().st_size / 1048576)
                    self._log(f"[{task.index}/{total}] 跳过(已完成) {task.output}")
                    self._emit_stats()
                    return
                raise FileExistsError(f"输出已存在但疑似不完整: {task.output}")
            output_path.parent.mkdir(parents=True, exist_ok=True)

            eff_start, eff_end = start_sec, end_sec
            eff_mode = self.cfg.mode
            if self.cfg.mode == "smart":
                # Smart Cut:起/终各自就近吸附关键帧 → 整段无损;
                # 任一端找不到(10s 窗口失败再试 60s)→ 回落精准重编码。
                snapped = None
                for width in (10.0, 60.0):
                    ka = self.probe_keyframes(task.source, start_sec, width)
                    kb = self.probe_keyframes(task.source, end_sec, width)
                    kf_start, kf_end = pick_keyframes(ka + kb, start_sec, end_sec)
                    if kf_start is not None and kf_end is not None and kf_end - kf_start > 0.2:
                        snapped = (kf_start, kf_end)
                        break
                if snapped:
                    eff_start, eff_end = snapped
                    eff_mode = "copy"
                    self._log(f"[{task.index}/{total}] Smart:吸附关键帧 "
                              f"{eff_start:.2f}→{eff_end:.2f}(无损)")
                else:
                    eff_mode = "encode"  # 回落:精准重编码

            # 切割前预检:目标区间读不到视频包 → 提前报错,不让用户等半天 ffmpeg 才失败
            window_ok, window_msg = self._probe_window_ok(task.source, eff_start, eff_end)
            if not window_ok:
                raise RuntimeError(f"预检失败: {window_msg}")

            media = self._probe_media(task.source)
            command = build_command(
                ffmpeg, input_path, output_path, eff_start, eff_end,
                eff_mode, self.cfg.overwrite, self.cfg.crf,
                self.encoder, with_progress=True,
                has_audio=media.get("has_audio", True),
            )
            self._patch_task(task, status="running", progress=0.0, error="",
                             planned_duration=eff_end - eff_start)
            self._log(f"[{task.index}/{total}] 切割 {task.source} {task.start}-{task.end}")
            started = time.monotonic()
            code, message = self._run_ffmpeg(command, task, eff_start, eff_end)
            if code != 0:
                raise RuntimeError(message or f"ffmpeg 退出码 {code}")
            size = output_path.stat().st_size
            if size < MIN_VALID_OUTPUT_BYTES:
                raise RuntimeError(f"ffmpeg 结束但输出缺失或过小({size} B)")
            verdict, reason = self.verify_output(task)
            if verdict == "bad":
                # 成品对不上 → 视为失败,让用户知道而不是收下一堆废片
                raise RuntimeError(f"成品质检未通过({step_reason(reason)})")
            self._patch_task(
                task, status="done", progress=1.0,
                output_mb=size / 1048576, seconds=time.monotonic() - started,
                verify=verdict, verify_reason=reason,
            )
            self._log(f"[{task.index}/{total}] 完成 {task.output} "
                      f"({time.monotonic() - started:.1f}s, {size / 1048576:.1f} MB)")
            self._emit_stats()
        except Exception as exc:  # noqa: BLE001 单任务失败不拖垮整轮
            if self._force_stop.is_set():
                self._patch_task(task, status="cancel", error="已强制停止")
            else:
                self._patch_task(task, status="fail", progress=0.0, error=str(exc))
                self._log(f"[{task.index}/{total}] 失败: {task.output} — {exc}")
            self._emit_stats()

    def _run_ffmpeg(
        self, command: list[str], task: ClipTask, start_sec: float, end_sec: float,
    ) -> tuple[int, str]:
        """跑单个 ffmpeg,边跑边解析 -progress 输出推送任务进度。"""
        duration = max(0.001, end_sec - start_sec)
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
        )
        with self._lock:
            self._procs.add(proc)
        stderr_text = ""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                state = parse_progress_line(line, {})
                if "out_sec" in state:
                    fraction = (state["out_sec"]) / duration
                    if 0.0 <= fraction <= 1.0:
                        self._patch_task(task, progress=fraction,
                                         speed=state.get("speed", task.speed))
                if state.get("ended"):
                    break
            stderr_text = (proc.stderr.read() or "").strip() if proc.stderr else ""
            code = proc.wait(timeout=self.cfg.timeout or None)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.returncode or 1, f"ffmpeg 超时({self.cfg.timeout}s)被终止"
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self._force_stop.is_set():
            return code, "已强制停止"
        return code, stderr_text

    # ---------- 控制与报告 ----------

    def stop(self, force: bool = False) -> None:
        """force=False 温和停止(不派新任务,等在跑的结束);True 立即强杀。"""
        if force:
            self._force_stop.set()
            self._stop.set()
            with self._lock:
                procs = list(self._procs)
            for proc in procs:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._log("[强制] 已终止全部 ffmpeg 进程")
        else:
            self._stop.set()
            self._log("[停止] 不再派新任务,等待在跑任务结束(再点一次停止=强制)")

    def update_task(
        self, index: int,
        start: str | None = None, end: str | None = None,
        output: str | None = None, source: str | None = None,
    ) -> ClipTask:
        """UI 行内编辑:更新任务与原始行,重新探测/校验该行。"""
        with self._lock:
            task = next((t for t in self.tasks if t.index == index), None)
        if task is None:
            raise ValueError(f"任务不存在: {index}")
        if self.stage == "running":
            raise RuntimeError("运行中不能编辑任务")
        updates = {"start": start, "end": end, "output": output, "source": source}
        with self._lock:
            for key, value in updates.items():
                if value is not None and value.strip():
                    value = value.strip()
                    setattr(task, key, value)
                    if key in self.raw_rows[index - 1] or self.fieldnames:
                        self.raw_rows[index - 1][key] = value
        src = Path(self.cfg.raw_dir) / task.source
        task.source_exists = src.is_file()
        task.source_duration = self._probe_duration(task.source)
        problem = ""
        try:
            start_sec = parse_time_to_seconds(task.start)
            end_sec = parse_time_to_seconds(task.end)
            if end_sec <= start_sec:
                problem = f"end 必须大于 start({task.start} -> {task.end})"
            elif not task.source_exists:
                problem = "源视频不存在"
            elif task.source_duration and end_sec > task.source_duration + 0.5:
                problem = f"end={task.end} 超出源时长 {task.source_duration:.1f}s"
        except ValueError as exc:
            problem = str(exc)
        self._patch_task(task, problem=problem, error="")
        return task

    def save_csv(self) -> Path:
        """把(可能被 UI 编辑过的)原始行写回 CSV(utf-8-sig,保留全部列)。"""
        path = Path(self.cfg.csv_path)
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.raw_rows)
        self._log(f"清单已写回 {path}")
        return path

    def probe_keyframes(self, source: str, t: float, width: float = 10.0) -> list[float]:
        """局部读 [t-width/2, t+width/2] 的关键帧(-read_intervals 纯 IO,秒级)。"""
        probe = ffprobe_path(self.cfg.ffmpeg)
        if not probe:
            return []
        lo = max(0.0, t - width / 2)
        hi = t + width / 2
        cache_key = (source, round(lo, 3), round(hi, 3))
        if cache_key in self._kf_cache:
            return self._kf_cache[cache_key]
        cmd = [
            probe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
            "-read_intervals", f"{lo:.3f}%{hi:.3f}",
            str(Path(self.cfg.raw_dir) / source),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 errors="replace", timeout=120)
        except Exception:
            return []
        keys = [k for k in parse_keyframes_csv(out.stdout) if lo - 0.01 <= k <= hi + 0.01]
        self._kf_cache[cache_key] = keys
        return keys

    def detect_scenes(self, source: str, threshold: float = 0.35) -> list[float]:
        """场景切换点(降采样 scale=480 + fps=8 提速 10~30 倍;监控阈值建议 0.2~0.5)。
        LosslessCut 生产写法:metadata=print:file=-:direct=1(direct 防管道缓冲)。"""
        vf = (
            "scale=480:-2,fps=8,"
            f"select='gt(scene\,{threshold:g})',metadata=print:file=-:direct=1"
        )
        cmd = [
            self.cfg.ffmpeg, "-v", "info", "-nostdin",
            "-i", str(Path(self.cfg.raw_dir) / source),
            "-an", "-sn", "-vf", vf, "-f", "null", "-",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors="replace", timeout=3600)
        return parse_scene_points(out.stdout)

    def detect_black(self, source: str, min_dur: float = 0.5) -> list[tuple[float, float, float]]:
        """黑场/信号丢失段。监控噪点黑屏需要放宽:pix_th 0.15 + pic_th 0.95 + d 0.5。"""
        vf = f"blackdetect=d={min_dur:g}:pic_th=0.95:pix_th=0.15"
        cmd = [
            self.cfg.ffmpeg, "-v", "info", "-nostdin",
            "-i", str(Path(self.cfg.raw_dir) / source),
            "-an", "-vf", vf, "-f", "null", "-",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors="replace", timeout=3600)
        return parse_blackdetect(out.stderr)

    def adopt_rows(self, rows: list[dict[str, str]]) -> int:
        """把(自动检测生成的)行装载为当前任务清单;output 走命名模板。"""
        extra_cols = [c for c in ("labels", "review_status", "note")
                      if rows and any((r.get(c) or "").strip() for r in rows)]
        self.fieldnames = ["source", "start", "end", "output", *extra_cols]
        for index, row in enumerate(rows, start=1):
            if not (row.get("output") or "").strip():
                row["output"] = expand_output(
                    self.cfg.output_template, index,
                    row.get("source", ""), row.get("start", ""), row.get("end", ""),
                )
        self.raw_rows = rows
        self.tasks = []
        for index, row in enumerate(rows, start=1):
            self.tasks.append(ClipTask(
                index=index,
                source=(row.get("source") or "").strip(),
                start=(row.get("start") or "").strip(),
                end=(row.get("end") or "").strip(),
                output=(row.get("output") or "").strip(),
                labels=(row.get("labels") or "").strip(),
                review_status=(row.get("review_status") or "").strip(),
            ))
            try:
                self.tasks[-1].planned_duration = (
                    parse_time_to_seconds(self.tasks[-1].end)
                    - parse_time_to_seconds(self.tasks[-1].start))
            except ValueError:
                self.tasks[-1].planned_duration = 0.0
        self._probe_all_sources()
        self.validate()
        return len(self.tasks)

    def verify_output(self, task: "ClipTask") -> tuple[str, str]:
        """切割后质检:时长对得上?黑场比例?返回 (ok/warn/bad, 说明)。"""
        out = Path(self.cfg.output_dir) / task.output
        if not out.is_file():
            return "bad", "成品文件不存在"
        expected = task.planned_duration or 0.0
        probe = ffprobe_path(self.cfg.ffmpeg)
        if not probe:
            return "ok", ""
        try:
            res = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
                capture_output=True, text=True, errors="replace", timeout=60)
            duration = float((res.stdout or "0").strip() or 0)
        except Exception:
            return "bad", "成品无法解析(ffprobe 读取失败)"
        black_pct = 0.0
        try:
            res = subprocess.run(
                [self.cfg.ffmpeg, "-v", "info", "-nostdin", "-i", str(out),
                 "-an", "-vf", "blackdetect=d=0.3:pic_th=0.95:pix_th=0.15", "-f", "null", "-"],
                capture_output=True, text=True, errors="replace", timeout=120)
            black_pct = parse_black_pct(res.stderr, duration)
        except Exception:
            pass
        return judge_output(duration, expected, black_pct)

    def retry_failed(self) -> int:
        """把失败任务重置为 pending 并返回数量(随后 start 即只跑这些)。"""
        retried = 0
        with self._lock:
            for task in self.tasks:
                if task.status == "fail":
                    task.status = "pending"
                    task.error = ""
                    retried += 1
        if retried:
            self._log(f"已重置 {retried} 个失败任务为待执行")
        return retried

    def failed_report_path(self) -> Path:
        output_dir = Path(self.cfg.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / "failed_clips.csv"

    def write_failed_report(self) -> Path | None:
        """失败清单写 CSV(utf-8-sig,Excel 直接双击可读);无失败返回 None。"""
        failed = [t for t in self.tasks if t.status == "fail"]
        if not failed:
            return None
        path = self.failed_report_path()
        fieldnames = ["row_index", "source", "start", "end", "output", "error"]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for t in failed:
                writer.writerow({
                    "row_index": t.index, "source": t.source, "start": t.start,
                    "end": t.end, "output": t.output, "error": t.error,
                })
        self._log(f"失败清单已写入 {path}")
        return path

    def _summary(self, run_list: list[ClipTask]) -> dict:
        counts = self._counts()
        return {
            "total": len(run_list),
            "done": counts["done"], "skip": counts["skip"],
            "fail": counts["fail"], "cancel": counts["cancel"],
            "elapsed": round(time.monotonic() - self._started_at, 1) if self._started_at else 0,
            "interrupted": self._stop.is_set(),
            "failed_report": str(self.write_failed_report() or ""),
        }


