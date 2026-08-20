"""The page's rate loop against the Python engine, with zero tolerance.

`app.js` cannot import the pure core. It therefore has a second, small rate sweep plus a
break-even bisection for the live sliders. Under ADR 0008 that duplication is acceptable only
because this test evaluates the real JavaScript with node and compares its integer-cent output
against `analyzer.core.sensitivity` exactly. A cent of drift is not display noise: both sides
round positive money half-up into cents before returning it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from analyzer.core.profile import load_profile
from analyzer.core.sensitivity import BaselineRate, rate_band, wait_vs_buy
from analyzer.core import tax

REPO = Path(__file__).resolve().parents[1]
HARNESS = r"""
global.DATA = {global:{tax:{prop13_rate:__TAX_RATE__,
  homeowners_exemption:__HOMEOWNERS_EXEMPTION__},insurance:{annual:__INS__}}};
const src = require('fs').readFileSync(__APPJS__,'utf8');
eval(src.match(/function pmt\([\s\S]*?\n}\n/)[0]);
eval(src.match(/function pitiParts\([\s\S]*?\n}\n/)[0]);
eval(src.match(/function sensitivityBand\([\s\S]*?\n}\n/)[0]);
eval(src.match(/function priceAtPitiCents\([\s\S]*?\n}\n/)[0]);
eval(src.match(/function waitVsBuy\([\s\S]*?\n}\n/)[0]);
console.log(JSON.stringify({
  band: sensitivityBand(__PRICE__, __DOWN__, 500, 750, 25, __HOA__),
  wait: waitVsBuy(__PRICE__, __DOWN__, __BASE_RATE__, __FUTURE_RATE__, __FUTURE_PRICE__, __HOA__)
}));
"""

CASES = [
    (950_000.0, 190_000.0, 0.0, 0.0667, 0.0645, 975_000.0),
    (900_000.0, 190_000.0, 125.0, 0.06, 0.0725, 875_000.0),
    (950_000.0, 950_000.0, 0.0, 0.05, 0.075, 950_000.0),  # cash purchase: rate cannot invent P&I
]


def js_result(price, down, hoa, baseline_rate, future_rate, future_price):
    node = shutil.which("node")
    if not node:
        if os.environ.get("HBA_REQUIRE_NODE"):
            pytest.fail("HBA_REQUIRE_NODE is set but node is not installed; page parity would have been skipped")
        pytest.skip("node not installed; page parity unchecked in this environment")
    profile = load_profile()
    script = (
        HARNESS.replace("__TAX_RATE__", str(tax.get_schedule(profile.tax_rate_area).total_rate()))
        .replace("__HOMEOWNERS_EXEMPTION__", str(tax.HOMEOWNERS_EXEMPTION))
        .replace("__INS__", str(profile.annual_insurance))
        .replace("__APPJS__", json.dumps(str(REPO / "app.js")))
        .replace("__PRICE__", repr(price))
        .replace("__DOWN__", repr(down))
        .replace("__HOA__", repr(hoa))
        .replace("__BASE_RATE__", repr(baseline_rate))
        .replace("__FUTURE_RATE__", repr(future_rate))
        .replace("__FUTURE_PRICE__", repr(future_price))
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, cwd=REPO)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(("price", "down", "hoa", "baseline_rate", "future_rate", "future_price"), CASES)
def test_every_browser_band_point_matches_python_to_the_cent(
    price, down, hoa, baseline_rate, future_rate, future_price
):
    profile = load_profile()
    # This test inputs down payment as a slider value, so make the Python household match it
    # rather than pretending the profile's $80K was used in every browser case.
    profile = replace(profile, down_payment=down)
    js = js_result(price, down, hoa, baseline_rate, future_rate, future_price)
    py = rate_band(
        profile,
        price,
        baseline=BaselineRate(baseline_rate, "test slider"),
        hoa_monthly=hoa,
        current_year=2026,
    )

    assert len(js["band"]) == len(py.points)
    for browser, engine in zip(js["band"], py.points):
        assert browser["rateBps"] == round(engine.annual_rate * 10_000)
        assert browser["piCents"] == round(engine.principal_interest * 100)
        assert browser["pitiCents"] == round(engine.piti * 100)
        assert browser["totalInterestCents"] == round(engine.total_interest * 100)


@pytest.mark.parametrize(("price", "down", "hoa", "baseline_rate", "future_rate", "future_price"), CASES)
def test_browser_wait_vs_buy_matches_python_to_the_cent(
    price, down, hoa, baseline_rate, future_rate, future_price
):
    profile = load_profile()
    profile = replace(profile, down_payment=down)
    js = js_result(price, down, hoa, baseline_rate, future_rate, future_price)["wait"]
    py = wait_vs_buy(
        profile,
        baseline_rate=baseline_rate,
        baseline_price=price,
        future_rate=future_rate,
        future_price=future_price,
        hoa_monthly=hoa,
        current_year=2026,
    )

    assert js["baselinePitiCents"] == round(py.baseline_piti * 100)
    assert js["futurePitiCents"] == round(py.future_piti * 100)
    assert js["monthlyDeltaCents"] == round(py.monthly_payment_delta * 100)
    assert js["lifetimeInterestDeltaCents"] == round(py.lifetime_interest_delta * 100)
    assert js["breakEvenPriceCents"] == round(py.break_even_future_price * 100)
    assert js["breakEvenPriceChangeCents"] == round(py.break_even_price_change * 100)
