"""Tests for outreach template generation and comps rendering.

Run from the repo root:
    python -m pytest tests/test_outreach.py -v
or without pytest:
    python tests/test_outreach.py
"""

import os
import sqlite3
import sys
import unittest
from datetime import date

# Make the repo root importable when the test is run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from outreach import (
    _build_email,
    _build_call_script,
    _build_linkedin,
    _fetch_comps,
    _first_name,
    _fmt_due_date,
    _fmt_price_short,
    _fmt_sold_month,
    _format_comps_bullets,
    _subject_line,
)


def _seed_db():
    """In-memory SQLite with the subset of Property/Transaction columns
    that _fetch_comps reads.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE Property (
            record_id TEXT PRIMARY KEY,
            address TEXT,
            city TEXT,
            region TEXT,
            unit_count INTEGER,
            price_per_unit REAL
        )
    """)
    conn.execute("""
        CREATE TABLE "Transaction" (
            property_record_id TEXT,
            sale_date TEXT,
            purchase_price REAL,
            portfolio_flag TEXT
        )
    """)
    # Fresh same-city comps, plus a portfolio sale (should be filtered),
    # a same-region fallback, and one with no unit_count (skipped).
    props = [
        ("p1", "123 Main St",     "Toronto",     "Toronto", 48,   258_000),
        ("p2", "500 Queen St W",  "Toronto",     "Toronto", 32,   253_000),
        ("p3", "900 Bloor",       "Toronto",     "Toronto", 24,   240_000),
        ("p4", "22 Elm Ave",      "Mississauga", "Peel",    56,   266_000),
        ("p5", "Portfolio Bldg",  "Toronto",     "Toronto", 100,  300_000),
        ("p6", "No Units Here",   "Toronto",     "Toronto", None, None),
        ("p7", "Old Sale",        "Toronto",     "Toronto", 40,   200_000),
        ("target", "100 King St", "Toronto",     "Toronto", 50,   None),
    ]
    for p in props:
        conn.execute(
            "INSERT INTO Property VALUES (?,?,?,?,?,?)", p
        )
    # Use DATE('now','-N months') equivalents via explicit ISO dates.
    # The query filters t.sale_date >= DATE('now','-36 months'), so use
    # dates close to today to keep the test deterministic.
    today = date.today().isoformat()
    this_year = date.today().year
    txns = [
        ("p1", today,                         12_400_000, None),
        ("p2", "%d-08-15" % this_year,         8_100_000, None),
        ("p3", "%d-06-10" % this_year,         5_800_000, ""),
        ("p4", "%d-05-01" % this_year,        14_900_000, None),
        ("p5", today,                         30_000_000, "portfolio"),
        ("p6", today,                          9_000_000, None),
        ("p7", "2019-01-01",                   8_000_000, None),  # > 36 mo
        ("target", today,                     13_000_000, None),
    ]
    for t in txns:
        conn.execute(
            'INSERT INTO "Transaction" VALUES (?,?,?,?)', t
        )
    conn.commit()
    return conn


class FormattersTest(unittest.TestCase):

    def test_due_date_buckets(self):
        self.assertEqual(_fmt_due_date("2026-07-05"), "early July 2026")
        self.assertEqual(_fmt_due_date("2026-07-15"), "mid-July 2026")
        self.assertEqual(_fmt_due_date("2026-07-28"), "late July 2026")
        self.assertEqual(_fmt_due_date(""), "the upcoming maturity")
        self.assertEqual(_fmt_due_date("garbage"), "garbage")

    def test_sold_month(self):
        self.assertEqual(_fmt_sold_month("2025-11-04"), "Nov 2025")
        self.assertEqual(_fmt_sold_month(""), "")

    def test_price_short(self):
        self.assertEqual(_fmt_price_short(12_400_000), "$12.4M")
        self.assertEqual(_fmt_price_short(258_000), "$258k")
        self.assertEqual(_fmt_price_short(999), "$999")
        self.assertEqual(_fmt_price_short(None), "")

    def test_subject_line(self):
        self.assertEqual(
            _subject_line({"address": "100 King St W", "city": "Toronto"}),
            "100 King St W, Toronto",
        )
        self.assertEqual(
            _subject_line({"address": "55 Elm", "city": ""}),
            "55 Elm",
        )

    def test_first_name(self):
        self.assertEqual(_first_name("John Smith"), "John")
        self.assertIsNone(_first_name("1234567 Ontario Inc"))
        self.assertIsNone(_first_name(""))
        self.assertIsNone(_first_name("Acme Holdings LP"))


