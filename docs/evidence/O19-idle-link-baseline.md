# 证据：空链路基线（O19 的采集与判定）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次**空链路基线**实验的实测结果与判定；记录新增的计量钩子 `main.py --idle-link` 与 `tools/bench.py --scenario idle-link` / `--baseline` |
| 效力 | 本档**本身不是验收依据**（规则 `00-ssot.md` §3.5）；其**结论值**经 §15 O19 就地裁定后回填 `config.yaml` 的 `metrics.idle_link_baseline` 与 `docs/measurements.md` §1，成为 §13「端到端延迟」行与 §6.5 的**归因扣减基线** |
| 对应主基准节 | §6.1（v3.4「必读」段）、§6.5（延迟分解与配对）、§10.4（`metrics.idle_link_baseline`）、§13（端到端延迟行）、§15（**O19** / O76 / O97） |
| 工具 | `main.py --idle-link`（新增，**计量钩子，非 config 键**）、`tools/bench.py --scenario idle-link`（由 `not_implemented` 转为可跑）、`tools/bench.py --baseline`（新增开关，§13 复现命令已含该词） |
| 实验时点 | **2026-09-22 23:25–23:52 CST**（板 `lubancat` / `192.168.0.104`，SSH 别名 `board`） |
| 状态 | `draft` |

## 1. 口径：本实验的「空链路」定义

**来源 = §6.1 的 v3.4「必读」段原文**：*"关闭增强，只跑"采集 → RGA 原图窗 → MIPI 显示"，实测 P50/P95 入 config"*。本轮按**字面口径**实现（用户 2026-09-22 选定「严格」）：

| 环节 | 空链路 | 理由 |
|---|---|---|
| TH1 采集（唯一 `QBUF` 权 / lease / `seq`） | **保留** | §5 硬契约；采集与 ISP/DQBUF 固有延迟正是基线要度量的对象 |
| RGA NV12→512×288 原图窗 | **保留** | §6.1 原文列举 |
| 画布合成 + DRM pageflip + `seq` 回填配对 | **保留** | §6.5 |
| TH5 HDMI 镜像 | **保留（同构）** | 它不是"增强"成本；常态链路同样有（P50 1.92 / P90 2.08 ms） |
| TH2a 曲线估计（RGA 缩放 + NPU + EMA） | **关**（不 `request`） | 它是 O57 / O123 的被归因对象 |
| TH2b 8-iter 单遍融合 | **关**（不 `apply_window`） | 同上 |
| OSD 统计 / 曲线图 / 直方图 / 按钮区渲染 | **关**（`DisplayMipi.init(with_osd=False)`） | O44 已实测其为实现成本（统计 7.94 / 曲线图 3.77 / 直方图 0.337 ms） |

`est.init()` **仍执行**——它是 RGA 几何（`curve_estim.resolve_geometry()`，O58 的几何单源）与 RGA 目标的唯一配置点，且落在 `boot()` 内（§6.5：帧窗不含进程启动）。**口径自证**：三轮报告的 `counters.estimates` 恒为 **0**（见 §3 表末行）。

## 2. 复现命令与运行条件

```bash
# 前置：TH4 需 DRM master ⇒ 停桌面/停演示单元（恢复见行尾）
sudo systemctl stop starlight-demo
cd /home/cat/starlight
sudo PYTHONPATH=/home/cat/.local/lib/python3.9/site-packages python3 tools/bench.py \
    --scenario idle-link --breakdown --rounds 3 --frames 300 \
    --weight-hash ca8855b90df9a80fa4195a831f33d3476b1964f787eb70602797c773067f3b84 \
    --curve-level 480x272 --precision fp16 \
    --config-hash <测量时刻的 config 快照哈希> \
    --out /tmp/idle_link_300x3.json
sudo systemctl start starlight-demo        # 恢复
```

