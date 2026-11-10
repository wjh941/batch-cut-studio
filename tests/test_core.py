#!/usr/bin/env python3
"""BatchCut Studio 核心纯函数测试。python -m unittest 或 pytest 皆可跑。"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import (  # noqa: E402
    build_command, format_seconds, is_valid_output, parse_progress_line,
    parse_time_to_seconds, read_rows,
)


class TestTimeParsing(unittest.TestCase):
    def test_hms(self):
        self.assertAlmostEqual(parse_time_to_seconds("01:02:03.5"), 3723.5)
        self.assertAlmostEqual(parse_time_to_seconds("00:00:30"), 30.0)

    def test_ms_and_seconds(self):
        self.assertAlmostEqual(parse_time_to_seconds("90"), 90.0)
        self.assertAlmostEqual(parse_time_to_seconds("02:30.25"), 150.25)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_time_to_seconds("")
        with self.assertRaises(ValueError):
            parse_time_to_seconds("1:2:3:4")

    def test_format_roundtrip(self):
        self.assertEqual(format_seconds(3723.5), "01:02:03.500")
        self.assertEqual(format_seconds(0), "00:00:00.000")


class TestBuildCommand(unittest.TestCase):
    def test_encode_pre_seek_math(self):
        """start=00:05:30(330s):粗寻 320s,精修偏移 10s,时长 15s。"""
        cmd = build_command(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"), 330.0, 345.0,
            "encode", False, 18, None, with_progress=True,
        )
        self.assertIn("-progress", cmd)
        self.assertEqual(cmd[cmd.index("-progress") + 1], "pipe:1")
        ss = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-ss"]
        self.assertEqual(ss, ["00:05:20.000", "10.000"])  # 粗寻 + 精修偏移
        self.assertEqual(cmd[cmd.index("-t") + 1], "15.000")
        self.assertIn("libx264", cmd)

    def test_encode_hw_encoder_params(self):
        cmd = build_command("ffmpeg", Path("i"), Path("o"), 0.0, 5.0,
                            "encode", True, 18, "h264_nvenc")
        self.assertIn("h264_nvenc", cmd)
        self.assertIn("-cq", cmd)
        self.assertIn("-y", cmd)  # overwrite=True

    def test_copy_input_seek_with_negative_ts_guard(self):
        """copy 模式必须输入端寻址 + make_zero(调研结论:输出端寻址在长视频上是性能地雷)。"""
        cmd = build_command("ffmpeg", Path("i.mp4"), Path("o.mp4"), 7200.0, 7210.0,
                            "copy", False, 18, None)
        i_pos = cmd.index("-i")
        ss_pos = cmd.index("-ss")
        self.assertLess(ss_pos, i_pos, "-ss 必须在 -i 之前(输入端寻址)")
        self.assertNotIn("-to", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "10.000")
        self.assertIn("make_zero", cmd)

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            build_command("ffmpeg", Path("i"), Path("o"), 0, 1, "magic", False, 18, None)


class TestProgressParsing(unittest.TestCase):
    def test_out_time_us(self):
        state = {}
        parse_progress_line("out_time_us=1500000", state)
        self.assertAlmostEqual(state["out_sec"], 1.5)

    def test_out_time_ms_is_microseconds(self):
        """ffmpeg 历史 bug:out_time_ms 名不副实,值也是微秒(源码已核实)。"""
        state = {}
        parse_progress_line("out_time_ms=1500000", state)
        self.assertAlmostEqual(state["out_sec"], 1.5)

    def test_speed_and_end(self):
        state = {}
        parse_progress_line("speed=3.4x", state)
        parse_progress_line("progress=end", state)
        self.assertEqual(state["speed"], "3.4x")
        self.assertTrue(state["ended"])

    def test_garbage_ignored(self):
        state = parse_progress_line("stream_0_0_q=28.0", {})
        self.assertEqual(state, {})
        state = parse_progress_line("random line", {})
        self.assertEqual(state, {})


class TestReadRows(unittest.TestCase):
    def _write(self, path, text, encoding):
        path.write_text(text, encoding=encoding)

    def test_utf8_bom_and_missing_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.csv"
            self._write(p, "\ufeffsource,start,end,output\nv.mp4,0,5,o.mp4\n", "utf-8")
            rows = read_rows(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "v.mp4")

    def test_uppercase_headers_losslesscut_style(self):
        """LosslessCut 导出的表头是 Start,End,Name 大写——列名应归一化后识别。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lc.csv"
            self._write(p, "Source,Start,End,Output\nv.mp4,0:01,0:06,o.mp4\n", "utf-8")
            rows = read_rows(p)
            self.assertEqual(rows[0]["start"], "0:01")

    def test_missing_column_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "b.csv"
            self._write(p, "source,start,end\nv.mp4,0,5\n", "utf-8")
            with self.assertRaises(ValueError):
                read_rows(p)

    def test_empty_rows_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.csv"
            self._write(p, "source,start,end,output\n,,,,\nv.mp4,0,5,o.mp4\n", "utf-8")
            self.assertEqual(len(read_rows(p)), 1)


