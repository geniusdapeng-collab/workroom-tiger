# 数据卫生与零基线纪律（v5.2）

> 用户硬性约束：**每一轮全链路从零开始**，绝不默认消费历史数据、脏数据、
> 上一轮生产残留。本文件是全系统状态存储的审计清单与处置结论。

## 一、零基线执行点

每轮生产运行（daily / premarket）在 pipeline 启动**之前**调用
`trading_system/state.py: purge_run_state()`（main.py `_daily` 第一行）：

| 清除目标 | 说明 |
|---|---|
| `cache/search/` | 搜索磁盘缓存——TTL 内会复用上一轮情报，**每轮必清** |
| `cache/universe_full.json` | 全市场清单缓存（上一轮下载的股票池），**每轮必清**，full 模式重新从 NASDAQ Trader 下载 |
| `cache/frames_*`（v5.4 补） | 回测帧缓存（全量 OHLCV 面板 pickle）——与"行情不落盘"约定矛盾，且 key 不含代码/指标版本，修复评分 bug 后当日会吃旧帧，**每轮必清** |

清除结果写入运行日志（`零基线: 清除 ...`）。

## 二、状态存储审计清单

| 存储 | 位置 | 保留策略 | 是否进入决策输入 |
|---|---|---|---|
| 搜索缓存 | `cache/search/*.json` | **每轮清除**（运行前） | 本轮内复用（性能），跨轮禁止 |
| 全市场清单缓存 | `cache/universe_full.json` | **每轮清除** | full 模式股票池来源；下载失败回退 extended 并在 `raw.universe_source` 披露（nasdaqtrader/cache/fallback） |
| 信号账本 | `reports/journal.json` | **白名单保留**——胜率追踪本质是跨轮会计台账（先落账后结算） | **否**。pipeline/agents 不读取账本（代码隔离），仅报告层追加"胜率追踪"章节展示 |
| WFA 调优参数 | `tuned_params.json` | 保留落盘，但 **pipeline 默认不加载**（`use_tuned=False`）；仅显式 `--use-tuned` 启用 | 默认否；显式启用时在 notes 披露 |
| 行情 OHLCV | 不落盘 | yahoo/stooq 实时拉取，进程内使用 | 是（本轮实时数据） |
| LLM 语义标注 | 不落盘 | 内存中随结果输出 | 是（本轮实时推理） |
| 历史日报/JSON | `reports/日报_*.html` 等 | 保留（产出物归档） | 否；intraday 模式仅读**当日** result JSON 做价位监控 |

## 三、硬编码 / 示例数据全局扫描结论（v5.2 复核）

| 项 | 位置 | 性质判定 | 处置 |
|---|---|---|---|
| ~~WFA 调优参数自动加载~~ | ~~pipeline.py~~ | **历史污染**（上一轮生产残留直接覆盖本轮闸门） | **已切除**：默认不加载，需 `--use-tuned` 显式启用 |
| demo 合成数据源 | `providers/demo.py`、`search/sources.py: DemoSearchSource` | 离线演示/测试专用 | 仅 `--demo` 显式启用；生产链路加守卫：`provider.name=="demo"` 且非 demo 模式直接 RuntimeError |
| 批量拉取降级链 | `pipeline._batch_with_fallback` | 仅真实源之间降级（yahoo→stooq），**不回退合成数据**；覆盖率不足如实记录 `raw.data_coverage` | 文档串已修正 |
| SEC EDGAR User-Agent | `search/sources.py` | ~~占位邮箱~~ → 项目标识 | 已修正 |
| 产业链/股票池清单 | `chains.py`、`universe.py` CORE/EXTENDED | **系统设计参数**（监测对象定义），非示例数据 | 保留；full 模式清单来源在报告披露 |
| 阈值/权重表 | `config.py` | 理论白皮书对齐的 single source of truth | 保留，改动需同步 UPGRADE_REPORT |
| 报告模板文案 | `report*.py` | 空账本/空仓等状态的说明文案 | 保留（非数据） |

## 四、数据管道通畅性核验（17 环节）

