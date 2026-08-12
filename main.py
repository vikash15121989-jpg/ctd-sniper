import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V143.0 TARGET MATRIX TEST (2% TO 10% TARGET SWEEP) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 1_000_000       # 10 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 5.0        # ₹5 Crore Daily Turnover
MAX_HOLDING_DAYS = 20            # 20 Days Holding Period

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


# ===== MULTI-TARGET ENGINE =====
def run_target_sweep(df_dict, target_pct):
    trades = []

    for stock, df in df_dict.items():
        n = len(df)
        i = 50

        while i < n - MAX_HOLDING_DAYS:
            if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
                
                price = df['Close'].iloc[i]
                ema20 = df['EMA_20'].iloc[i]
                
                above_20_ema = price > ema20
                prev_10_low = df['Low'].iloc[i-10:i].min()
                swept_low = df['Low'].iloc[i] < prev_10_low
                closed_above_low = price > prev_10_low
                
                candle_range = df['High'].iloc[i] - df['Low'].iloc[i]
                strong_rejection = False
                if candle_range > 0:
                    close_pos = (price - df['Low'].iloc[i]) / candle_range
                    strong_rejection = close_pos >= 0.50
                
                high_vol = df['Volume'].iloc[i] >= (1.2 * df['Vol_Avg_20'].iloc[i])

                if above_20_ema and swept_low and closed_above_low and strong_rejection and high_vol:
                    entry_price = price
                    stop_loss = round(df['Low'].iloc[i] * 0.995, 2)
                    risk = entry_price - stop_loss
                    
                    # Fixed Target Percent Test
                    target_price = round(entry_price * (1 + target_pct / 100), 2)

                    if risk > 0 and (risk / entry_price) <= 0.07:
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
                        trades.append({"Win": win, "PnL_%": pnl_pct})
                        i += MAX_HOLDING_DAYS
            i += 1

    if not trades:
        return None

    df_res = pd.DataFrame(trades)
    total_tr = len(df_res)
    wins = df_res[df_res['Win'] == True]
    losses = df_res[df_res['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0

    return {
        "Target_%": f"{target_pct}%",
        "Total_Trades": total_tr,
        "Win_Rate_%": round(win_rate, 2),
        "Profit_Factor": round(profit_factor, 2),
        "Avg_Win_%": round(wins['PnL_%'].mean(), 2) if not wins.empty else 0,
        "Avg_Loss_%": round(losses['PnL_%'].mean(), 2) if not losses.empty else 0
    }


# ===== DATA PREPARATION =====
stock_data = {}
print("\nFetching Stock Data...", flush=True)

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
        df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()

        stock_data[stock] = df

    except Exception:
        pass

# ===== MATRIX SWEEP EXECUTION =====
print("\nRunning Target Matrix Sweep (2% to 10%)...\n", flush=True)
matrix_results = []

for t_pct in range(2, 11):
    res = run_target_sweep(stock_data, t_pct)
    if res:
        matrix_results.append(res)

df_matrix = pd.DataFrame(matrix_results)

print("=========================================================================================")
print("🏆 TARGET MATRIX SWEEP RESULTS (WIN RATE vs TARGET %)")
print("=========================================================================================")
print(df_matrix.to_string(index=False))
print("=========================================================================================")
