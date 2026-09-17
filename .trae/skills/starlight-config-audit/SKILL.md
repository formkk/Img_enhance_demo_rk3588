---
name: starlight-config-audit
description: Audit config.yaml against the baseline SSOT key table for this RK3588 low-light demo repo. Use when the user asks to check config completeness, key names, or provisional status, or before a milestone exit. Do not use to invent or relax keys.
---

# config 键表 SSOT 审计

**只读审计**：核对 `config.yaml` 与主基准 §10.4 键表的一致性。**不写值、不改文件、不裁定开放项**；发现缺口只报告。

## 唯一来源

主基准 **§10.4 config.yaml 键表**。纪律：**键不存在即未定稿**（§10.4 表头）；禁止在代码或 config 中出现表外键（`.trae/rules/20-contracts.md` §1）。

## 步骤

1. 读主基准 §10.4 全文，建立键清单：`键 → 取值来源/依据 → 定稿时点`。
2. 读 `config.yaml`（若不存在，直接报告"未创建"，并列出本应存在的键范围）。
3. 三类比对：
   - **表外键**（违规）：出现在 config 或代码中、但不在 §10.4 的键；另需排除两类"看似键而非键"的项——`deploy/starlight-demo.service` 的**单元参数不是 config 键**（§10.6-A），构建选项**不在 config、属编译期**（`docs/build.md`）。
   - **表内缺失 / 空值**（未定稿）：按该键"定稿时点"判断是否阻塞当前阶段；**禁止用默认值顶替空值**。
   - **三态合规性**：`provisional` **仅**允许用于 §10.4 明列的代用数据项（M0 阶段、M1 复检），且必须带明确的复检时点；其余位置出现 `provisional`（尤其 `docs/measurements.md` 的实测四项）一律判违规。
4. 交叉校验（**只报不改**）：
   - `model.zde_output_layout` / `model.zde_output_dims` 是否标注为 `rknn_query` 实测所得（禁止预设 NCHW/NHWC）。
   - `runtime.*` 三元组是否一致（§2.2 / §11.1-1）。
   - `logging.level` 是 config 与 systemd 单元的唯一交集（§10.6-A）。
   - `display.*_connector` 是否留空 CRTC id（**禁止写死 crtc id**）。
5. 输出审计表与结论：**合规 / 存在阻断项（列出键与定稿时点）**。

## 输出模板

| 域 | 键 | 状态（已定稿 / 空值 / 缺失 / 表外违规 / provisional 合规 / provisional 违规） | 定稿时点 | 阻塞里程碑 | 证据 |
|---|---|---|---|---|---|

结论行：`当前阶段 = M<k>`；**阻断项数**；逐条给出键名与解除方式（补测 / 走 §15 裁定 / 补回写）。

## 硬规则

- 不复制规范取值：审计表只写键名与状态，取值一律指向 §10.4 对应行。
- 不因"演示需要"建议现场改值（`.trae/rules/20-contracts.md` §4）。
- 不发明键；需要新键时必须先走主基准变更并回写 §10.4 + §14。
- 未回填的实测登记项（`docs/measurements.md`）视为未定稿，报告时单列，不与 config 键混算。