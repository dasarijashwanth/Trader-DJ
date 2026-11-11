#!/usr/bin/env python3
"""Earnings reader for trader-analyst. Standard library only.

Pulls a company's reported numbers from SEC's XBRL "company facts" API and
lays out what changed, quarter by quarter: revenue, gross, operating and net
margins, net income, diluted EPS, and growth against the same quarter a year
earlier. It also finds the latest earnings press release (the EX-99 exhibit
of the newest 8-K with item 2.02) and the latest 10-Q or 10-K.

    python3 facts.py AAPL
    python3 facts.py RELIANCE --market nse    # NSE: screener.in + NSE announcements
    python3 facts.py AAPL KO --quarters 8
    python3 facts.py AAPL --json
    python3 facts.py AAPL AXP GOOGL KO BAC --metrics trader/metrics.json

Sources (SEC; a User-Agent with your name and email is required, from
--user-agent, SEC_USER_AGENT or sec_user_agent in trader/config.json):
  https://www.sec.gov/files/company_tickers.json            ticker -> CIK
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
  https://data.sec.gov/submissions/CIK##########.json        filings list
  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/index.json

How the quarters are built:
  - A quarter is a reported value covering about three months (80 to 100
    days). When a period was reported more than once (a later filing repeats
    it as a comparison), the latest filing wins, so restatements are used.
  - Companies do not file a fourth-quarter 10-Q. The fourth quarter is the
    full year (10-K) minus the three quarters inside it, and it is marked
    "derived". Derived EPS is approximate: share counts change during a year.
  - Revenue is read from the first concept a company uses of:
    RevenueFromContractWithCustomerExcludingAssessedTax, Revenues,
    RevenuesNetOfInterestExpense, SalesRevenueNet and a few more. Banks and
    insurers report revenue differently; margins for them can be blank.

--metrics FILE merges one row per ticker into a JSON file (for notes or a sheet)
(TTM revenue and EPS, margins, growth). Offline: --offline DIR reads
company_tickers.json, companyfacts-CIK##########.json,
submissions-CIK##########.json and <accession>/index.json from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import sys
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
PLACEHOLDER = "you@example.com"
UA_HELP = ("SEC requires a User-Agent with your name and email. Set \"sec_user_agent\": "
           "\"Your Name you@example.com\" in trader/config.json (your real name and email), "
           "or export SEC_USER_AGENT, or pass --user-agent.")

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income")


class FactsError(Exception):
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
    """Switch the active market: 'nse' or 'us'. Raises FactsError otherwise."""
    global MARKET
    name = (name or "nse").lower()
    if name not in MARKETS:
        raise FactsError("--market wants nse or us, got %r" % name)
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


def user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua and config and os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as fh:
                ua = str(json.load(fh).get("sec_user_agent") or "").strip()
        except (OSError, ValueError):
            ua = ""
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


class Fetcher(object):
    def __init__(self, ua=None, offline=None, pause=0.15):
        self.ua, self.offline, self.pause, self._last = ua, offline, pause, 0.0

    def text(self, url, name):
        """A page (screener.in HTML) as text."""
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            raise FactsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url, exc.reason))
        return data.decode("utf-8", "replace")

    def json(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if not self.ua:
            raise FactsError(UA_HELP)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FactsError("SEC has no data at %s (no XBRL filings for this CIK?)" % url)
            raise FactsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url, exc.reason))
        return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------- NSE (nseindia.com + screener.in)
# SEC EDGAR has no Indian companies, and Yahoo's quoteSummary is 401-blocked
# from here, so for --market nse the numbers come from screener.in quarterly
# results (consolidated page, #quarters table, Rs Crores) and the latest
# results/board-meeting announcements from NSE (the Indian 8-K equivalent).
# NSE's site 403s intermittently behind its bot check: the announcements call
# retries once with a fresh session and says so plainly if it still fails.
NSE_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NSE_BASE = "https://www.nseindia.com"
NSE_ANNOUNCE_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
NSE_ANNOUNCE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
SCREENER_URL = "https://www.screener.in/company/%s/consolidated/"
SCREENER_STANDALONE_URL = "https://www.screener.in/company/%s/"
SCREENER_PAGE = "https://www.screener.in/company/%s/"
BROWSER_UA = ("Mozilla/5.0 (compatible; day-trader-skill/2.0; "
              "+https://github.com/Jakeschincariol/day-trader-skill)")


def nse_get_json(url, offline_path=None):
    """GET an NSE JSON API. offline_path reads a saved file instead."""
    if offline_path is not None:
        if not os.path.isfile(offline_path):
            raise FactsError("offline: no file %s" % offline_path)
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
            last = FactsError("NSE answered HTTP %d for %s" % (e.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = FactsError("could not read NSE %s: %s" % (url.split("?")[0], e))
    raise last


def nse_announcements(symbol, offline=None):
    """Corporate announcements for one NSE symbol."""
    path = os.path.join(offline, "nse-ann-%s.json" % symbol) if offline else None
    return nse_get_json(NSE_ANNOUNCE_URL % urllib.parse.quote(symbol), offline_path=path)


def _nse_ann_date(value):
    """'06-Nov-2026 19:10:58' -> datetime, for newest-first ordering."""
    try:
        return _dt.datetime.strptime((value or "").strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return _dt.datetime.min


def latest_nse_filings(data):
    """Newest Financial Result Updates and Outcome of Board Meeting items.
    The API list is not in date order, so the newest is picked by an_dt."""
    out = {"results": None, "board": None}
    for a in data if isinstance(data, list) else []:
        desc = (a.get("desc") or "").strip()
        rec = {"category": desc, "filed": (a.get("an_dt") or "")[:11],
               "url": a.get("attchmntFile") or NSE_ANNOUNCE_PAGE,
               "text": " ".join((a.get("attchmntText") or "").split()),
               "_when": _nse_ann_date(a.get("an_dt"))}
        key = "results" if desc == "Financial Result Updates" else \
            "board" if desc == "Outcome of Board Meeting" else None
        if key and (out[key] is None or rec["_when"] > out[key]["_when"]):
            out[key] = rec
    for rec in out.values():
        if rec:
            rec.pop("_when", None)
    return out


def _norm_label(text):
    return " ".join(text.replace("\xa0", " ").split()).rstrip("+").strip().lower()


class _QuartersParser(HTMLParser):
    """The #quarters table of a screener.in company page.

    -> headers (['', 'Sep 2023', ...]) and rows {'sales': ['231,886', ...]}.
    Row labels carry a trailing '+' on screener ('Sales +'); it is stripped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_quarters = self.in_table = self.in_row = self.in_cell = False
        self.cell, self.row, self.headers, self.rows = [], [], None, {}

    def handle_starttag(self, tag, attrs):
        if tag == "section" and dict(attrs).get("id") == "quarters":
            self.in_quarters = True
        if not self.in_quarters:
            return
        if tag == "table" and not self.in_table:
            self.in_table = True
        elif tag == "tr" and self.in_table:
            self.in_row, self.row = True, []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag == "section" and self.in_quarters:
            self.in_quarters = False
            return
        if not self.in_quarters:
            return
        if tag == "table" and self.in_table:
            self.in_table = False
        elif tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            self.row.append(" ".join("".join(self.cell).split()))
        elif tag == "tr" and self.in_row:
            self.in_row = False
            if self.row:
                if self.headers is None:
                    self.headers = self.row
                else:
                    self.rows[_norm_label(self.row[0])] = self.row[1:]

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def _screener_num(text):
    """'2,36,217' -> 236217.0; '18%' -> 18.0; '-' / '' -> None."""
    if text is None:
        return None
    t = text.strip().replace(",", "").rstrip("%").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_screener_quarters(html):
    """Screener.in company page HTML -> {'quarters': [...], 'rows': {...}}."""
    p = _QuartersParser()
    p.feed(html)
    if not p.headers or "sales" not in p.rows:
        raise FactsError("no quarterly results table on the screener.in page")
    return {"quarters": [h for h in p.headers[1:] if h], "rows": p.rows}


