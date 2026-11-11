# 🎬 BatchCut Studio · 剪辑标注工场

**两个模块,一条闭环:在源视频上标注(保留上下文)→ 标注清单驱动切割(后期绑定)。**

> **为什么是「标注先行」而不是「先切后标」**:行为/事件标注需要前后文才能判准(CVAT、BORIS、ELAN
> 都是直接在源视频上标时间区间)。先切后标适合检测框训练集,但会丢掉上下文、冻结切点。
> 我们的取舍:**边界可以反复改,切点最后才绑定**——所以老数据改完标签不用重新切。


**数据集生产流水线:切片 → 标注 → 审核 → 训练集导出,一条龙。**
监控视频 CSV 任务清单 + ffmpeg 并行切割(Smart Cut 关键帧吸附)+ 浏览器内行为标注 + 审核流 +
train/val 数据集导出。零第三方依赖(Python 标准库 + SQLite + 手写前端),`python server.py` 一条命令起界面。

> v2.0 起吸收了原 video_labeler(PySide6 桌面标注工具)的全部核心资产:四态审核状态机、
> append-only 审核历史、10 个内置行为标签、完整版 CSV 25 列迁移兼容(`/api/migrate-vl` 端点)。

```
来源: batch_cut_clips.py(6.8/10 个人脚本) → BatchCut Studio(9+/10 本地产品)
```

## 🧩 两个模块

| 模块 | 做什么 | 产出 |
| --- | --- | --- |
| **A · 标注工作台** | 在长视频时间轴上边看边标:关键帧刻度、S/E 打点、连标衔接、片段打多标签、A 通过 / X 需修正 / R 剔除 | **标注清单 CSV**(`source,start,end,output,labels,review_status,note`)——这是模块间的契约 |
| **B · 剪辑车间** | 导入标注清单 → 切割 → **成品质检**(时长偏差 / 黑场占比)→ 数据集导出 | 成品视频 + `train/val` + `classes.txt` + `label_stats.csv` + `manifest.csv` |

**闭环**:A 标注 → 导出清单 → B 导入切割 → 质检 → 数据集。中间随时可回 A 改边界,不用重切。

**为什么要质检**:切割成功不代表成品可用——区间落在静止/黑场段、关键帧吸附导致时长偏差,
都要在交付前发现。质检分 `ok / warn / bad`,`bad` 直接判为任务失败并给出原因。

**train/val 按源视频分组划分**,同一摄像头不会跨 train 和 val——防止数据泄漏
(切片的相邻帧高度相似,随机划分会虚高验证指标)。

## 🚀 快速开始

**Windows 双击即用**(推荐):

```
启动.bat    ← 启动服务(自动挑可用端口 + 自动开浏览器),关窗口即停
打开.bat    ← 服务已在跑时,读 running.json 里的真实地址直接打开
```

> 服务的端口会被系统动态占用(本机 8963 起多段被代理/虚拟化驱动圈占),启动时会自动顺延并把自己的真实地址写入 `running.json`——所以**永远用 `打开.bat` 或看启动日志,不要收藏固定端口**。

命令行:

```bash
python server.py            # 自动挑端口 + 自动开浏览器
python server.py --no-open  # 不自动开浏览器
python cli.py --dry-run     # 命令行校验清单
```



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

- **v2.1 时间轴打点台**:选源视频 → 时间轴(关键帧刻度)上 S/E 打点 → Enter 追加片段(连标语义:起点自动衔接上一段终点)→ 前后各留 N 秒 margin → 一键装载为任务。快捷键:Space 播放 / J 慢放 / K 暂停 / L 倍速 / ←→ ±1s / Shift±5s / 逗号句号逐帧
- **离线横幅**:后端不在时不再只弹 toast,页面顶部红色横幅 + 重试连接按钮

- **v2.2 可靠性与性能实测**:装载前对每个来源做容器级体检(ffprobe 错误计数),损坏源标黄「疑似损坏(N 个错误)」;每刀切割前再做**区间预检**(目标区间读不到视频包 → 立刻报错,不让用户等 ffmpeg);无音轨源自动探测 🔇 徽标 + 切割命令智能省略音频参数(encode 不硬塞 aac / copy 只映射视频流);打点台可静音播放。**实测**:3000 行清单装载 0.34s(按源探测设计)、10 分钟长视频全量关键帧扫描 0.21s、场景检测 1.6s(降采样)、长视频任意区间 Smart Cut 约 0.6s

## 🧭 Roadmap

- **v2.1 时间轴打点台**:选源视频 → 时间轴(关键帧刻度)上 S/E 打点 → Enter 追加片段(连标语义:起点自动衔接上一段终点)→ 前后各留 N 秒 margin → 一键装载为任务。快捷键:Space 播放 / J 慢放 / K 暂停 / L 倍速 / ←→ ±1s / Shift±5s / 逗号句号逐帧
- **离线横幅**:后端不在时不再只弹 toast,页面顶部红色横幅 + 重试连接按钮

- **v2.2 可靠性与性能实测**:装载前对每个来源做容器级体检(ffprobe 错误计数),损坏源标黄「疑似损坏(N 个错误)」;每刀切割前再做**区间预检**(目标区间读不到视频包 → 立刻报错,不让用户等 ffmpeg);无音轨源自动探测 🔇 徽标 + 切割命令智能省略音频参数(encode 不硬塞 aac / copy 只映射视频流);打点台可静音播放。**实测**:3000 行清单装载 0.34s(按源探测设计)、10 分钟长视频全量关键帧扫描 0.21s、场景检测 1.6s(降采样)、长视频任意区间 Smart Cut 约 0.6s

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
