#!/usr/bin/env python3
"""Congress trade tracker for trader-pelosi. Standard library only.

Reads the House Clerk's public financial disclosure index and the Periodic
Transaction Reports (PTRs) members of the House file when they, their spouse
or a dependent child buy or sell a stock.

    python3 ptr.py nse --tickers RELIANCE            # NSE: insider disclosures, pledge, FII/DII
    python3 ptr.py index --member pelosi                 # this year's PTRs by a member
    python3 ptr.py index --year 2026 --since 2026-10-01  # every PTR filed since a date
    python3 ptr.py new --member pelosi                   # only what is new since last check
    python3 ptr.py fetch 20035143                        # download one PTR and read it
    python3 ptr.py parse ptr.txt --filed 2026-08-21      # rows from `pdftotext -layout` text

Where the data comes from:
  - The index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
    (a ZIP with <YEAR>FD.xml: one <Member> per filing, FilingType P = PTR).
  - Each PTR: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf
  - Reading a PDF needs `pdftotext` (poppler) if you want rows from this tool.
    Without it, the tool prints the PDF path and Claude reads the PDF itself.

What a PTR is, and is not:
  - It is a PUBLIC disclosure under the STOCK Act, due within 30 days of the
    member learning of the trade and never later than 45 days after it. So it
    arrives days or weeks after the trade, and amounts are ranges, not prices.
  - Reading them fast is not inside information. There is none here.
  - This covers the House. Senate PTRs are on efdsearch.senate.gov behind an
    agreement you must accept yourself; see SKILL.md for the manual route.

State: `new` remembers which DocIDs it has shown in trader/state/pelosi.json,
so each PTR alerts once. Offline: --offline FILE reads a saved <YEAR>FD.zip
(or the .xml inside it) instead of the network. The tests use it.
"""

import argparse
import datetime as _dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d%s.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf"
FD_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d/%s.pdf"
UA = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"
FILING_TYPES = {
    "P": "Periodic Transaction Report", "O": "Annual report", "A": "Amendment",
    "C": "Candidate report", "X": "Extension", "T": "Termination report",
    "W": "Waiver", "D": "Disclosure (new member or candidate)", "H": "New filer report",
}
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child", "": "self"}
LATE_DAYS = 45


class PtrError(Exception):
    pass

# ---------------------------------------------------------------- market (NSE first at the CLI; --market us restores NYSE)
# The module default is "us": the original NYSE behaviour, so direct callers
# and the offline tests see exactly what they saw before. main() sets the
# market from --market (default nse) or trader/config.json "market".
MARKET = "us"

NSE_HOLIDAYS = {  # NSE capital-market holidays 2026, from NSE holiday-master (CM segment).
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# NSE has no 1:00 p.m. early closes. Diwali's Muhurat session is a separate
# evening session (about an hour, announced by circular); these tools treat
# that day as closed for the day session. NSE's 2027 holidays are not
# published yet (expected December 2026): years outside the list above are
# treated as open on weekdays, so confirm them on nseindia.com.
NSE_EARLY_CLOSE = {}
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}

MARKETS = {
    "nse": {"tz": "IST", "open": _dt.time(9, 15), "close": _dt.time(15, 30),
            "early": None, "holidays": NSE_HOLIDAYS, "early_close": NSE_EARLY_CLOSE,
            "currency": "\u20b9", "exchange": "NSE", "bars_per_day": 75},
    "us": {"tz": "ET", "open": _dt.time(9, 30), "close": _dt.time(16, 0),
           "early": _dt.time(13, 0), "holidays": NYSE_HOLIDAYS, "early_close": NYSE_EARLY_CLOSE,
           "currency": "$", "exchange": "NYSE", "bars_per_day": 78},
}


def set_market(name):
    """Switch the active market: 'nse' or 'us'. Raises PtrError otherwise."""
    global MARKET
    name = (name or "nse").lower()
    if name not in MARKETS:
        raise PtrError("--market wants nse or us, got %r" % name)
    MARKET = name


