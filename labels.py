#!/usr/bin/env python3
"""BatchCut Studio · 标注库(SQLite,零第三方依赖)。

数据集生产流水线的第三环:切片(引擎)→ 标注(本模块)→ 导出(训练集)。

设计要点:
  - 每个切片以「相对输出路径」为主键——引擎重跑/服务重启后标签依然挂得住
  - 片段可多标签(labels 行),状态机单行(status)
  - 审计字段沿用 video_labeler 的习惯:谁在什么时候改的
  - WAL 模式 + 每方法短事务,Web 多线程读写安全
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from pathlib import Path

# video_labeler 内置行为标签(业务语义资产,原样继承保证迁移兼容)
DEFAULT_LABELS = [
    ("fall", "#f87171", "1"),
    ("strangers_climbs", "#fb923c", "2"),
    ("strangers_linger", "#fbbf24", "3"),
    ("strangers_peep_car", "#f97316", "4"),
    ("strangers_pick_up_packages", "#a3e635", "5"),
    ("cat_come", "#38bdf8", "6"),
    ("cat_out", "#818cf8", "7"),
    ("dog_come", "#2dd4bf", "8"),
    ("dog_out", "#c084fc", "9"),
    ("pool", "#64748b", ""),
]

# 审核四态:与 video_labeler 的 REVIEW_STATUSES 逐字对齐(迁移脚本可直接映射)
STATUSES = {
    "pending": "待审核",
    "approved": "已通过",
    "needs_fix": "需修正",
    "rejected": "已剔除",
}


def import_full_csv(store: "LabelStore", path: Path, reviewer: str = "migrate") -> int:
    """video_labeler 完整版 CSV(25 列)→ 标签库。behaviors 列是 JSON 字符串;
    review_status 四值直接映射;缺失标签自动注册。返回导入片段数。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    existing = {l["name"]: l["id"] for l in store.list_labels()}
    clips = set()
    now = time.time()
    with store._lock:
        for row in rows:
            clip = (row.get("output") or "").strip()
            if not clip:
                continue
            clips.add(clip)
            try:
                behaviors = json.loads(row.get("behaviors") or "[]")
            except json.JSONDecodeError:
                behaviors = [b.strip() for b in (row.get("behaviors") or "").split("+") if b.strip()]
            for name in behaviors:
                name = name.strip()
                if not name:
                    continue
                if name not in existing:
                    cur = store._conn.execute(
                        "INSERT INTO labels(name, color, hotkey, created_at) VALUES (?,?,?,?)",
                        (name, "#8b5cf6", "", now))
                    existing[name] = cur.lastrowid
                store._conn.execute(
                    "INSERT OR IGNORE INTO clip_labels(clip, label_id, updated_by, updated_at) "
                    "VALUES (?,?,?,?)", (clip, existing[name], row.get("annotator") or "migrate", now))
            status = (row.get("review_status") or "").strip()
            if status in STATUSES:
                store._conn.execute(
                    "INSERT INTO clip_status(clip, status, updated_by, note, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(clip) DO UPDATE SET status=excluded.status",
                    (clip, status, row.get("reviewer") or reviewer,
                     row.get("review_comment") or "", now))
        store._conn.commit()
    return len(clips)


class LabelStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL DEFAULT '#8b5cf6',
                hotkey TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clip_status (
                clip TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                updated_by TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clip_labels (
                clip TEXT NOT NULL,
                label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                UNIQUE(clip, label_id)
            );
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewer TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                reviewed_at REAL NOT NULL
            );
            """)
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM labels")
            if cur.fetchone()["n"] == 0:
                now = time.time()
                for name, color, hotkey in DEFAULT_LABELS:
                    self._conn.execute(
                        "INSERT INTO labels(name, color, hotkey, created_at) VALUES (?,?,?,?)",
                        (name, color, hotkey, now))
            self._conn.commit()

    # ---------- 标签定义 ----------

    def list_labels(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, color, hotkey FROM labels ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_label(self, name: str, color: str = "#8b5cf6", hotkey: str = "") -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO labels(name, color, hotkey, created_at) VALUES (?,?,?,?)",
                (name.strip(), color, hotkey, time.time()))
            self._conn.commit()
        row = self._conn.execute(
            "SELECT id, name, color, hotkey FROM labels WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def delete_label(self, label_id: int) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
            self._conn.commit()
        return cur.rowcount

    # ---------- 片段标注 ----------

    def toggle_clip_label(self, clip: str, label_id: int, updated_by: str = "") -> bool:
        """切换标签;返回切换后是否打上了。"""
        with self._lock:
            hit = self._conn.execute(
                "SELECT 1 FROM clip_labels WHERE clip=? AND label_id=?",
                (clip, label_id)).fetchone()
            if hit:
                self._conn.execute(
                    "DELETE FROM clip_labels WHERE clip=? AND label_id=?", (clip, label_id))
                on = False
            else:
                self._conn.execute(
                    "INSERT INTO clip_labels(clip, label_id, updated_by, updated_at) VALUES (?,?,?,?)",
                    (clip, label_id, updated_by, time.time()))
                on = True
            # 有标签即视为完成标注:无状态行的片段自动登记为 pending(进审核流)
            st = self._conn.execute(
                "SELECT 1 FROM clip_status WHERE clip=?", (clip,)).fetchone()
            if st is None:
                self._conn.execute(
                    "INSERT INTO clip_status(clip, status, updated_by, updated_at) "
                    "VALUES (?,'pending',?,?)", (clip, updated_by, time.time()))
            self._conn.commit()
        return on

    def labels_for(self, clip: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT label_id FROM clip_labels WHERE clip=?", (clip,)).fetchall()
        return [r["label_id"] for r in rows]

    def set_clip_status(self, clip: str, status: str, updated_by: str = "", note: str = "") -> dict:
        if status not in STATUSES:
            raise ValueError(f"未知状态: {status}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO clip_status(clip, status, updated_by, note, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(clip) DO UPDATE SET "
                "status=excluded.status, updated_by=excluded.updated_by, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (clip, status, updated_by, note, now))
            # append-only 审核日志(video_labeler review_history 的语义继承)
            self._conn.execute(
                "INSERT INTO review_history(clip, status, reviewer, comment, reviewed_at) "
                "VALUES (?,?,?,?,?)", (clip, status, updated_by, note, now))
            self._conn.commit()
        return self.clip_record(clip)

    def clip_record(self, clip: str) -> dict:
        st = self._conn.execute(
            "SELECT status, updated_by, note, updated_at FROM clip_status WHERE clip=?",
            (clip,)).fetchone()
        labels = self.labels_for(clip)
        return {
            "clip": clip,
            "status": st["status"] if st else "pending",
            "updatedBy": st["updated_by"] if st else "",
            "note": st["note"] if st else "",
            "labelIds": labels,
        }

    def all_records(self) -> dict[str, dict]:
        out = {}
        for row in self._conn.execute("SELECT clip FROM clip_status"):
            rec = self.clip_record(row["clip"])
            out[rec["clip"]] = rec
        for row in self._conn.execute("SELECT DISTINCT clip FROM clip_labels"):
            if row["clip"] not in out:
                out[row["clip"]] = self.clip_record(row["clip"])
        return out

    def stats(self) -> dict:
        counts = {k: 0 for k in STATUSES}
        for rec in self.all_records().values():
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        return counts

    # ---------- 导出 ----------

    def manifest_rows(self, include_statuses: tuple[str, ...] = ("approved",)) -> list[dict]:
        """训练集 manifest:按标签展开(一个片段多个标签 = 多行)。"""
        rows = []
        for clip, rec in sorted(self.all_records().items()):
            if rec["status"] not in include_statuses or not rec["labelIds"]:
                continue
            label_names = []
            for lid in rec["labelIds"]:
                row = self._conn.execute(
                    "SELECT name FROM labels WHERE id=?", (lid,)).fetchone()
                if row:
                    label_names.append(row["name"])
            for name in label_names:
                rows.append({"clip": clip, "label": name, "status": rec["status"]})
        return rows
