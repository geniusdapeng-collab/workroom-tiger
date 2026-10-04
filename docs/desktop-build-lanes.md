# Tiger 源码合入与原生客户端构建

源码 PR 执行 `py-gate`、`oss-gate`、`protocol-gate`。这些检查验证交易内核、开源清单、三端消费契约、提交与并发锁、治理、媒体解码和 CLI 回归；普通合并仍须满足 CNB 的必需状态检查与真实评审。

`py-gate` 显式设置 `RUN_TIGER_LIVE_MACRO_TESTS=0`，运行完整 pytest 并用 `-rs` 显示可选真实宏观源 smoke 的跳过原因。`tests/test_provider_official.py` 的解析、职责边界和异常单测继续使用 mock；收集测试时不会调用官方源的网络可用性探测。源码门禁的通过记录只证明已执行的源码回归。

真实宏观源通过独立事件 `api_trigger_tiger_live_macro` 检查。触发时选择待验证的 commit，`tiger-live-macro` 流水线安装同一份 Python 依赖，显式设置 `RUN_TIGER_LIVE_MACRO_TESTS=1`，只运行原始 `test_smoke_real_fred_cboe`。FRED DGS10 和 CBOE VIX 都必须取得至少 30 个有效数据点；原有 20 秒请求超时、CSV 解析与数据不足时抛错的行为保留。该任务没有 `allow_failure` 或条件旁路；启用后的请求超时、HTTP 错误和短序列都会使流水线失败。事件与版本选择见 [CNB 触发规则](https://docs.cnb.cool/zh/build/trigger-rule.html)，跳过原因的输出方式见 [pytest skipif 与 -rs](https://docs.pytest.org/en/stable/how-to/skipping.html)，请求超时语义见 [Requests timeouts](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)。

本机需要真实数据连通性检查时执行 `RUN_TIGER_LIVE_MACRO_TESTS=1 python -m pytest -q -rs tests/test_provider_official.py::test_smoke_real_fred_cboe`。源码门禁的 skip、自有 mock 夹具的回归和独立联网流水线的成功记录各自保留证据；真实源连通性以该 commit 的联网运行回执为准。

2026-10-04，[PR #80](https://cnb.cool/workloom-ai/workroom-tiger/-/pulls/80) 的构建 `cnb-aog-1k4253tfb` 在交易内核测试中记录 815 个通过、1 个失败：可选 smoke 的 FRED 探测通过后，CBOE `VIX_History.csv` 请求发生 `ConnectTimeout`（20 秒）。本次把该检查转入显式联网事件；原失败记录保留，尚未取得新联网事件通过的回执。

原生候选使用同一份 `.cnb.yml` 中的 `api_trigger_tiger_native_candidate` 事件。触发时选择待验证的 commit，两条流水线分别在组织的 `[mac, arm64]` 和 `[windows]` runner 上执行现有原生构建脚本和最终应用 smoke。事件中的两平台任务均为必需任务，没有 `allow_failure`。配置与事件格式见 [CNB 触发规则](https://docs.cnb.cool/zh/build/trigger-rule.html) 和 [构建节点说明](https://docs.cnb.cool/zh/build/build-node.html)。

`main` 的 push 继续执行两平台正式构建。正式包需要行业 Ed25519 信任、平台证书、Mac 公证、载荷完整性、正确产品身份以及最终安装资源和运行进程核验。原生 runner 或凭据缺失时构建失败；源码合入结果不能代替客户端交付证明。

2026-10-04 的发布调整依据产品所有者的明确指令：已实现的代码先提交、合入，未闭环的问题保留后续处理。当时构建 `cnb-b4k-1k4238mni` 的两个原生候选在 Prepare 阶段分别报告没有匹配的 Mac/arm64 和 Windows runner，没有实际启动构建。后续必须配置对应 runner，并在要交付的同一 commit 上重新执行候选、签名正式构建和真机验收。

当前 MCD 独立断言、服务测试活库执行、Windows 真机与平台签名等状态按各自真实回执记录。skip、静态映射和作者回归均不能被记录为独立通过。发布优先安排不会关闭这些验收债务。
