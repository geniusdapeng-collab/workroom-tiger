# Tiger 源码合入与原生客户端构建

源码 PR 执行 `py-gate`、`oss-gate`、`protocol-gate`。这些检查验证交易内核、开源清单、三端消费契约、提交与并发锁、治理、媒体解码和 CLI 回归；普通合并仍须满足 CNB 的必需状态检查与真实评审。

原生候选使用同一份 `.cnb.yml` 中的 `api_trigger_tiger_native_candidate` 事件。触发时选择待验证的 commit，两条流水线分别在组织的 `[mac, arm64]` 和 `[windows]` runner 上执行现有原生构建脚本和最终应用 smoke。事件中的两平台任务均为必需任务，没有 `allow_failure`。配置与事件格式见 [CNB 触发规则](https://docs.cnb.cool/zh/build/trigger-rule.html) 和 [构建节点说明](https://docs.cnb.cool/zh/build/build-node.html)。

`main` 的 push 继续执行两平台正式构建。正式包需要行业 Ed25519 信任、平台证书、Mac 公证、载荷完整性、正确产品身份以及最终安装资源和运行进程核验。原生 runner 或凭据缺失时构建失败；源码合入结果不能代替客户端交付证明。

2026-10-04 的发布调整依据产品所有者的明确指令：已实现的代码先提交、合入，未闭环的问题保留后续处理。当时构建 `cnb-b4k-1k4238mni` 的两个原生候选在 Prepare 阶段分别报告没有匹配的 Mac/arm64 和 Windows runner，没有实际启动构建。后续必须配置对应 runner，并在要交付的同一 commit 上重新执行候选、签名正式构建和真机验收。

当前 MCD 独立断言、服务测试活库执行、Windows 真机与平台签名等状态按各自真实回执记录。skip、静态映射和作者回归均不能被记录为独立通过。发布优先安排不会关闭这些验收债务。
