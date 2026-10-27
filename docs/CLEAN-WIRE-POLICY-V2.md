# v2 clean 与最终判断的无损传输约定

本约定属于 Tiger 的 `us-public-model-required-*-paper-v2` 研究请求策略。模型仍为 `deepseek-flash`，六个必需语义阶段仍须得到真实、完整且通过 schema 与独立回放验证的回答。输入编码精确保留原值；新 clean 回答保留逐来源的全部语义值和原有类型化输出投影，不宣称保留旧模型任意数组行序。编码不产生新闻事实、来源归属、模型理由、数值批准或成交。

实现入口为 `trading_system/llm/required.py` 的 clean/final wire helpers、`clean_v2`、`decision_v2`，以及 `deepseek_required.py` 的 `build_chat_request`、`stage_thinking_mode`。`required_proof_v2.py` 从同一确定性 builder 重建准确请求、回答展开及预算，不能只信保存的成功标记。

## thinking 策略

| 范围 | thinking.type | reasoning_effort | 模型是否仍须真实回答 |
|---|---|---|---|
| 新显式 strict policy 的 v2 `clean.llm_semantic` | `disabled` | `none` | 是，单次固定函数输出 |
| 未声明 strict policy 的历史 v2 `clean.llm_semantic` | `disabled` | `low`，保留原请求 | 是，原 JSON 输出 |
| v2 舆情、风险、板块、公司研究、最终判断 | `enabled` | `high` | 是 |
| v1 默认请求 | `enabled` | `high` | 按原 v1 契约 |

上表记录准确发送的请求字段。clean 仍由同一个 Flash 模型解释每篇资料并输出全部语义标注；`disabled` 不是关闭模型能力、必需阶段或调用。新通道由可信 execution 的显式 `clean_output_policy_id` 及活动阶段共同派生，调用者不能借请求参数改变其他阶段。请求仍预留原来的 clean `max_tokens=16384`；此阶段策略不增加重试或输出额度，整轮额度由后文明确的预算版本选择。

关闭 clean thinking 的目的，是让这个抽取请求的输出窗口用于显式结构化回答。它不构成“语义质量提高”或“下一轮一定成功”的证据。其他研究、风险和最终判断继续使用 high thinking。v1 默认 request 对象及 `required.py` 的原 v1 前缀保持原样。

2026-10-27 已检索并打开官方依据：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。官方支持显式 `thinking.type=disabled`，JSON Output 仍要求提示中明确 JSON，并设置合适输出上限；`finish_reason=length` 不能当作完整回答。

同日新读的 Chat Completions API 将 `reasoning_effort=none` 描述为关闭 thinking，`low/high/max` 描述为开启；Thinking Mode guide 未说明与 toggle 冲突时的优先级。历史 `disabled/low` 字节保留，不能据此证明提供方内部最终模式，也不把该歧义当作历史 schema 失败的已知原因。新显式 strict 通道一致使用 `disabled/none`；其他阶段和 v1 默认策略不变。

## 保留的原 clean codec：`tiger.clean-wire.v1`

旧 `clean_payload_wire_v2`、`clean_schema_wire_v2`、`clean_output_wire_v2` 的接口和函数源码保留，用于原 codec 的独立精确 round trip 和新协议的内部字段/身份变换。新生产调用使用下一节单独版本的 keyed 协议。每批仍最多八篇，完整来源索引仍在每次调用中展示。`clean_payload_v2` 构造原始语义输入，旧输入 codec 只做以下机械变换：

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

## 当前 clean：`tiger.clean-keyed-wire.v1`

`clean_keyed_payload_v2` 精确保留旧输入 codec 的 documents、全部正文片段和完整全局来源表，只新增明确的来源绑定说明。`clean_wire.source_bindings` 是 `source_ref/allowed_quote_part_ids` 表，列出本批每篇自己允许选择的片段。即使相邻资料允许 `p0`，本篇若 `p0` 不合引文格式，仍不能选择。

模型必须返回 `d` 对象，以本批每个 `source_ref` 为唯一键，例如 `d0`、`d1`；每个本批来源恰好一次，缺失、未知或额外来源均拒绝。来源键内不再重复输出 `i/id`，其余全部短字段、文本、枚举、数组数量和 claim 顺序保留。来源键由模型实际返回，程序只将它机械映射为原文档 ID；`origin_id` 仍由模型自己判断并可引用完整全局来源索引。

`clean_keyed_schema_v2` 的两个 `allOf` 分支分别应用共享的完整文档 schema，以及封闭的必需来源键和各来源自己的 `q` 枚举。共享 schema 只出现一次，不内联八份完整规则；不再让模型通过 `i → oneOf` 组合推断每篇可用引文。原支持的 JSON Schema 子集已经支持这个组合，不增加新 schema keyword。解码后仍执行原 `clean_schema_v2`、`_coverage`、本篇 quote lookup 和原 typed 文档构造。

