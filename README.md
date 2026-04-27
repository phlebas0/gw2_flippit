gw2-flippr
==========
Tracks Guild Wars 2 Trading Post flips using the official GW2 API.
Matches completed buy orders to completed sell orders and calculates net profit.

Requirements
------------
Python 3.10+
requests library (pip install requests)

Setup
-----
1. pip install requests
2. python gw2_flip_tracker.py
3. Click "API Key" and enter a GW2 API key with the "tradingpost" permission.
   Generate one at: account.arena.net -> My Account -> Applications
4. Press "Sync Now".

Usage
-----
Sync Now       Fetches your latest transaction history from the GW2 API.
               Item names are cached locally so only new ones are resolved.
 
Filters        Filter the table by columns.
 
Export CSV     Exports the current filtered view to a CSV file.
 
Columns        Item, quantity, buy price, sell price, net profit, buy date, sell date.
               Rows are green if profitable, red if not.

How profit is calculated
------------------------
Net profit = floor(sell_price x quantity x 0.85) - (buy_price x quantity)

The 0.85 factor accounts for the 15% Trading Post fee:
  5%  listing fee (charged when you post the listing)
  10% sales tax   (deducted from proceeds when sold)

Buy and sell orders are matched using FIFO (first in, first out).
The GW2 API does not expose which buy order funded which sale, so this
is an approximation. Partial lots are split automatically.

Limitations
-----------
- FIFO is an approximation, and not always (or often) accurate.
- Cancelled orders and expired listings are not returned by the GW2 API
  and cannot be tracked. Fees lost to cancellations will not appear.
- The GW2 API retains approximately 90 days of transaction history.
  Transactions older than this will not appear. Sync regularly to avoid
  buy orders ageing out before their matching sale is recorded.
- Only completed transactions are shown. Open orders are not included.

Files
-----
gw2_flip_tracker.py   the application
gw2_flips.db          transaction cache and matched flips (SQLite, auto-created)
gw2_config.json       saved API key (auto-created)