| 条件 | 取值 |
|---|---|
| 板型 / 运行时 | `lubancat` / aarch64；`librknnrt 2.3.2 (429f97ae6b@2025-04-09)`；python 3.9.2 |
| 权重哈希 | `ca8855b9…f3b84`（`model.zde_weight_hash`；**空链路不消费权重**——`estimates=0`，此项为口径随行而记） |
| 曲线档 / 精度档 | `480x272` / `fp16`（`pipeline.curve_level` / `pipeline.curve_field_precision`） |
| config 快照哈希 | **`eca799e2b1881d46165e6e6b2f1a62994c224fa2ad9fd3a64d51d545ce913f8a`** = **测量时刻**的 config（该键当时仍为上一轮数值）。回填后 config 快照哈希变为 **`2fe4800b084b5bc308e2260c89e084bb9437b08820107c9667cabdee123c4fc6`**（两哈希的**唯一差异 = `metrics.idle_link_baseline` 一行**，已用 `diff` 核对） |
| 时钟 / 时间戳源 | `mono_ns`（`CLOCK_MONOTONIC`）；**`EOF`**（§6.5：驱动只给 EOF ⇒ 本基线为 `EOF→vblank` 口径；`metrics.timestamp_source` 的定稿属 M1 十键，本轮不擅自定稿） |
| 传感器帧率 | `camera.vblank_lines = 7906` ⇒ VMAX = 8986 行（`sensor_lock` 施加并回读一致） |
| 面板刷新 | 60 Hz ⇒ 实测刷新周期 **16.667 ms** |
| SoC 温度 / 调度 | 49.9 °C；亲和 0–7；loadavg 1.71（报告 `scheduling` / `soc_temp_c`） |

## 3. 结果（依据报告 = `docs/evidence/reports/O19/idle_link_300x3.json`）

| 指标 | 第 1 轮 | 第 2 轮 | 第 3 轮 | **合并（n=900）** |
|---|---|---|---|---|
| 端到端（EOF→vblank）P50 | 17.589 | 17.472 | 17.664 | **17.538 ms** |
| 端到端 P95 | 25.898 | 25.314 | 25.671 | **25.670 ms** |
| 采集→提交 P50 / P95 | — | — | — | **9.165 / 9.860 ms** |
| 提交→vblank P50 / P95 | — | — | — | **8.768 / 16.222 ms** |
| 帧率（`fps_active` / `fps_steady`） | 13.52 / 14.23 | 13.42 / 14.31 | 13.41 / 14.32 | 见 §6 |
| 配对率 · 丢弃 | 1.0000 · 0 | 1.0000 · 0 | 1.0000 · 0 | **1.0000**；`flip_skipped` / `dropped_stale` / `lease_timeouts` **全 0** |
| `stream_on_count` / `counters.estimates` | 1 / **0** | 1 / **0** | 1 / **0** | 契约：恒为 1；口径自证：恒为 0 |

- `提交→vblank` 的 P95 = **16.222 ms ≈ 面板刷新周期 16.667 ms** ⇒ 显示排队段已被面板节拍支配（无额外滞留）。
- 三轮逐轮 P95 离散度 **≤0.59 ms**、P50 离散度 **≤0.19 ms** ⇒ 基线稳定、可复现。

**（v3.86 追加 72 附注：代码变更后的交叉核对）** 本基线采集完成后，交付链路又落了一处 §15 O24 的改动（越阈停机）。该改动**只新增失败分支**的行为（失败分支多一次 `counters()` 读 + 5 ms 退避；正常帧路径改为"仅在失败过后才读一次 `counters()`" ⇒ **零新增调用**）。为免"值—报告—代码"三者脱钩，在改动后**补跑 1 轮 300 帧作交叉核对**（`docs/evidence/reports/O19/idle_link_crosscheck_1r.json`）：**端到端 P50 18.314 / P95 25.650 ms**、采集→提交 P50 9.251、提交→vblank P95 16.408、配对率 1.0000、`estimates=0` ⇒ **P95 与入库值（25.670）差 −0.02 ms**（P50 差 +0.78 ms，在轮间离散度量级）⇒ 判定为**无系统性偏移，入库值继续有效**（§2 的配置快照哈希差异说明照旧成立）。