`format_example` 只示范实际来源键与一个本篇允许的 q；它明确省略全部必需语义字段，不是完整答案，也不说明示例片段支持任何事实。不提供或补填摘要、情感、事实、来源归属或风险默认值。提示同时要求模型自行阅读每篇完整正文、填写全部语义字段，不得复制结构片段充当答案。

旧模型数组的任意行序不是新对象的可逆信息。原 `clean_v2` 本来就按 ID 索引模型回答，再按输入 batch 顺序生成 `CleanDocumentV2`；新协议保持这个相同投影，以及逐来源所有语义值、自然文字和 claim 顺序。测试明确覆盖旧合法数组乱序得到同一 typed 结果，不把这称为所有旧原始 JSON 的逐字双射。

安全诊断只在一个 `allOf` 子分支明确失败时向下定位，且共享 additionalProperties 只能使用兄弟 schema 明确声明的可信键。多个失败分支或未解决的 `oneOf` 保持父位置；未知模型键、无效值、原始回答和推理内容均不进入诊断。接受/拒绝校验器保持原样，诊断没有补值、修复或重试能力。

## 新清洗输出通道：`tiger.clean-output.strict-function.v1`

普通 `response_format=json_object` 只保证 JSON 语法，不能保证来源局部枚举。第七次真实运行的第一批八篇清洗回答通过，第二批在 `/d/d11/c/1/q` 的枚举校验失败；两次调用共 43480 tokens，均已结算，账户未提交。该篇输入和 schema 的允许编号一致，违规编号未保留，仍未知。不能把其他片段中一个不可引用编号当作已观察的实际错误答案。

新的 v2 准入显式冻结 `clean_output_policy_id=tiger.clean-output.strict-function.v1`，并要求对应的 600k budget v3。缺省字段继续走原 JSON 通道，包括历史 300k 与历史 600k。准入、preflight、pipeline.prepare、完整源码清单与独立重放共同绑定这个选择；不能靠模型自报或数值上限选择新通道。

源码闭包由可信的 `trading_system/source_contract_v1.py` 静态注册表 `tiger.strict-source-inventory.v1` 规定；它不从运行者声明的 manifest 或裁剪后的冻结目录推导预期集合。当前注册的全部必需执行路径必须存在，唯一可选路径为 `events_calendar.json`，其内容另由原日历输入合同绑定。严格策略在复制源码、启动 worker 和付费前先验清单；事后准入与独立回放再次要求同一固定合同和逐文件字节绑定。同步删减源码及 manifest、再重算所有摘要，仍须拒绝。历史未声明 strict 的策略继续原合同；此闭包不保护任意改写可信验证器的主体，也不是远端签名。

只有清洗阶段向固定 `/beta/chat/completions` 发出一次请求：`tools` 恰有一个 `strict=true` 的 `emit_tiger_clean_v2`，并强制选择该函数。它只承载模型的完整结构化答案；程序不执行函数、不发送 tool result、不续接第二轮，不启用任意工具、流式或重试。模型、全部来源正文和原语义职责不变。其他五个语义阶段继续原 JSON/high 通道。

`clean_output.py::strict_clean_schema` 将现有 keyed schema 的公共规则和每来源约束展开为精确必需来源键、闭合文档字段及各篇自己的 q 枚举。没有可引用片段的来源只能返回空 claims。官方 strict 子集不支持字符串长度和数组数量限制，`uniqueItems` 也未列为支持；这些限制仅从提供方 schema 投影中省略，完整原 schema 同时展示给模型，返回后仍逐项执行原 keyed schema、解码后语义 schema、覆盖及逐字来源校验。提供方的格式约束不能替代语义质量、引文支持关系或本地完整验收。

原 `token_upper_bound()` 和旧策略仍拒绝 tools；只有新冻结选择允许这个单次、闭合、固定的输出函数。完整工具定义、每篇 schema、全部 messages 和最大输出均计入 UTF-8 保守预留，原 token/费用/次数/期限继续分别执行。新 I/O 使用 `tiger.deepseek-request.v2` / `tiger.deepseek-call.v2`，保留真实 `finish_reason=tool_calls`、唯一工具调用身份、完整参数及安全响应摘要；不改写成 stop，不保留隐藏推理。失败调用照实结算；独立重放重新构建原工具定义、端点、schema、参数、响应和预算后核对。旧请求、codec 和证据不改写。