def mkt():
    return MARKETS[MARKET]


def tz_label():
    return mkt()["tz"]


def resolve_market(args, cfg=None):
    """CLI/config market selection: --market wins, then trader/config.json
    'market', then nse. Call before any market-aware work."""
    name = getattr(args, "market", None)
    if not name and cfg:
        name = cfg.get("market")
    set_market(name or "nse")


# ---------------------------------------------------------------- NSE: the Indian equivalent
# US Congress disclosures have no Indian equivalent: no politician's trades
# are published like PTRs. The closest Indian public disclosures are the
# ones this tool reads for `ptr.py nse`:
#   - NSE corporate announcements filtered to "Disclosure under Insider
#     Trading Regulation" (promoter / insider trades, SEBI PIT Regulations)
#   - NSE promoter pledge / shareholding data (best effort: the endpoint
#     404/403s from here, so a failure is reported honestly, never faked)
#   - NSE FII/DII daily flows (foreign vs domestic institutional net buying)
#   - NSE bulk/block deals (best effort: all three endpoints 404/403 from
#     here; on failure the tool prints the NSE page URL instead of rows)
# Nothing here is inside information: all of it is public, and insider
# disclosures arrive after the trade, like PTRs.
NSE_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NSE_BASE = "https://www.nseindia.com"
NSE_ANNOUNCE_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
NSE_ANNOUNCE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_PLEDGE_URL = ("https://www.nseindia.com/api/corporate-shareholdings"
                  "?index=equities&symbol=%s")
NSE_BULK_URLS = ("https://www.nseindia.com/api/live-analysis-bulk-deals",
                 "https://www.nseindia.com/api/bulk-block-deals",
                 "https://www.nseindia.com/api/bulk-deals")
NSE_BULK_PAGE = "https://www.nseindia.com/market-data/bulk-block-deals"