## 4. O19 判定（就地裁定）

| 项 | 内容 |
|---|---|
| 判据（§13 / `docs/measurements.md` §1） | 基线 **P95 ≥ 60 ms**（= §13 上限 80 ms 的 75%）⇒ 自动触发 §13「端到端延迟」重议；**< 60 ms ⇒ 登记「不触发」并关闭 O19** |
| 实测 | 逐轮 P95 = 25.898 / 25.314 / 25.671 ⇒ **中位 25.671 ms** |
| **判定** | **25.67 ms < 60 ms ⇒ 不触发重议；O19 登记「不触发」并关闭** |
| 余量 | 80 − 25.67 = **54.33 ms** 留给应用侧（§13 该行的"应用侧余量"口径） |

## 5. 归因：常态链路 − 空链路（依据报告 = `docs/evidence/reports/M2-gate/rt_baseline_300x3.json`）

复现 = `bench.py --scenario realtime --breakdown --baseline`（§13 该行指定的命令），3 轮 × 300 帧，条件与 §2 同（config 快照哈希 = `2fe4800b…`，与最终 config 一致）。

| 段 | 空链路（P50 / P95） | 常态（P50 / P95） | **差值 P95** |
|---|---|---|---|
| 端到端（EOF→vblank） | 17.538 / 25.670 | 52.376 / **106.161** | **+80.49 ms** |
| 采集→提交（应用侧） | 9.165 / 9.860 | 43.668 / **97.623** | **+87.76 ms** |
| 提交→vblank（显示排队） | 8.768 / 16.222 | 8.318 / 16.170 | **−0.05 ms（≈0）** |

**结论（归因已闭合到"段"）**：
1. **超支全部落在应用侧**（采集→提交，+87.76 ms P95）；
2. **显示排队段与空链路无差异**（16.17 vs 16.22 ms，均被面板 16.667 ms 节拍支配）⇒ 超支**不是** DRM/翻页排队造成的；
3. 常态轮零丢弃（`flip_skipped` / `dropped_stale` / `lease_timeouts` 全 0）、配对率 1.0000 ⇒ 超支**不是**丢帧/配对失败造成的；
4. ⇒ 规则 `30-board-safety.md` §3 的「先扣空链路基线」义务**已履行**，O76 的归因闭合条件成立（见主基准 §15 O76）。

**残留（未归因到子项）**：应用侧 +87.76 ms 内部尚未拆分「TH2a worker 的 DDR/CPU 争用」与「TH2b 应用 + OSD 渲染」。§6.1 的分量预算（应用 4.2 / 统计 7.94 / 曲线图 3.77 ms 等）之和明显小于实测，故**须一次专门实验**（如"只开 TH2a"与"只开 TH2b"两个变体）才能定性——本档不宣称已定位。

## 6. 随行发现①：帧率报告的**分母**含退出序（fps 判据口径缺陷）

**事实（三轮实测 + 两点法 + 独立帧周期测量）**：

| 证据 | 值 |
|---|---|
| **V4L2 帧周期**（独立进程取 120 帧，按 `t_capture_ns` 差） | p50 **66.682 ms** / mean 66.683 / min 66.514 / max 66.889 ⇒ 传感器帧率 **14.997 fps**，零抖动 |
| 空链路 300 帧轮的 `fps_steady`（分母 = `wall − boot_s`） | 14.23 / 14.31 / 14.32 |
| 常态 300 帧轮的 `fps_steady` | 14.17 / 14.40 / 14.24 |
| 两点法残差（`wall − boot_s − N×66.682 ms`） | N=60 → **0.821 s**；N=360 → **1.007 s**；300 帧轮 → 0.96–1.08 s |