class CompsBulletsTest(unittest.TestCase):

    def setUp(self):
        self.today = date(2026, 4, 5)
        self.comps = [
            {"address": "123 Main St", "city": "Toronto", "region": "Toronto",
             "units": 48, "sale_date": "2025-11-04", "price": 12_400_000, "ppu": 258_000},
            {"address": "500 Queen Street West", "city": "Toronto", "region": "Toronto",
             "units": 32, "sale_date": "2025-08-15", "price": 8_100_000, "ppu": 253_000},
        ]

    def test_fresh_same_city_header(self):
        out = _format_comps_bullets(self.comps, "Toronto", today=self.today)
        self.assertIn("Recent sales in Toronto:", out)
        self.assertIn("123 Main St, Toronto: 48 units, sold Nov 2025, $258k/unit ($12.4M)", out)
        self.assertNotIn("(past 3 years)", out)

    def test_region_fallback_header(self):
        mixed = self.comps + [{
            "address": "22 Elm", "city": "Mississauga", "region": "Peel",
            "units": 56, "sale_date": "2025-06-01", "price": 14_900_000, "ppu": 266_000,
        }]
        out = _format_comps_bullets(mixed, "Toronto", today=self.today)
        self.assertIn("Recent sales nearby:", out)
        self.assertIn("22 Elm, Peel:", out)

    def test_stale_switches_header(self):
        stale = [dict(c, sale_date="2024-01-15") for c in self.comps]
        out = _format_comps_bullets(stale, "Toronto", today=self.today)
        self.assertIn("Sales in Toronto over the past 3 years:", out)

    def test_under_two_returns_empty(self):
        self.assertEqual(_format_comps_bullets(self.comps[:1], "Toronto"), "")
        self.assertEqual(_format_comps_bullets([], "Toronto"), "")


class FetchCompsTest(unittest.TestCase):

    def test_query_excludes_portfolio_stale_and_missing_units(self):
        conn = _seed_db()
        # Use target_region="Peel" so p4 (Mississauga/Peel) matches via
        # the region fallback branch of the OR clause.
        comps = _fetch_comps(conn, "Toronto", "Peel", exclude_record_id="target", limit=10)
        ids = [c["_record_id"] for c in comps]
        # Target property, portfolio, no-units, and > 36mo sale all filtered.
        self.assertNotIn("target", ids)
        self.assertNotIn("p5", ids)
        self.assertNotIn("p6", ids)
        self.assertNotIn("p7", ids)
        # Same-city comps are included.
        self.assertIn("p1", ids)
        self.assertIn("p2", ids)
        self.assertIn("p3", ids)
        # Same-region fallback is also included.
        self.assertIn("p4", ids)

    def test_same_city_ranks_first(self):
        conn = _seed_db()
        comps = _fetch_comps(conn, "Toronto", "Peel", exclude_record_id="target", limit=4)
        # All Toronto comps should come before the Peel-only fallback.
        first_non_toronto = next(
            (i for i, c in enumerate(comps) if c["city"] != "Toronto"), len(comps)
        )
        toronto_count = sum(1 for c in comps if c["city"] == "Toronto")
        self.assertEqual(first_non_toronto, toronto_count)

    def test_ppu_falls_back_to_price_over_units(self):
        conn = _seed_db()
        comps = _fetch_comps(conn, "Toronto", "Toronto", exclude_record_id="target", limit=10)
        # Target excluded, so find p3 which has 24 units and $5.8M → ~241,667/unit
        p3 = next(c for c in comps if c["_record_id"] == "p3")
        # price_per_unit was set to 240000 in seed, so use that.
        self.assertEqual(p3["ppu"], 240_000)


