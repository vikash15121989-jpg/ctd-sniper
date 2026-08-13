import json
import os
import warnings
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== STATISTICAL BACKTEST: MOTHER CANDLE + INSIDE BAR + VOLUME BREAKOUT ===", flush=True)

# ===== CONFIGURATION =====
LOOKBACK_DAYS = 1095  # 3 Years Backtest Data
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


# ===== STRATEGY BACKTEST ENGINE =====
def run_mother_candle_backtest(df, stock_symbol):
    trades = []
    
    if len(df) < 50:
        return pd.DataFrame()

    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
    n = len(df)

    i = 2
    while i < n - 15:
        # Step 1: Detect Mother Candle (i-2) and Inside Bar (i-1)
        m_high, m_low = df['High'].iloc[i-2], df['Low'].iloc[i-2]
        i_high, i_low = df['High'].iloc[i-1], df['Low'].iloc[i-1]
        
        # Inside Bar Condition
        is_inside = (i_high < m_high) and (i_low > m_low)

        if is_inside:
            # Step 2: Look for Breakout within next 3 days
            for k in range(i, min(i + 3, n - 15)):
                b_close = df['Close'].iloc[k]
                b_vol = df['Volume'].iloc[k]
                v_sma = df['Vol_SMA20'].iloc[k]

                # Breakout Condition: Close above Mother High with 1.3x+ Volume
                if b_close > m_high and b_vol >= (1.3 * v_sma):
                    
                    entry_price = round(b_close, 2)
                    sl_price = round(m_low, 2)  # Stop-Loss below Mother Low
                    risk = entry_price - sl_price
                    
                    if risk > 0 and (risk / entry_price) <= 0.10: # Risk <= 10%
                        target_price = round(entry_price + (2.0 * risk), 2)  # 1:2 Risk-Reward

                        # Step 3: Track Performance (Hold up to 15 Days)
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
                            "Win": win,
                            "PnL_%": round(pnl_pct, 2)
                        })
                        
                        i = k + 15 # Skip holding period window
                        break
        i += 1

    return pd.DataFrame(trades) if trades else pd.DataFrame()


# ===== EXECUTION & METRICS CALCULATION =====
all_trades = []

print("\nExecuting Strategy Backtest across Watchlist...", flush=True)

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
    
    total_trades = len(df_results)
    wins = df_results[df_results['Win'] == True]
    losses = df_results[df_results['Win'] == False]
    
    win_rate = (len(wins) / total_trades) * 100
    loss_rate = 100 - win_rate
    
    avg_win = wins['PnL_%'].mean() if not wins.empty else 0
    avg_loss = abs(losses['PnL_%'].mean()) if not losses.empty else 0
    
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
    
    # Expectancy Per Trade
    expectancy = ((win_rate / 100) * avg_win) - ((loss_rate / 100) * avg_loss)

    print("\n==================================================================")
    print("🏆 STATISTICAL BACKTEST RESULTS (3 YEARS PERFORMANCE)")
    print("==================================================================")
    print(f"Total Executed Trades          : {total_trades}")
    print(f"Winning Trades                 : {len(wins)}")
    print(f"Losing Trades                  : {len(losses)}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(profit_factor, 2)}")
    print(f"Average Win Trade              : +{round(avg_win, 2)}%")
    print(f"Average Loss Trade             : -{round(avg_loss, 2)}%")
    print(f"Expectancy Per Trade           : +{round(expectancy, 2)}%")
    print("==================================================================")
    
    print("\nRecent Sample Trades Output:")
    print(df_results.head(10).to_string(index=False))
else:
    print("\nNo trades executed across the watchlist for this setup.")
    