class TestValidOutput(unittest.TestCase):
    def test_size_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.mp4"
            small.write_bytes(b"\x00" * 512)
            big = Path(tmp) / "big.mp4"
            big.write_bytes(b"\x00" * 2048)
            self.assertFalse(is_valid_output(small))
            self.assertTrue(is_valid_output(big))
            self.assertFalse(is_valid_output(Path(tmp) / "missing.mp4"))


if __name__ == "__main__":
    unittest.main()


class TestExpandOutput(unittest.TestCase):
    def test_tokens(self):
        from core import expand_output
        name = expand_output("{source}_{index}_{start}", 7, "cam02.mp4", "00:01:05.2", "0:02:30")
        self.assertEqual(name, "cam02_007_000105")
        name2 = expand_output("{source}_clip{index}", 12, "D:/x/frontdoor.avi", "5", "9")
        self.assertEqual(name2, "frontdoor_clip012")

    def test_bad_time_falls_back(self):
        from core import expand_output
        name = expand_output("{source}_{start}", 1, "a.mp4", "not-a-time", "")
        self.assertEqual(name, "a_000000")


class TestUpdateTaskAndSave(unittest.TestCase):
    def _engine_with_tmp(self, tmp):
        from core import CutConfig, CutEngine
        raw = Path(tmp) / "origin"; out = Path(tmp) / "clips"
        raw.mkdir(); out.mkdir()
        (raw / "v.mp4").write_bytes(b"\x00" * 64)
        csv_path = Path(tmp) / "list.csv"
        csv_path.write_text(
            "source,start,end,output,extra\nv.mp4,0,5,a.mp4,keepme\nv.mp4,10,20,b.mp4,x\n",
            encoding="utf-8")
        cfg = CutConfig(raw_dir=str(raw), output_dir=str(out), csv_path=str(csv_path))
        return CutEngine(cfg, on_event=lambda e: None)

    def test_update_and_write_back_preserves_extra_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_tmp(tmp)
            engine.load_csv()
            task = engine.update_task(1, start="1", end="4", output="renamed.mp4")
            self.assertEqual(task.start, "1")
            engine.save_csv()
            with open(Path(engine.cfg.csv_path), encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["output"], "renamed.mp4")
            self.assertEqual(rows[0]["extra"], "keepme")  # 额外列原样保留
            self.assertEqual(rows[1]["start"], "10")      # 其他行不受影响

    def test_template_fills_empty_output_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_tmp(tmp)
            csv_path = Path(engine.cfg.csv_path)
            csv_path.write_text("source,start,end,output\nv.mp4,0,5,\nv.mp4,10,20,keep.mp4\n",
                                encoding="utf-8")
            engine.load_csv()
            self.assertEqual(engine.tasks[0].output, "v_001_000000")
            self.assertEqual(engine.tasks[1].output, "keep.mp4")  # 已命名的行不动


class TestSmartCutHelpers(unittest.TestCase):
    def test_parse_keyframes_csv(self):
        from core import parse_keyframes_csv
        text = "1.0,K__\n1.041666,___\n2.0,K__\n"
        self.assertEqual(parse_keyframes_csv(text), [1.0, 2.0])

    def test_pick_keyframes_snap_directions(self):
        from core import pick_keyframes
        kfs = [0.0, 2.0, 4.0, 6.0, 8.0]
        # 起点 5 → 前一个关键帧 4(不丢内容);终点 7 → 后一个关键帧 8(完整覆盖)
        self.assertEqual(pick_keyframes(kfs, 5.0, 7.0), (4.0, 8.0))
        # 起点恰在关键帧上 → 精确匹配
        self.assertEqual(pick_keyframes(kfs, 4.0, 6.0), (4.0, 6.0))
        # 窗口内没有起点之前的关键帧 → None(触发回落)
        self.assertEqual(pick_keyframes([6.0, 8.0], 5.0, 7.0), (None, 8.0))