class EmailTemplatesTest(unittest.TestCase):

    def _draft(self, window="6_month", chargee="TD Bank", city="Toronto"):
        return {
            "owner_name": "John Smith",
            "address": "100 King St W",
            "city": city,
            "region": "Toronto",
            "chargee": chargee,
            "due_date": "2026-07-15",
            "window": window,
        }

    def test_all_windows_render_without_ai_tells(self):
        bad = ["—", "happy to", "Happy to", "reach out", "no obligation",
               "No obligation", "What works", "Worth a", "I'd love",
               "I hope this", "you're financing right now",
               "Hi there"]
        for w in ("9_month", "6_month", "3_month"):
            for chargee in ("TD Bank", ""):
                d = self._draft(window=w, chargee=chargee)
                s, b = _build_email(d, "Jake", "")
                cs = _build_call_script(d, "Jake")
                dm = _build_linkedin(d, "Jake")
                blob = "\n".join([s, b, cs, dm])
                for phrase in bad:
                    self.assertNotIn(phrase, blob,
                        "%r in %s (chargee=%r)" % (phrase, w, chargee))

    def test_lender_name_never_in_body(self):
        d = self._draft(chargee="TD Bank")
        s, b = _build_email(d, "Jake", "")
        cs = _build_call_script(d, "Jake")
        dm = _build_linkedin(d, "Jake")
        for text in (b, cs, dm):
            self.assertNotIn("TD Bank", text)
            self.assertNotIn("charge", text.lower())
        self.assertIn("your mortgage", b.lower())

    def test_corp_owner_greeting_drops_name(self):
        d = self._draft()
        d["owner_name"] = "1234567 Ontario Inc"
        _, b = _build_email(d, "Jake", "")
        self.assertTrue(b.startswith("Hi,\n\n"))
        self.assertNotIn("Hi there", b)
        cs = _build_call_script(d, "Jake")
        self.assertTrue(cs.startswith("Hi, this is"))
        self.assertNotIn("Hi there", cs)
        dm = _build_linkedin(d, "Jake")
        self.assertTrue(dm.startswith("Hi, I was"))
        self.assertNotIn("Hi there", dm)

    def test_individual_owner_greeting_uses_first_name(self):
        d = self._draft()
        d["owner_name"] = "Sarah Mitchell"
        _, b = _build_email(d, "Jake", "")
        self.assertTrue(b.startswith("Hi Sarah,\n\n"))

    def test_subject_has_city(self):
        s, _ = _build_email(self._draft(), "Jake", "")
        self.assertEqual(s, "100 King St W, Toronto")

    def test_human_date_in_body(self):
        _, b = _build_email(self._draft(), "Jake", "")
        self.assertIn("mid-July 2026", b)
        self.assertNotIn("2026-07-15", b)

    def test_blank_chargee_fallback(self):
        _, b = _build_email(self._draft(chargee=""), "Jake", "")
        self.assertIn("Your mortgage", b)
        self.assertNotIn("your  charge", b)
        self.assertNotIn("current lender", b)

    def test_blank_city_uses_your_area(self):
        _, b = _build_email(self._draft(city=""), "Jake", "")
        self.assertIn("in your area", b)

    def test_comps_block_appended_after_signoff(self):
        block = "Recent sales in Toronto:\n  - foo"
        _, b = _build_email(self._draft(), "Jake", comps_block=block)
        self.assertTrue(b.endswith(block))
        # Signoff still appears before the comps block.
        self.assertIn("Jake\n\n" + block, b)

    def test_address_in_body_has_no_city(self):
        _, b = _build_email(self._draft(), "Jake", "")
        # Avoid "comps in Toronto ... acquisition of 100 King St W, Toronto"
        self.assertNotIn("100 King St W, Toronto", b)
        self.assertIn("100 King St W", b)

    def test_linkedin_signoff_no_best(self):
        dm = _build_linkedin(self._draft(), "Jake")
        self.assertNotIn("Best,", dm)
        self.assertNotIn("Regards", dm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
