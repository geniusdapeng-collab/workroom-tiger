"""Industry-chain prose must describe the rows in the actual rendered report."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from html.parser import HTMLParser
import re

import pytest

from trading_system.data_models import ChainState, PipelineResult
from trading_system.report_html import render_html


class _ChainSectionParser(HTMLParser):
    """Read the industry-chain section and its table from the generated HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.in_section = False
        self.sections = 0
        self.text = []
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self.in_section = False
            self.heading = []
        elif self.in_section:
            if tag == "tr":
                self.row = []
            elif tag in ("th", "td"):
                self.cell = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.in_section:
            self.text.append(data)
            if self.cell is not None:
                self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "h2" and self.heading is not None:
            self.in_section = "".join(self.heading) == "三、产业链景气周期"
            self.sections += int(self.in_section)
            self.heading = None
        elif self.in_section:
            if tag in ("th", "td") and self.cell is not None:
                self.row.append("".join(self.cell))
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.rows.append(self.row)
                self.row = None


@pytest.mark.parametrize("indices, expected_count", [
    pytest.param((), 0, id="empty"),
    pytest.param((6,), 1, id="one"),
    pytest.param(tuple(range(9)), 9, id="nine"),
    pytest.param(tuple(range(10)), 10, id="ten"),
    pytest.param((8, 2, 5), 3, id="filtered-subset-preserves-order"),
])
def test_chain_count_matches_rendered_rows(indices, expected_count):
    # Ten distinct candidates let a filtered result differ from its source pool.
    # Scores deliberately differ from input order; names also exercise escaping.
    pool = [ChainState(
        chain_id=f"fixture-{i}", name=f"产业链 {i} & <组件 {i}>",
        ics=(i * 3) % 10 + 0.25, stage=("复苏", "扩张", "过热", "衰退")[i % 4],
        leading_link=("upstream", "midstream", "downstream")[i % 3],
        breadth=50.0, rotation_signal=f"轮动 {i} & <观察>", hot=i % 2 == 0,
    ) for i in range(10)]
    chains = [pool[i] for i in indices]
    result = PipelineResult("2026-10-20", "fixture", chains=chains)
    before = deepcopy(asdict(result))

    section = _ChainSectionParser()
    section.feed(render_html(result))
    section.close()

    assert asdict(result) == before, "Rendering must not modify report inputs"
    if expected_count == 0:
        assert section.sections == 0
        assert section.rows == []
        assert section.text == []
        return

    assert section.sections == 1
    assert section.rows[0] == ["产业链", "景气", "阶段", "领涨环节", "轮动", "热区"]
    data_rows = section.rows[1:]
    assert len(data_rows) == expected_count
    leading_labels = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
    assert data_rows == [
        [chain.name, str(chain.ics), chain.stage, leading_labels[chain.leading_link],
         chain.rotation_signal, "🔥" if chain.hot else ""]
        for chain in chains
    ]
    displayed_counts = re.findall(
        r"下表为(\d+)条产业链当前所处的阶段与领涨环节。", "".join(section.text))
    assert displayed_counts == [str(expected_count)]
