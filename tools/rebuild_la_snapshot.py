"""Rebuild LA-specific dashboard sections from recorded live layers and local research.

This script is intentionally an augmenting writer.  It preserves the separately
maintained ``hazards`` and ``rules`` blocks and only replaces the geographic,
commute, POI, global-context, and submarket sections assigned to this port.
"""

from __future__ import annotations

import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "data.json"
PROFILE = REPO / "buyer_profile.toml"
LIVE = REPO / "data" / "la_live"
RESEARCH = REPO.parent / "research"
REDFIN_SOURCE = "https://www.redfin.com/news/data-center/"
SCHOOL_SOURCE = "https://caaspp-elpac.ets.org/caaspp/"
OSRM_SOURCE = "https://router.project-osrm.org/"


def ratio(value: float, lo: float, hi: float, *, reverse: bool = False) -> float:
    if hi == lo:
        return 1.0
    scaled = (value - lo) / (hi - lo)
    if reverse:
        scaled = 1 - scaled
    return round(min(1.0, max(0.0, scaled)), 3)


def weighted_market(zip_data: dict[str, Any], zips: list[str]) -> dict[str, Any]:
    rows = [zip_data[z] for z in zips]
    weights = [float(row["HOMES_SOLD"]) for row in rows]
    total = sum(weights)

    def weighted(key: str) -> float:
        return sum(float(row[key]) * weight for row, weight in zip(rows, weights, strict=True)) / total

    return {
        "above_list_share": round(weighted("SOLD_ABOVE_LIST"), 3),
        "dom": round(weighted("MEDIAN_DOM"), 1),
        "homes_sold": round(total, 1),
        "inventory": round(sum(float(row["INVENTORY"]) for row in rows), 1),
        # Redfin reports ZIP medians. A multi-ZIP row is explicitly a homes-sold-
        # weighted aggregation of those medians, never asserted to be a pooled median.
        "median_price": round(weighted("MEDIAN_SALE_PRICE"), 2),
        "period": f"{rows[0]['PERIOD_BEGIN']} to {rows[0]['PERIOD_END']}",
        "ppsf": round(weighted("MEDIAN_PPSF"), 3),
        "yoy_price": round(weighted("MEDIAN_SALE_PRICE_YOY"), 6),
        "src": REDFIN_SOURCE,
        "aggregation_note": "Homes-sold-weighted aggregation of Redfin ZIP-level metrics; not a pooled submarket median.",
    }


def district_info(rankings: dict[str, Any], name: str) -> tuple[float, str]:
    item = next(row for row in rankings["districts_ranked"] if row["district_name"] == name)
    return float(item["proficiency_pct"]), item["source_url"]


