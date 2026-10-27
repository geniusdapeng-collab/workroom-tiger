# Tiger 严格工作流实现与维护边界

本文件说明 Tiger 专用 Agent 入口的实现约束。操作者命令、Codex/DeepSeek Harness 配置和错误码见 [AGENT-CLIENTS.md](AGENT-CLIENTS.md)；请求及返回结构见 `bundles/trading/agent-workflow.schema.json`。严格入口初始任务为 [issue 26](https://github.com/geniusdapeng-collab/workroom-tiger/issues/26)，短线 v2 升级任务为 [T-2026-1027-0001 / issue 39](https://github.com/geniusdapeng-collab/workroom-tiger/issues/39)。

## 使用者、场景和范围

使用者是通过 Codex、DeepSeek Harness 或其他支持 shell/STDIO MCP 的 Agent 调用 Tiger 的操作者。目标是执行完整公开行情研究和本地模拟账户流程，并能区分“任务已接纳”“流程失败”“结果通过独立校验”。调用者无需自行拼接内部阶段。

三个具体场景：

1. 操作者要求查看当前公开美股研究结果。Agent 先检查运行条件，提交固定请求，保存 run_id，等待后读取并校验回执，分别展示新信号、已有模拟持仓、待成交订单和现金。
2. 桌面软件在运行期间关闭或连接中断。已接纳的业务进程继续运行；Agent 使用原 run_id 查询。若提交响应丢失，则用原请求和原幂等键恢复身份，不新建任务来猜测是否执行过。
3. 操作者复核历史报告或发现文件损坏。receipt/verify 从磁盘重新核验来源、阶段、文件和账户提交；损坏时拒绝结果，不通过重跑、修补文件或编辑状态来制造成功。

P0 为完整阶段执行、固定请求边界、独立模拟账户、持久幂等、进程超时、可核验回执和失败传播。缺少任何一项，Agent 都可能把部分执行或重复写入误报为完成。P1 为两种传输入口、项目配置生成/检查和操作指引，降低各宿主自行包装造成的不一致。P2 为后续 profile、平台和部署扩展；当前接口不接收这些扩展参数。

当前不提供真实券商下单、用户真实持仓导入、策略调优/审批、自由阶段执行、远程多租户服务、分布式锁或远程签名审计。内层模型仅按下述独立 model-required 固定合同接入。通用 WorkLoom 只读能力目录继续保持其权限约束。

## 方案选择

| 方案 | 成本与优点 | 风险与适用前提 |
|---|---|---|
| 只有 CLI，调用者自行等待和整合 | 接口数量少，适合只支持 shell 的固定客户端 | 各宿主容易重复实现生命周期和完成判据；后加 MCP 可能复制业务逻辑 |
| 一个 Python 运行器，薄 CLI 和专用 MCP | 阶段、账户、幂等和验证只有一个执行实现；CLI 可独立复核 MCP 的行为 | 需要额外协议、配置和进程清理测试；宿主挂载仍需实际连接证明 |

实现采用第二种。Node 校验请求、启动可信 Python、管理观察期限和协议返回；Python 执行完整流程并持有账户状态。工具请求不能指定 Python、state-root、环境变量、provider、阶段或策略。

## 固定 profile 和版本

新的 AI 研究和官网更新默认必须显式选择 `us-public-model-required-paper-v2`；盘前/盘中分别为 `us-public-model-required-premarket-paper-v2` / `us-public-model-required-intraday-paper-v2`。三个 v2 profile 共用独立连续 paper 账户 `public-usd`、完整股票池、`reviewed_source_defaults` 和 `whitepaper-execution-v2`，不启用 active 实验。首次 seed 为 100000 USD，后续使用当前有日期的实际 paper NAV，不以固定本金代替估值。最多 10 个持仓是扩展上限，允许 0 个新买入，不强迫每日交易。旧 v1 三配置保留旧政策和验证器；不能在旧身份下改写语义或自动迁移旧待成交。

固定内层模型 `deepseek-flash`，2026-10-27 官方映射观察为 `DeepSeek-V4.1-Flash-1017`，不是不可变快照。只从可信启动环境保留四项 LLM 配置；请求不能设置模型或 key。14 个广域 Search 主题后，发行人 research 研究最多 10 个候选及全部最多 10 个持仓的并集，每目标一次初搜、按真实缺口最多一次补搜和一次已观察 URL 的 Fetch。空搜索和未知发布日期保持未知。六阶段语义完整性与计费预算分别证明，不能以发出 HTTP 或阶段标签充当模型完成。

v2 采用共享确定性执行策略：A/B/C 模板和全部数值闸门必须成立；单笔计划风险最多 NAV×0.008、单票最多 20%，在现有现金、持仓、整笔未完成计划和分组风险预约后再分配，总组合止损风险新增 3% 工程上限。40/40/20 按整数股切分；首次执行为信号后 T+1 开盘，后续分批必须有实际保存的先前完整日确认和下一开盘重查，不能用当前收盘回写当前开盘，也不摊低成本。结构止损优先于同根盈利目标；HIGH 达到 2R 后下一根生效 +0.5R 保护。第 5 日无进展或板块落后 2 个百分点退出，第 7 日硬退出；2pp 为显式工程假设，不冒充回测校准。paper、Journal 与数值回测消费同一 policy；无成交回执不生成真实交易。

当前完整账户与来源快照保留在私有 cohort 证据，用于独立重算。模型仅消费当前紧凑预算、当前选中持仓及 hash 引用，不声称读过历史账户操作。宏观/财报事实在生成 fx 哈希前移除私有覆盖文件路径，公私投影不在事后改变事实身份。公开 v3 只含白名单研究、当前运行及全部活跃机会、当前 paper 资金与成熟度测量；旧记录继续按原 schema 读取和哈希重试。

市场日期在接纳时以 `America/New_York` 冻结，保存 UTC 时间、宿主偏移和市场日期。后续计算使用该日期，不能在运行跨日时重新读取“今天”来改变记账日期。外部数据自身的日期、缺失和来源另行披露。

严格入口的 Yahoo 日线下载显式保留交易所时区（`ignore_tz=False`），先把供应商原始带时区索引和值写入证据，再由现有观察边界投影到冻结的纽约日期。韩、台与美股混合取数时不能先丢失时区，也不能事后把无时区日期统一猜成 UTC、纽约时间或直接放行次日；真正越过纽约日期的观测仍然拒绝。日线日期标签不是实时成交时间，也不证明该交易日已经收盘。普通 Yahoo 入口保留原来的默认行为。参数语义见 [yfinance 官方下载文档](https://ranaroussi.github.io/yfinance/reference/yfinance.functions.html)。

profile 的字段、允许源、阶段注册表和策略语义必须视为版本化合同。当前验证器按对应 v1/v2 policy 精确比较，并对 v2 重放全部语义请求和数值风险输入。源码变化可以作为历史来源披露；这不表示任何未来 policy 都能验证旧回执。改变阶段或 policy 时必须采用新 profile，并保留匹配的历史验证器或设计明确的账户迁移；不能在原标识下悄悄改变定义，也不能承诺尚未实现的跨版本兼容。

## 执行顺序和证据

唯一阶段定义位于 `trading_system/redline/execution.py::daily_stage_specs`，由现有 pipeline 注册表与 daily 外层闭环组成。v2 为 43 阶段，旧 v1 保留原 39 阶段；v2 实际顺序为：

```text
daily.preflight → state.reset → pipeline.prepare
→ search.collect → clean.rule_base → clean.llm_semantic → clean.cross_validate
→ data.prepare → tech.monitor → tech.cycle_linkage → tech.sentiment → tech.risk
→ tech.fusion → sector.narrative → layer1.mrs → layer2.sector → layer2b.chain
→ layer0.scan → layer3.tss → layer4.risk → research.candidates → decision.debate
→ iteration.calibration → report.emit
→ accounting.validate → report.initial → journal.log → journal.settle
→ journal.stats → paper.step → paper.persist → journal.execution_sync → governance.initialize
→ review.daily → governance.emit → governance.verify
→ opportunity.lifecycle → business.evaluate → report.final_json
→ report.plan → report.markdown → report.html → daily.result
```

开始步骤时立即保存输入快照；结束前必须保存一次输出。重复、乱序、重叠、缺输出、跨线程写证据和未完成 I/O 均违反执行合同。异常结束记录 failed，并阻止成功推进。旧模型禁用 profile，允许有明确证据的 passthrough；v2 的六个 LLM 阶段全部 `allow_passthrough=false`，不可用、空结果或透传均为失败，不能把外层聊天模型当成内层成功。

v2 在六个注册语义阶段实际调用固定 Flash。每个响应要求正确 model/id/usage/finish_reason、完整闭合 schema 与确切条目覆盖。当前清洗每批最多 8 篇，逐篇展示完整规则清洗正文：按原顺序切为最多 600 字符的 `content_parts`，拼接后与原 `source_view` 完全一致；规则清洗正文完整不等于供应商原生文章完整，原生字符数与覆盖标记另行保留。模型决定事实含义、来源归属与时效，返回本篇可引用的 `quote_part_id`；程序只按该来源的编号取回逐字引文，不模糊匹配、删改事实或自动重试。格式不允许引用的片段仍展示给模型，不能把可引用标记当成可信度判断。每批还包含全部来源的归属索引，索引不能冒充别篇正文已展示。

当前清洗请求采用 `tiger.clean-keyed-wire.v1`：完整索引保留真实 id、标题、URL、日期，以 `dN` 引用连接本批正文表；回答的 `d` 为按本批来源键闭合的对象，每篇恰好一次，各篇引文编号只取自自己的允许列表。公共语义规则在 schema 中只声明一次，再与精确来源键和各篇引文限制相交；格式示例只说明键和编号形状，不含摘要、事实或语义答案。模型回答的短字段和受控来源引用逐项逆变换，再通过原完整语义 schema 与来源覆盖校验。旧 `tiger.clean-wire.v1` 编解码保持原字节，历史记录不改写。摘要、事实、来源说明的上下界保持原值。仅此 v2 清洗阶段显式设置 `thinking.type=disabled`、`reasoning_effort=low`；其他五类语义阶段继续 enabled/high，v1 默认 request 保持 enabled/high。关闭 thinking 是本轮容量策略，不能据此声称语义质量提高或真实任务必定成功。完整字段映射、请求证明和容量边界见 [CLEAN-WIRE-POLICY-V2.md](CLEAN-WIRE-POLICY-V2.md)。

清洗结果及其私有证据保留完整逐字引文。后续舆情、风险、叙事与最终判断收到全部来源的完整摘要、事实 statement、来源及时间限定，采用显式 `columns`/`rows` 机械编码；嵌套列名使用稳定字典序，使封存 JSON 重放得到相同请求。引文文本以父文档 id 绑定的 `quote_ref`（SHA256、字符数）代替，交付说明明确本次未重读引文或正文。任何来源、事实、限定或反证都不能因改用引用而省略；跨域转载不自动当独立报道。单次请求的容量检查不能证明整轮实际用量会通过预算，完整成功仍须实际调用及结算回执。

发行人 research 另按每批最多 3 个目标、正文总量 60000 字符、每源最多 24000 字符执行；优先本轮真实 Fetch/补搜并公平分配，保存每目标跨实际轮次累计见过的最长前缀字符数、完整性和 SHA。最终 decision 从该记录重建实际前缀，只能保留发行人研究实际展示并绑定的公司引文，不能从广域 `quote_ref` 新增原文事实，也不能把 5000 字符读过的资料扩大成完整 20000 字符新闻证据。技术型充分假设可以没有公司利好，新闻事件型必须有 7 日内、发行人归属明确且完整展示的原始证据。五段理由、反证、缺口保留，无买入仍执行模型 WAIT/AVOID。

最终输入的 `tiger.final-identity-wire.v1` 只缩写明确身份字段及表列，完整真实 ID 注册表与原 payload SHA 一并展示；正文、claim_key/claim_id、引文 hash 和不同父来源不合并或替换。最终模型可完整修订 thesis，也可返回闭合的同股票 `reuse_thesis`，沿用本轮已展示的原模型研究；这种沿用通过 `model_reference_usage` 披露，不能冒充新写的研究。模型仍以 high 完成选择/否决和五个人工执行理由，展开后执行原完整 schema、研究归属、事实集合和数值买入权限校验。独立回放从上游重建原输入，不能只靠可重算的自包含 hash。

所有远端 I/O 共用先预留后调用预算。新 v2-profile 接纳显式选择 `tiger.budget-policy.v3`：chat≤40、实际加预留 tokens≤600000、Search≤60、Fetch≤20、总请求≤120、保守估算≤1 USD、业务期限 2400 秒。原默认守卫、未声明新预算的历史 policy 和 `tiger.request-budget.v2` 仍为 300000 tokens；新 proof/reservation 为 v3 并携带精确 policy_id，不能靠填写 600000 数值升级。接纳、preflight、pipeline.prepare 的完整执行策略、实际 I/O 和独立重放必须一致。600000×1.2/1000000 加 80×0.00075 的冻结保守估算为 0.78 USD，费用、次数与期限上限继续分别执行；预算扩展不证明真实完成率。计费失败照实结算，结果未知时冻结；不自动重试或循环补证。每个成功模型阶段的实际 usage、唯一响应身份与全部预算记录必须一致且无未结算预留。

独立验证器从事件和子 I/O 快照重算全部请求/schema/安全响应哈希、响应身份和 usage，重建完整模型输入文档及数值上下文，逐字段核对清洗/舆情/风险/叙事实际输出与 parsed 模型答案，并核对下游消费。最终模型选择只允许缩小原风险批准集合；人读计划中的价格/股数/止损/仓位/风险绑定原 TradePick。任何对应缺失或规则代写均为 `MODEL_REQUIRED_FAILED`，在账户指针提交之前失败。成功仅为 `completed`，不能发布 completed_degraded。原始远端响应不落盘，仅保存摘要；安全响应和模型 JSON 可重算，属于本地协作进程完整性证据，不是远端签名证明。

快照保留实际已知类型的数据，包括表格索引、列和值；非有限值用明确缺失表示，未知对象和循环引用拒绝序列化。输出不能只用数量代替内容，也不能用无约束的字符串转换掩盖类型。阶段输出包含实际子 I/O 的输入、输出或错误引用。

严格报告目前不包含白皮书 PDF 附件；「核心交易理念」明确显示未附 PDF，不生成活动下载链接，也不从源码仓、工作目录或旧报告中猜测补入附件。该边界沿用现有冻结源码、报告文件清单与回执合同，不增加未记录的附件读取。普通日报、研究报告及 JSON 转 HTML 工具只在实际 HTML 输出目录内有可读、路径安全且通过 PDF 基础结构检查的同名白皮书时显示下载，并使用实测字节数；未附或校验失败均在页面披露。基础检查覆盖 PDF 文件头、末尾 EOF 与交叉引用位置，不等于完整 PDF 内容或交易结论审核。网站导出保留页面声明的本地 PDF 及 HTML 原始字节，核验复制后的摘要；声明的附件缺失、不可读、结构无效、路径越界、含符号链接或多市场同 URL 对应不同内容时失败，不宣布站点生成成功。

`events.jsonl` 为顺序追加记录，每条绑定 run_id、序号、步骤编号、输入/输出引用、前一事件摘要和前一步输出。验证器重新计算哈希、顺序、状态与引用，并检查完整业务结果和报告。保存的 `verified=true` 标记或文件存在均不能替代这些检查。

子 I/O 还必须符合写入端的语义合同：方法名非空且不含换行；原错误快照保留 error_type、固定脱敏 message 和布尔 outcome_observed 三个字段。新的 model-required 模型 I/O 可另附闭合的安全 diagnostic，内容限固定错误码、HTTP 状态或受控 schema 路径/关键字；不保存原响应、推理内容、凭据或任意异常正文。未及时观察到结果的 false 标志可以合法记录，不能被当成字符串或数字。即使重新计算所有文件摘要和事件链，错误字段类型、未知或不合法的诊断字段及非法方法名仍会被拒绝；原有无诊断证据不追补原因。

事件日历覆盖文件是可选输入。运行接纳时将冻结源码中的覆盖文件原样复制；未提供时显式生成空覆盖层并记录 `source=builtin_only`。内置日历规则仍由冻结源码执行。`calendar-input.json` 与实际输入字节均受核验；已有文件的格式错误不能被当成“没有覆盖文件”静默忽略。当前没有自动财报日期数据源。

## 状态、账户和进程

状态目录包含短期提交锁、账户锁、幂等键映射、运行目录和账户版本目录。对外业务请求仅含 schema_version、profile、paper_account、idempotency_key 四个字段。第一次接纳持久化请求身份后，才启动独立业务进程；同键重试只能返回原身份与实际状态。

运行期间，所有业务写入发生在该 run 的工作目录。账户提交的步骤为：

1. 持有排他的账户锁，记录当前账户指针和只读输入快照。
2. 完成全部阶段，生成最终 JSON、Markdown、HTML、复盘与账本。
3. 建立包含父指针及文件摘要的新账户版本。
4. 封存回执，独立核验阶段、文件、来源、日期、金额和父指针。
5. 原子替换 `accounts/public-usd/current.json`；这是账户提交的权威位置。

替换前的失败不会使该运行成为已提交账户；替换后的状态写入失败也不能抹去已发生的提交。只读查询沿当前指针和父版本链证明历史提交，不依赖可变状态文件。多个普通 JSON 文件写入并非数据库事务；实现采用准备文件和原子发布指针，并披露目录同步失败或缺少同步确认的情况。

进程分为提交/查询进程、supervisor、guardian 和 engine。业务进程与客户端观察进程分离；断连或取消观察不会取消已接纳任务。supervisor 负责截止时间、清理与账户发布；guardian 独立监测 supervisor 的生命通道，并约束 engine 所属进程组。清理在回收保留的组首进程前执行，不能仅凭 PID 文件推断任务是否活跃。

控制管道关闭也不等于 guardian 已退出。supervisor 先验证完整成功控制记录，再在原业务期限内观察真实退出：Linux 使用保留子进程待回收状态的 WNOWAIT；macOS 读取自有组首进程的 PID、父 PID、组 ID 和僵尸状态。身份不符、查询失败或到期均不能作为成功。观察到退出后仍先清理进程组，最后回收并要求真实退出码为零，才允许发布账户。

状态查询先采样运行锁，再读取状态文件和账户提交链。写者完成最后的状态写入后才释放运行锁，同一个 run 不会重新启动。若采样时锁仍被持有，查询可以保守地多返回一次 running；不得把完成前的状态/指针快照与完成后的无锁状态拼成虚假的 interrupted。已发布的账户提交仍优先于可变状态文件。

当前支持 macOS/Linux 所需的 POSIX 锁和目录写入语义，Python 最低 3.11。运行环境按允许列表构造，保留原 HOME；仅 model-required v1/v2 的业务启动额外保留 LLM_BACKEND/LLM_BASE_URL/LLM_MODEL/LLM_API_KEY 四项，禁止通过请求覆写，其他 profile 不继承模型/provider 凭据；业务缓存和临时文件在 run 内。每次新业务进程在首次使用 yfinance 前设置实际缓存位置。第三方包身份记录解释器字节和安装元数据，未声称逐文件证明全部依赖供应链。

## 时间、并发和错误

单个业务任务的总期限为 2400 秒。Node 单次提交/查询观察上限为 60 秒，CLI 默认等待窗口为 2460 秒；观察超时和业务超时是两个不同结果。

原两个 profile 的公开搜索 45 秒收集预算适用于每一批查询；主题会展开为多批查询，不能把它描述为整个搜索阶段只花 45 秒。收集期限之后的数据不再参与决策，已启动线程另有最多 15 秒的收尾等待；未结束的线程阻止流程成功。model-required 使用同步、有界响应的原生 Search/Fetch，每次请求连接/读取期限和 1 MiB 响应上限由适配器固定；无跨模型或来源回退，任一必需主题失败即停，最外层 2400 秒业务期限仍然生效。

模型传输连接期限为 10 秒，原生 Search/Fetch 的读取空闲期限为 60 秒；非流式 Chat 允许在原有 180 秒响应接纳窗口内等待。响应正文的空行保活不会延长这个窗口：适配器使用单调时钟检查，并在剩余期限到点时中断已持有响应的阻塞读取；清理时取消并等待该中断器退出，再关闭响应。底层 urllib3 超时、协议中断及 TLS 错误分别映射为固定安全错误码，不保留原始异常文本，不自动重试；用量未知仍冻结原预留。该正文期限不等于 DNS 解析或慢速响应头的全局硬墙钟保证，外层业务进程期限继续约束整个任务。依据：[DeepSeek 保活说明](https://api-docs.deepseek.com/quick_start/rate_limit/)、[urllib3 Timeout 语义](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Timeout)。

一个 state-root 的模拟账户同时只允许一个业务写者，短期提交锁负责键映射和锁交接。专用 MCP 最多允许四个在途请求；多数为只读观察，这不会放宽账户写互斥。

所有外部调用均需区分普通来源不可用和运行合同/证据失败。普通公开源失败按原有真实源回退规则处理并披露；不能切换合成数据。证据写入、日期边界、阶段合同、账户状态或回执校验失败必须终止严格任务，不允许转为合法 LLM 透传。

严格工作流的 Stooq 串行日线批次采用每批连续三次无有效产出的可用性探测预算；观察到有效 DataFrame 即重置连续计数，已取得的原始帧保留。预算耗尽后，未尝试的 ticker 交给原有可信真实源回退链，不降低覆盖率、新鲜度或数据质量门槛。少数普通缺失不能证明远端服务失败，所以证据只声明 `source_exhausted=true`、`reason=availability_budget_exhausted`、`service_failure_asserted=false`，列明尝试数、无产出数、按类型汇总的错误及未尝试清单；不序列化原始错误正文。批次恰好结束或空输入不会伪报预算耗尽。普通非严格 Stooq 调用默认仍无此探测数量上限。

`EvidenceWriteError`、`ExecutionContractError` 和 `StrictObservationError` 必须原样终止，不能作为无产出累计后继续回退。严格模式保持 `allow_challenge=false`，遇到访问验证仍拒绝，不求解或绕过挑战。三次是探测次数边界，不保证整个公开取数阶段的墙钟上限；各原有来源仍有自己的网络等待，最外层2400秒截止时间继续有效。该修复只避免一个连续无产出的串行来源先消耗数百次请求，不能保证公开源一定提供足够数据。

错误返回统一 envelope 和受控 code/message/run_id/stage/details_ref；不把原始 HTTP 正文、密钥或完整环境返回给 Agent。详细退出码见操作指南。

## 验证、发布和恢复

新增 Python 测试验证真实本机进程、锁、幂等、截止时间、进程组清理、输入输出证据、账本版本和故障注入；Node 测试验证协议、模式、取消、流边界和配置保存。离线夹具明确标识为 TEST_FIXTURE，不能替代公开行情实跑。测试结果由各次执行日志、源摘要和独立验收回执提供，不在本文维护容易漂移的通过数量。

云端现行 `workloom-ui-contract` 保留全部原有检查，并在已安装 Python 依赖之后执行专用 Node 套件。第一 main 的 MC-041 加载时序故障仅修测试就绪判据；原始完整内容、顺序和几何断言继续存在，详见 [TIGER-CLIENT-REPAIRS.md](TIGER-CLIENT-REPAIRS.md)。

发布需普通 PR、准确候选 CI、会话审查与独立验收、普通 main 合并及读回。实际公开数据执行另记录源提交、冻结文件树、run_id、市场日期、模型缺失和 receipt/verify。配置解析和原始 MCP 通信不证明某个宿主已挂载；Codex 项目信任仍由正常客户端机制处理。

回退代码使用普通版本控制流程。失败任务和已提交账户证据保留；不得自动删除幂等键、回退账户指针或通过新键隐藏失败。需要新的逻辑任务时，明确记录修复原因和新任务身份。若代码回退涉及不同 policy 语义，先使用匹配验证器检查历史账户兼容性。

这些保证适用于同一用户下协作运行的进程。拥有任意源码、证据及 shell 写权限的主体仍能绕开本入口；本地哈希链不能替代权限隔离、远端签名或不可回滚存储。实现也不保证同时恶意终止所有监护进程、任意外部进程并发修改或所有主机断电故障下的恢复。
