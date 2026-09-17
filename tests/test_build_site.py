"""多市场官网生成器测试（v3.4）：组合总览 + 缺数据诚实披露。"""
import json
import subprocess
from pathlib import Path


def _mk_market(d: Path, action="HOLD", mrs=5.7, equity=100000.0, with_html=True):
    d.mkdir(parents=True, exist_ok=True)
    (d / "result_20260830.json").write_text(json.dumps({
        "trade_date": "2026-08-30", "action": action,
        "mrs": {"mrs_star": mrs, "position_cap": [0.1, 0.25]}, "picks": [{}],
        "raw": {}}))
    (d / "sim_portfolio.json").write_text(json.dumps({
        "cash": equity, "positions": [], "pending": [],
        "equity_curve": [{"date": "2026-08-30", "equity": equity}]}))
    if with_html:
        (d / "日报_20260830.html").write_text("<html>日报</html>")


def test_portal_renders_three_markets(tmp_path):
    for mid in ("us", "cn", "hk"):
        _mk_market(tmp_path / mid)
    out = tmp_path / "site"
    r = subprocess.run(["python3", "scripts/build_site.py",
                        "--us", str(tmp_path / "us"), "--cn", str(tmp_path / "cn"),
                        "--hk", str(tmp_path / "hk"), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "组合总览" in html and "HOLD" in html
    assert "us.html" in html and "cn.html" in html and "hk.html" in html
    assert "300,000" in html or "300000" in html.replace(",", "")
    for mid in ("us", "cn", "hk"):
        assert (out / f"{mid}.html").exists()


def test_missing_market_honest_na(tmp_path):
    """缺数据市场必须如实标注，绝不编造（D2 延伸到组合层）。"""
    _mk_market(tmp_path / "us")
    (tmp_path / "cn").mkdir(parents=True)   # 空目录 = 当日无有效数据
    _mk_market(tmp_path / "hk")
    out = tmp_path / "site"
    r = subprocess.run(["python3", "scripts/build_site.py",
                        "--us", str(tmp_path / "us"), "--cn", str(tmp_path / "cn"),
                        "--hk", str(tmp_path / "hk"), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "当日无有效数据" in html
    assert "诚实失败" in html
    cn_page = (out / "cn.html").read_text(encoding="utf-8")
    assert "当日无有效日报" in cn_page