依据为 2026-10-27 实际读取的 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Tool Calls / strict Mode](https://api-docs.deepseek.com/guides/tool_calls/)。严格函数输出属于 Beta 能力；本地测试和供应商文档不能替代一次完整真实任务及独立 receipt/verify，未来仍可能因服务、输出长度或语义约束失败。

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

当前新 v2 准入使用显式版本 `tiger.budget-policy.v3`，整轮上限为 **600000 实际加预留 tokens**。历史/默认 `BudgetGuard()`、`LIMITS`、`BudgetGuard.for_policy(None)` 和注册的 `tiger.budget-policy.v2` 仍为 **300000**，旧 snapshot/reservation schema2 没有 policy_id；v3 snapshot/reservation 为 schema3，并严格携带固定 policy_id。版本来自可信的 execution/admission 策略及冻结回放绑定，不能由模型回答或用户 payload 中任意数值 limits 选择。

这是明确版本化的 token 准入调整，不修改旧失败或把历史300k拒绝改成通过。两版其余硬限相同：40 chat、60 Search、20 Fetch、120 远端请求、保守估算 **1 USD**、2400 秒。按仓内冻结保守估算器，将所有 token 都按1.2 USD/百万及最多80次原生请求各0.00075 USD计算，600k与全部原生请求的合计上界为0.78 USD，仍受原1 USD guard约束；这是估算，不是提供方账单。每次远端调用仍先预留，失败调用保留真实 usage 结算，未知结果冻结，不自动重试。

对于两条 messages 的 chat，本次 admission 条件为：

```text
先前实际 tokens + 尚未结算预留
+ 完整 canonical request 的 UTF-8 字节数
+ 4096 + 64 × 2 + 本请求 max_tokens
<= 所选固定 policy 的 max_tokens（v3 为600000；历史/默认v2为300000）
```

UTF-8 字节是输入的保守预留，不是实际 tokenizer 计数。必须按同一个 guard 的真实顺序记录 reserve/settle、每次前缀、剩余量和第一次拒绝，不能把各阶段分别放进“先前 actual=100000”的新 guard 当作全链证明。来源数、摘要/事实长度、候选与持仓并集、补搜和 Fetch 都会改变最终请求大小。

本变更的纯合成验证覆盖空输入、55/56 来源、多批全消费、全部原文字段上限、跨批来源、别名形似正文、来源本篇 quote 绑定、异常及不重试、daily/premarket/intraday 完整 43 阶段、合法 BUY、失败后账户保持及重算哈希后的篡改拒绝。容量探针必须标明其合成输入和假设 usage；它不是未来真实用量或模型质量预测。

第五次历史真实运行以 `finish_reason=length` 失败；本变更不修改它或此前任何失败，也不能将旧失败计为成功。仅在新逻辑任务完成全部阶段、原始账户提交与独立 receipt/verify 后，才能报告一次新的真实完整成功。代码测试与容量改进本身不代表已完成真实业务、main 集成或生产发布。

第六次真实运行在第一次 clean 的 `/d/0`、`allOf` 校验失败；提供方自然结束 `stop`，实际 prompt/completion 为 19664/4931，总计 24595 tokens，已结算。旧诊断先检查组合，且无效回答未保留，不能据此推断实际 `i`、`q`、某个值或具体错误字段。失败在 paper/account 提交前停止，无 receipt。当前新协议改善来源归属表达，不把这个历史失败计为成功，也不声称已证实其具体字段根因。

以下至八行对照表记录的是 **strict 通道加入前的 JSON 请求**，保留其历史身份。实际第六次的55篇规则清洗来源及准确冻结 clean assessment 用于该纯容量探针时，七批 keyed clean 请求分别为 **73998、61739、68314、59591、60161、63596、57986 UTF-8 bytes**；首批相比历史真实72744 bytes增加1254 bytes，而非重复八份 schema。较早采用声明合成 assessment 的表每批少7 bytes，原表保留，不能与准确冻结 assessment 的新表混用。下游语义、市场和公司研究仍使用显式合成值，因为真实运行尚未到达这些阶段。

把已观测首批 24595 tokens 重复七次只能作为压力条件，不能预测真实用量；它给出 172165-token clean 前缀。在“摘要390/来源说明180/四事实”的合成条件下，下一舆情请求预留 195814，原 guard 即拒绝。在“120/60/两事实、十研究目标、补证”的合成条件下，最终请求为 182695 bytes，允许的先前 actual 上限仅 96697，压力情形的实际前缀 205165 仍拒绝。仅第一批采用真实 usage、其余每 chat 假设 3000 的另一条件下，简洁三/十目标可准入，较宽三目标也可能在最终阶段被拒。每个条件均用同一个原 guard 顺序验证；单独展示 0、24595、172165 前缀的容量列不冒充整轮证明。

上述拒绝仍是原300k版本的有效历史结果。新的纯对照在同一批55来源、同八组目标/语义条件、同三种 usage 压力下，分别真实调用 default300k、注册v2和注册v3 guard，逐请求 reserve/settle；default与显式v2完全相同，旧24条300k整链 trace及最终预算逐项保持。共72次 guard 回放，其中24次仅验证旧v2别名等价，不冒充额外独立场景。

在“七次 clean 均重复已观测24595，其余每 chat 为声明的2000 prompt+1000 completion”条件下，八组v3整链均准入，完整次数与所有原硬限相容：

| 合成摘要/来源说明/每篇claims | 研究目标并集 | 原300k首次拒绝 | v3最终请求bytes | v3最终actual前缀 | v3最小剩余token预留空间 |
|---|---:|---|---:|---:|---:|
| 390/180/4 | 0 | 舆情 | 200975 | 184165 | 194252 |
| 390/180/4 | 3 | 舆情 | 222859 | 187165 | 169368 |
| 390/180/4 | 10 | 舆情 | 273685 | 205165 | 100542 |
| 390/180/4 | 20 | 舆情 | 332433 | 223165 | 23794 |
| 120/60/2 | 0 | 最终判断 | 109985 | 184165 | 285242 |
| 120/60/2 | 3 | 最终判断 | 131869 | 187165 | 260358 |
| 120/60/2 | 10 | 最终判断 | 182695 | 205165 | 191532 |
| 120/60/2 | 20 | 公司研究 | 241443 | 223165 | 114784 |

零目标没有公司补证；三/十/二十目标均包含声明的补搜和Fetch。目标并集包括候选及持仓，最终执行决定最多十条。上述最紧的较宽二十目标情形完成25 chat、54 Search、20 Fetch，共99请求；最终实际合成usage为226165 tokens，估算已结算0.326898 USD，过程中已结算加本次预留峰值约0.7469472 USD，最小token剩余空间为23794。所有请求、usage、次数、前缀、剩余量和首次拒绝均保留在独立工作区证据中，模型/网络调用为零。

这仍不保证任意合法最大输入、最大输出或真实未来用量都能完成。“每次使用全部输入上界加全部输出上限”的更强压力中，七次 clean 结算合计589641后，v3较宽与简洁两个摘要档位均在下一舆情请求被拒；原300k则在第四批clean前拒绝。仅首批采用真实usage、其余每chat假设3000的另一个条件下，八组v3均准入。600k的新版本修复了上述已声明条件中的容量冲突，没有取消原guard的真实拒绝能力。

无效回答缺失，无法从安全摘要恢复逐文档completion tokens或给出它们的实际上界；不得用平均数冒充上界。未来真实语义、行情、补证与用量仍未知，必须由新的完整真实任务、独立receipt/verify及原始账户提交证明成功，不能用这些纯探针替代，也不能靠修改失败历史、截断语义、放宽原校验、静默改变版本或自动重试获得成功标签。

strict 通道另有独立容量证据。只读重建第七次真实运行的两份原输入时，完整新请求为 81461 / 73667 bytes，连同全部输出与 framing 的预留为 102069 / 94275 tokens；两者的原正文、本地完整约束和每来源 q 枚举保留，提供方 schema 自身增加约 17.3k bytes。这些是新请求重建与 guard 准入，没有新模型用量。

再以第六次的55篇原来源和同八组显式合成下游条件，顺序执行新 strict guard 的24个整链条件。只把首批旧观测24595与后续每chat3000结合，或七个clean均假设旧观测24595时，两个条件各8/8准入；最大目标并集仍为25 chat、54 Search、20 Fetch。它们没有测得新 strict 模型的实际 token 用量。每次都按完整输入上界和输出上限结算的更强压力则8/8拒绝：第六个clean前，先前actual515898加新预留101995超过600000。旧300k与未声明strict的600k守卫仍拒绝该工具形状。新工具定义的额外容量必须据实计算，不能沿用上表旧JSON请求字节或把声明usage当成未来预测。

验证入口包括新 `tests/test_clean_strict_output_v2.py`、`tests/test_clean_strict_policy_replay_v2.py`，以及 `tests/test_clean_keyed_wire_v2.py`、`tests/test_clean_keyed_diagnostics_v2.py`、`tests/test_clean_capacity_wire_v2.py`、`tests/test_final_capacity_wire_v2.py`、`tests/test_model_reasoning_policy_v2.py`、`tests/test_short_term_core_v2.py`、`tests/test_required_proof_v2.py` 与既有语义、预算、传输和完整工作流回归。它们使用明确的合成模型答案，不能替代真实模型验收。