_Q_END = {"Mar": (3, 31), "Jun": (6, 30), "Sep": (9, 30), "Dec": (12, 31)}
_Q_START = {"Mar": (1, 1), "Jun": (4, 1), "Sep": (7, 1), "Dec": (10, 1)}


def _quarter_end(label):
    m = re.match(r"^(Mar|Jun|Sep|Dec)\s+(\d{4})$", (label or "").strip())
    if not m:
        return None
    mon, day = _Q_END[m.group(1)]
    return "%s-%02d-%02d" % (m.group(2), mon, day)


def _quarter_start(label):
    m = re.match(r"^(Mar|Jun|Sep|Dec)\s+(\d{4})$", (label or "").strip())
    if not m:
        return None
    mon, day = _Q_START[m.group(1)]
    return "%s-%02d-%02d" % (m.group(2), mon, day)


def _prior_year(end):
    """The same quarter-end a year earlier ('2026-09-30' -> '2025-09-30')."""
    d = _dt.date.fromisoformat(end)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return None


def _in_fiscal(end):
    """Quarter end -> Indian fiscal label: Sep 2026 is Q2 FY27 (FY ends Mar 31)."""
    d = _dt.date.fromisoformat(end)
    q = (d.month - 1) // 3 + 1
    fq = {1: 4, 2: 1, 3: 2, 4: 3}[q]
    fy = d.year + 1 if d.month >= 4 else d.year
    return "Q%d FY%d" % (fq, fy % 100)


