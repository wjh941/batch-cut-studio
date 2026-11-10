#!/usr/bin/env python3
"""BatchCut Studio · 本地 Web 服务(零第三方依赖,标准库 http.server)。

    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000

路由:
    GET  /                     静态前端(static/)
    GET  /api/state            当前配置 + 任务清单 + 统计
    POST /api/config           保存设置(路径/模式/并发…),持久化到 config.json
    POST /api/load             读取 CSV 装载任务并校验(dry-run 数据源)
    POST /api/start            开始执行
    POST /api/stop             停止 {force: true|false}
    POST /api/retry            失败任务重置为待执行
    GET  /api/events           SSE 实时事件流
    GET  /media/source/<p>     源视频(带 Range,支持拖动)
    GET  /media/output/<p>     切好的成品视频(带 Range)

安全:仅监听 127.0.0.1;/media 做目录逃逸防护。路径来自本机配置,不对外网暴露。
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import threading
import time
import urllib.parse
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import CutConfig, CutEngine, segments_from_points
from labels import LabelStore, STATUSES, import_full_csv

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CONFIG_FILE = ROOT / "config.json"
HOST = "127.0.0.1"


# ======================================================================
# 全局单例:配置 + 引擎 + SSE 广播
# ======================================================================

def load_config() -> CutConfig:
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            fields = {f for f in CutConfig.__dataclass_fields__}
            return CutConfig(**{k: data[k] for k in data if k in fields})
        except Exception:
            pass
    return CutConfig()


def save_config(cfg: CutConfig) -> None:
    CONFIG_FILE.write_text(json.dumps({
        "raw_dir": cfg.raw_dir, "output_dir": cfg.output_dir,
        "csv_path": cfg.csv_path, "ffmpeg": cfg.ffmpeg,
        "mode": cfg.mode, "overwrite": cfg.overwrite,
        "hw_encoder": cfg.hw_encoder, "crf": cfg.crf,
        "workers": cfg.workers, "timeout": cfg.timeout,
        "output_template": cfg.output_template,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()


class Broadcast:
    """引擎事件 -> SSE 订阅队列;保留最近 400 条供新连接回放。"""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: list[dict] = []

    def push(self, event: dict) -> None:
        with self._lock:
            self._history.append(event)
            if len(self._history) > 400:
                self._history = self._history[-400:]
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(sub)
        for event in self._history:
            sub.put_nowait(event)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)


BROADCAST = Broadcast()
LABELS = LabelStore(ROOT / "labeldb.sqlite3")
ENGINE = CutEngine(CONFIG, on_event=lambda e: BROADCAST.push(e))


def snapshot() -> dict:
    with ENGINE._lock:
        tasks = [t.public() for t in ENGINE.tasks]
    counts = ENGINE._counts()
    return {
        "config": {
            "rawDir": CONFIG.raw_dir, "outputDir": CONFIG.output_dir,
            "csvPath": CONFIG.csv_path, "ffmpeg": CONFIG.ffmpeg,
            "mode": CONFIG.mode, "overwrite": CONFIG.overwrite,
            "hwEncoder": CONFIG.hw_encoder, "crf": CONFIG.crf,
            "workers": CONFIG.workers, "timeout": CONFIG.timeout,
            "outputTemplate": CONFIG.output_template,
        },
        "stage": ENGINE.stage,
        "tasks": tasks,
        "counts": counts,
    }


# ======================================================================
# HTTP Handler
# ======================================================================

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
}
VIDEO_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".flv": "video/x-flv",
    ".webm": "video/webm",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "BatchCutStudio/1.0"

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # 静默访问日志,SSE 心跳太吵
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---------- GET ----------

    def do_GET(self):  # noqa: N802 (http.server 命名约定)
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self._json(snapshot())
        if path == "/api/sources":
            files = []
            try:
                for entry in sorted(Path(CONFIG.raw_dir).iterdir()):
                    if entry.is_file() and entry.suffix.lower() in VIDEO_MIME:
                        files.append(entry.name)
            except OSError:
                pass
            return self._json({"files": files})
        if path == "/api/keyframes":
            qs = parse_qs(urlparse(self.path).query)
            source = (qs.get("source") or [""])[0].strip()
            src = Path(CONFIG.raw_dir) / source
            if not source or not src.is_file():
                return self._json({"error": "源视频不存在"}, 404)
            duration = ENGINE._probe_duration(source) or 0.0
            keys = ENGINE.probe_keyframes(source, duration / 2, width=duration + 20) if duration else []
            return self._json({"source": source, "duration": duration, "keyframes": keys})
        if path == "/api/labels":
            return self._json({"labels": LABELS.list_labels()})
        if path == "/api/annotations":
            return self._json({"records": LABELS.all_records(), "statuses": STATUSES})
        if path == "/api/events":
            return self._sse()
        if path.startswith("/media/source/") or path.startswith("/media/output/"):
            return self._serve_media(path)
        return self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            file_path = STATIC_DIR / "index.html"
        else:
            file_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                return self._json({"error": "forbidden"}, 403)
        if not file_path.is_file():
            return self._json({"error": "not found"}, 404)
        body = file_path.read_bytes()
        ext = file_path.suffix.lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, path: str) -> None:
        """带 Range 的视频服务(<video> 拖动进度必需)。"""
        rel = path[len("/media/"):] if path.startswith("/media/") else ""
        rel = urllib.parse.unquote(rel)
        if rel.startswith("source/"):
            root = Path(CONFIG.raw_dir)
            rel = rel[len("source/"):]
        elif rel.startswith("output/"):
            root = Path(CONFIG.output_dir)
            rel = rel[len("output/"):]
        else:
            return self._json({"error": "not found"}, 404)
        target = (root / rel).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        size = target.stat().st_size
        mime = VIDEO_MIME.get(target.suffix.lower(), "application/octet-stream")
        range_header = self.headers.get("Range") or ""
        start, end = 0, size - 1
        status = 200
        if range_header.startswith("bytes="):
            try:
                start_s, _, end_s = range_header[6:].partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _sse(self) -> None:
        sub = BROADCAST.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.write(f"data: {json.dumps({'type': 'snapshot', 'state': snapshot()}, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    event = sub.get(timeout=15)
                    payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            BROADCAST.unsubscribe(sub)

    # ---------- POST ----------

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/config":
            return self._update_config(body)
        if path == "/api/load":
            return self._load_csv()
        if path == "/api/start":
            return self._start()
        if path == "/api/stop":
            ENGINE.stop(force=bool(body.get("force")))
            return self._json({"ok": True})
        if path == "/api/retry":
            count = ENGINE.retry_failed()
            return self._json({"ok": True, "retried": count})
        if path == "/api/autolist":
            return self._autolist(body)
        if path == "/api/adopt":
            return self._adopt(body)
        if path == "/api/labels":
            return self._json({"labels": LABELS.list_labels()})
        if path == "/api/labels-add":
            label = LABELS.add_label(body.get("name", ""), body.get("color", "#8b5cf6"),
                                     body.get("hotkey", ""))
            return self._json({"ok": True, "label": label})
        if path == "/api/labels-delete":
            removed = LABELS.delete_label(int(body.get("id", 0)))
            return self._json({"ok": removed > 0})
        if path == "/api/annotate":
            clip = (body.get("clip") or "").strip()
            if not clip:
                return self._json({"error": "缺少 clip"}, 400)
            if body.get("labelId") is not None:
                on = LABELS.toggle_clip_label(clip, int(body["labelId"]),
                                              body.get("reviewer", "local-user"))
                return self._json({"ok": True, "on": on, "record": LABELS.clip_record(clip)})
            if body.get("status"):
                try:
                    rec = LABELS.set_clip_status(clip, body["status"],
                                                 body.get("reviewer", "local-user"),
                                                 body.get("note", ""))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True, "record": rec})
            return self._json({"error": "缺少 labelId 或 status"}, 400)
        if path == "/api/annotations":
            return self._json({"records": LABELS.all_records(), "statuses": STATUSES})
        if path == "/api/export-dataset":
            return self._export_dataset(body)
        if path == "/api/migrate-vl":
            src = (body.get("path") or "").strip()
            p_in = Path(src)
            if not p_in.is_file():
                return self._json({"error": f"文件不存在: {src}"}, 400)
            count = import_full_csv(LABELS, p_in)
            return self._json({"ok": True, "clips": count})
        if path == "/api/save-csv":
            path_out = ENGINE.save_csv()
            return self._json({"ok": True, "path": str(path_out)})
        if path == "/api/update-task":
            if ENGINE.stage == "running":
                return self._json({"error": "运行中不能编辑任务"}, 409)
            try:
                task = ENGINE.update_task(
                    int(body.get("index", 0)),
                    start=body.get("start"), end=body.get("end"),
                    output=body.get("output"), source=body.get("source"),
                )
                if body.get("save"):
                    ENGINE.save_csv()
                BROADCAST.push({"type": "task", "index": task.index, "patch": task.public()})
                return self._json({"ok": True, "task": task.public(), "problem": task.problem})
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 400)
        return self._json({"error": "not found"}, 404)

    def _update_config(self, body: dict) -> None:
        mapping = {
            "rawDir": "raw_dir", "outputDir": "output_dir", "csvPath": "csv_path",
            "ffmpeg": "ffmpeg", "mode": "mode", "overwrite": "overwrite",
            "hwEncoder": "hw_encoder", "crf": "crf", "workers": "workers",
            "timeout": "timeout", "outputTemplate": "output_template",
        }
        for src, dst in mapping.items():
            if src in body:
                setattr(CONFIG, dst, body[src])
        if ENGINE.stage == "running":
            return self._json({"error": "运行中不能修改配置"}, 409)
        save_config(CONFIG)
        self._json({"ok": True, "config": snapshot()["config"]})

    def _load_csv(self) -> None:
        if ENGINE.stage == "running":
            return self._json({"error": "运行中不能重新装载"}, 409)
        try:
            if not Path(CONFIG.raw_dir).is_dir():
                return self._json({"error": f"来源目录不存在: {CONFIG.raw_dir}"}, 400)
            body = self._body()
            content = body.get("content")
            if content:
                uploads = ROOT / "uploads"
                uploads.mkdir(exist_ok=True)
                uploaded = uploads / f"clips_{int(time.time())}.csv"
                uploaded.write_text(content, encoding="utf-8")
                CONFIG.csv_path = str(uploaded)
                save_config(CONFIG)
            if not Path(CONFIG.csv_path).is_file():
                return self._json({"error": f"CSV 不存在: {CONFIG.csv_path}"}, 400)
            Path(CONFIG.output_dir).mkdir(parents=True, exist_ok=True)
            total = ENGINE.load_csv()
            problems = ENGINE.validate()
            BROADCAST.push({"type": "snapshot", "state": snapshot()})
            self._json({
                "ok": True, "total": total,
                "csvPath": CONFIG.csv_path,
                "problems": [{"index": t.index, "problem": t.problem} for t in problems],
            })
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 400)

    def _autolist(self, body: dict) -> None:
        """异步跑场景/黑场检测,结果经 SSE autolist-done 推送。"""
        if ENGINE.stage == "running":
            return self._json({"error": "运行中不能检测"}, 409)
        source = (body.get("source") or "").strip()
        src = Path(CONFIG.raw_dir) / source
        if not source or not src.is_file():
            return self._json({"error": f"源视频不存在: {source}"}, 400)
        kind = body.get("kind", "scene")
        threshold = float(body.get("threshold", 0.35))
        min_seg = float(body.get("minSeg", 2.0))

        def worker() -> None:
            try:
                duration = ENGINE._probe_duration(source)
                if kind == "black":
                    ranges = ENGINE.detect_black(source, min_dur=min(min_seg, 0.5))
                    segments = [(round(a, 3), round(b, 3)) for a, b, _ in ranges]
                    ENGINE._log(f"黑场检测完成:{len(segments)} 段({source})")
                else:
                    points = ENGINE.detect_scenes(source, threshold=threshold)
                    segments = segments_from_points(
                        points, duration or 0.0,
                        min_seg=min_seg, merge=min(1.5, min_seg * 0.75))
                    ENGINE._log(f"场景检测完成:{len(points)} 个切点 → {len(segments)} 个片段({source})")
                BROADCAST.push({"type": "autolist-done", "source": source,
                                "kind": kind, "segments": segments})
            except Exception as exc:  # noqa: BLE001
                BROADCAST.push({"type": "log", "message": f"自动检测失败: {exc}"})
                BROADCAST.push({"type": "autolist-done", "source": source,
                                "kind": kind, "segments": []})

        threading.Thread(target=worker, daemon=True).start()
        self._json({"ok": True})

    def _adopt(self, body: dict) -> None:
        source = (body.get("source") or "").strip()
        segments = body.get("segments") or []
        if not source or not segments:
            return self._json({"error": "缺少 source 或 segments"}, 400)
        rows = [{"source": source, "start": f"{a:.3f}", "end": f"{b:.3f}", "output": ""}
                for a, b in segments]
        total = ENGINE.adopt_rows(rows)
        BROADCAST.push({"type": "snapshot", "state": snapshot()})
        self._json({"ok": True, "total": total})

    def _export_dataset(self, body: dict) -> None:
        """审核通过(可选+已标注)的切片 → dataset/{train,val} + classes.txt + labels/*.txt + manifest.csv。"""
        import shutil
        ratio = float(body.get("ratio", 0.8))
        rows = LABELS.manifest_rows()
        if not rows:
            return self._json({"error": "没有可导出的已审核标注"}, 400)
        classes = sorted({r["label"] for r in rows})
        class_ids = {name: i for i, name in enumerate(classes)}
        out_root = Path(CONFIG.output_dir) / "dataset"
        for split in ("train", "val"):
            (out_root / split).mkdir(parents=True, exist_ok=True)
            (out_root / split / "labels").mkdir(exist_ok=True)
        manifest_path = out_root / "manifest.csv"
        counts = {"train": 0, "val": 0}
        seen = {}
        with manifest_path.open("w", encoding="utf-8-sig", newline="") as mf:
            writer = csv.writer(mf)
            writer.writerow(["split", "clip", "label", "class_id"])
            for r in rows:
                if r["clip"] not in seen:
                    # 80/20 轮转分配(同片段文件只复制一次,多标签共用该文件)
                    split = "val" if (counts["train"] + counts["val"]) % 5 == 4 else "train"
                    src = Path(CONFIG.output_dir) / r["clip"]
                    if not src.is_file():
                        continue
                    shutil.copy2(src, out_root / split / r["clip"])
                    seen[r["clip"]] = split
                    counts[split] += 1
                split = seen[r["clip"]]
                stem = Path(r["clip"]).stem
                with (out_root / split / "labels" / (stem + ".txt")).open("a", encoding="utf-8") as lf:
                    lf.write(str(class_ids[r["label"]]) + chr(10))
                writer.writerow([split, r["clip"], r["label"], class_ids[r["label"]]])
        (out_root / "classes.txt").write_text(chr(10).join(classes), encoding="utf-8")
        self._json({"ok": True, "classes": classes, "train": counts["train"],
                    "val": counts["val"], "path": str(out_root)})

    def _start(self) -> None:
        if ENGINE.stage == "running":
            return self._json({"error": "已在运行"}, 409)
        if not ENGINE.tasks:
            return self._json({"error": "请先装载 CSV"}, 400)
        threading.Thread(target=ENGINE.start, daemon=True).start()
        self._json({"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="BatchCut Studio 本地服务")
    parser.add_argument("--port", type=int, default=8963)
    args = parser.parse_args()

    class Server(ThreadingHTTPServer):
        # Windows 上 SO_REUSEADDR 会允许两个进程静默共用端口(请求被路由给对方,
        # 排查半小时起步)。关掉它:端口被占就大声报错。
        allow_reuse_address = False

    # 这台机器部分端口被底层驱动(代理 TUN/虚拟化)圈走,bind 报 10013;
    # 自动顺延扫描,直到找到真正能绑定的端口。
    chosen = None
    for port in range(args.port, args.port + 40):
        try:
            server = Server((HOST, port), Handler)
            chosen = port
            break
        except OSError as exc:
            if port == args.port + 39:
                print(f"{args.port}~{port} 全部无法绑定({exc}),请手动指定:python server.py --port <port>")
                raise SystemExit(1) from exc
    if chosen != args.port:
        print(f"端口 {args.port} 被系统圈占,自动改用 {chosen}", flush=True)
    print(f"BatchCut Studio → http://{HOST}:{chosen}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")


if __name__ == "__main__":
    main()