**判定**：
- 残差**不随 N 增长**（60 / 300 / 360 帧均 ≈0.8–1.1 s）⇒ 是**固定开销**，量级与 `main()` 里 `wall = time.monotonic() − t0` **跨过 `d.shutdown()`** 的事实相符（本档不宣称已逐项打点到具体函数）；
- 分子侧**逐帧消费、零丢失**（`dropped_stale=0` / `lease_timeouts=0`，共 900 帧）⇒ 链路吞吐 = 传感器满帧率；
- ⇒ 报告的 `fps_active` / `fps_steady` **系统性低估**（300 帧轮低估 ≈0.7 fps），**与实现无关**。

**对 M2 出口的直接含义**：§11 M2 / §13 的「无 HQ **≥14.99 FPS**」（v3.86 裁定 O89 / O93）与物理帧率 14.997 fps 完全对齐；**若分母正确，链条本身已达标**。本档只登记事实，**口径的裁定属 §15 O97（未决），不由本档裁定**。

## 7. 随行发现②：两侧共有的长尾（未归因）

| 轮/场景 | 端到端 `max_ms` | 采集→提交 `max_ms` |
|---|---|---|
| 空链路（900 帧） | **1063.6** | 1062.8 |
| 常态（900 帧） | **1384.0** | 1368.3 |

两侧都有 ~1.1–1.4 s 的**单次**长尾，且**出现在空链路上** ⇒ 与 TH2a/TH2b/OSD **无关**（一次性的调度/驱动/翻页事件：待查）。**未列入 O76 的归因结论**，仅登记为残留。

## 8. 落地物清单与验证

| # | 落地物 | 说明 |
|---|---|---|
| 1 | `main.py` | 新增 `--idle-link`（计量钩子，非 config 键）；`Demo.idle_link`；`boot()` 的 `init(with_osd=not idle_link)`；`run()` 内 `if not self.idle_link:` 包裹 TH2a 取用（`self.curve` 恒为 `None` ⇒ TH2b 与 OSD 统计自然短路）；`st["curve_field"]` 空值守卫；报告新增 `idle_link` 字段 + stdout 一行口径自述 |
| 2 | `tools/bench.py` | `scenario_idle_link()`（每轮独立进程跑 `main.py --idle-link --report-json`，≥300 帧 × 3 轮）；`--scenario idle-link` 分派；**`--baseline`** 开关（读 `metrics.idle_link_baseline` 产出归因差值）；`--breakdown` 语义不变；`CHAIN_SCENARIOS` 收敛为 `['step-response']`（`idle-link` 已接通，`step-response` 仍 `not_implemented`） |
| 3 | `config.yaml` | `metrics.idle_link_baseline` 由 `null` 定稿为**位置化数列**（6 值，次序见该行注释） |

**验证**：本轮**无 C / native 改动** ⇒ 无板端构建（零告警构建项不适用）；Python 侧开发机与板端各跑一次 `python3 -m py_compile`（通过）；`config.load()` 通过（无表外键、无缺失键，未定稿键 12 → 12，其中本键已定稿）；三份报告均为 `status=ok`。

## 9. 未验残留

| # | 残留 | 处置 |
|---|---|---|
| 1 | 应用侧 +87.76 ms 的子项拆分 | 待专门实验（"只开 TH2a" / "只开 TH2b" 两变体）；不阻塞 O19 关闭 |
| 2 | 两侧共有的 ~1.1–1.4 s 单次长尾 | 未归因；登记为本档 §7 |
| 3 | `fps` 分母口径（含 `shutdown()`） | 登记于 §15 **O97**（未决）；M2 出口前须裁定 |
| 4 | 测量时刻与最终 config 的快照哈希不同（唯一差异 = 本键一行） | 已在 §2 如实登记，两值并给 |
| 5 | `metrics.timestamp_source` 的定稿 | 属 **M1 十键**，本轮只随行注明 `EOF`，不擅自定稿 |

---

**报告与原始数据**：`docs/evidence/reports/O19/idle_link_300x3.json`（空链路，3 轮 × 300 帧）、`docs/evidence/reports/M2-gate/rt_baseline_300x3.json`（常态 + `--baseline` 归因，3 轮 × 300 帧）。两者**均落在工程目录内**（规则 `10-evidence.md` §4；`storage.dir` 已随 O102 取消）。