def analyse_screener(parsed, quarters=6, name=None):
    """Screener.in quarterly rows -> the analyse() result shape, in Rs Crores
    (EPS in Rs). No derived Q4: screener lists every quarter as reported."""
    labels = parsed["quarters"]
    rows = parsed["rows"]

    def series_for(key):
        out = {}
        for lab, v in zip(labels, rows.get(key, [])):
            end = _quarter_end(lab)
            num = _screener_num(v)
            if end and num is not None:
                out[end] = num
        return out

    rev = series_for("sales")
    oi = series_for("operating profit")
    opm = series_for("opm %")
    ni = series_for("net profit")
    eps = series_for("eps in rs")
    if not rev and not ni:
        raise FactsError("no quarterly sales or net profit on the screener.in page")
    end_to_label = {_quarter_end(x): x for x in labels if _quarter_end(x)}
    ends = sorted(set(rev) | set(ni), reverse=True)[:quarters]
    out_rows = []
    for end in ends:
        r, o, n, e = rev.get(end), oi.get(end), ni.get(end), eps.get(end)
        prior = _prior_year(end)
        out_rows.append({
            "end": end, "start": _quarter_start(end_to_label.get(end, "")),
            "fiscal": _in_fiscal(end),
            "derived": False, "revenue": r, "gross_profit": None,
            "operating_income": o, "net_income": n,
            "eps_diluted": round(e, 2) if e is not None else None,
            "gross_margin": None, "operating_margin": opm.get(end),
            "net_margin": margin(n, r),
            "revenue_yoy": pct(r, rev.get(prior)) if r is not None else None,
            "net_income_yoy": pct(n, ni.get(prior)) if n is not None else None,
            "eps_yoy": pct(e, eps.get(prior)) if e is not None else None,
            "filed": None, "form": "screener.in",
        })
    ttm = {}
    for key, s in (("revenue", rev), ("net_income", ni), ("eps_diluted", eps)):
        last4 = sorted(s)[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            ttm[key] = sum(s[e] for e in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": name, "cik": None, "rows": out_rows, "ttm": ttm,
            "concepts": {"revenue": ["screener.in Sales"], "net_income": ["screener.in Net Profit"],
                         "eps_diluted": ["screener.in EPS in Rs"]},
            "unit": "INR", "source": "screener.in quarterly results (consolidated)"}


def fetch_screener(fetcher, symbol):
    """Consolidated quarterly results page; falls back to the standalone page
    when the consolidated one has no quarters table."""
    for url, name in ((SCREENER_URL % symbol, "screener-%s.html" % symbol),
                      (SCREENER_STANDALONE_URL % symbol, "screener-%s-standalone.html" % symbol)):
        try:
            html = fetcher.text(url, name)
        except FactsError:
            continue
        try:
            return parse_screener_quarters(html), url
        except FactsError:
            continue
    raise FactsError("no quarterly results table on screener.in for %s (see %s)"
                     % (symbol, SCREENER_PAGE % symbol))


# ---------------------------------------------------------------- facts

def _date(s):
    return _dt.date.fromisoformat(s)


def facts_for(companyfacts, concept, unit_hint=None):
    """Raw facts for one us-gaap concept (USD, or USD/shares for EPS)."""
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    unit = unit_hint if unit_hint in units else next(iter(units), None)
    return units.get(unit, []) if unit else []


def periods(facts):
    """{(start, end): fact} for duration facts from 10-Q/10-K.

    The value comes from the latest filing (restatements win). The fiscal
    label (fy, fp) comes from the first filing that reported the period: a
    later 10-Q that repeats it as a comparison carries its own fy and fp."""
    latest, first = {}, {}
    for f in facts:
        if "start" not in f or f.get("form", "").split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        stamp = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
    out = {}
    for key, f in latest.items():
        g = dict(f)
        g["fy"], g["fp"] = first[key].get("fy"), first[key].get("fp")
        g["first_filed"] = first[key].get("filed")
        out[key] = g
    return out


def quarterly(facts, derive_q4=True):
    """{end: {"value", "start", "end", "derived", "fy", "fp", "form"}} three-month values."""
    per = periods(facts)
    q = {}
    years = []
    for (start, end), f in per.items():
        days = (_date(end) - _date(start)).days
        if 80 <= days <= 100:
            q[end] = {"value": f["val"], "start": start, "end": end, "derived": False,
                      "fy": f.get("fy"), "fp": f.get("fp"), "form": f.get("form"),
                      "filed": f.get("filed")}
        elif 350 <= days <= 380:
            years.append((start, end, f))
    if derive_q4:
        for start, end, f in years:
            if end in q:
                continue
            inside = [v for v in q.values() if v["start"] >= start and v["end"] < end]
            inside.sort(key=lambda v: v["end"])
            if len(inside) != 3:
                continue
            last_q_start = _date(inside[-1]["end"]) + _dt.timedelta(days=1)
            q[end] = {"value": f["val"] - sum(v["value"] for v in inside),
                      "start": last_q_start.isoformat(), "end": end, "derived": True,
                      "fy": f.get("fy"), "fp": "Q4", "form": f.get("form"),
                      "filed": f.get("filed")}
    return q


def series(companyfacts, key, derive_q4=True):
    """Quarterly series for a metric, trying its concepts in order and filling gaps."""
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    merged, used = {}, []
    for concept in CONCEPTS[key]:
        q = quarterly(facts_for(companyfacts, concept, unit), derive_q4)
        if not q:
            continue
        added = False
        for end, v in q.items():
            if end not in merged:
                merged[end] = dict(v, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def _yoy_match(series_, end, days=365, slack=10):
    target = _date(end) - _dt.timedelta(days=days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(companyfacts, quarters=6, name=None):
    """Every number in the report, as a dict."""
    s = {}
    concepts = {}
    for key in CONCEPTS:
        s[key], concepts[key] = series(companyfacts, key, derive_q4=True)
    rev = s["revenue"]
    if not rev and not s["net_income"]:
        raise FactsError("no quarterly revenue or net income in the XBRL facts")
    ends = sorted(set(rev) | set(s["net_income"]), reverse=True)[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end, {}).get("value")
        gp = s["gross_profit"].get(end, {}).get("value")
        if gp is None and r is not None and end in s["cost_of_revenue"]:
            gp = r - s["cost_of_revenue"][end]["value"]
        oi = s["operating_income"].get(end, {}).get("value")
        ni = s["net_income"].get(end, {}).get("value")
        eps = s["eps_diluted"].get(end, {}).get("value")
        prior = {k: _yoy_match(s[k], end) for k in ("revenue", "net_income", "eps_diluted")}
        base = rev.get(end) or s["net_income"].get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "derived": any(s[k].get(end, {}).get("derived") for k in ("revenue", "net_income")),
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": pct(ni, (prior["net_income"] or {}).get("value")),
            "eps_yoy": pct(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": base.get("filed"), "form": base.get("form"),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(s[key])[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            ttm[key] = sum(s[key][e]["value"] for e in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": name or companyfacts.get("entityName"),
            "cik": str(companyfacts.get("cik", "")).zfill(10),
            "rows": rows, "ttm": ttm, "concepts": concepts}


def stale_note(result, filings):
    """A note when SEC's facts lag a report that is already filed."""
    rep = (filings or {}).get("report")
    if not rep or not result["rows"] or not rep.get("period"):
        return None
    latest = result["rows"][0]["end"]
    if rep["period"] > latest and (_date(rep["period"]) - _date(latest)).days > 20:
        return ("SEC's XBRL facts stop at %s, but a %s for the period ending %s was filed %s. "
                "Read it directly: %s" % (latest, rep["form"], rep["period"], rep["filed"],
                                         rep["url"]))
    return None


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []

    inr = result.get("unit") == "INR"

    def money(v):
        if v is None:
            return "n/a"
        if inr:
            return "\u20b9{:,.0f} Cr".format(v)
        return "$%.2fB" % (v / 1e9) if abs(v) >= 1e9 else "$%.1fM" % (v / 1e6)

    label = "Quarter ending %s%s" % (cur["end"], " (derived from the 10-K)" if cur["derived"] else "")
    line = "%s: revenue %s" % (label, money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % money(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s%.2f" % ("\u20b9" if inr else "$", cur["eps_diluted"])
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the "
                   "business (investments, tax items). Check the 10-Q before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- filings

EXHIBIT_RANK = [r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", r"ex(?:hibit)?[-_]?99",
                r"earnings|release|press", r"ex(?:hibit)?[-_]?\d"]


def exhibit_name(names, primary=None):
    """The press release among a filing's files (EX-99.1 by its usual names)."""
    docs = [n for n in names if n.lower().endswith((".htm", ".html", ".txt", ".pdf"))
            and n != primary and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6})", n)]
    for rx in EXHIBIT_RANK:
        hits = sorted(n for n in docs if re.search(rx, n, re.I))
        if hits:
            return hits[0]
    return None


def latest_filings(submissions, cik, fetcher=None):
    """Newest 10-Q/10-K and newest earnings 8-K (item 2.02) with its EX-99 exhibit."""
    rec = submissions.get("filings", {}).get("recent", {})
    out = {"report": None, "earnings_8k": None}
    base_cik = int(cik)
    for i, form in enumerate(rec.get("form", [])):
        acc = rec["accessionNumber"][i]
        folder = ARCHIVE_URL % (base_cik, acc.replace("-", ""))
        if out["report"] is None and form in ("10-Q", "10-K", "20-F", "40-F"):
            out["report"] = {"form": form, "filed": rec["filingDate"][i],
                             "period": rec["reportDate"][i], "accession": acc,
                             "url": folder + rec["primaryDocument"][i]}
        if out["earnings_8k"] is None and form == "8-K" and "2.02" in (rec.get("items", [""] * (i + 1))[i] or ""):
            e = {"form": form, "filed": rec["filingDate"][i], "accession": acc,
                 "url": folder + rec["primaryDocument"][i], "exhibit": None,
                 "index": folder + "%s-index.htm" % acc}
            if fetcher is not None:
                try:
                    idx = fetcher.json(folder + "index.json", os.path.join(acc.replace("-", ""), "index.json"))
                    names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
                    pick = exhibit_name(names, rec["primaryDocument"][i])
                    if pick:
                        e["exhibit"] = folder + pick
                except FactsError:
                    pass
            out["earnings_8k"] = e
        if out["report"] and out["earnings_8k"]:
            break
    return out


# ---------------------------------------------------------------- output

def _m(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 1e6)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render_inr(ticker, result, filings):
    """Screener.in quarterly table (Rs Crores, EPS in Rs) + NSE announcements."""
    out = ["%s (%s). Rs Crores except EPS (Rs). Source: %s." % (
        result["entity"], ticker, result.get("source") or "screener.in"), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "sales", "sales yoy", "opm", "net mgn",
        "net profit", "EPS", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %10s %8s %7s" % (
            r["end"], r["fiscal"].strip()[:9], _m_inr(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m_inr(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"])))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: sales %s, net profit %s, EPS %s." % (
            "\u20b9" + _m_inr(t.get("revenue")) + " Cr" if t.get("revenue") is not None else "-",
            "\u20b9" + _m_inr(t.get("net_income")) + " Cr" if t.get("net_income") is not None else "-",
            "\u20b9%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filings:
        out.append("")
        res, brd = filings.get("results"), filings.get("board")
        if res:
            out.append("Latest results announcement (%s): %s  %s" % (res["filed"], res["text"][:160], res["url"]))
        if brd:
            out.append("Latest board-meeting outcome (%s): %s  %s" % (brd["filed"], brd["text"][:160], brd["url"]))
        if filings.get("error"):
            out.append("NOTE: NSE announcements could not be read: %s. See %s"
                       % (filings["error"], NSE_ANNOUNCE_PAGE))
    out.append("")
    out.append("Screener.in lists every quarter as reported; there is no derived Q4 here.")
    return "\n".join(out) + "\n"


def _m_inr(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v)


def render(ticker, result, filings):
    if result.get("unit") == "INR":
        return render_inr(ticker, result, filings)
    out = ["%s (%s), CIK %s. US$ millions except EPS. Source: SEC XBRL company facts." % (
        result["entity"], ticker, result["cik"]), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net inc", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s%s" % (
            r["end"], r["fiscal"].strip()[:9], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"]), "  derived" if r["derived"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            "$" + _m(t.get("revenue")) + "M" if t.get("revenue") is not None else "-",
            "$" + _m(t.get("net_income")) + "M" if t.get("net_income") is not None else "-",
            "$%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filings:
        out.append("")
        rep, e8 = filings.get("report"), filings.get("earnings_8k")
        if rep:
            out.append("Latest %s: period %s, filed %s  %s" % (rep["form"], rep["period"],
                                                              rep["filed"], rep["url"]))
        if e8:
            out.append("Latest earnings 8-K (item 2.02): filed %s  %s" % (
                e8["filed"], e8["exhibit"] or e8["index"]))
    note = stale_note(result, filings)
    if note:
        out.append("")
        out.append("NOTE: " + note)
    if any(r["derived"] for r in result["rows"]):
        out.append("")
        out.append("Derived quarters are the 10-K year minus the three 10-Qs; derived EPS is "
                   "approximate.")
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": ticker, "name": result["entity"], "cik": result["cik"],
            "period_end": cur.get("end"), "revenue_ttm": t.get("revenue"),
            "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "unit": result.get("unit", "USD"),
            "source": result.get("source") or "SEC XBRL company facts"}


def clean_title(title):
    return re.sub(r"\s*/[A-Z]{2,5}/?\s*$", "", title or "").strip() or None


def ticker_ciks(fetcher):
    """ticker -> (10-digit CIK, company name) from company_tickers.json."""
    data = fetcher.json(TICKERS_URL, "company_tickers.json")
    out = {}
    for v in data.values():
        out.setdefault(v["ticker"].upper().replace("-", "."),
                       (str(v["cik_str"]).zfill(10), clean_title(v.get("title"))))
    return out


def read_config(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="tickers, e.g. AAPL KO")
    ap.add_argument("--cik", help="use this CIK instead of a ticker lookup (one company)")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--no-filings", action="store_true", help="skip the 8-K and 10-Q links")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    ap.add_argument("--user-agent")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--market", choices=["nse", "us"], default=None,
                    help="nse (default) or us; or set \"market\" in trader/config.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    prev_market = MARKET
    resolve_market(args, read_config(args.config))
    try:
        return _run_main(args, ap)
    finally:
        set_market(prev_market)


def _run_main(args, ap=None):
    if not args.tickers and not args.cik:
        if ap is not None:
            ap.print_help()
        return 2
    ua = user_agent(args.user_agent, args.config)
    if not ua and not args.offline and MARKET == "us":
        print("facts: " + UA_HELP, file=sys.stderr)
        return 2
    fetcher = Fetcher(ua, offline=args.offline)
    results, rows, failed = [], [], 0
    try:
        # NSE never touches SEC: skip the ticker->CIK lookup (it needs a UA).
        ciks = {} if args.cik or MARKET == "nse" else ticker_ciks(fetcher)
    except FactsError as exc:
        print("facts: %s" % exc, file=sys.stderr)
        return 2
    wanted = args.tickers or [args.cik]
    for t in wanted:
        tick = t.upper()
        if MARKET == "nse":
            # SEC EDGAR has no Indian companies and Yahoo quoteSummary is
            # 401-blocked: screener.in quarterly results + NSE announcements.
            try:
                parsed, _url = fetch_screener(fetcher, tick)
                result = analyse_screener(parsed, quarters=args.quarters, name=tick)
                filings = None
                if not args.no_filings:
                    try:
                        filings = latest_nse_filings(nse_announcements(tick, fetcher.offline))
                    except FactsError as exc:
                        filings = {"error": str(exc)}
            except FactsError as exc:
                print("facts: %s: %s" % (tick, exc), file=sys.stderr)
                failed += 1
                continue
            results.append({"ticker": tick, "result": result, "filings": filings,
                            "changes": changes(result), "note": None})
            rows.append(metrics_row(tick, result))
            if not args.json:
                sys.stdout.write(render(tick, result, filings))
                sys.stdout.write("\n")
            continue
        if args.cik:
            cik, name = str(args.cik).zfill(10), None
        else:
            cik, name = ciks.get(tick.replace("-", "."), (None, None))
        if not cik:
            print("facts: no SEC CIK for %s (is it a US filer? ETFs and foreign filers "
                  "may not have XBRL facts)" % tick, file=sys.stderr)
            failed += 1
            continue
        try:
            cf = fetcher.json(FACTS_URL % cik, "companyfacts-CIK%s.json" % cik)
            result = analyse(cf, quarters=args.quarters, name=name)
            filings = None
            if not args.no_filings:
                sub = fetcher.json(SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
                filings = latest_filings(sub, cik, fetcher)
        except FactsError as exc:
            print("facts: %s: %s" % (tick, exc), file=sys.stderr)
            failed += 1
            continue
        results.append({"ticker": tick, "result": result, "filings": filings,
                        "changes": changes(result), "note": stale_note(result, filings)})
        rows.append(metrics_row(tick, result))
        if not args.json:
            sys.stdout.write(render(tick, result, filings))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": _dt.date.today().isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