def main() -> None:
    snapshot = json.loads(SNAPSHOT.read_text())
    live = json.loads((LIVE / "geospatial.json").read_text())
    routes = json.loads((LIVE / "submarket_routes.json").read_text())
    zip_data = json.loads((RESEARCH / "zip_latest.json").read_text())
    rankings = json.loads((RESEARCH / "la_district_rankings.json").read_text())
    with open(PROFILE, "rb") as fh:
        profile = tomllib.load(fh)
    finance, market = profile["finance"], profile["market_velocity"]

    lausd, lausd_url = district_info(rankings, "Los Angeles Unified")
    montebello, montebello_url = district_info(rankings, "Montebello Unified")
    south_pas, south_pas_url = district_info(rankings, "South Pasadena Unified")
    pasadena, pasadena_url = district_info(rankings, "Pasadena Unified")
    alhambra, alhambra_url = district_info(rankings, "Alhambra Unified")
    san_gabriel, san_gabriel_url = district_info(rankings, "San Gabriel Unified")
    garvey, garvey_url = district_info(rankings, "Garvey Elementary")
    # The Monterey Park/Rosemead ZIP-centroid overlay is elementary Garvey plus
    # Alhambra's 9-12 secondary district.  A transparent equal-district summary is
    # preferable to pretending this split-district area has a unified-school result.
    split_monterey = round((garvey + alhambra) / 2, 1)

    specs = [
        ("eastside_la", "Boyle Heights / East Los Angeles", ["90022", "90023", "90033"], "90022",
         "Los Angeles Unified", lausd, lausd_url),
        ("lincoln_el_sereno", "Lincoln Heights / El Sereno", ["90031", "90032"], "90031",
         "Los Angeles Unified", lausd, lausd_url),
        ("echo_park", "Echo Park / Silver Lake", ["90026"], "90026",
         "Los Angeles Unified", lausd, lausd_url),
        ("northeast_la", "Highland Park / Eagle Rock / Glassell Park", ["90041", "90042", "90065"], "90041",
         "Los Angeles Unified", lausd, lausd_url),
        ("commerce_montebello", "Commerce / Montebello", ["90040", "90640"], "90040",
         "Montebello Unified", montebello, montebello_url),
        ("south_pasadena", "South Pasadena", ["91030"], "91030",
         "South Pasadena Unified", south_pas, south_pas_url),
        ("pasadena", "Pasadena", ["91106", "91107"], "91106",
         "Pasadena Unified", pasadena, pasadena_url),
        ("monterey_park_rosemead", "Monterey Park / Rosemead", ["91754", "91755", "91770"], "91754",
         "Garvey Elementary / Alhambra Unified Secondary", split_monterey, garvey_url),
        ("alhambra", "Alhambra", ["91801", "91803", "91804"], "91801",
         "Alhambra Unified", alhambra, alhambra_url),
        ("san_gabriel", "San Gabriel / Temple City", ["91776"], "91776",
         "San Gabriel Unified", san_gabriel, san_gabriel_url),
    ]

    # All target ZIPs must be represented exactly once.
    flattened = [zip_code for _, _, zips, *_ in specs for zip_code in zips]
    if sorted(flattened) != sorted(market["target_zips"]):
        raise RuntimeError(f"Submarket ZIP mismatch: {sorted(set(market['target_zips']) ^ set(flattened))}")

    submarkets: list[dict[str, Any]] = []
    for ident, name, zips, route_zip, district, proficiency, district_url in specs:
        r = weighted_market(zip_data, zips)
        route = routes[route_zip]
        submarkets.append(
            {
                "crime": {
                    "note": "No comparable, current submarket crime rate was sourced; the safety score is a neutral unknown, not a crime finding.",
                    "per1k": None,
                    "src": None,
                },
                "district": district,
                "district_url": district_url,
                "drive_mi": route["mi"],
                "drive_min": route["min"],
                "fiber": {
                    "coverage_pct": None,
                    "providers": [],
                    "src": None,
                    "note": "No address-level FCC availability result was recorded for this aggregated ZIP submarket.",
                },
                "id": ident,
                "invalidation": (
                    "Verify the exact address, school assignment, insurance quote, flood status, "
                    "and 06:30–07:00 traffic before an offer; this row is ZIP/district context, not parcel advice."
                ),
                "name": name,
                "notes": [
                    f"Redfin ZIP Market Tracker data: {r['period']}; multi-ZIP metrics are homes-sold-weighted.",
                    f"School proficiency is the district 2025 CAASPP ELA/math average ({proficiency:.1f}%).",
                    "OSRM route is a static road-network route to Keck Hospital; it is not a live-traffic estimate.",
                ],
                "redfin": r,
                "school_district": district,
                "school_proficiency_pct": proficiency,
                "school_district_url": district_url,
                "scores": {},  # populated after all observed market and route values are available
                "zips": zips,
            }
        )

    prices = [s["redfin"]["median_price"] for s in submarkets]
    doms = [s["redfin"]["dom"] for s in submarkets]
    inventories = [s["redfin"]["inventory"] for s in submarkets]
    drive_minutes = [s["drive_min"] for s in submarkets]
    for submarket in submarkets:
        market_row = submarket["redfin"]
        submarket["scores"] = {
            "price": ratio(market_row["median_price"], min(prices), max(prices), reverse=True),
            "leverage": round(
                (ratio(market_row["dom"], min(doms), max(doms)) + ratio(market_row["inventory"], min(inventories), max(inventories)))
                / 2,
                3,
            ),
            "commute": ratio(submarket["drive_min"], min(drive_minutes), max(drive_minutes), reverse=True),
            # No sourced comparable crime or address-level fiber results were available.
            # These are deliberate neutral *scoring policies*, not measurements.
            "safety": 0.5,
            "fiber": 0.5,
            "schools": round(submarket["school_proficiency_pct"] / 100, 3),
        }

    snapshot["drivetime"] = live["drivetime"]
    snapshot["geojson"] = {
        "districts": live["districts"],
        "flood": live["flood"],
        "zips": live["zips"],
    }
    # Keep the field a list for the existing browser renderer.  The list is empty rather
    # than carrying obsolete Spartanburg POIs because both public Overpass endpoints failed.
    snapshot["poi"] = live["poi"]
    snapshot["submarkets"] = submarkets
    snapshot["global"] = {
        "anchor": {
            "label": "Keck Hospital of USC, 1500 San Pablo St, Los Angeles CA 90033",
            "lat": 34.062379537908,
            "lon": -118.203042269976,
        },
        "asof": str(date.today()),
        "closing": {
            "buyer_pct": None,
            "la_county_documentary_transfer_tax_per_1000": 1.10,
            "city_los_angeles_documentary_transfer_tax_per_1000": 4.50,
            "customarily_paid_by": "seller",
            "note": (
                "LA County documentary transfer tax is $1.10 per $1,000; within the City of Los Angeles "
                "the base city tax is an additional $4.50 per $1,000. It is customarily seller-paid and negotiable. "
                "Measure ULA may apply at higher City of LA consideration thresholds."
            ),
            "src": "https://www.lavote.gov/home/records/property-document-recording/documentary-transfer-taxes/general-info",
        },
        "county": {
            "name": "Los Angeles County, California",
            "fips": "06037",
            "population": None,
            "population_note": "No current county population figure was recorded in this snapshot rebuild.",
            "src": "https://www.census.gov/programs-surveys/popest.html",
        },
        "employers": {
            "anchor_employer": "Keck Medicine of USC",
            "keck_medicine_employees_2019": 7000,
            "note": "USC reported more than 7,000 Keck Medicine employees across three hospitals and 40+ outpatient locations in 2019; do not treat as current Keck Hospital-only headcount.",
            "src": "https://hscnews.usc.edu/keck-medicine-celebrates-10-year-anniversary-of-hospitals-purchase",
        },
        "insurance": {
            "annual": finance["annual_insurance"],
            "california_average_annual": 1571,
            "note": "Profile planning amount is $2,058/year; California Department of Insurance reports a $1,571 statewide average. Earthquake cover is separate and excluded from PITI.",
            "src": "https://www.insurance.ca.gov/01-consumers/180-climate-change/sustainable-insurance-strategy.Cfm",
        },
        "insurance_project": "https://www.insurance.ca.gov/01-consumers/180-climate-change/sustainable-insurance-strategy.Cfm",
        "metro": {
            "name": "Los Angeles-Long Beach-Anaheim, CA",
            "cbsa": "31080",
            "market_metrics_note": "No CBSA-wide market metric was recorded; use the sourced ZIP-level Redfin rows in submarkets.",
            "dom": None,
            "inventory": None,
            "median_price": None,
            "median_price_yoy": None,
            "months_supply": None,
            "src": "https://www.census.gov/content/dam/Census/programs-surveys/ahs/tech-documentation/2015/PUF_Geography_2015_and_Beyond.pdf",
        },
        "rates": {
            "date": "2026-08-13",
            "pmms_30yr": finance["mortgage_rate"],
            "src": "https://www.freddiemac.com/pmms",
        },
        "tax": {
            "tax_rate_area": "00004",
            "total_rate_pct": 1.18738,
            "rate_fiscal_year": "2025-26",
            "homeowners_exemption": 7000,
            "homeowners_exemption_deadline": "February 15",
            "purchase_resets_assessed_value": True,
            "annual_assessed_value_growth_cap_pct": 2,
            "note": (
                "California Proposition 13 generally resets base-year value to the purchase price on change in ownership; "
                "the factored base-year value grows by CPI, capped at 2% annually. The $7,000 homeowners' exemption "
                "requires filing by February 15 for the full current-year amount."
            ),
            "src_rate": "https://auditor.lacounty.gov/tax-rate-area-lookup/",
            "src_prop13": "https://www.boe.ca.gov/proptaxes/pdf/pub29.pdf",
            "src_exemption": "https://www.boe.ca.gov/proptaxes/homeowners_exemption.htm",
        },
        "user": {
            "gross_income": finance["gross_annual_income"],
            "monthly_non_housing": finance["monthly_non_housing"],
            "down_payment": finance["down_payment"],
            "target_dti_pct": finance["target_front_end_dti"] * 100,
            "target_price": finance["target_price"],
            "mortgage_rate": finance["mortgage_rate"],
            "src": "buyer_profile.toml",
        },
    }
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    report = {
        "tax_keys": list(snapshot["global"]["tax"]),
        "submarkets": [
            {
                "name": submarket["name"],
                "zips": submarket["zips"],
                "school_district": submarket["school_district"],
                "school_proficiency_pct": submarket["school_proficiency_pct"],
                "drive_min": submarket["drive_min"],
                "drive_mi": submarket["drive_mi"],
            }
            for submarket in submarkets
        ],
        "normalization": {
            "schools": "school_proficiency_pct / 100, rounded to 3 decimals",
            "price": "inverse min-max of aggregated Redfin median sale price",
            "leverage": "mean of min-max median DOM and min-max inventory",
            "commute": "inverse min-max OSRM driving minutes",
            "safety_fiber": "0.5 neutral unknown policy because no comparable source was recorded",
        },
        "unsourced_or_unavailable": {
            "poi": live.get("poi_error"),
            "zcta_91804": "Census TIGERweb returned no ZCTA feature for USPS ZIP 91804.",
            "submarket_crime_rate": "No comparable current submarket crime rate recorded; per1k remains null.",
            "submarket_fiber": "No address-level FCC availability result recorded; coverage_pct remains null.",
            "county_population": "No current county population value recorded; field remains null.",
        },
        "routing_source": OSRM_SOURCE,
        "geospatial_source_summary": str(LIVE / "summary.json"),
    }
    (LIVE / "implementation_report.json").write_text(json.dumps(report, indent=1) + "\n")
    print(f"Wrote {SNAPSHOT} and {LIVE / 'implementation_report.json'}")


if __name__ == "__main__":
    main()
