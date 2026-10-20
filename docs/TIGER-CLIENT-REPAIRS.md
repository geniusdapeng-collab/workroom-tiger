# Tiger 客户端修复登记

本次任务 `T-2026-1020-0901` 按产品所有者的当前请求修复并合并指定的 GitHub 仓库。MC-012 的 StarRing 续跑／留言修复、MC-018 的 C 端聊天参数类型修复，以及 MC-015 的消费端浏览器测试修复涉及三个受管客户端文件，因此原始基座校验器会报告指纹差异。新增的组件回归测试也不属于原始快照。MC-015 将登录标题的硬编码 WorkLoom 改为受控产品清单的实际名称，保留精确文本匹配、登录表单交互、首帧与长文本尺寸断言；三端浏览器用例实际执行通过。

`governance/.workloom-client-foundation.json` 和原始 `verify-client-foundation-consumer.mjs` 保留审计基线的字节内容。`governance/.workloom-client-repairs.json` 单独登记三个文件的原始摘要、修复后摘要、修复提交、问题卡以及唯一新增测试的摘要；它没有声称这些改动已经发布为新的上游 UI 版本。

从仓库根目录运行：

```bash
node scripts/ci/verify-tiger-client-repairs.mjs
node --test tests/client-repair-provenance.test.mjs
```

校验器先执行原基座的完整检查，再核验本任务登记。只有三个精确路径的摘要差异和已绑定的组件测试可以被接受。其他文件差异、缺失文件、执行模式、版本、非法路径、符号链接及新增运行时代码仍然阻断。登记的基座 state 和校验器摘要也必须相符。原摘要不能被直接替换为修复摘要，测试文件不能省略。

这是一份本仓修复来源记录，授权事实来自当前用户请求与该任务的代码审查、实测；JSON 字段不构成身份认证或额外业务授权。它不允许实盘、真实资金或客户外发，也不改变舰队登记、实验车道或基座分发策略。适用范围仅为 [issue 24](https://github.com/geniusdapeng-collab/workroom-tiger/issues/24)／[PR 25](https://github.com/geniusdapeng-collab/workroom-tiger/pull/25) 的这三项客户端修复。

后续修改任何已登记文件都需要重新审查、实际回归和更新精确摘要。基座升级会使旧版本／state 摘要绑定失效，届时必须检查新版本是否已包含修复，删除已合入上游的登记或重新评审剩余差异。不得扩大通配路径、伪造上游版本，或把本仓修复登记当作跨仓同步源。
