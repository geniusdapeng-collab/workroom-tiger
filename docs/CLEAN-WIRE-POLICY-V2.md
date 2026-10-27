# v2 clean 与最终判断的无损传输约定

本约定属于 Tiger 的 `us-public-model-required-*-paper-v2` 研究请求策略。模型仍为 `deepseek-flash`，六个必需语义阶段仍须得到真实、完整且通过 schema 与独立回放验证的回答。这里的 wire 是请求和回答的可逆编码，不产生新闻事实、来源归属、模型理由、数值批准或成交。

实现入口为 `trading_system/llm/required.py` 的 clean/final wire helpers、`clean_v2`、`decision_v2`，以及 `deepseek_required.py` 的 `build_chat_request`、`stage_thinking_mode`。`required_proof_v2.py` 从同一确定性 builder 重建准确请求、回答展开及预算，不能只信保存的成功标记。

## thinking 策略

| 范围 | thinking.type | reasoning_effort | 模型是否仍须真实回答 |
|---|---|---|---|
| v2 `clean.llm_semantic` | `disabled` | `low`，保留显式阶段策略值 | 是 |
| v2 舆情、风险、板块、公司研究、最终判断 | `enabled` | `high` | 是 |
| v1 默认请求 | `enabled` | `high` | 按原 v1 契约 |

`disabled` 在这里关闭的是 DeepSeek 的 thinking 模式，不是 Tiger 的模型能力、必需模型阶段或模型调用。clean 仍由同一个 Flash 模型解释每篇资料并输出全部语义标注。策略由可信 execution 的 v2 标识和活动阶段派生，调用者不能借参数改变其他阶段。请求仍预留原来的 clean `max_tokens=16384`；不增加重试、输出额度或整轮预算。

关闭 clean thinking 的目的，是让这个抽取请求的输出窗口用于显式结构化回答。它不构成“语义质量提高”或“下一轮一定成功”的证据。其他研究、风险和最终判断继续使用 high thinking。v1 默认 request 对象及 `required.py` 的原 v1 前缀保持原样。

2026-10-27 已检索并打开官方依据：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。官方支持显式 `thinking.type=disabled`，JSON Output 仍要求提示中明确 JSON，并设置合适输出上限；`finish_reason=length` 不能当作完整回答。

## clean：`tiger.clean-wire.v1`

每批仍最多八篇，完整来源索引仍在每次调用中展示。`clean_payload_v2` 构造原始语义输入，`clean_payload_wire_v2` 只做以下机械变换：

- `all_source_index.columns` 为 `ref/id/title/url/published_at`。每个 `dN` 唯一对应该行原始 ID；完整标题、URL、日期和全局行顺序保留，未知日期仍为 null。
- `documents` 变为显式 `columns/rows` 表。`source_ref` 连接全局索引，避免本批重复抄写 ID、标题、URL和日期；其余全部正文分片、来源、时间、哈希、覆盖和提供方完整性元数据保持原值。
- 正文仍按 `content_parts.columns = [part_id, text, is_quoteable]` 顺序完整展示。连接所有 text 得到之前同一份规范化正文。不可引用片段仍须阅读和解释，不能因为格式不合规而删除。
- 输出短字段仅由 `clean_wire.output_fields` 声明的双射还原。只有受控 `id` 与 `origin_id` 位置使用 `dN`，自然文本不替换。

| 输出位置 | 短字段到原字段 |
|---|---|
| 根对象 | `d → documents` |
| 文档 | `i → id`、`t → tickers`、`s → sentiment`、`v → sentiment_score`、`e → events`、`z → summary_zh`、`r → relevance`、`h → chain_ids`、`f → sector_etfs`、`p → provenance`、`c → claims` |
| provenance | `k → evidence_kind`、`o → origin_id`、`n → origin_note`、`t → time_role` |
| claim | `s → statement`、`q → quote_part_id`、`k → claim_key`、`t → time_role` |

原 schema 的所有字段、枚举语义、模式、上下界和来源绑定保留。摘要上限 800、来源说明上限 600、每篇最多四个事实、事实 statement 上限 600、claim_key 上限 160 均未缩减。wire schema 校验后，程序逆变换，再按原完整语义 schema 校验。

模型只选择本篇允许的 `quote_part_id`。程序从该篇本次展示的片段表逐字取回 quote，不模糊匹配、不改字、不截断、不删除无效事实。`is_quoteable` 只是格式条件，不代表真实性、独立性、时效或相关性。跨批 `origin_id` 仍可以引用本轮完整来源索引中的原始 ID。

提示要求摘要、逐事实 claims 和来源说明各自保留必要内容，避免跨字段重复抄写或为了凑满上限增加文字。必要事实、反证、未知日期、观点性质与不确定性不能省略；程序不会替模型压缩这些文字。

## 最终输入：`tiger.final-identity-wire.v1`

