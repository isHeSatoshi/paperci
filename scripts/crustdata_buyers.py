"""Company-level buyer map via Crustdata (companies only; no people data is requested or stored).
  python scripts/crustdata_buyers.py probe          # shape of one response (keys only)
  python scripts/crustdata_buyers.py run            # enrich the candidate list -> docs/buyer-map.md"""
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
H = {"authorization": "Bearer " + os.environ.get("CRUSTDATA_API_KEY", ""), "content-type": "application/json",
     "x-api-version": "2025-11-01"}
URL = "https://api.crustdata.com/company/enrich"  # 2 credits per company (docs.crustdata.com/general/pricing)

# Hypothesised buyer segments (assumption to be tested, not a claim about these companies' needs):
SEGMENTS = {
    "RL environment / eval data vendors": ["scale.com", "surgehq.ai", "mercor.com", "turing.com", "invisible.co"],
    "Open model / RL training labs": ["primeintellect.ai", "huggingface.co", "together.ai", "nousresearch.com"],
    "Agent eval / research tooling": ["harborframework.com", "laude.org", "epoch.ai", "metr.org"],
    "Reproducibility / publishing": ["cascad.tech", "codeocean.com", "paperswithcode.com", "openreview.net"],
}
KEEP = ["company_name", "company_website_domain", "headcount", "total_investment_usd", "last_funding_round_type",
        "last_funding_round_investment_usd", "hq_country", "year_founded", "linkedin_industries", "taxonomy"]


def enrich(domain: str):
    r = requests.post(URL, headers=H, json={"domains": [domain], "fields": ["basic_info", "headcount", "funding"]}, timeout=60)
    if r.status_code != 200:
        return {"domain": domain, "error": r.status_code}
    d = r.json()
    try:
        return d[0]["matches"][0]["company_data"]
    except (KeyError, IndexError, TypeError):
        return {"domain": domain, "error": "no match"}


def flat_pick(rec: dict) -> dict:
    """Company-level fields only; contact fields in the payload are deliberately dropped."""
    b, h, f = rec.get("basic_info") or {}, rec.get("headcount") or {}, rec.get("funding") or {}
    g = h.get("growth_percent") or {}
    return {"company_name": b.get("name"), "headcount": h.get("total"), "headcount_range": b.get("employee_count_range"),
            "headcount_growth_yoy_pct": g.get("yoy") if isinstance(g, dict) else None,
            "total_investment_usd": f.get("total_investment_usd"), "last_funding_round_type": f.get("last_round_type"),
            "hq_country": h.get("largest_headcount_country"), "year_founded": b.get("year_founded")}


if __name__ == "__main__":
    if sys.argv[1] == "probe":
        rec = enrich("huggingface.co")
        print(type(rec).__name__, sorted(rec.keys())[:80] if isinstance(rec, dict) else rec)
    else:
        rows = []
        for seg, doms in SEGMENTS.items():
            for d in doms:
                rec = enrich(d)
                if not rec or "error" in rec:
                    rows.append((seg, d, None))
                else:
                    rows.append((seg, d, flat_pick(rec)))
        L = ["# Buyer map (company-level, via Crustdata)", "",
             "Segments are our hypotheses about who buys verified executable tasks or reproducibility checks.",
             "Data: Crustdata company enrichment, fetched once; company fields only (no people data).", "",
             "| segment | domain | company | headcount | YoY headcount % | total funding (USD) | last round | largest country |",
             "|---|---|---|---|---|---|---|---|"]
        for seg, d, r in rows:
            if r is None:
                L.append(f"| {seg} | {d} | (not found) | | | | | |")
            else:
                inv = r.get("total_investment_usd")
                inv = f"{inv / 1e6:,.0f}M" if isinstance(inv, (int, float)) and inv else "-"
                L.append(f"| {seg} | {d} | {r.get('company_name')} | {r.get('headcount') or r.get('headcount_range')} | "
                         f"{r.get('headcount_growth_yoy_pct')} | {inv} | {r.get('last_funding_round_type') or '-'} | {r.get('hq_country')} |")
        (ROOT / "docs" / "buyer-map.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
        print("\n".join(L))
