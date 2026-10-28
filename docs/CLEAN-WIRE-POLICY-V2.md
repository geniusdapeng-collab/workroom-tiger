# v2 clean 与最终判断的无损传输约定

本约定属于 Tiger 的 `us-public-model-required-*-paper-v2` 研究请求策略。模型仍为 `deepseek-flash`，六个必需语义阶段仍须得到真实、完整且通过 schema 与独立回放验证的回答。输入编码精确保留原值；新 clean 回答保留逐来源的全部语义值和原有类型化输出投影，不宣称保留旧模型任意数组行序。编码不产生新闻事实、来源归属、模型理由、数值批准或成交。

实现入口为 `trading_system/llm/required.py` 的 clean/final wire helpers、`clean_v2`、`decision_v2`，以及 `deepseek_required.py` 的 `build_chat_request`、`stage_thinking_mode`。`required_proof_v2.py` 从同一确定性 builder 重建准确请求、回答展开及预算，不能只信保存的成功标记。

## thinking 策略

| 范围 | 原生 API / 输出 | 思考控制字段 | 模型是否仍须真实回答 |
|---|---|---|---|
| 当前 compact regeneration policy 的 v2 `clean.llm_semantic` | Beta Chat / 固定标量输出函数 | `thinking.type=disabled`、`reasoning_effort=none` | 是，最多一次有界完整重新生成 |
| 历史 Responses slots policy 的 v2 `clean.llm_semantic` | `/responses` / 固定可空槽位 `text.format.json_schema` | `reasoning.effort=none` | 是，单次完整 JSON |
| 历史 `responses-schema.v1` 的 v2 `clean.llm_semantic` | `/responses` / 原完整 keyed schema | `reasoning.effort=none` | 是，保留原请求 |
| 历史 `strict-function.v1` 的 v2 `clean.llm_semantic` | Beta Chat / 固定函数 | `thinking.type=disabled`、`reasoning_effort=none` | 是，原单次函数输出 |
| 未声明输出 policy 的历史 v2 `clean.llm_semantic` | Chat / JSON | `thinking.type=disabled`、`reasoning_effort=low`，保留原请求 | 是 |
| v2 舆情、风险、板块、公司研究、最终判断 | Chat / JSON | `thinking.type=enabled`、`reasoning_effort=high` | 是 |
| v1 默认请求 | 原 Chat | `thinking.type=enabled`、`reasoning_effort=high` | 按原 v1 契约 |

上表记录准确发送的请求字段。clean 仍由同一个 Flash 模型解释每篇资料并输出全部语义标注；`disabled` 不是关闭模型能力、必需阶段或调用。新通道由可信 execution 的显式 `clean_output_policy_id` 及活动阶段共同派生，调用者不能借请求参数改变其他阶段。当前 compact policy 的 clean 最大输出为32768；历史各协议的16384及单次行为保留。新的已知schema拒绝重新生成规则见下文；budget v4依操作者授权扩大整轮额度，旧版本上限保持原样。

关闭 clean thinking 的目的，是让这个抽取请求的输出窗口用于显式结构化回答。它不构成“语义质量提高”或“下一轮一定成功”的证据。其他研究、风险和最终判断继续使用 high thinking。v1 默认 request 对象及 `required.py` 的原 v1 前缀保持原样。

2026-10-27 已检索并打开官方依据：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。官方支持显式 `thinking.type=disabled`，JSON Output 仍要求提示中明确 JSON，并设置合适输出上限；`finish_reason=length` 不能当作完整回答。

同日新读的 Chat Completions API 将 `reasoning_effort=none` 描述为关闭 thinking，`low/high/max` 描述为开启；Thinking Mode guide 未说明与 toggle 冲突时的优先级。历史 `disabled/low` 字节保留，不能据此证明提供方内部最终模式，也不把该歧义当作历史 schema 失败的已知原因。历史 strict-function 通道一致使用 `disabled/none`；历史 Responses 通道只使用其原生 `reasoning.effort=none`。其他阶段和 v1 默认策略不变。

## 保留的原 clean codec：`tiger.clean-wire.v1`

旧 `clean_payload_wire_v2`、`clean_schema_wire_v2`、`clean_output_wire_v2` 的接口和函数源码保留，用于原 codec 的独立精确 round trip 和新协议的内部字段/身份变换。该历史版本的后续 keyed 分支见下一节，保留旧每批最多八篇；当前compact在budget v4下为一篇，旧显式v3为四篇。历史批次的完整来源索引仍在每次调用中展示。`clean_payload_v2` 构造原始语义输入，旧输入 codec 只做以下机械变换：

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

