import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: MOTHER CANDLE LOW RETRACEMENT (DEMAND ZONE ENTRY) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 500_000          # 5 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 3.0        # ₹3 Crore Daily Turnover
MAX_HOLDING_DAYS = 20            # 20 Days Holding Limit

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Backtest

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


# ===== MOTHER CANDLE DEMAND RETRACEMENT ENGINE =====
def backtest_mother_demand_entry(df):
    trades = []
    n = len(df)
    i = 50

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Step 1: Lookback within last 6 days for a valid Mother Candle
            for m in range(i - 6, i):
                m_range = df['High'].iloc[m] - df['Low'].iloc[m]
                m_atr = df['ATR'].iloc[m]
                m_vol = df['Volume'].iloc[m]
                m_vol_avg = df['Vol_Avg_20'].iloc[m]
                
                # High Volume + Large Range Green Mother Candle
                is_mother_candle = (m_range >= 1.8 * m_atr) and (m_vol >= 1.8 * m_vol_avg) and (df['Close'].iloc[m] > df['Open'].iloc[m])

                if is_mother_candle:
                    mother_low = df['Low'].iloc[m]
                    mother_high = df['High'].iloc[m]

                    # Step 2: Ensure Mother Low has not been broken between mother day and current day
                    low_protected = df['Low'].iloc[m+1 : i+1].min() >= mother_low

                    # Step 3: Check if Current Price is in Demand Zone (Lower 35% of Mother Candle)
                    demand_zone_upper = mother_low + (0.35 * m_range)
                    in_demand_zone = df['Low'].iloc[i] <= demand_zone_upper

                    # Step 4: Rejection Confirmation (Pinbar / Demand Support Candle)
                    candle_range = df['High'].iloc[i] - df['Low'].iloc[i]
                    strong_rejection = False
                    if candle_range > 0:
                        close_pos = (df['Close'].iloc[i] - df['Low'].iloc[i]) / candle_range
                        strong_rejection = close_pos >= 0.40  # Close in upper 60% / Lower wick >= 40%

                    if low_protected and in_demand_zone and strong_rejection:
                        entry_price = df['Close'].iloc[i]
                        stop_loss = round(mother_low * 0.995, 2)  # SL 0.5% below Mother Low
                        target_price = round(mother_high, 2)      # Target = Mother Candle High
                        
                        risk = entry_price - stop_loss
                        reward = target_price - entry_price

                        # Minimum 1.5 : 1 Reward-to-Risk Requirement
                        if risk > 0 and reward >= (1.5 * risk) and (risk / entry_price) <= 0.06:
                            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                            win = False
                            exit_price = entry_price

                            for _, f_row in future_df.iterrows():
                                if f_row['High'] >= target_price:
                                    exit_price = target_price
                                    win = True
                                    break
                                if f_row['Low'] <= stop_loss:
                                    exit_price = stop_loss
                                    win = False
                                    break

                            if exit_price == entry_price and not future_df.empty:
                                exit_price = future_df['Close'].iloc[-1]
                                win = exit_price > entry_price

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct, "Risk_%": (risk / entry_price) * 100})
                            i += MAX_HOLDING_DAYS
                        break
        i += 1

    return pd.DataFrame(trades) if trades else None


# ===== MAIN EXECUTION =====
all_trades = []

print("\nExecuting Demand Zone Retracement Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        df['Turnover'] = df['Close'] * df['Volume']
        df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
        df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

        # ATR (14)
        df['TR'] = np.maximum(df['High'] - df['Low'], 
                              np.maximum(abs(df['High'] - df['Close'].shift(1)), 
                                         abs(df['Low'] - df['Close'].shift(1))))
        df['ATR'] = df['TR'].rolling(14).mean()

        res = backtest_mother_demand_entry(df)
        if res is not None and not res.empty:
            all_trades.append(res)

    except Exception:
        pass

if all_trades:
    df_all = pd.concat(all_trades, ignore_index=True)
    total_tr = len(df_all)
    wins = df_all[df_all['Win'] == True]
    losses = df_all[df_all['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print("\n==================================================================")
    print("🏆 MOTHER CANDLE DEMAND ZONE RETRACEMENT RESULTS")
    print("==================================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print("==================================================================")
else:
    print("\nNo trades executed for Demand Zone Strategy.")
    
