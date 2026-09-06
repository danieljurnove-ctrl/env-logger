"""Tests for the dashboard's JavaScript, run through node.

The dashboard is deliberately one self-contained HTML file with no build step
(see docs/design.md#dashboard), so there is no module to import and no bundler
to hook into. This extracts the one function whose behaviour is subtle enough to
regress silently -- gap detection -- and exercises it in node.

Skipped when node is absent, so a Pi without it still runs the rest of the suite.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")

# Extraction markers. If either moves, this fails loudly rather than silently
# testing nothing -- which is the behaviour you want from a scraping test.
START = "function bucketSeconds()"
END = "/* The null gap between placements is one bucket wide"

HARNESS = """
const cols = ["ts", "co2_ppm"];
const seg = rows => ({ rows });
const nulls = a => a.filter(v => v === null).length;
let failures = 0;
function check(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.log(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
    failures++;
  }
}

// 6h uses 60s buckets against a 45s cadence, so an empty bucket is routine and
// must NOT break the line. This is the case that makes a tight threshold wrong.
range = { bucket: "60" };
check("6h one empty bucket", nulls(toData([seg([[0,500],[120,510],[180,520]])], ["co2_ppm"], cols)[1]), 0);
check("6h two empty buckets", nulls(toData([seg([[0,500],[180,510]])], ["co2_ppm"], cols)[1]), 0);
check("6h three empty buckets", nulls(toData([seg([[0,500],[240,510]])], ["co2_ppm"], cols)[1]), 1);
check("6h long outage breaks once", nulls(toData([seg([[0,500],[6000,510]])], ["co2_ppm"], cols)[1]), 1);

range = { bucket: "300" };
check("24h contiguous", nulls(toData([seg([[0,500],[300,510],[600,520]])], ["co2_ppm"], cols)[1]), 0);
check("24h outage", nulls(toData([seg([[0,612],[6000,550],[6300,552]])], ["co2_ppm"], cols)[1]), 1);
check("move still breaks", nulls(toData([seg([[0,500]]), seg([[300,510]])], ["co2_ppm"], cols)[1]), 1);
check("single sample", nulls(toData([seg([[0,500]])], ["co2_ppm"], cols)[1]), 0);

// uPlot requires a strictly increasing x. Inserting synthetic points is exactly
// where that invariant gets broken.
const xs = toData([seg([[0,500],[6000,510]]), seg([[9000,520]])], ["co2_ppm"], cols)[0];
check("x strictly increasing", xs.every((v, i) => i === 0 || v > xs[i - 1]), true);

range = { bucket: "day" };
check("1y consecutive days", nulls(toData([seg([[0,500],[86400,510]])], ["co2_ppm"], cols)[1]), 0);
check("1y multi-day gap", nulls(toData([seg([[0,500],[432000,510]])], ["co2_ppm"], cols)[1]), 1);

// A slow sensor shares its buckets with fast ones, so most rows hold null for
// it. Those nulls are its cadence, not an outage: left in, every sample is
// isolated between two gaps and the series draws as dots with no line.
const pmCols = ["ts", "pm2_5_atm", "co2_ppm"];
// PM every 300s, CO2 every 60s, on a 60s-bucket range.
const mixed = [];
for (let t = 0; t <= 1200; t += 60) mixed.push([t, t % 300 === 0 ? 5 : null, 400]);
range = { bucket: "60" };
const pmData = toData([seg(mixed)], ["pm2_5_atm"], pmCols);
check("slow sensor draws a line, not dots", nulls(pmData[1]), 0);
check("slow sensor keeps only its own points", pmData[0].length, 5);
// The fast metric on the same rows is unaffected.
check("fast sensor keeps every point", toData([seg(mixed)], ["co2_ppm"], pmCols)[0].length, 21);
// A real outage still breaks it: 300s cadence, so >1050s of nothing.
const pmGap = [[0,5],[300,5],[600,5],[2400,5],[2700,5]].map(r => [r[0], r[1], 400]);
check("slow sensor still breaks on a real outage",
      nulls(toData([seg(pmGap)], ["pm2_5_atm"], pmCols)[1]), 1);

process.exit(failures);
"""


def _extract() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        src = fh.read()
    assert START in src, f"marker {START!r} not found in index.html"
    assert END in src, f"marker {END!r} not found in index.html"
    return src[src.index(START) : src.index(END)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_charts_break_on_outages_but_tolerate_bucket_jitter(tmp_path):
    """A run of empty buckets breaks the line; ordinary jitter does not.

    Drawing a straight line across an outage asserts the room did nothing in
    between -- the same lie as drawing a slope across a move.
    """
    script = tmp_path / "todata.js"
    script.write_text("let range;\n" + _extract() + textwrap.dedent(HARNESS), encoding="utf-8")
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_inline_script_parses():
    """The dashboard is one file with no build step, so nothing else would catch
    a syntax error in it -- the page would simply load blank on the Pi.

    `node --check` parses without executing, so uPlot and the DOM being absent
    here does not matter.
    """
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    with open(INDEX, encoding="utf-8") as fh:
        html = fh.read()
    blocks = re.findall(r"<script>\n(.*?)\n</script>", html, re.S)
    assert len(blocks) == 1, f"expected one inline script, found {len(blocks)}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(blocks[0])
        path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    finally:
        os.unlink(path)
