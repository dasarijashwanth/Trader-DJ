---
name: trader-analyst
description: >-
  The Earnings Analyst. Reads a company's reported numbers straight from its
  filings and sums up what changed: revenue, gross, operating and net
  margins, net income and diluted EPS, quarter by quarter, each against the
  same quarter a year earlier, with the trailing twelve months. US stocks:
  SEC EDGAR XBRL company facts from 10-Qs and 10-Ks, plus the latest
  earnings 8-K and its press release. NSE stocks (the default market):
  screener.in quarterly results (consolidated, Rs Crores) plus the latest
  results and board-meeting announcements from NSE. Reads an earnings call
  transcript when you paste one. Use when the user says "how did Reliance's
  earnings look", "analyze the earnings", "what changed this quarter", "is
  revenue growing", "check the 10-Q", "compare margins", "sum up the call",
  "does it report this week", or before trading a stock around its earnings.
argument-hint: "[tickers]"
---

# trader-analyst

Earnings move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill reads the numbers
the company actually filed with the SEC, lines them up quarter by quarter, and
says plainly what changed. For a day trader it answers two questions before a
trade: what did the last report say, and is the next one inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
```

For `--market us` it needs the SEC User-Agent (`sec_user_agent` in
`trader/config.json`, or `SEC_USER_AGENT`): "Your Name you@example.com" with
the user's real name and email. It refuses to call SEC without one. The NSE
path needs no key: screener.in works without a login, and NSE announcements
are read with a cookied session (retried once on 403; an honest failure
message if it still fails).

## What it reads and writes

- US: reads `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
  (every number the company has tagged in its filings), the company's
  submissions list (for the newest 10-Q or 10-K and the newest 8-K with item
  2.02, "Results of Operations") and that 8-K's folder to find the press
  release (the EX-99.1 exhibit).
- NSE (default): reads the quarterly results table (`#quarters`) from
  `https://www.screener.in/company/RELIANCE/consolidated/` (standalone page
  as fallback) and the latest Financial Result Updates and Outcome of Board
  Meeting items from NSE corporate announcements. Figures are Rs Crores,
  EPS in Rs, quarters labelled by Indian fiscal year (Sep 2026 is Q2 FY27).
  There is no derived Q4: screener lists every quarter as reported. Yahoo's
  quoteSummary is 401-blocked from here and SEC EDGAR has no Indian
  companies, so neither is used for NSE. One quirk: the NSE announcements
  API slice can lag on the results category (for RELIANCE its newest
  Financial Result Updates item was Jan 2025 while the Oct 30, 2026 board
  outcome already carries the Sep 2026 results), so the tool prints both
  and the board outcome is the fresher pointer.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net income and diluted EPS, the latest margins and growth), for
  your notes or a spreadsheet. `metrics.example.json` in this folder is a
  real run for AAPL, AXP, GOOGL, KO and BAC on 2026-10-13.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's IR page has it), read it and sum up what management said
  changed: guidance, margins, demand, anything they declined to answer.
- The next report date is not in SEC data. Look for it in `/trader-news`
  headlines or on the company's investor relations page, and say where it
  came from.

## The rules

- **Filed numbers first.** The table comes from XBRL facts the company filed.
  Use the press release for context (guidance, segment colour), and say which
  numbers came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown but many businesses are
  seasonal (Apple's December quarter is always its biggest).
- **Say what is derived.** Companies do not file a Q4 10-Q. The fourth
  quarter is the 10-K year minus the three quarters inside it and is marked
  "derived"; derived EPS is approximate.
- **Say when the data lags.** SEC's facts API can trail a filing. When the
  newest 10-Q is later than the newest quarter in the facts, the tool prints a
  NOTE with the link: read the 10-Q itself before summarising.
- **Flag one-offs.** When net income is above operating income, gains outside
  the business (investment marks, tax items) are inside it. The tool says so.
  Banks and insurers have no gross margin: blank, not zero.

## Steps

1. Run it for the companies in question (or the tickers on
   `trader/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --market nse
   python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE TCS --market nse --quarters 8
   python3 "${CLAUDE_SKILL_DIR}/facts.py" TSLA --market us
   python3 "${CLAUDE_SKILL_DIR}/facts.py" TSLA NVDA AMD --market us --metrics trader/metrics.json
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--cik` a company without a ticker.
2. Open the latest earnings press release (the EX-99 link) and the 10-Q when
   the numbers need context, and read them.
3. Write the summary: one line on the quarter (revenue, EPS, growth), one on
   margins, one on anything unusual (a one-off, a derived quarter, data that
   lags), and the links. Plain words, real numbers, no adjectives the numbers
   do not support.
4. If the next report falls inside the planned trade (an overnight hold, or a
   report before the open), say it before `/trader-exit` sets the stop: a stop
   does not protect against a gap, the stock can open past it.

## Example: NSE

`facts.py RELIANCE --market nse` (run 2026-11-11): quarter ending 2026-09-30
(Q2 FY27), sales ₹330,601 Cr, +29.8% against a year earlier, net profit
₹29,550 Cr, diluted EPS ₹18.88 (+40.7%); operating margin 17.0% (+2.0
points on the quarter before). Last four quarters: sales ₹1,199,033 Cr,
net profit ₹95,625 Cr, EPS ₹60.68. TCS the same day: quarter ending
2026-09-30, sales ₹73,188 Cr (+11.2%), net profit ₹13,934 Cr, EPS ₹38.37,
operating margin 26.0%.

## Example: the reel's stock

`facts.py TSLA` (run 2026-10-20): quarter ending 2026-06-30 (Q2 2026),
revenue $28.24B, +25.5% against a year earlier, net income $1.11B (-4.9%),
diluted EPS $0.32 (-3.0%); gross margin 16.8% (-4.3 points on the quarter
before), operating margin 1.4%; last four quarters $103.6B revenue and $1.08
EPS. The tool flags that net income is above operating income, so gains
outside the business are inside it. Latest 10-Q filed 2026-07-23. Monday's
headlines said Tesla reports this week, so the summary ends: "The next report
is days away. A day trade that closes before the bell is not exposed to it;
anything held overnight is."

`facts.py AAPL` (2026-10-13): revenue $109.42B, +16.4% against a year
earlier, diluted EPS $2.02 (+28.7%). For Coca-Cola the tool noted that SEC's
facts stopped at 2026-04-03 while the 10-Q for 2026-07-03 was already filed,
so the summary read that 10-Q directly.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filings do not have, or estimates a quarter it
  cannot derive.
- Never calls SEC without the User-Agent, and stays well under 10 requests a
  second.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/trader-broker`, after `/trader-risk` passes
and the user types an explicit yes for that order.