def nse_get_json(url, offline_path=None):
    """GET an NSE JSON API with a cookied session (home first, ignored on
    failure; Chrome UA + Referer; one retry with a fresh session on 403/401).
    offline_path reads a saved file instead. Raises PtrError on failure;
    never returns fake data."""
    if offline_path is not None:
        if not os.path.isfile(offline_path):
            raise PtrError("offline: no file %s" % offline_path)
        with open(offline_path, encoding="utf-8") as fh:
            return json.load(fh)
    last = None
    for _ in range(2):
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            try:
                req = urllib.request.Request(NSE_BASE + "/", headers={"User-Agent": NSE_CHROME_UA})
                op.open(req, timeout=30).read()
            except Exception:
                pass  # the home page itself 403s from here; the API often still answers
            req = urllib.request.Request(url, headers={
                "User-Agent": NSE_CHROME_UA, "Referer": NSE_BASE + "/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9"})
            with op.open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = PtrError("NSE answered HTTP %d for %s" % (e.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = PtrError("could not read NSE %s: %s" % (url.split("?")[0], e))
    raise last


def _nse_iso_date(value):
    """'31-May-2013 16:52:02' -> '2013-05-31' (ISO, so cutoffs sort right)."""
    try:
        return _dt.datetime.strptime((value or "").strip(), "%d-%b-%Y %H:%M:%S").date().isoformat()
    except ValueError:
        return None


def parse_nse_announcements(data, symbol):
    """NSE corporate-announcements JSON -> [{date, category, text, link, ...}]."""
    out = []
    for a in data if isinstance(data, list) else []:
        out.append({"date": _nse_iso_date(a.get("an_dt") or a.get("sort_date")),
                    "category": (a.get("desc") or "").strip(),
                    "text": " ".join((a.get("attchmntText") or "").split()),
                    "link": a.get("attchmntFile") or NSE_ANNOUNCE_PAGE,
                    "xbrl": bool(a.get("hasXbrl")), "isin": a.get("sm_isin"),
                    "symbol": symbol})
    return out


def insider_disclosures(items):
    """Announcements that are insider-trading disclosures (SEBI PIT Reg. 7(2))."""
    return [a for a in items
            if a["category"] == "Disclosure under Insider Trading Regulation"]


def parse_fiidi(data):
    """FII/DII JSON list -> {'date', 'dii_net', 'fii_net'} in Rs Crores."""
    out = {"date": None, "dii_net": None, "fii_net": None}
    for row in data if isinstance(data, list) else []:
        cat = (row.get("category") or "")
        try:
            net = float(row.get("netValue"))
        except (TypeError, ValueError):
            continue
        out["date"] = out["date"] or row.get("date")
        if cat == "DII":
            out["dii_net"] = net
        elif cat.startswith("FII"):
            out["fii_net"] = net
    return out


def _walk_numbers(obj, words, found):
    """Recursively collect numbers whose key contains any of `words`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if any(w in lk for w in words) and isinstance(v, (int, float)):
                found.setdefault(lk, v)
            _walk_numbers(v, words, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_numbers(v, words, found)


def parse_pledge(data):
    """Promoter pledge numbers from NSE's shareholdings payload. The payload
    shape varies and the endpoint is flaky from here, so this looks for any
    likely key ('pledge', 'promoter' + 'holding'/'percent') instead of
    assuming one shape; missing numbers come back None, never invented."""
    found = {}
    _walk_numbers(data, ("pledge",), found)
    pledged = None
    for k, v in found.items():
        if "share" in k or "no" in k or "qty" in k or "quantity" in k:
            pledged = v
            break
    if pledged is None and found:
        pledged = next(iter(found.values()))
    hold = {}
    _walk_numbers(data, ("promoter",), hold)
    pct = None
    for k, v in hold.items():
        if "percent" in k or "holding" in k or "%" in k:
            pct = v
            break
    return {"pledged_shares": int(pledged) if pledged is not None else None,
            "promoter_holding_pct": float(pct) if pct is not None else None}


def fetch_nse_bulk_deals(offline=None):
    """Bulk/block deals, best effort: tries each known endpoint once. Returns
    (rows, error): rows is a list (possibly empty), error a plain message."""
    if offline:
        path = os.path.join(offline, "nse-bulk-deals.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), None
    last = None
    for url in NSE_BULK_URLS:
        try:
            data = nse_get_json(url)
            if isinstance(data, dict):
                data = data.get("data") or data.get("bulkDeals") or []
            return list(data) if isinstance(data, list) else [], None
        except PtrError as exc:
            last = str(exc)
    return [], ("NSE bulk/block deals could not be read from this machine (%s). "
                "See %s" % (last, NSE_BULK_PAGE))


def render_nse(symbol, disclosures, pledge, pledge_error, fiidi, bulk, bulk_error):
    """Plain-text report for `ptr.py nse --tickers SYM`."""
    out = ["NSE disclosures for %s (public filings, not inside information)." % symbol, ""]
    out.append("Insider-trading disclosures (NSE announcements, SEBI PIT Regulations): %d"
               % len(disclosures))
    for d in disclosures[:10]:
        out.append("  - %s  %s" % (d["date"], (d["text"] or d["category"])[:160]))
        out.append("      %s" % d["link"])
    if len(disclosures) > 10:
        out.append("  ... and %d more (see %s)" % (len(disclosures) - 10, NSE_ANNOUNCE_PAGE))
    if not disclosures:
        out.append("  none found in the announcements returned.")
    out.append("")
    if pledge_error:
        out.append("Promoter pledge: %s" % pledge_error)
    elif pledge.get("pledged_shares") is not None or pledge.get("promoter_holding_pct") is not None:
        bits = []
        if pledge.get("pledged_shares") is not None:
            bits.append("%s shares pledged" % "{:,}".format(pledge["pledged_shares"]))
        if pledge.get("promoter_holding_pct") is not None:
            bits.append("promoter holding %.2f%%" % pledge["promoter_holding_pct"])
        out.append("Promoter pledge: " + ", ".join(bits) + ".")
    else:
        out.append("Promoter pledge: NSE returned no pledge numbers for %s." % symbol)
    out.append("")
    if fiidi.get("date"):
        out.append("FII/DII flows, %s (Rs Crores, net): DII %s, FII/FPI %s."
                   % (fiidi["date"],
                      "%+.2f" % fiidi["dii_net"] if fiidi.get("dii_net") is not None else "n/a",
                      "%+.2f" % fiidi["fii_net"] if fiidi.get("fii_net") is not None else "n/a"))
    else:
        out.append("FII/DII flows: not available.")
    out.append("")
    if bulk_error:
        out.append("Bulk/block deals: %s" % bulk_error)
    elif bulk:
        out.append("Bulk/block deals: %d rows." % len(bulk))
        for row in bulk[:10]:
            out.append("  - %s" % row)
    else:
        out.append("Bulk/block deals: no rows returned.")
    out.append("")
    out.append("These are public disclosures filed after the trades. Amounts and "
               "dates come from NSE; a disclosure is a reason to look at a chart, "
               "not a setup.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the index

def iso(mdy):
    """'8/21/2026' or '08/21/2026' -> '2026-08-21' (two-digit years -> 20xx)."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def download(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise PtrError("HTTP %s for %s" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise PtrError("could not reach %s: %s" % (url, exc.reason))


def read_index_bytes(data):
    """Index XML bytes from the ZIP (or the XML itself)."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise PtrError("no XML inside the ZIP (files: %s)" % ", ".join(z.namelist()))
            return z.read(names[0])
    return data


def parse_index(xml_bytes, year):
    """Every filing in a <YEAR>FD.xml as a dict, newest first."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise PtrError("index is not valid XML: %s" % exc)
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        doc = f.get("DocID", "")
        kind = f.get("FilingType", "")
        y = int(f.get("Year") or year)
        url = (PTR_URL if kind == "P" else FD_URL) % (y, doc)
        name = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last"),
                                    f.get("Suffix")) if x)
        out.append({"doc_id": doc, "type": kind, "type_name": FILING_TYPES.get(kind, kind),
                    "last": f.get("Last", ""), "first": f.get("First", ""), "name": name,
                    "district": f.get("StateDst", ""), "year": y,
                    "filed": iso(f.get("FilingDate", "")), "url": url})
    out.sort(key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)
    return out