class TestSegmentsFromPoints(unittest.TestCase):
    def test_full_coverage_with_merge_and_tail(self):
        from core import segments_from_points
        # 噪声点 8.2(与 8.0 距离 <1.5 合并);尾段 28→30(2s)恰达 min_seg
        segs = segments_from_points([8.0, 8.2, 16.0, 28.0], 30.0, min_seg=2, merge=1.5)
        self.assertEqual(segs, [(0.0, 8.0), (8.0, 16.0), (16.0, 28.0), (28.0, 30.0)])

    def test_tail_too_short_merges_back(self):
        from core import segments_from_points
        segs = segments_from_points([10.0, 29.0], 30.0, min_seg=2, merge=1.5)
        self.assertEqual(segs, [(0.0, 10.0), (10.0, 30.0)])

    def test_out_of_bounds_points_dropped(self):
        from core import segments_from_points
        segs = segments_from_points([-3.0, 5.0, 99.0], 30.0, min_seg=2, merge=1.5)
        self.assertEqual(segs, [(0.0, 5.0), (5.0, 30.0)])


class TestSceneAndBlackParsing(unittest.TestCase):
    def test_parse_scene_points(self):
        from core import parse_scene_points
        text = "frame:12 pts:480 pts_time:4.8\nlavfi.scene_score=0.512345\nframe:30 pts_time:12.25\n"
        self.assertEqual(parse_scene_points(text), [4.8, 12.25])

    def test_parse_blackdetect(self):
        from core import parse_blackdetect
        text = "[blackdetect @ 0x] black_start:12.0 black_end:15.5 black_duration:3.5\n"
        self.assertEqual(parse_blackdetect(text), [(12.0, 15.5, 3.5)])


class TestMediaProbe(unittest.TestCase):
    def test_parse_stream_info(self):
        from core import parse_stream_info
        info = parse_stream_info('{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"30.5"}}')
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 30.5)
        no_audio = parse_stream_info('{"streams":[{"codec_type":"video"}],"format":{"duration":"12"}}')
        self.assertFalse(no_audio["has_audio"])

    def test_count_probe_errors(self):
        from core import count_probe_errors
        self.assertEqual(count_probe_errors(""), 0)
        self.assertEqual(count_probe_errors("error line 1\nerror line 2\n\n"), 2)


class TestNoAudioCommand(unittest.TestCase):
    def test_encode_without_audio_omits_audio_args(self):
        """无音轨源(监控常见):encode 不再硬塞 aac 参数(脱离音轨)。"""
        cmd = build_command("ffmpeg", Path("i.mp4"), Path("o.mp4"), 0.0, 5.0,
                            "encode", False, 18, None, has_audio=False)
        self.assertNotIn("-c:a", cmd)
        self.assertNotIn("aac", cmd)

    def test_encode_with_audio_keeps_aac(self):
        cmd = build_command("ffmpeg", Path("i.mp4"), Path("o.mp4"), 0.0, 5.0,
                            "encode", False, 18, None, has_audio=True)
        self.assertIn("-c:a", cmd)

    def test_copy_without_audio_maps_video_only(self):
        """无音轨源:copy 只映射视频流,避免坏音轨拖垮整条命令。"""
        cmd = build_command("ffmpeg", Path("i.mp4"), Path("o.mp4"), 0.0, 5.0,
                            "copy", False, 18, None, has_audio=False)
        self.assertIn("-map", cmd)
        self.assertIn("0:v", cmd)
        self.assertNotIn("0,v", str(cmd))

    def test_copy_unknown_audio_keeps_full_map(self):
        cmd = build_command("ffmpeg", Path("i.mp4"), Path("o.mp4"), 0.0, 5.0,
                            "copy", False, 18, None, has_audio=None)
        self.assertIn("0", cmd[cmd.index("-map") + 1])