- demo 全链路：17 环节逐一点名全过，73 项测试全绿；
- 真实源状态（沙箱实测，随网络环境波动）：kimi_search / Federal Register 真实可用；
  yahoo / stooq 受限流与墙钟保护（45s/8s 预算、熔断器）约束，失败走透传/降级并如实披露，
  绝不产出脏数据报告——硬依赖（SPY/TNX/VIX）双源皆失败时**立即中止**并给出中文原因，
  属于"诚实失败"而非"带污点继续"。

## 五、行情源通道与限流根修（v5.3 补充）

| 通道 | 路径 | 状态 |
|---|---|---|
| yahoo（yfinance） | 本地出口 IP 抓 Yahoo 网页接口 | 共享 IP 环境常态性限流 |
| stooq | 本地 HTTP 出口 | 受限网络大面积超时；v5.4 反爬 PoW 求解 |
| **tencent（v6.2）** | 腾讯 ifzq/gtimg 免费接口（无需 key） | **已实测全通**（日K+实时）；未复权→内置拆股复权 |
| **sina（v6.2）** | 新浪 hq.sinajs/finance API（无需 key） | **已实测全通**（日K全历史+实时）；未复权→内置拆股复权；末根或滞后 1 日 |
| **eastmoney（v6.2）** | 东财 push2his/push2(delay) 免费接口 | 按公开契约实现；K 线端点 available() 环境自适应，fqt=1 服务端前复权 |
| **agentgw（根修）** | **agent-gw 服务端转发 yahoo_finance 数据源** | **不经本地出口，已实证全链路独立跑通** |
| ifind_gw | agent-gw 服务端转发同花顺 iFinD | 个股主力，指数/ETF 覆盖不稳定 |
| tiingo | 官方 REST（需 TIINGO_API_KEY） | 可选强化环 |

降级链（v6.2 七环制）：yahoo → stooq → tencent → sina → eastmoney →
agentgw → ifind_gw → tiingo；全败 → 硬依赖（SPY/TNX/VIX）立即中止
（诚实失败），批量覆盖率不足如实记录，绝不回退合成数据。
注：新浪/腾讯美股 K 线为未复权原始价，provider 内置拆股/并股检测 +
自动前复权（整数倍比例 ±6% 容差，事件记日志披露）——未复权数据直接
进指标 = 拆股日假暴跌，属"看着正常实际是错的"数据造假。

## 六、S3 数据层新缓存的清除口径（v3 小节，v6.3 S3）

S3（Tiger Data Fabric）引入可信度分级 / 交叉验证 / 分层 TTL 后，全部状态
存储的审计结论：

| 存储 | 位置 | 保留策略 | 是否进入决策输入 |
|---|---|---|---|
| 分层 TTL 搜索缓存 | `cache/search/*.json`（key 含类别维度：quote/news/announcement/macro） | **每轮清除**（沿用既有 purge 目标，无需新增） | 本轮内复用（性能），跨轮禁止 |
| 可信度/交叉验证缓存 | `cache/credibility/`（指定落盘位置） | **每轮清除**（已纳入 `state.PURGE_TARGETS`）；当前实现为**内存态**——`Evidence` 挂载在 `CleanDocument` 上、`CrossValidator` 结果随 `PipelineResult.raw` 输出，进程结束即消亡，不落盘、无跨轮残留 | 是（本轮实时计算） |
| 交叉验证统计 | `PipelineResult.raw["cross_validation"]` → 日报 JSON/MD | 随报告归档保留（产出物） | 否（披露口径） |

白名单边界复核：**仅** `journal.json` / `sim_portfolio.json` /
`tuned_params.json` / `calibration_samples.json` 四个会计台账跨轮保留；
S3 新增的任何缓存一律不在白名单内——可信度分级是纯规则映射（每轮重算
成本为零），交叉验证依赖本轮 LLM 语义标注，跨轮复用即"上一轮生产残留"，
违反零基线纪律。

Point-in-Time 纪律：`Evidence.published_at` 只取源真实提供的发布时间
（ISO/RFC2822/epoch 三种格式解析），解析失败记 `None` 并在交叉验证统计
中披露 `missing_published_at` 计数——绝不编造时间戳。
