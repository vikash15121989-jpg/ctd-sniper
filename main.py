import json
import os
import warnings
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ADVANCED SCANNER: SAME-DAY BREAKOUT MOVE + MULTI-DAY HOLDING ===", flush=True)

# ===== CONFIGURATION =====
LOOKBACK_DAYS = 1095  # 3 Years Data (2023 - 2026)
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=LOOKBACK_DAYS)

# ===== LOAD WATCHLIST =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    import gspread
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")
    raw_stocks = ws_watchlist.col_values(1)
    
    STOCKS = []
    REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not any(k in clean_s for k in REJECT_KEYWORDS):
                if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                    clean_s += ".NS"
                STOCKS.append(clean_s)
    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Valid Stocks Loaded: {len(STOCKS)}", flush=True)
except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== STRATEGY ENGINE WITH SAME-DAY MOVE METRICS =====
def run_mother_candle_backtest(df, stock_symbol):
    trades = []
    
    if len(df) < 50:
        return pd.DataFrame()

    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    n = len(df)

    i = 2
    while i < n - 15:
        # Step 1: Detect Mother Candle (i-2) and Inside Bar (i-1)
        m_high, m_low = df['High'].iloc[i-2], df['Low'].iloc[i-2]
        i_high, i_low = df['High'].iloc[i-1], df['Low'].iloc[i-1]
        
        # Compression Condition
        is_inside = (i_high < m_high) and (i_low > m_low)

        if is_inside:
            # Step 2: Check Breakout within next 3 days
            for k in range(i, min(i + 3, n - 15)):
                b_open = df['Open'].iloc[k]
                b_high = df['High'].iloc[k]
                b_low = df['Low'].iloc[k]
                b_close = df['Close'].iloc[k]
                b_vol = df['Volume'].iloc[k]
                v_sma = df['Vol_SMA20'].iloc[k]
                ema_val = df['EMA20'].iloc[k]

                # Breakout Trigger: Close > Mother High, Close > 20 EMA, Vol >= 1.5x Avg
                if b_close > m_high and b_close > ema_val and b_vol >= (1.5 * v_sma):
                    
                    entry_price = round(b_close, 2)
                    sl_price = round(m_low, 2)
                    risk = entry_price - sl_price
                    
                    # Same-Day Breakout Move Metrics:
                    # 1. Breakout Day Gain (Open to Close %)
                    same_day_close_gain = round(((b_close - b_open) / b_open) * 100, 2)
                    # 2. Breakout Day Max Expansion (Low to High %)
                    same_day_max_range = round(((b_high - b_low) / b_low) * 100, 2)

                    if risk > 0 and (risk / entry_price) <= 0.10: # Risk Limit 10%
                        target_price = round(entry_price + (2.0 * risk), 2)  # 1:2 R:R

                        # Step 3: Performance Tracking (Forward 15 Trading Days)
                        future = df.iloc[k + 1 : k + 16]
                        win = False
                        exit_price = entry_price

                        for _, f_row in future.iterrows():
                            if f_row['High'] >= target_price:
                                exit_price = target_price
                                win = True
                                break
                            if f_row['Low'] <= sl_price:
                                exit_price = sl_price
                                win = False
                                break

                        if exit_price == entry_price and not future.empty:
                            exit_price = round(future['Close'].iloc[-1], 2)
                            win = exit_price > entry_price

                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

                        trades.append({
                            "Stock": stock_symbol.replace(".NS", ""),
                            "Date": df.index[k].strftime('%Y-%m-%d'),
                            "Entry": entry_price,
                            "SL": sl_price,
                            "Target": target_price,
                            "Vol_Ratio": round(b_vol / v_sma, 2),
                            "SameDay_Move_%": same_day_close_gain, # Breakout Day Gain %
                            "SameDay_Range_%": same_day_max_range, # Breakout Day Volatility Range %
                            "Win": win,
                            "Hold_PnL_%": round(pnl_pct, 2)
                        })
                        
                        i = k + 15  # Skip holding period
                        break
        i += 1

    return pd.DataFrame(trades) if trades else pd.DataFrame()


# ===== EXECUTION & ANALYSIS =====
all_trades = []

print("\nExecuting Strategy Analysis across Watchlist...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 60:
            continue

        res = run_mother_candle_backtest(df, stock)
        if not res.empty:
            all_trades.append(res)

    except Exception:
        pass

if all_trades:
    df_results = pd.concat(all_trades, ignore_index=True)
    
    # Sort strictly by Date (Earliest to Latest)
    df_results['Date'] = pd.to_datetime(df_results['Date'])
    df_results = df_results.sort_values(by='Date', ascending=True)

    total_trades = len(df_results)
    wins = df_results[df_results['Win'] == True]
    losses = df_results[df_results['Win'] == False]
    
    win_rate = (len(wins) / total_trades) * 100
    avg_sameday_move = df_results['SameDay_Move_%'].mean()
    avg_sameday_range = df_results['SameDay_Range_%'].mean()
    avg_hold_pnl = df_results['Hold_PnL_%'].mean()

    print("\n==================================================================")
    print("🏆 SUMMARY: SAME-DAY BREAKOUT VS HOLDING PERFORMANCE")
    print("==================================================================")
    print(f"Total Executed Trades             : {total_trades}")
    print(f"Win-Rate (1:2 Target)             : {round(win_rate, 2)}%")
    print(f"Average Breakout Day Move (Open-Close) : +{round(avg_sameday_move, 2)}%")
    print(f"Average Breakout Day Range (Low-High)  : +{round(avg_sameday_range, 2)}%")
    print(f"Average Holding PnL (15 Days)     : +{round(avg_hold_pnl, 2)}%")
    print("==================================================================")
    
    # Format Date column back to YYYY-MM-DD
    df_results['Date'] = df_results['Date'].dt.strftime('%Y-%m-%d')

    print("\n==========================================================================================================")
    print("📌 RECENT TRADES OUTPUT (2025 - 2026) WITH BREAKOUT DAY MOVE %:")
    print("==========================================================================================================")
    print(df_results.tail(20).to_string(index=False))
    print("==========================================================================================================")
else:
    print("\nNo trades executed across the watchlist for this setup.")