## 保留的 keyed clean：`tiger.clean-keyed-wire.v1`

`clean_keyed_payload_v2` 精确保留旧输入 codec 的 documents、全部正文片段和完整全局来源表，只新增明确的来源绑定说明。`clean_wire.source_bindings` 是 `source_ref/allowed_quote_part_ids` 表，列出本批每篇自己允许选择的片段。即使相邻资料允许 `p0`，本篇若 `p0` 不合引文格式，仍不能选择。

模型必须返回 `d` 对象，以本批每个 `source_ref` 为唯一键，例如 `d0`、`d1`；每个本批来源恰好一次，缺失、未知或额外来源均拒绝。来源键内不再重复输出 `i/id`，其余全部短字段、文本、枚举、数组数量和 claim 顺序保留。来源键由模型实际返回，程序只将它机械映射为原文档 ID；`origin_id` 仍由模型自己判断并可引用完整全局来源索引。

`clean_keyed_schema_v2` 的两个 `allOf` 分支分别应用共享的完整文档 schema，以及封闭的必需来源键和各来源自己的 `q` 枚举。共享 schema 只出现一次，不内联八份完整规则；不再让模型通过 `i → oneOf` 组合推断每篇可用引文。原支持的 JSON Schema 子集已经支持这个组合，不增加新 schema keyword。解码后仍执行原 `clean_schema_v2`、`_coverage`、本篇 quote lookup 和原 typed 文档构造。

`format_example` 只示范实际来源键与一个本篇允许的 q；它明确省略全部必需语义字段，不是完整答案，也不说明示例片段支持任何事实。不提供或补填摘要、情感、事实、来源归属或风险默认值。提示同时要求模型自行阅读每篇完整正文、填写全部语义字段，不得复制结构片段充当答案。

旧模型数组的任意行序不是新对象的可逆信息。原 `clean_v2` 本来就按 ID 索引模型回答，再按输入 batch 顺序生成 `CleanDocumentV2`；新协议保持这个相同投影，以及逐来源所有语义值、自然文字和 claim 顺序。测试明确覆盖旧合法数组乱序得到同一 typed 结果，不把这称为所有旧原始 JSON 的逐字双射。

安全诊断只在一个 `allOf` 子分支明确失败时向下定位，且共享 additionalProperties 只能使用兄弟 schema 明确声明的可信键。多个失败分支或未解决的 `oneOf` 保持父位置；未知模型键、无效值、原始回答和推理内容均不进入诊断。接受/拒绝校验器保持原样，诊断没有补值、修复或重试能力。

## 保留的清洗输出通道：`tiger.clean-output.strict-function.v1`

普通 `response_format=json_object` 只保证 JSON 语法，不能保证来源局部枚举。第七次真实运行的第一批八篇清洗回答通过，第二批在 `/d/d11/c/1/q` 的枚举校验失败；两次调用共 43480 tokens，均已结算，账户未提交。该篇输入和 schema 的允许编号一致，违规编号未保留，仍未知。不能把其他片段中一个不可引用编号当作已观察的实际错误答案。

这个历史版本的 v2 准入显式冻结 `clean_output_policy_id=tiger.clean-output.strict-function.v1`，并要求对应的 600k budget v3。该历史时期的新准入选择后文独立的 Responses policy；历史 strict-function 的请求、响应和验证分支保留。缺省字段继续走原 JSON 通道，包括历史 300k 与历史 600k。准入、preflight、pipeline.prepare、完整源码清单与独立重放共同绑定这个选择；不能靠模型自报或数值上限选择新通道。

源码闭包由可信的 `trading_system/source_contract_v1.py` 静态注册表 `tiger.strict-source-inventory.v1` 规定；它不从运行者声明的 manifest 或裁剪后的冻结目录推导预期集合。当前注册的全部必需执行路径必须存在，唯一可选路径为 `events_calendar.json`，其内容另由原日历输入合同绑定。严格策略在复制源码、启动 worker 和付费前先验清单；事后准入与独立回放再次要求同一固定合同和逐文件字节绑定。同步删减源码及 manifest、再重算所有摘要，仍须拒绝。历史未声明 strict 的策略继续原合同；此闭包不保护任意改写可信验证器的主体，也不是远端签名。

