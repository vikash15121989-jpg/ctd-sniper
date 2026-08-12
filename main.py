import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V134.0: EXHAUSTION RED CANDLE BREAKOUT (10% TARGET) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 2.0        # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 30             # 10% Target ke liye 30 days maximum window
TARGET_PCT = 0.10                 # Fixed 10% Target

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Backtest

# ===== 1. READ WATCHLIST FROM GOOGLE SHEET =====
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


# ===== 2. STRATEGY ENGINE =====
def backtest_v134_engine(df_daily):
    trades = []
    df = df_daily.copy()

    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000
    
    df['Candle_Range'] = df['High'] - df['Low']
    df['Range_Avg_10'] = df['Candle_Range'].rolling(10).mean()
    
    # 10-day High tracking for "Swing High Fall" context
    df['Swing_High_10'] = df['High'].rolling(10).max()

    n = len(df)
    i = 20 # Start index after indicators warm up

    while i < n - MAX_HOLDING_DAYS:
        # Basic Liquidity Filter
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Condition 1: Stock is falling from Swing High (Current Close is at least 3% below 10-day Swing High)
            recent_high = df['Swing_High_10'].iloc[i]
            is_falling_from_swing = df['Close'].iloc[i] <= (recent_high * 0.97)

            # Condition 2: Red Candle + Range Bda (>= 1.3x Avg Range) + Volume Chhota (<= 0.8x 20-Day Avg Vol)
            is_red = df['Close'].iloc[i] < df['Open'].iloc[i]
            is_large_range = df['Candle_Range'].iloc[i] >= (1.3 * df['Range_Avg_10'].iloc[i])
            is_low_volume = df['Volume'].iloc[i] <= (0.8 * df['Vol_Avg_20'].iloc[i])

            if is_falling_from_swing and is_red and is_large_range and is_low_volume:
                red_high = df['High'].iloc[i]
                red_low = df['Low'].iloc[i]

                search_limit = min(n - 1, i + 10) # Wait up to 10 days for high break

                for k in range(i + 1, search_limit):
                    # Condition 3: Agli candle is Low-Vol Red Candle ka High BREAK/CLOSE kare
                    if df['Close'].iloc[k] > red_high:
                        
                        entry_price = df['Close'].iloc[k]
                        stop_loss = round(red_low * 0.995, 2) # Buffer below Red Candle Low
                        target_price = round(entry_price * (1 + TARGET_PCT), 2) # Fixed 10% Target

                        risk = entry_price - stop_loss

                        if risk > 0 and (risk / entry_price) <= 0.08: # Max 8% SL Risk cap
                            future_df = df.iloc[k + 1 : k + 1 + MAX_HOLDING_DAYS]
                            win = False
                            exit_price = entry_price

                            for _, f_row in future_df.iterrows():
                                # Target 10% Hit
                                if f_row['High'] >= target_price:
                                    exit_price = target_price
                                    win = True
                                    break
                                
                                # Stop Loss Hit at Red Candle Low
                                if f_row['Low'] <= stop_loss:
                                    exit_price = stop_loss
                                    win = False
                                    break

                            # Time-based Exit at end of 30 days if neither hit
                            if exit_price == entry_price and not future_df.empty:
                                exit_price = future_df['Close'].iloc[-1]
                                win = exit_price >= target_price

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct, "Risk_%": (risk / entry_price) * 100})

                            i = k + MAX_HOLDING_DAYS
                            break
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V134.0 Custom Logic Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        df_res = backtest_v134_engine(df)
        if df_res is not None and not df_res.empty:
            all_trades.append(df_res)
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
    print("🏆 RESULTS: CUSTOM LOW-VOL EXHAUSTION RED BREAKOUT (10% TARGET)")
    print("==================================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +10.0%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print("==================================================================")

else:
    print("\nNo trades met the criteria in the backtest period.")
    
