# 🎬 BatchCut Studio · 剪辑标注工场

**数据集生产流水线:切片 → 标注 → 审核 → 训练集导出,一条龙。**
监控视频 CSV 任务清单 + ffmpeg 并行切割(Smart Cut 关键帧吸附)+ 浏览器内行为标注 + 审核流 +
train/val 数据集导出。零第三方依赖(Python 标准库 + SQLite + 手写前端),`python server.py` 一条命令起界面。

> v2.0 起吸收了原 video_labeler(PySide6 桌面标注工具)的全部核心资产:四态审核状态机、
> append-only 审核历史、10 个内置行为标签、完整版 CSV 25 列迁移兼容(`/api/migrate-vl` 端点)。

```
来源: batch_cut_clips.py(6.8/10 个人脚本) → BatchCut Studio(9+/10 本地产品)
```

## 🚀 快速开始

```bash
python server.py            # Web UI → http://127.0.0.1:8963(端口被占自动顺延)
python cli.py --dry-run     # 命令行模式,兼容原脚本用法
python -m unittest tests.test_core   # 17 个纯函数测试
python scripts/make_demo_fixtures.py # 生成演示数据(3 视频 + 6 行清单)
```

前端零构建:`static/` 三个文件直接由标准库 HTTP 服务托管。

## ✨ 相比原脚本的升级

| 能力 | 原脚本 | 现在 |
| --- | --- | --- |
| 单任务进度 | 只有全局计数行 | ffmpeg `-progress pipe:1` 解析,每个任务实时进度条 + 倍速徽章(SSE 推送) |
| 切割前校验 | 无(end 超时长让 ffmpeg 撞错) | ffprobe 探测源时长,**end 超时长/源不存在/时间倒挂**提前标黄 + dry-run |
| 失败处理 | 失败清单 CSV | 清单 + 表格内错误摘要 + **一键重试失败项** |
| copy 模式 | 输出端 `-ss`(长视频性能地雷:切第 20 小时要解码 20 小时) | **输入端关键帧寻址 + `-avoid_negative_ts make_zero`**(LosslessCut 同款,秒级差异) |
| CSV 兼容 | utf-8-sig/utf-16/gbk | 加 **LosslessCut 大写表头兼容**(Start/End/Name 归一化) |
| 预览 | 无 | **浏览器内预览**:源片按媒体片段 `#t=start,end` 直接定位到切片起点;成品就地播放对比 |
| 控制方式 | 双段式 Ctrl+C | 双段式停止按钮(温和→强制)+ 状态徽章 + 全局进度环 |
| 路径配置 | 硬编码在源码顶部 | UI 配置 + `config.json` 持久化 + CSV 拖拽上传 |
| 端口 | — | Windows 圈占端口自动顺延;禁用 SO_REUSEADDR 防「双进程静默共端口」 |
| 测试 | 0 | 17 个纯函数测试(时间解析/命令拼装/进度解析/CSV 兼容/有效性判定) |

**保留的原版硬功夫**:encode「先粗后细」双 `-ss` 预寻址、NVENC/QSV/AMF 真试编探测、
`MIN_VALID_OUTPUT_BYTES` 断点续跑、双段式中断、失败清单 Excel 兼容(utf-8-sig)。

## 📖 开源借鉴

- **[LosslessCut](https://github.com/mifi/lossless-cut)**(45.9k★):输入端关键帧寻址 + `avoid_negative_ts`;
  `Start,End,Name` CSV 交换格式(官方承认 "there's no batch export"——本工具的差异化正在真·批量并行);
  源片/成品对比预览的体验思路。
- **ffmpeg 源码级结论**(调研核实):`out_time_ms` 名不副实、单位是**微秒**(与 `out_time_us` 同值);
  控制刷新间隔的正确选项是 `-stats_period`(不存在 `-progress_interval`);`-t` 优先于 `-to`,
  输入 `-ss` 后时间戳归零,跨版本最稳写法是 `-ss <start> -i in -t <dur>`。
- **FileFlows/Tdarr** 的仪表盘卡片、行内进度条、失败闭环布局;**VidCutter** 的 SmartCut 思路
  (切点在关键帧上则整段免重编码——列入 Roadmap)。

## 🆕 v1.1 新增(2026-11-10 第二轮开源调研落地)

- **智能自适应模式(默认)**:起/终点就近吸附关键帧整段无损(LosslessCut 同款 `-show_packets` K 标志 +
  `-read_intervals` 局部读取,不用 `-skip_frame nokey`——它会漏 open-GOP 访问点);
  10s 窗口失败自动扩到 60s,再失败回落精准重编码
- **场景切换 / 黑场检测自动生成切片清单**:不用现成 CSV 也能用——选源视频一键检测
  (`scale=480:-2,fps=8` 降采样提速 10~30 倍;`metadata=print:file=-:direct=1` LosslessCut 生产写法;
  blackdetect 参数按监控噪点黑屏放宽 pix_th=0.15),切片点算法:短闪丢弃 / 噪声合并 / 首段归零 / 尾段并入
- **行内任务编辑**:铅笔按钮 → 播放器里按「把播放位置设为起点/终点」→ 保存**写回 CSV**(保留原有全部列)
- **输出命名模板**:`{source}_{index}_{start}` 可配,output 列为空时自动补名

调研另发现(列入 Roadmap):DeepRegular/SmartCut 的 11 个 smart-rendering 坑文档(open-GOP、
`-frames:v N` 替代 `-t` 防 B 帧重排误差)、Frigate 的多摄像头同段批量导出、auto-editor 的
事件前后 margin 概念。

## 🧭 Roadmap

## 🧭 Roadmap

- [ ] Smart Cut 快速路径:切点恰在关键帧上时 copy 而非重编码(码率 ×1.2 补偿,参考 LosslessCut #126)
- [ ] 输出命名模板变量(`${SOURCE}`/`${SEG_NUM}`,LosslessCut recipes 同款)
- [ ] 单任务取消 + 段表达式筛选(`duration < 5 && label=='x'`)
- [ ] 视频源缩略图条 / 时间轴打点(I/O 键 + J/K/L 穿梭)

## 🏗️ 架构

```
core.py    引擎:CutConfig/CutTask/CutEngine + 全部纯函数(可单测)
           事件驱动:所有状态变化经 on_event 抛出,UI/CLI 各自渲染
server.py  零依赖服务:静态托管 + JSON API + SSE 实时流 + Range 视频流
cli.py     命令行模式(原脚本用法兼容)
static/    手写前端:设计令牌深色主题 + vanilla JS,零构建零依赖
```

安全:服务只监听 127.0.0.1;`/media` 做目录逃逸防护;不对外网暴露。