只有清洗阶段向固定 `/beta/chat/completions` 发出一次请求：`tools` 恰有一个 `strict=true` 的 `emit_tiger_clean_v2`，并强制选择该函数。它只承载模型的完整结构化答案；程序不执行函数、不发送 tool result、不续接第二轮，不启用任意工具、流式或重试。模型、全部来源正文和原语义职责不变。其他五个语义阶段继续原 JSON/high 通道。

`clean_output.py::strict_clean_schema` 将现有 keyed schema 的公共规则和每来源约束展开为精确必需来源键、闭合文档字段及各篇自己的 q 枚举。没有可引用片段的来源只能返回空 claims。官方 strict 子集不支持字符串长度和数组数量限制，`uniqueItems` 也未列为支持；这些限制仅从提供方 schema 投影中省略，完整原 schema 同时展示给模型，返回后仍逐项执行原 keyed schema、解码后语义 schema、覆盖及逐字来源校验。提供方的格式约束不能替代语义质量、引文支持关系或本地完整验收。

原 `token_upper_bound()` 和旧策略仍拒绝 tools；只有新冻结选择允许这个单次、闭合、固定的输出函数。完整工具定义、每篇 schema、全部 messages 和最大输出均计入 UTF-8 保守预留，原 token/费用/次数/期限继续分别执行。新 I/O 使用 `tiger.deepseek-request.v2` / `tiger.deepseek-call.v2`，保留真实 `finish_reason=tool_calls`、唯一工具调用身份、完整参数及安全响应摘要；不改写成 stop，不保留隐藏推理。失败调用照实结算；独立重放重新构建原工具定义、端点、schema、参数、响应和预算后核对。旧请求、codec 和证据不改写。