最终判断仍接收完整广域摘要、claims、provenance、引文 hash/长度、公司研究、五段假设、事实证据、行情证据和研究来源索引。`final_payload_wire_v2` 在 `final_wire.identity_index` 的 `ref/id` 表中保留每个真实 ID，并在受控身份位置使用唯一 `wN`。

允许映射的字段为 `id`、`origin_id`、`source_id`、`date_source_id`、`search_source_id`、`fetch_source_id`、`evidence_ids`、`source_ids`、`issuer_identity_evidence_ids`，以及明确列式表中同名的列。输出 schema 也只在这些身份字段的 enum/const 中使用同一映射。不同原始来源 ID 不合并；引文仍由父文档行 ID 约束。

自然文本、JSON 字典键、`claim_key`、`claim_id`、quote 和 quote hash 不替换，即使它们的字面值恰好等于真实 ID、`d0` 或 `w0`。全部自然语言回答在逆变换后保留原字符串；没有事后截断、缺省补值或规则代写。

`source_payload_sha256` 绑定进入该编码器的完整原始语义 payload；冻结 assessment 由外层 `_call_v2` 单独加入并由完整请求证明绑定。纯解码器检查规范化 round trip 与该内容绑定。这个自包含 hash 不是外部签名；对攻击者同时改值并重算 hash 的情形，仍须由独立回放从真实上游阶段重建原 payload 和完整 request 后拒绝。

## 最终输出：完整 thesis 或同股票原模型引用

最终每条决定的 `thesis` 有两个严格互斥分支：

1. 原完整 thesis。所有原字段与上限保留；模型可以完整修订。
2. 只有一个字段的 `{"reuse_thesis":"该条决定自己的symbol"}`。它精确引用本轮已完成且已经展示的 `candidate_research.theses` 同股票结果，不重新生成或拼接这些文字。

引用必须与当前决定的 symbol 相同，且该股票有唯一的本轮已完成研究。其他股票、未研究股票、错误类型、额外字段，以及引用对象中夹带的修改均拒绝。`model_reference_usage` 记录哪些字段使用了哪个本轮研究结果。真实模型调用证据保留实际返回的引用，完整语义结果保留精确展开后的原模型文字。

最终模型仍须在 high thinking 的真实调用中选择、修订或否决，并生成 `why_this/why_now/entry_condition/invalidation/risk_note` 等人工执行理由。引用不是缺失模型回答的回退，也不能增加买入集合。展开后仍依次执行原完整 schema、每股票覆盖、数值买入权限、`validate_thesis`、本人行情/资料归属、完整展示范围、原研究 claims 集合与未完成补证检查。最后才绑定原获准数值计划和修改上下文。

这个可选引用避免强制重抄已有五段和 claims。五个人工理由仍可使用原完整上限，因此任何合法最大回答都必能在 16,384 tokens 内完成的保证并不存在；模型若返回 length、空内容或非法引用，仍按失败处理。

## 整轮预算与证据边界

预算硬限保持 40 chat、300000 实际加预留 tokens、60 Search、20 Fetch、120 远端请求、保守估算 1 USD、2400 秒。每次远端调用仍先预留；失败调用保留真实 usage 结算，未知结果冻结，不自动重试。

对于两条 messages 的 chat，本次 admission 条件为：

```text
先前实际 tokens + 尚未结算预留
+ 完整 canonical request 的 UTF-8 字节数
+ 4096 + 64 × 2 + 本请求 max_tokens
<= 300000
```

UTF-8 字节是输入的保守预留，不是实际 tokenizer 计数。必须按同一个 guard 的真实顺序记录 reserve/settle、每次前缀、剩余量和第一次拒绝，不能把各阶段分别放进“先前 actual=100000”的新 guard 当作全链证明。来源数、摘要/事实长度、候选与持仓并集、补搜和 Fetch 都会改变最终请求大小。

本变更的纯合成验证覆盖空输入、55/56 来源、多批全消费、全部原文字段上限、跨批来源、别名形似正文、来源本篇 quote 绑定、异常及不重试、daily/premarket/intraday 完整 43 阶段、合法 BUY、失败后账户保持及重算哈希后的篡改拒绝。容量探针必须标明其合成输入和假设 usage；它不是未来真实用量或模型质量预测。

第五次历史真实运行以 `finish_reason=length` 失败；本变更不修改它或此前任何失败，也不能将旧失败计为成功。仅在新逻辑任务完成全部阶段、原始账户提交与独立 receipt/verify 后，才能报告一次新的真实完整成功。代码测试与容量改进本身不代表已完成真实业务、main 集成或生产发布。

验证入口为 `tests/test_clean_capacity_wire_v2.py`、`tests/test_final_capacity_wire_v2.py`、`tests/test_model_reasoning_policy_v2.py`、`tests/test_short_term_core_v2.py`、`tests/test_required_proof_v2.py` 与既有语义、预算、传输和完整工作流回归。它们全部使用明确的合成夹具，不能替代真实模型验收。