def load_index(year, offline=None):
    if offline:
        with open(offline, "rb") as fh:
            data = fh.read()
    else:
        data = download(INDEX_URL % (year, "FD"))
    return parse_index(read_index_bytes(data), year)


def member_match(row, member):
    """'pelosi', 'Nancy Pelosi' or 'Pelosi, Nancy' against an index row."""
    member = (member or "").strip().lower()
    if not member:
        return True
    if "," in member:
        last, first = [x.strip() for x in member.split(",", 1)]
    else:
        parts = member.split()
        last, first = parts[-1], " ".join(parts[:-1])
    if row["last"].lower() != last:
        return False
    return not first or row["first"].lower().startswith(first)


def select(rows, members=None, kind="P", since=None):
    out = []
    for r in rows:
        if kind and r["type"] != kind:
            continue
        if members and not any(member_match(r, m) for m in members):
            continue
        if since and (r["filed"] or "") < since:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def new_since(rows, state, today, first_days=LATE_DAYS):
    """Rows not seen before. On the first run: only those filed in the last
    first_days days, so you are not flooded with the whole year."""
    seen = set(state.get("seen", []))
    if state.get("last_check") is None:
        cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
        return [r for r in rows if (r["filed"] or "") >= cutoff]
    return [r for r in rows if r["doc_id"] not in seen]


# ---------------------------------------------------------------- parsing a PTR

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABELS = [
    ("status", re.compile(r"^\s*F(?:ILING)?\s+S(?:TATUS)?\s*:\s*(.*)$", re.I)),
    ("description", re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)),
    ("account", re.compile(r"^\s*S(?:UBHOLDING)?\s+O(?:F)?\s*:\s*(.*)$", re.I)),
    ("location", re.compile(r"^\s*L(?:OCATION)?\s*:\s*(.*)$", re.I)),
]
# Column headers repeat at the top of every page, sometimes in the middle of a
# row that continues on the next page: skip them without ending the row.
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)",
                         re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)
