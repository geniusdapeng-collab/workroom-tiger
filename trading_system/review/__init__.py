"""复盘团队包（v6.4 S6）— 数字负责人：诸葛。

四个角色对齐 research/UPGRADE_PLAN_v3.md §3.4：
  attribution.py  归因分析师：日度逐笔归因 + 违规六条自动检测（附录D 口径）
  weekly.py       统计员：周度统计体检（三档结论）+ 校准层样本状态联动
  monthly.py      策略优化师：月度 WFA 参数提案（只提案不生效；DSR 不显著
                  自动 reject——D7 迭代诚实的机器执行）
  chief.py        诸葛（复盘主持）：编排日/周/月三个频率，产出复盘纪要
                  reports/复盘_<日期>.md，纪要进治理五元事件；审批流
                  （approve/reject 三手势对齐）也在此落账。

红线（D5 零基线 + 附录D）：
  - 复盘产物（纪要/提案/审批记录）属会计账白名单，只进报告层与治理事件，
    绝不进入决策输入——本包【禁止】import gate/agents/pipeline/simulator
    决策链路，pytest 静态审查锁定；
  - 只复盘流程不复盘运气：归因只基于已落账数据与当日快照，不重构决策；
  - 提案绝不自动生效：approve 后复用 --use-tuned 纪律，次日生效并披露。
"""

from . import attribution, chief, monthly, weekly  # noqa: F401
