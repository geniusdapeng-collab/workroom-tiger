"""市场抽象基类（v6.3 S4）— "框架不变、输入替换"的市场规格（MarketSpec）。

每个市场声明：
  - 身份：market_id / name / timezone / currency / 结算规则（T+0/T+1）
  - 交易日历：is_trading_day / next_trading_day / prev_trading_day（规则实现：
    周末 + config 内置节假日表；HK 另有半日市标记）
  - 交易规则：涨跌停（US 无个股涨跌停但有 LULD 熔断标记；CN ±10%/±20%/ST ±5%；
    HK 无涨跌停但有 VCM 冷静期标的集合）、最小价位
  - 基准映射：benchmarks（指数/利率/波动率符号，MRS 输入替换的唯一出口）
  - 合规校验：check_order → ComplianceVerdict（围栏前置，供 L4 与未来围栏层消费）

所有参数（节假日表/阈值/标的集合）均以 trading_system/config.py 为
single source of truth（D3），本包只做规则计算，不持有可调参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .. import config


def _d(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    return d


def _parse_dates(strs) -> frozenset[date]:
    return frozenset(datetime.strptime(s, "%Y-%m-%d").date() for s in strs)


@dataclass(frozen=True)
class ComplianceVerdict:
    """合规校验结果（围栏前置的标准产出，写入日报披露）。"""
    allowed: bool
    reason: str = ""
    rule_id: str = ""

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "rule_id": self.rule_id}


class MarketSpec:
    """市场规格基类。子类在 us.py / cn.py / hk.py 中实例化规则差异。"""

    market_id: str = ""
    name: str = ""
    short_name: str = ""             # 日报标题用短名（美股/A股/港股）
    timezone: str = ""
    currency: str = ""
    settlement: str = "T+0"          # 结算规则（T+0 / T+1）
    limit_note: str = ""             # 涨跌停/熔断规则披露文本

    # ---- 日历（子类提供节假日表）----
    holidays: frozenset[date] = frozenset()
    half_days: frozenset[date] = frozenset()

    # ---------------------------------------------------------------- 身份/映射

    @property
    def benchmarks(self) -> dict:
        """MRS 基准组：index/rate/vol/vol_short 符号与展示名（config 单一口径）。"""
        return config.MARKET_BENCHMARKS[self.market_id]

    @property
    def sector_symbols(self) -> list[str]:
        return list(config.MARKET_SECTOR_SYMBOLS[self.market_id])

    @property
    def benchmark_hard_fail(self) -> bool:
        return config.MARKET_BENCHMARK_HARD_FAIL[self.market_id]

    @property
    def sector_hard_fail(self) -> bool:
        return config.MARKET_SECTOR_HARD_FAIL[self.market_id]

    def scan_filters(self) -> dict:
        """扫描硬过滤参数（US 回退全局 SCAN_* 常量，保证现状行为不变）。"""
        f = config.MARKET_SCAN_FILTERS.get(self.market_id)
        if f:
            return dict(f)
        return {"min_price": config.SCAN_MIN_PRICE,
                "min_adv": config.SCAN_MIN_ADV_USD, "currency": "USD"}

    def graduation(self) -> dict:
        """轻仓通道毕业状态（D7）：满 MARKET_GRAD_MIN_SETTLED 笔结算且 DSR 过关。"""
        g = config.MARKET_GRADUATION.get(self.market_id,
                                         {"settled_trades": 0, "dsr_pass": False})
        settled = int(g.get("settled_trades", 0))
        graduated = (settled >= config.MARKET_GRAD_MIN_SETTLED
                     and bool(g.get("dsr_pass", False)))
        return {"graduated": graduated, "settled": settled,
                "required": config.MARKET_GRAD_MIN_SETTLED,
                "dsr_pass": bool(g.get("dsr_pass", False))}

    # ---------------------------------------------------------------- 日历

    def is_trading_day(self, d) -> bool:
        d = _d(d)
        return d.weekday() < 5 and d not in self.holidays

    def is_half_day(self, d) -> bool:
        """半日市标记（HK：仅上午交易；其他市场恒 False）。"""
        return _d(d) in self.half_days

    def next_trading_day(self, d) -> date:
        d = _d(d) + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def prev_trading_day(self, d) -> date:
        d = _d(d)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    # ---------------------------------------------------------------- 交易规则

    def price_limit_pct(self, ticker: str, name: str = "") -> float | None:
        """个股涨跌停幅度（None = 无个股涨跌停）。基类：无。"""
        return None

    def limit_prices(self, ticker: str, prev_close: float,
                     name: str = "") -> tuple[float, float] | None:
        """(涨停价, 跌停价)；无涨跌停规则返回 None。"""
        pct = self.price_limit_pct(ticker, name)
        if pct is None or prev_close <= 0:
            return None
        return (round(prev_close * (1 + pct), 2), round(prev_close * (1 - pct), 2))

    def min_tick(self, price: float) -> float:
        """最小价位。基类 0.01（US/CN）；HK 按价位档覆盖。"""
        return 0.01

    def in_vcm(self, ticker: str) -> bool:
        """是否 VCM（市调机制）冷静期适用标的（仅 HK）。"""
        return False

    # ---------------------------------------------------------------- 合规校验

    def check_order(self, side: str, ticker: str, price: float,
                    prev_close: float, trade_date, buy_date=None,
                    name: str = "", vcm_cooling: bool = False) -> ComplianceVerdict:
        """订单合规校验（围栏前置）。基类：无额外限制，放行。"""
        return ComplianceVerdict(True, f"{self.market_id.upper()} 无个股级围栏限制",
                                 f"{self.market_id.upper()}_OK")

    def compliance_rules(self) -> list[str]:
        """本市场启用的合规规则清单（日报披露用）。"""
        return [f"{self.market_id.upper()}_OK"]

    def to_dict(self) -> dict:
        g = self.graduation()
        return {
            "market_id": self.market_id, "name": self.name,
            "short_name": self.short_name,
            "timezone": self.timezone, "currency": self.currency,
            "settlement": self.settlement, "limit_note": self.limit_note,
            "benchmarks": {k: v for k, v in self.benchmarks.items()},
            "sector_symbols": self.sector_symbols,
            "scan_filters": self.scan_filters(),
            "graduation": g,
            "compliance_rules": self.compliance_rules(),
        }