AMOUNT = re.compile(r"\$([\d,]+)")
OPTION = re.compile(r"([\d,]+)\s+(call|put)\s+options?\b.*?strike price of \$([\d,.]+)"
                    r".*?expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.S)
SHARES = re.compile(r"(?:purchased|sold|bought)\s+([\d,]+)\s+shares", re.I)


def _amount(text):
    nums = [int(x.replace(",", "")) for x in AMOUNT.findall(text or "")]
    low = nums[0] if nums else None
    high = nums[1] if len(nums) > 1 else None
    return low, high


def _finish(row, filed):
    asset = " ".join(row.pop("_asset").split())
    tick = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
    code = re.findall(r"\[([A-Z]{2})\]", asset)
    row["ticker"] = tick[-1] if tick else None
    row["asset_type"] = code[-1] if code else None
    name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
    if tick:
        name = re.sub(r"\s*\(%s\)" % re.escape(tick[-1]), "", name)
    row["asset"] = name.strip(" -")
    row["amount"] = " ".join(row["amount"].split())
    row["amount_low"], row["amount_high"] = _amount(row["amount"])
    row["description"] = " ".join(row.get("description", "").split()) or None
    opt = OPTION.search(row["description"] or "")
    if opt:
        row["option"] = {"contracts": int(opt.group(1).replace(",", "")),
                         "kind": opt.group(2).lower(),
                         "strike": float(opt.group(3).replace(",", "")),
                         "expires": iso(opt.group(4)),
                         "exercised": bool(re.match(r"\s*exercised\b", row["description"], re.I))}
    sh = SHARES.search(row["description"] or "")
    if sh:
        row["shares"] = int(sh.group(1).replace(",", ""))
    if filed and row["date"]:
        days = (_dt.date.fromisoformat(filed) - _dt.date.fromisoformat(row["date"])).days
        row["days_to_disclose"] = days
        row["late"] = days > LATE_DAYS
    return row


def parse_ptr_text(text, filed=None):
    """Rows from `pdftotext -layout` output of a House PTR.

    Returns {"name", "status", "district", "doc_id", "filed", "rows": [...]}.
    Each row: owner, owner_name, asset, ticker, asset_type, type, type_name,
    date, notified, amount, amount_low, amount_high, status, description,
    account, option {contracts, kind, strike, expires}, shares,
    days_to_disclose, late."""
    head = {"name": None, "status": None, "district": None, "doc_id": None, "filed": filed}
    rows, cur, mode = [], None, None
    amount_open = False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        m = re.match(r"^Name:\s+(.*)$", s)
        if m and not head["name"]:
            head["name"] = m.group(1).strip()
            continue
        m = re.match(r"^Status:\s+(.*)$", s)
        if m and not head["status"]:
            head["status"] = m.group(1).strip()
            continue
        m = re.match(r"^State/District:\s*(\S+)", s)
        if m:
            head["district"] = m.group(1)
            continue
        m = re.search(r"Filing ID #(\d+)", s)
        if m:
            head["doc_id"] = m.group(1)
            continue
        m = re.match(r"^Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})", s)
        if m:
            head["filed"] = head["filed"] or iso(m.group(1))
            continue
        t = TXN.match(line)
        if t and iso(t.group("date")):
            if cur:
                rows.append(cur)
            owner = t.group("owner") or ""
            cur = {"owner": owner, "owner_name": OWNERS.get(owner, owner),
                   "_asset": t.group("asset"), "type": t.group("type"),
                   "type_name": TXN_TYPES.get(t.group("type"), t.group("type")),
                   "date": iso(t.group("date")), "notified": iso(t.group("notified")),
                   "amount": t.group("amount"), "status": None, "account": None}
            amount_open = cur["amount"].endswith("-")
            mode = "asset"
            continue
        if cur is None:
            continue
        if not s:
            if mode in ("description", "asset"):
                mode = "labels" if mode == "description" or "[" in cur["_asset"] else mode
            continue
        if PAGE_HEADER.match(s):
            continue
        if END.match(s):
            mode = "labels"
            continue
        label = next(((k, rx.match(line)) for k, rx in LABELS if rx.match(line)), None)
        if label:
            key, lm = label
            value = lm.group(1).strip()
            if key == "description":
                cur["description"] = value
                mode = "description"
            elif key in ("status", "account"):
                cur[key] = value
                mode = "labels"
            else:
                mode = "labels"
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["_asset"] += " " + " ".join(parts)
        elif mode == "description":
            cur["description"] = cur.get("description", "") + " " + s
    if cur:
        rows.append(cur)
    head["rows"] = [_finish(r, head["filed"]) for r in rows]
    return head


def pdf_to_text(pdf_path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-layout", pdf_path, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


# ---------------------------------------------------------------- output

def watchlist(args):
    tickers = {t.upper() for t in (args.tickers or [])}
    path = getattr(args, "portfolio", None)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for p in json.load(fh).get("positions", []):
                if p.get("ticker"):
                    tickers.add(p["ticker"].upper())
    return tickers


def describe(row):
    bits = ["%s %s" % (row["owner_name"], row["type_name"])]
    what = row["asset"]
    if row.get("ticker"):
        what += " (%s)" % row["ticker"]
    if row.get("option"):
        o = row["option"]
        what += ": %s%d %s options, $%g strike, expires %s" % (
            "exercised " if o.get("exercised") else "", o["contracts"], o["kind"],
            o["strike"], o["expires"])
    elif row.get("shares"):
        what += ": %s shares" % "{:,}".format(row["shares"])
    bits.append(what)
    bits.append(row["amount"])
    when = "traded %s" % row["date"]
    if row.get("days_to_disclose") is not None:
        when += ", disclosed %d days later%s" % (row["days_to_disclose"],
                                                   " (LATE: over 45)" if row.get("late") else "")
    bits.append(when)
    if row.get("account"):
        bits.append("account: %s" % row["account"])
    return ", ".join(bits)


def render_report(report, watch=()):
    out = ["%s (%s), %s. PTR %s, filed %s." % (
        report.get("name") or "?", report.get("district") or "?", report.get("status") or "?",
        report.get("doc_id") or "?", report.get("filed") or "?")]
    for r in report["rows"]:
        flag = "  <- %s is on your list" % r["ticker"] if r.get("ticker") in watch else ""
        out.append("  - %s%s" % (describe(r), flag))
        if r.get("description") and not r.get("option") and not r.get("shares"):
            out.append("      %s" % r["description"])
    if not report["rows"]:
        out.append("  no transaction rows found: read the PDF directly")
    out.append("Public disclosure under the STOCK Act, filed up to 45 days after the trade. "
               "Amounts are ranges. Not inside information.")
    return "\n".join(out) + "\n"


def render_index(rows):
    out = []
    for r in rows:
        out.append("%s  %-28s %-5s %-4s doc %-9s %s" % (
            r["filed"], r["name"][:28], r["district"], r["type"], r["doc_id"], r["url"]))
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--market", choices=["nse", "us"], default=None,
                    help="nse (default for the nse command) or us")
    sub = ap.add_subparsers(dest="cmd")
    year = _dt.date.today().year

    def common(p):
        p.add_argument("--year", type=int, default=year)
        p.add_argument("--member", action="append",
                       help="last name or 'First Last'; repeat for more members")
        p.add_argument("--type", default="P", help="FilingType (default P = PTR; '' for all)")
        p.add_argument("--since", help="only filings on or after YYYY-MM-DD")
        p.add_argument("--offline", metavar="FILE", help="a saved <YEAR>FD.zip or .xml")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("index", help="list filings from the yearly index")
    common(p)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("new", help="filings not seen at the last check")
    common(p)
    p.add_argument("--state", default="trader/state/pelosi.json")
    p.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    p.add_argument("--read", action="store_true",
                   help="also download and read each new PTR (needs pdftotext)")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*", help="flag trades in these tickers")
    p.add_argument("--portfolio", default="trader/portfolio.json")

    p = sub.add_parser("fetch", help="download one PTR PDF and read it")
    p.add_argument("doc_id")
    p.add_argument("--year", type=int, default=year)
    p.add_argument("--filed", help="filing date YYYY-MM-DD if known")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("nse", help="Indian equivalent: NSE insider disclosures, "
                           "promoter pledge, FII/DII flows, bulk/block deals")
    p.add_argument("--tickers", nargs="*", required=True, help="NSE symbols, e.g. RELIANCE TCS")
    p.add_argument("--days", type=int, default=90,
                   help="only disclosures from the last N days (default 90; 0 = all)")
    p.add_argument("--offline", metavar="DIR",
                   help="saved nse-ann-<SYM>.json, nse-fiidii.json, nse-pledge-<SYM>.json, "
                        "nse-bulk-deals.json")
    p.add_argument("--no-pledge", action="store_true", help="skip the promoter pledge lookup")
    p.add_argument("--no-fiidii", action="store_true", help="skip the FII/DII flows")
    p.add_argument("--no-bulk", action="store_true", help="skip the bulk/block deals lookup")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("parse", help="rows from pdftotext -layout text of a PTR")
    p.add_argument("text_file")
    p.add_argument("--filed", help="filing date YYYY-MM-DD (else read from the signature)")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")
    return ap


def _fetch_and_read(doc_id, year, pdf_dir, filed=None):
    os.makedirs(pdf_dir, exist_ok=True)
    path = os.path.join(pdf_dir, "%s.pdf" % doc_id)
    if not os.path.isfile(path):
        data = download(PTR_URL % (year, doc_id))
        if not data.startswith(b"%PDF"):
            raise PtrError("doc %s did not come back as a PDF" % doc_id)
        with open(path, "wb") as fh:
            fh.write(data)
    text = pdf_to_text(path)
    if text is None:
        return path, None
    report = parse_ptr_text(text, filed=filed)
    report["pdf"] = path
    report["url"] = PTR_URL % (year, doc_id)
    return path, report


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    prev_market = MARKET
    try:
        if args.cmd == "nse":
            set_market("nse")
        elif getattr(args, "market", None):
            set_market(args.market)
        return _run(args)
    except (PtrError, OSError) as exc:
        print("ptr: %s" % exc, file=sys.stderr)
        return 2
    finally:
        set_market(prev_market)


def _run_nse(args):
    """`ptr.py nse --tickers RELIANCE`: the Indian disclosures for one or more
    NSE names. FII/DII flows and bulk deals are market-wide, fetched once."""
    tickers = [t.upper() for t in args.tickers]
    fiidi, fiidi_error = {"date": None, "dii_net": None, "fii_net": None}, None
    if not args.no_fiidii:
        try:
            path = os.path.join(args.offline, "nse-fiidii.json") if args.offline else None
            fiidi = parse_fiidi(nse_get_json(NSE_FIIDII_URL, offline_path=path))
        except PtrError as exc:
            fiidi_error = str(exc)
    bulk, bulk_error = [], None
    if not args.no_bulk:
        bulk, bulk_error = fetch_nse_bulk_deals(args.offline)
    cutoff = (_dt.date.today() - _dt.timedelta(days=args.days)).isoformat() if args.days else None
    results, failed = [], 0
    for sym in tickers:
        try:
            path = os.path.join(args.offline, "nse-ann-%s.json" % sym) if args.offline else None
            items = parse_nse_announcements(
                nse_get_json(NSE_ANNOUNCE_URL % urllib.parse.quote(sym), offline_path=path), sym)
        except PtrError as exc:
            print("ptr: NSE announcements for %s: %s. See %s" % (sym, exc, NSE_ANNOUNCE_PAGE),
                  file=sys.stderr)
            failed += 1
            continue
        disc = insider_disclosures(items)
        disc.sort(key=lambda d: d["date"] or "", reverse=True)  # API order is not date order
        if cutoff:
            disc = [d for d in disc if (d["date"] or "") >= cutoff]
        pledge, pledge_error = {"pledged_shares": None, "promoter_holding_pct": None}, None
        if not args.no_pledge:
            try:
                ppath = os.path.join(args.offline, "nse-pledge-%s.json" % sym) if args.offline else None
                pledge = parse_pledge(
                    nse_get_json(NSE_PLEDGE_URL % urllib.parse.quote(sym), offline_path=ppath))
            except PtrError as exc:
                pledge_error = ("NSE pledge data for %s could not be read from this machine (%s); "
                                "check the shareholding pattern for %s on nseindia.com"
                                % (sym, exc, sym))
        results.append({"symbol": sym, "disclosures": disc, "pledge": pledge,
                        "pledge_error": pledge_error, "fiidi": fiidi,
                        "fiidi_error": fiidi_error, "bulk": bulk, "bulk_error": bulk_error})
        if not args.json:
            sys.stdout.write(render_nse(sym, disc, pledge, pledge_error, fiidi, bulk, bulk_error))
    if fiidi_error:
        print("ptr: FII/DII flows: %s" % fiidi_error, file=sys.stderr)
    if args.json:
        print(json.dumps(results, indent=2))
    return 0 if results and not failed else (1 if results else 2)


def _run(args):
    if args.cmd == "nse":
        return _run_nse(args)
    if MARKET == "nse":
        # House PTRs have no Indian equivalent; the nse command is the route.
        print("ptr: House PTRs cover the US Congress only. For Indian disclosures use "
              "`ptr.py nse --tickers RELIANCE` (or --market us for the House).",
              file=sys.stderr)
        return 2
    if args.cmd == "parse":
        with open(args.text_file, encoding="utf-8", errors="replace") as fh:
            report = parse_ptr_text(fh.read(), filed=args.filed)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    if args.cmd == "fetch":
        path, report = _fetch_and_read(args.doc_id, args.year, args.pdf_dir, args.filed)
        if report is None:
            print("Saved %s. pdftotext is not installed, so read the PDF directly "
                  "(Claude can open it), or install poppler (brew install poppler)." % path)
            return 0
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    rows = load_index(args.year, args.offline)
    if args.cmd == "new" and not args.offline:
        today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
        if today.month == 1 and args.year == today.year:
            try:
                rows += load_index(args.year - 1)
            except PtrError:
                pass
    picked = select(rows, args.member, args.type or None, args.since)

    if args.cmd == "index":
        picked = picked[:args.limit]
        if args.json:
            print(json.dumps(picked, indent=2))
        else:
            sys.stdout.write(render_index(picked))
            if not picked:
                print("No matching filings in the %d index." % args.year)
        return 0 if picked else 1

    # new
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    state = load_state(args.state)
    fresh = new_since(picked, state, today)
    watch = watchlist(args)
    reports = []
    if args.read:
        for r in fresh:
            if r["type"] != "P":
                continue
            path, report = _fetch_and_read(r["doc_id"], r["year"], args.pdf_dir, r["filed"])
            reports.append({"filing": r, "pdf": path, "report": report})
    if args.json:
        print(json.dumps({"new": fresh, "reports": reports}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        for r in fresh:
            print("NEW %s: %s (%s) filed %s, doc %s" % (
                r["type_name"], r["name"], r["district"], r["filed"], r["doc_id"]))
            print("    %s" % r["url"])
        for rep in reports:
            if rep["report"] is None:
                print("    read %s (pdftotext not installed)" % rep["pdf"])
            else:
                sys.stdout.write(render_report(rep["report"], watch))
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(r["doc_id"] for r in picked)
        state["seen"] = sorted(seen)
        state["last_check"] = _dt.datetime.now().replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
