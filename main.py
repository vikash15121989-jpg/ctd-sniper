import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: MULTI-YEAR WYCKOFF ACCUMULATION (WEEKLY TIMEFRAME) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_TURNOVER_CR = 0.5         # ₹50 Lakhs+ Weekly Turnover
MAX_HOLDING_WEEKS = 52             # 1 Year Holding Period (Swing/Position)

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=365 * 6) # 6 Years Data for Weekly Analysis

# ===== READ WATCHLIST =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
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


# ===== WEEKLY WYCKOFF ENGINE =====
def backtest_wyckoff_weekly(df):
    trades = []
    
    # Resample Daily Data to Weekly (W-MON)
    df_weekly = df.resample('W-MON').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()

    if len(df_weekly) < 100:
        return pd.DataFrame()

    df_weekly['Turnover_Cr'] = (df_weekly['Close'] * df_weekly['Volume']) / 10_000_000
    df_weekly['Vol_Avg_20'] = df_weekly['Volume'].rolling(20).mean()

    n = len(df_weekly)
    i = 80  # Lookback baseline (~1.5 years of weekly data)

    while i < n - MAX_HOLDING_WEEKS:
        curr_turnover = df_weekly['Turnover_Cr'].iloc[i]

        if curr_turnover >= MIN_AVG_TURNOVER_CR:
            
            # STEP 1: DEFINE BASE TRADING RANGE (Past 52 to 12 Weeks window)
            range_data = df_weekly.iloc[i-52 : i-12]
            range_high = range_data['High'].max()  # Box High
            range_low = range_data['Low'].min()    # Box Low
            range_pct = ((range_high - range_low) / range_low) * 100

            # Valid Consolidation Range: 15% to 65% width over ~1 year base
            if 15.0 <= range_pct <= 65.0:
                
                # STEP 2: DETECT DEEP SHAKEOUT / SPRING (In the recent 12 weeks)
                recent_weeks = df_weekly.iloc[i-12 : i]
                
                # Deep Shakeout: Price dropped below Box Low (Deep Liquidity Sweep)
                shakeout_bars = recent_weeks[recent_weeks['Low'] < range_low]
                
                if not shakeout_bars.empty:
                    shakeout_lowest_low = shakeout_bars['Low'].min()
                    
                    # Ensure Shakeout wasn't a structural crash (Max 40% depth below Box Low)
                    shakeout_depth_pct = ((range_low - shakeout_lowest_low) / range_low) * 100

                    if 2.0 <= shakeout_depth_pct <= 40.0:
                        
                        # STEP 3: BREAKOUT OF BOX HIGH (Current Weekly Close > Range High)
                        curr_close = df_weekly['Close'].iloc[i]
                        prev_close = df_weekly['Close'].iloc[i-1]

                        if curr_close > range_high and prev_close <= range_high:
                            
                            entry_price = curr_close
                            sl_price = round(shakeout_lowest_low, 2) # SL below Shakeout Low
                            risk = entry_price - sl_price
                            target_price = round(entry_price + (2.5 * risk), 2) # 1:2.5 Risk-to-Reward

                            risk_pct = (risk / entry_price) * 100

                            # Safe Position Sizing Filter (Risk <= 25% on weekly timeframe)
                            if risk > 0 and risk_pct <= 25.0:
                                future_df = df_weekly.iloc[i + 1 : i + 1 + MAX_HOLDING_WEEKS]
                                win = False
                                exit_price = entry_price

                                for _, f_row in future_df.iterrows():
                                    if f_row['High'] >= target_price:
                                        exit_price = target_price
                                        win = True
                                        break
                                    if f_row['Low'] <= sl_price:
                                        exit_price = sl_price
                                        win = False
                                        break

                                if exit_price == entry_price and not future_df.empty:
                                    exit_price = future_df['Close'].iloc[-1]
                                    win = exit_price > entry_price

                                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                                trades.append({
                                    "Stock": df.name if hasattr(df, 'name') else "Stock",
                                    "Entry_Date": df_weekly.index[i].strftime('%Y-%m-%d'),
                                    "Win": win,
                                    "PnL_%": pnl_pct,
                                    "Holding_Weeks": len(future_df)
                                })
                                i += MAX_HOLDING_WEEKS # Skip holding period window
        i += 1

    return pd.DataFrame(trades) if trades else pd.DataFrame()


# ===== MAIN EXECUTION =====
all_trades = []

print("\nExecuting Weekly Wyckoff Scanner across Watchlist...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        df.name = stock
        res = backtest_wyckoff_weekly(df)
        if not res.empty:
            all_trades.append(res)

    except Exception:
        pass

if all_trades:
    df_results = pd.concat(all_trades, ignore_index=True)
    total_tr = len(df_results)
    wins = df_results[df_results['Win'] == True]
    losses = df_results[df_results['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print(f"\n==================================================")
    print(f"🏆 RESULTS: WEEKLY WYCKOFF ACCUMULATION BACKTEST")
    print(f"==================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print(f"==================================================")
    print("\nSample Detected Trades:")
    print(df_results.head(10).to_string())
else:
    print("\nNo trades executed for Weekly Wyckoff Engine.")
    
