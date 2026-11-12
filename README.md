# Trader-DJ

11 Claude skills for day trading on NSE, rebuilt for the Indian market from Jake Schincariol's day-trader-skill (MIT, credited below). The originals were the reference only. Every skill here is NSE-first.

NSE hours 9:15 AM to 3:30 PM IST, rupees throughout, .NS tickers. Data comes from Yahoo Finance .NS charts, Google News RSS, NSE corporate announcements, screener.in quarterlies, NSE bulk and block deals, FII/DII flows, and insider disclosures. Upstox paper tickets only. Nothing in this pack can place a live order, paper trading is the default, and no trade goes through without your yes. There is no US pattern-day-trader rule in India, and every run notes T+1 settlement.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans NSE charts for setups: opening-range breakouts, flags, oversold bounces. Curated 172-name liquid universe, or your own --symbols list. |
| `/trader-news` | News Monitor | Tracks breaking news on every stock you watch, from Google News and NSE announcements, and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. Entry window 9:30 AM to 3:00 PM IST. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter. Flat by 3:25 PM IST either way. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits in rupees and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, in rupees. |
| `/trader-broker` | Order Executor | Shows you an Upstox paper ticket in plain words. Paper only, never a live order, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, or the 9:15 AM IST open hits. |
| `/trader-analyst` | Earnings Analyst | Reads quarterly results from screener.in and NSE announcements and sums up what changed, in Rs Crores. |
| `/trader-pelosi` | Disclosures Tracker | Reads Indian public disclosures the minute they post: NSE insider trades, promoter pledges, bulk and block deals, FII/DII flows. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close                                       (while it is open)
news / analyst / pelosi                                                        (on a schedule)
```

A real example, the trade that built this pack: RELIANCE, Tuesday November 11 2026. The scanner and the entry timer both caught the opening-range breakout at 9:35 AM IST at 1,342.80, against a range high of 1,340.90. The Time Machine replayed it 10,000 times: target first 48.7 percent, stop first 49.4 percent. Its verdict: close to a coin flip. That is the point of looking before you risk a rupee.

## The tools

All standard-library Python. The data tools read public endpoints and are polite about it. Use `--market nse` (the default) for India, `--market us` for the original US behavior.

```
python3 trader-scanner/scan.py daily                                   # pre-market setups, NSE universe
python3 trader-scanner/scan.py orb --at 09:35                          # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE.NS --hours 12           # headlines, NSE announcements, today's move
python3 trader-entry/entry.py check RELIANCE.NS --setup orb            # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE.NS --every 30             # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py RELIANCE.NS                            # quarters, growth, EPS
python3 trader-pelosi/ptr.py nse                                       # insider, pledge, FII/DII, bulk and block deals
```

```
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data. 365 of 369 pass. The 4 failures are pre-existing layout checks in test_repo.py that expect the vendor repo root.

## Honest gaps

The Time Machine pool ends at 3:20 PM IST because Yahoo's NSE 5-minute feed has no 15:20 bar, so the last 10 minutes of the session are not in the pool. The NSE pledge and bulk and block deal endpoints could not be verified live, so they are best-effort and report an honest failure with the NSE page URL instead of faking rows. NSE publishes no downloadable symbol directory, so the scanner uses a curated 172-name liquid universe, and --symbols covers anything else. NSE 2027 holidays are not published yet, so weekdays in 2027 are treated as open, with a warning.

## Fine print

**Not financial advice.** This is software that reads public data and does arithmetic. It does not know your situation and it cannot see the future. Most people who day trade lose money. You can lose money. Decide for yourself, and talk to a licensed professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to an order, and only after `/trader-risk` passes that exact order and you type yes for it in the chat. Paper tickets only, never a live order.

**Paper first.** `"paper": true` is the default in `config-nse.example.json`. Keys live in environment variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the stock's own last 60 days of 5-minute bars. The future can do things those 60 days never did: an earnings report, a halt, a headline. Treat its odds as a reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a stop order can fill well past its price. The max loss shown is the loss at the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an official API: it can lag, change or rate-limit you. Check anything that matters against your broker before you act on it.

**Settlement in India is T+1.** Cash from a sell is available the next trading day.

## Files

```
trader-scanner/     SKILL.md, scan.py, nse_universe.txt
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config-nse.example.json, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-pelosi/      SKILL.md, ptr.py
tests/              unittest suite + offline fixtures
```

## Credit

Adapted from [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT). The originals were the reference. Everything in this repo was rebuilt for NSE by Jashwanth Dasari.

MIT.
