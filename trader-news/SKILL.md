---
name: trader-news
description: >-
  News Monitor. Tracks breaking news on every stock you watch and flags what
  moves it: headlines per ticker, each company's filings (8-K on SEC EDGAR
  for US stocks; NSE corporate announcements for Indian stocks) and how far
  each stock is from yesterday's close. Shows only what is new since the
  last check, newest first, in market time (IST for NSE, ET for NYSE), tags
  each item (earnings, analyst, deal, halt, regulatory, macro...) and never
  repeats one. NSE is the default market (--market us for NYSE). Use when
  the user says "any news on RELIANCE", "why is this moving", "what's the
  news on my watchlist", "morning briefing", "pre-market news", "monitor
  the news", "did anything break", "check the 8-Ks", or after
  /trader-scanner finds setups.
argument-hint: "[tickers]"
---

# trader-news

A chart can look perfect and still be about to gap on an earnings date, a
halt or a lawsuit. Before a setup becomes a trade, this skill reads the two
feeds that carry those first, for every stock on the watchlist, and tells you
only what is new. It also prints how far each stock has moved today, so a
headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers TSLA NVDA --hours 12 --market us
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --market nse
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-sec --json
```

NSE is the default market (`--market` defaults to nse, or set `"market"` in
`trader/config.json`). `--market us` restores the NYSE behaviour below.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| Yahoo Finance headline RSS (US) | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA&region=US&lang=en-US` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag); works for `.NS` tickers too |
| SEC EDGAR company Atom feed (US) | 8-K by default (`--forms 8-K,10-Q` for more); needs `sec_user_agent` in `trader/config.json` or `SEC_USER_AGENT`, else filings are skipped with a note |
| Google News RSS (NSE) | headlines: `https://news.google.com/rss/search?q=RELIANCE.NS+stock&hl=en-IN&gl=IN&ceid=IN:en` (Yahoo's headline RSS is empty for `.NS` tickers, so it is not used for NSE) |
| NSE corporate announcements (NSE) | the Indian 8-K equivalent: `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE` (results, board meetings, insider-trading disclosures, credit ratings). NSE's site 403s intermittently behind its bot check; the tool retries once with a fresh session and says so plainly, with the announcements page URL, instead of faking anything |
| `trader/state/news.json` | what has been shown (kept 30 days) and each ticker's CIK |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an 8-K and a headline disagree, the 8-K is
  what the company told the SEC. For NSE stocks the same holds for an NSE
  announcement: it is what the company told the exchange.
- **Say what it touches.** Every item lists the tickers it touches and its
  tags: earnings, analyst, legal, deal, capital, people, risk, halt,
  regulatory, macro. Filings marked `!` carry an important 8-K item: earnings
  results (2.02), an officer change (5.02), an impairment (2.06), a delisting
  notice (3.01), past financials no longer reliable (4.02), bankruptcy (1.03)
  or a cyber incident (1.05).
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged earnings,
   halt, legal, regulatory or risk, and every MOVING ticker first.
3. Write a short brief, per ticker: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "earnings
   are this week: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: an earnings date inside the trade, a halt, an offering.
   Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the open.

## Example: NSE

`news.py --tickers RELIANCE --market nse --hours 240` (run 2026-11-11):
`RELIANCE 1,335.40 +0.83% vs yesterday's close 1,324.40 at 15:29 IST`, then
Google News headlines plus NSE announcements, newest first: the Nov 6
announcement that Reliance executives will meet investors at the CLSA India
Forum on Nov 16, and the Nov 4 credit-rating announcement, each with its PDF
link on nseindia.com. Financial Result Updates are tagged earnings and marked
`!`, like an earnings 8-K.

## Example: the reel

Monday 2026-10-19, TSLA broke its opening range at 10:15 AM ET. The check at
10:15 (Yahoo Finance's TSLA feed, 16 headlines since 8:00) leads with the
morning's real stories: "Meta and Musk earnings and September pending home
sales: What to Watch" (8:00, earnings), "Tesla-SpaceX Merger Mania Strikes
Wall Street Ahead of Earnings" (8:04, earnings, deal), "Tech Stock Rebound
Nudges US Equity Futures Mostly Higher Pre-Bell" (8:02, macro). The brief:
"Tesla reports this week, so the stock can gap overnight; today's trade is
in and out before the close. No new 8-K." At 10:52 the price line reads
`TSLA 366.48 +3.31% vs yesterday's close 354.72 at 10:52 ET  MOVING`. When a
ticker has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per ticker, a pause between
  calls, and SEC's 10 requests a second limit stays far away.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
