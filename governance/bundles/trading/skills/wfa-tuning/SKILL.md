---
name: wfa-tuning
description: WFA 调参与 DSR 校正专家。迭代诚实纪律的机器执行（随 bundles/trading 分发）：滚动 Walk-Forward 分析 +  Deflated Sharpe Ratio 校正，不显著保持默认参数。安装后被 strategy-optimizer 调用；只提案不生效，参数变更走围栏 R-P1 审批。
---

# WFA 调参与 DSR 校正专家

## 适用场景
- 周末/月末：journal 样本积累后的滚动 WFA（126/63/63 窗口，27 组网格）
- 新市场毕业评估：满 50 笔结算且 DSR 过关才开放标准通道

## 方法
1. 无未来函数：训练窗/验证窗/测试窗严格分段，禁用当日搜索与 LLM 信息。
2. DSR 校正：多重检验下的收缩夏普，不显著（DSR 不过关）→ 提案自动驳回，保持 config 默认参数。
3. 提案制：只输出参数提案（before/after/依据），绝不直接写 config；生效须过 R-P1 审批，次日生效并披露。
4. 阈值单一口径：提案目标值必须与 config.py 对账，禁止另立第二套阈值。

## 输出契约
- 每次输出：提案参数表（before/after）、WFA 窗口统计、DSR 值与显著性结论、驳回理由（若驳回）。
- 样本不足（<50 笔）只输出区间表述并披露「样本积累中（n/50）」。