依据为 2026-10-27 实际读取的 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Tool Calls / strict Mode](https://api-docs.deepseek.com/guides/tool_calls/)。严格函数输出属于 Beta 能力；本地测试和供应商文档不能替代一次完整真实任务及独立 receipt/verify，未来仍可能因服务、输出长度或语义约束失败。

## 保留的清洗输出通道：`tiger.clean-output.responses-schema.v1`

第八次真实任务 `run-4b2c28ca6e5b4165b8cfb2867e942fb9` 使用来源 `1ba9cbbfb7e9e8b6a38e84a36ef6ed6ac6ff4a09`，在 14 次原生 Search 之后，首个 Beta strict 清洗请求收到 HTTP 400。没有取得成功模型响应，也没有进入本地答案校验；提供方错误正文未保留，具体拒绝原因未知。用量未知的 106849 tokens 预留仍在原任务冻结，不能写成零真实费用或被新策略释放。原身份及失败链保留，receipt/verify 均为 `RUN_INCOMPLETE`，账户未提交。

2026-10-27 已读取官方 [Responses API](https://api-docs.deepseek.com/api/create-response/) 和 [Responses guide](https://api-docs.deepseek.com/guides/responses_api/)。该 API 明确列出 `text.format.json_schema`；没有给出 Responses 专属的 schema 关键字子集、大小或深度限制，也没有证据表明它降为 Chat 的 `json_object`。本协议采用这条原生结构化输出通道，不把第八次 400 归因于未经证实的字段。

新的可信准入冻结 `tiger.clean-output.responses-schema.v1`，仍要求 budget v3 和 `tiger.strict-source-inventory.v1` 的完整执行路径闭包。只有清洗阶段向固定 `https://api.deepseek.com/responses` 发起一次请求：原完整 system 使用 `instructions`，原完整 user 使用单 user `input`，`reasoning={effort:"none"}`、`max_output_tokens=16384`、`temperature=0.2`、`stream=false`，`text.format` 为 `type=json_schema`、固定 schema 名和本次完整 keyed wire schema。请求不添加未列出的 `strict`，不携带 tools、tool_choice 或续轮参数。

这里传入的是现有 `clean_v2` 的完整本地 keyed output_schema，保留公共 `allOf`、精确来源键、各篇 q 枚举、全部字符串/数组上限和唯一性约束；不是上一节删去部分 bounds 的 Beta provider projection。正文、来源表、语义职责和确定性 codec 不改。服务端接受这个具体完整 schema 的能力仍须正式真实运行验证；无论提供方如何约束，返回后仍逐项执行原完整 keyed schema、解码语义 schema、覆盖和本篇逐字引文校验。程序不修补回答、代写语义、截断事实或重试。

原生 Responses 的成功终态为 `object=response`、`status=completed`，不是 Chat 的 `finish_reason`。本地接受契约要求一个 completed assistant message 和一个非空 `output_text`；incomplete、failed、in_progress、推理或工具条目、空/重复键/非有限或带尾随文字的 JSON 均不能成为成功。新私有 I/O/proof 保留原生 response status、created_at、message 身份和 native usage；不制造 `stop` 或工具调用身份，重放从同一 builder 重建端点、完整正文/schema、解析结果及安全响应。

原生消息还可能带可选 `phase`，文本项可能带可选 `logprobs`。适配器接受 `phase` 缺省/null/`final_answer`，拒绝中间 `commentary` 或未知值；`logprobs` 仅接受缺省/null/空数组，原 `annotations` 仍仅接受缺省/空数组。这些经验证的惰性元数据与原空 annotations 一样不进入安全响应投影；原传输 SHA 仍区分完整收到的正文。未知字段、非空未请求的元数据、推理/工具条目和所有原语义/来源/数量限制继续拒绝，不通过宽泛忽略字段来修复正文。该兼容修复不改变业务 policy、请求或 proof 版本。各原生响应拒绝位置另有固定安全错误码，避免以同一个 `INCOMPLETE_MODEL_RESPONSE` 掩盖不同边界；不保留原字段值或回答。

原生 usage 的 `input_tokens/output_tokens/total_tokens` 保留；为既有预算显式映射为 prompt/completion/total。reasoning tokens 是 output 子集，不能再加一次。合法已知用量在内容/schema 拒绝前结算；缺失、矛盾或超出预留仍冻结。每个请求按包括 schema 的完整 canonical UTF-8 body、framing 和最大输出先预留，同一完整 run 仍受 600k、1 USD、次数和期限约束。旧任务的未知预留留在原任务，不迁入或释放；新源码修正任务有明确新逻辑身份和同样的 per-run 硬限，累计未知费用不能冒称免费。

新的 `/responses` 400/422 诊断只允许有界读取后得到的固定提供方声称类别及固定 body 状态，不保留原 message、任意 type/code/param、正文或秘密。这种分类不是独立证实的根因；未知保持未知。原 JSON 和 strict-function 历史证据不追补新字段。Responses 的无状态会话也不等于供应商完全不缓存输入，官方仍说明有自动 context cache；本协议不发送无效的 `store:false` 来冒充缓存控制。[Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)

## 保留的清洗：`tiger.clean-output.responses-slots.v1` / `tiger.clean-slots-wire.v1`

第十次完整任务 `run-b2d2c452ad484675afa37a7684193950` 的前四批八篇清洗通过，第五批 completed 答案在 `/d/d35/t` 违反 `maxItems`。总共116914 tokens 已结算，账户未提交；非法答案没有保留，具体超过几个 ticker 未知。这个原八标签上限是语义标注容量，不是仓位或风险参数。不能通过增大上限、截断答案或删掉必要事实掩盖失败。

新准入对三个 v2 profile 选择独立的 slots 输出 policy，并仅为这个版本增加 `clean_output_codec_id=tiger.clean-slots-wire.v1`。请求仍是原四字段，不能自由选择或覆盖 codec；历史 JSON、strict-function、Responses-schema policy、预算与重放保留各自原合同。静态完整源码闭包继续是 `tiger.strict-source-inventory.v1`。

`clean_slots_schema_v1` 以原 `clean_keyed_schema_v2` 为语义合同，将每来源的共享完整规则与本篇 q 枚举合成闭合对象。只把原有有界列表机械变换为 `s0` 至 `s(N-1)` 的固定必需可空槽位；ticker、事件、产业链、ETF、claims 一起变换，不逐次只修一个 maximum。每个非空槽位的项目 schema 保留原字段、字符串、数值、枚举、来源局部 q 与约束；新提供方 schema 不依赖旧共享 `allOf/additionalProperties` 的装配。

槽位必须形成非空前缀，剩余尾槽为 null。解码按模型实际顺序恢复原列表，null 从未成为事实或默认值。洞、缺键、未知键、类型错误、重复、原 min/max 或语义字段违规仍拒绝。编码超过原上限的列表也拒绝，禁止事后截断或补足。ticker 至多八个模型选出的相关实体，不承诺穷尽实体；必要事实、反证与限制仍属于原摘要和 claims。模型仍自行决定情感、事件、来源、时效和事实，不从 slot schema 得到语义答案。

`clean_slots_payload_v1` 保留完整正文分片、原字段值、覆盖元数据、全部来源索引和本篇引用绑定，只版本化 wire 元数据并增加从原 schema 派生的槽位说明。新提示替换原 variable-array `format_example` 指导；不向模型灌入完整或缺省语义答案。原 keyed payload helper 和旧格式示例保持历史行为。

仅新 selector 的清洗在原固定 `/responses`、相同模型和 reasoning/output/temperature/stream 控制下发送一次完整槽位请求。该新分支以 canonical UTF-8 bytes 直接发送 JSON，使用原 `application/json` header；预算和 request hash 绑定这份完整实际正文，包括全部 schema、正文及元数据。历史通道保留原 `json=request` 发送行为，不能把新分支的字节相等结论追溯给旧调用。Requests 支持直接 `data` 发送并由调用者明确设置 JSON Content-Type，依据：[Requests 官方 Quickstart](https://requests.readthedocs.io/en/latest/user/quickstart/#more-complicated-post-requests)。

私有 request/call 继续使用 v3 原生 Responses 结构：`output_schema/schema_sha256` 始终指原完整 keyed 合同；明确的 `output_policy_id` 选择新版本，完整 `request_sha256` 另绑定实际槽位 provider schema。安全 response 和 parsed 保留模型真正返回的槽位对象，不伪造一个“原生返回的解码数组”。可信 clean 调用立即逆变换，再执行原完整 keyed schema、原语义 schema、覆盖、source-local quote lookup 和 typed 文档构造。独立 replay 从真实上游重建新 payload、完整实际请求、槽位答案与解码结果，不能只验证一个可重算的自包含 hash。

新的预算 allowlist 为独立 `allow_clean_slots`，只有可信新 selector 派生；旧 JSON、strict function 与 Responses-schema 守卫不因此允许槽位格式。40 chat、600k 实际加预留、60 Search、20 Fetch、120 总请求、1 USD 和2400秒均保留。原生状态、usage、空元数据兼容和已知失败结算仍如上一节；不自动重试、换模型或规则回退。固定槽位不保证语义质量、所有提供方 schema keyword 都生效、任意最大回答适合输出窗口，或下次真实完整业务一定成功。

## 当前清洗：compact lists 与有界完整重新生成

三个 v2 profile 的新准入显式冻结 `tiger.clean-output.compact-lists-regeneration.v1`、`tiger.clean-compact-lists-wire.v1` 和 `tiger.model-regeneration.clean-schema.v1`。当前budget v4每批一篇，每来源28个必需标量；旧显式budget v3仍每批最多四篇。历史八篇协议保留原builder、请求、证明和语义。逐篇分配只减少同时输出的结构，不删除来源、正文、事实字段、来源索引或时效检查。仅清洗使用固定 `/beta/chat/completions`、一个强制 `strict=true` 的 `emit_tiger_clean_v2` 输出函数、disabled/none、`max_tokens=32768`。输出函数不执行业务动作。其余五个语义阶段继续原 high/JSON 通道。

ticker、事件、产业链、ETF 列表使用逗号标量，只有原项目语法能证明分隔符和空项目不可能出现时才可编译。空串仅表示空列表，逆变换保留原顺序，不 trim、排序、去重、截断或补值。四个 claims 槽的空字符串仅表示尾部未使用；洞、部分空字段、重复或本篇 q 违规仍拒绝。完整原 keyed schema、原语义 schema、覆盖、逐字引文与 typed 文档构造继续执行。摘要原 800 上限不放宽；提示中的 200–450 字符是表达建议，不是必要事实删减权限或新验收下界。

全部正文分片、全局来源索引、assessment、覆盖和本篇引用绑定精确保留。提供方以规范 `$def/$ref` 共享相同叶 schema，严格展开必须等于本地完整标量投影；外部、循环、悬空、链式、未用或非规范引用拒绝。仅投影文档未支持的长度 keyword，其原上界仍由注册 pattern 和完整本地合同执行。固定字段映射只有一份；请求与预算绑定完整 canonical UTF-8 bytes。官方通道依据：[Tool Calls / strict Mode](https://api-docs.deepseek.com/guides/tool_calls/)。该文档不能证明服务端必定执行每个约束，本地完整验收始终必需。

只有以下条件同时成立才允许新的完整生成：实际响应完整、模型/响应/工具身份合法且唯一、`finish_reason=tool_calls`、标准 usage 已知并已结算、严格 JSON 没有重复键或非有限值、原文与全部解码键值的安全脱敏结果均与原值相同，随后完整 schema/逆变换以固定 `OUTPUT_SCHEMA_MISMATCH` 拒绝。每来源最多追加一次，budget v4每完整任务最多追加56次；旧显式v3仍每批一次、整轮最多四次。第二次拒绝、额度不足或预算不准入即失败。语法、重复键、秘密、身份、HTTP、超时、长度终止和未知结果均终止，不触发重新生成；未知用量继续冻结。安全检查不通过的原拒绝文本不封存。

新生成仍使用相同完整 user 输入和 schema；只有固定 system 后缀加入从前次拒绝派生的闭合反馈。不发送旧答案、任意异常文字或语义提示，不修补已经返回的答案。非阻塞逻辑调用锁阻止重入；generation1 的输入和前次拒绝摘要须在付费 I/O 前持久记录，不提供崩溃后模型调用恢复或第二控制器自动重跑。

私有 request/call v4 记录 accepted/rejected 的有序并集、实际参数、安全响应、真实用量、预算、generation_index、logical_call_sha256 和 previous_rejection_sha256。拒绝是已完成的模型 I/O，但不进入语义管线；终止传输/安全错误仍走原 error_ref。独立回放从真实上游重建请求，重新解析并复现原答案被拒绝，核对同一批紧邻且只消费一次的新生成；有效答案谎报拒绝、遗漏已付费调用、重复身份、换输入、改反馈或连贯重算外层 hash 均不能替代可信采集锚点。只有接纳值进入后续阶段。

完成汇总的 call_count、response_ids、stage_counts 计入全部真实尝试；增加精确的 regeneration_policy_id、accepted_call_count、rejected_call_count，后两者之和等于 call_count，拒绝数随可信预算版本分别为v4的0..56、v3的0..4，且不得超过已接纳的clean逻辑调用数。六阶段仍各需接纳答案，Node、受控回执和公开研究投影按同一闭合合同检查；旧/disabled/非 upgrade 形状不能夹带这些字段。安全响应及其原拒绝文本仅留私有证明，不公开。哈希与回放提供相对可信源码/采集/准入锚点的本地完整性证据，不是提供方签名，也不能排除所有锚点均被替换的完整伪造。

当前新任务显式使用 `tiger.budget-policy.v4`：160 chat、20000000 实际加预留 tokens、60 Search、20 Fetch、240 远端请求、保守估算 30 USD、7200 秒；逐次先预留后调用，全部尝试共享同一账本。最多56篇逐篇清洗的结构上界为 56 clean + 56 重新生成 + 14 发行人调用 + 4 其他语义调用 = 130 chat，另最多54 Search和20 Fetch，总计204次远端调用。旧显式v3仍为四篇一批、最多四次追加，40 chat/600000 tokens/120远端/1 USD/2400秒，旧回执不变。这些是容量上限，不是真实费用或成功率承诺。新的 `allow_clean_compact_regeneration` 只从可信 selector 派生，旧各协议 allowlist 不扩大。

第十一次完整任务 `run-553dcd7df45c4dcea389276e79e2cfda` 使用来源 `2acab5a0aac74e90a6b729e0da2c7c25e214f117` 的 Responses slots 版本，首个原生清洗回答未通过 JSON 解析；实际输入 48741、输出 13683、合计 62424 tokens，14 Search、零 Fetch，已结算、零剩余预留、账户未提交。原无效回答未保留，具体语法原因未知。后续单独协议诊断不改写这个未知原因，也不计为完整业务成功。该新策略须由全新的完整任务及独立 receipt/verify 证明；代码、合成测试和诊断均不算真实完整成功。

2026-10-28 重新读取官方 [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) 后，compact 入口接受响应消息中的 `content=null` 或普通字符串；官方未要求 `finish_reason=tool_calls` 时 content 必须为空。字符串须通过原值安全检查、UTF-8 及响应大小边界，并原样保留在私有安全响应中；它不参与清洗参数、语义解码或下游业务。独立回放重建并核对该字段，缺省的历史安全响应仍保持 `content=null`。模型/角色/完成原因、唯一固定函数、参数严格 JSON、本地完整 schema、来源与引文的原校验继续执行。结构失败使用固定字段级错误码，不把任意响应字段、文本或秘密写入诊断。

第十二次完整任务 `run-a60339d90778460c970b03f30d2ea334` 使用来源 `70856e2da3648a93f276a7f2c6216b7590dbcd5f`，完成14次原生Search后在首个compact清洗调用以 `INCOMPLETE_MODEL_RESPONSE` 失败。该调用 `finish_reason=tool_calls`，输入10854、输出1247、合计12101 tokens均已结算，剩余预留为0，未生成成功回执。旧复合诊断没有保留精确触发字段，不能认定非空content就是此次失败原因；本次修复依据官方允许的响应形式和离线反例，同时补齐后续诊断。旧失败及其账本保持原样。

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

当前新v2准入显式使用 `tiger.budget-policy.v4`：**20000000实际加预留tokens、160 chat、60 Search、20 Fetch、240远端请求、30 USD保守估价、7200秒**。snapshot/reservation均使用schema4并携带精确policy_id，只能和当前compact清洗一起选择。历史显式v3保留600000 tokens、40 chat、120远端、1 USD、2400秒及schema3；历史/默认`BudgetGuard()`、`LIMITS`、`for_policy(None)`和预算v2仍为300000及原schema2。版本来自可信execution/admission和冻结回放绑定，模型答案、请求payload或任意数值limits不能选择新政策。

这次额度扩大来自操作者授权，目的是容纳逐来源清洗及完整下游输入。按仓内冻结估价器，20000000 tokens和全部80次原生调用的估算为24.06 USD，30 USD是另行执行的上限；估价不是提供方账单，也不限制或代表Codex额度。每次远端调用仍先预留，失败保留真实usage，未知结果冻结；仅已知且安全的schema拒绝允许上述有界完整重新生成。历史失败不改写。

对于两条 messages 的 chat，本次 admission 条件为：

```text
先前实际 tokens + 尚未结算预留
+ 完整 canonical request 的 UTF-8 字节数
+ 4096 + 64 × 2 + 本请求 max_tokens
<= 所选固定 policy 的 max_tokens（v4为20000000；v3为600000；历史/默认v2为300000）
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

历史 strict-function.v1 通道另有独立容量证据。只读重建第七次真实运行的两份原输入时，完整新请求为 81461 / 73667 bytes，连同全部输出与 framing 的预留为 102069 / 94275 tokens；两者的原正文、本地完整约束和每来源 q 枚举保留，提供方 schema 自身增加约 17.3k bytes。这些是新请求重建与 guard 准入，没有新模型用量。

再以第六次的55篇原来源和同八组显式合成下游条件，顺序执行该 strict-function guard 的24个整链条件。只把首批旧观测24595与后续每chat3000结合，或七个clean均假设旧观测24595时，两个条件各8/8准入；最大目标并集仍为25 chat、54 Search、20 Fetch。它们没有测得新 strict 模型的实际 token 用量。每次都按完整输入上界和输出上限结算的更强压力则8/8拒绝：第六个clean前，先前actual515898加新预留101995超过600000。旧300k与未声明strict的600k守卫仍拒绝该工具形状。新工具定义的额外容量必须据实计算，不能沿用上表旧JSON请求字节或把声明usage当成未来预测。

当前 compact 验证入口为 `tests/test_clean_compact_regeneration_core_v1.py`、`tests/test_clean_compact_lists_policy_proof_v1.py` 与 `bundles/trading/capabilities/workflow-clean-regeneration.test.mjs`，包括实际 Python 汇总到 Node/研究/回执的合成联动。历史验证入口包括 `tests/test_clean_responses_output_v1.py`、`tests/test_clean_responses_policy_replay_v1.py` 及历史 `tests/test_clean_strict_output_v2.py`、`tests/test_clean_strict_policy_replay_v2.py`，以及 `tests/test_clean_keyed_wire_v2.py`、`tests/test_clean_keyed_diagnostics_v2.py`、`tests/test_clean_capacity_wire_v2.py`、`tests/test_final_capacity_wire_v2.py`、`tests/test_model_reasoning_policy_v2.py`、`tests/test_short_term_core_v2.py`、`tests/test_required_proof_v2.py` 与既有语义、预算、传输和完整工作流回归。它们使用明确的合成模型答案，不能替代真实模型验收。
