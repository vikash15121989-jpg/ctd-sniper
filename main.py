import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== EXACT CHART PATTERN (DRY VOLUME SQUEEZE + BREAKOUT) BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_TURNOVER = 10_000_000   # ₹1 Crore Liquidity Filter (Mid/Small Cap Friendly)
MAX_HOLDING_DAYS = 45       # Holding up to 45 Trading Days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data

# ===== 1. READ WATCHLIST =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    raw_stocks = ws_watchlist.col_values(1)
    STOCKS = []
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                clean_s += ".NS"
            STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== 2. CHART PATTERN ENGINE =====
def backtest_chart_pattern(df_daily):
    trades = []
    df = df_daily.copy()

    # Calculate Turnover & Volume Moving Averages
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['Vol_MA20'] = df['Volume'].rolling(20).mean()

    n = len(df)
    i = 60  # Start after sufficient data history

    while i < n - MAX_HOLDING_DAYS:
        # Check Liquidity
        if df['Turnover_MA20'].iloc[i] >= MIN_TURNOVER:
            
            # Step 1: Find Resistance Level (Mother High / Peak Range in last 20-40 days)
            past_window = df.iloc[i-40 : i-10]
            if len(past_window) > 0:
                resistance_high = past_window['High'].max()
                res_idx = past_window['High'].idxmax()
                res_vol = df.loc[res_idx, 'Volume']

                # Step 2: Dry Volume Squeeze (Price & Volume down after resistance)
                dry_window = df.iloc[i-10 : i]
                avg_dry_vol = dry_window['Volume'].mean()
                lowest_point = dry_window['Low'].min()

                # Dry Condition: Volume during squeeze is < 50% of peak volume
                if avg_dry_vol < (res_vol * 0.50):
                    
                    # Step 3: Volume Spike Alert (1st Volume Bar Expansion)
                    curr_vol = df['Volume'].iloc[i]
                    is_vol_spike = curr_vol > (avg_dry_vol * 1.8)

                    # Step 4: Breakout Trigger (Price crosses Resistance Level)
                    curr_close = df['Close'].iloc[i]
                    prev_close = df['Close'].iloc[i-1]

                    if is_vol_spike and curr_close >= resistance_high * 0.98 and prev_close < resistance_high:
                        entry_price = curr_close
                        
                        # Stop Loss = Lowest Point of the Dry Consolidation Phase
                        stop_loss = lowest_point
                        risk = entry_price - stop_loss

                        # Risk Cap check (Max 8% Risk per trade)
                        if risk > 0 and (risk / entry_price) <= 0.08:
                            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]

                            win = False
                            exit_price = entry_price
                            trail_sl = stop_loss

                            for _, f_row in future_df.iterrows():
                                # Trail Stop Loss with Swing Lows when trade moves > 1.5R in profit
                                if f_row['Close'] > entry_price + (risk * 1.5):
                                    trail_sl = max(trail_sl, f_row['Low'])

                                if f_row['Low'] <= trail_sl:
                                    exit_price = trail_sl
                                    win = exit_price > entry_price
                                    break

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct})

                            i += 10 # Skip days to prevent duplicate entries
                            continue
        i += 1

    if not trades:
        return None

    df_trades = pd.DataFrame(trades)
    total_tr = len(df_trades)
    wins = df_trades[df_trades['Win'] == True]
    losses = df_trades[df_trades['Win'] == False]

    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999

    return {
        "Trades": total_tr,
        "Win_Rate": win_rate,
        "Gross_Profit": gross_profit,
        "Gross_Loss": gross_loss,
        "Profit_Factor": profit_factor
    }


# ===== 3. EXECUTE BACKTEST =====
all_trades = 0
all_profit = 0.0
all_loss = 0.0
winrate_list = []

print("\nRunning Chart-Exact Dry Volume Squeeze Breakout Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_chart_pattern(df)
        if res:
            all_trades += res["Trades"]
            all_profit += res["Gross_Profit"]
            all_loss += res["Gross_Loss"]
            winrate_list.append(res["Win_Rate"])
    except Exception:
        pass

if all_trades > 0:
    avg_winrate = np.mean(winrate_list)
    overall_pf = all_profit / all_loss if all_loss > 0 else 999

    print("\n==================================================================")
    print("🏆 RESULTS: EXACT CHART PATTERN (DRY VOLUME SQUEEZE BREAKOUT)")
    print("==================================================================")
    print(f"Total Selected Trades Executed : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo chart pattern trades met the exact criteria.")
    
