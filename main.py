import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ========== MODE LOGIC CONFIG ==========
TARGET_PCT = 10.0
STOP_LOSS_PCT = 5.0
HOLD_DAYS = 20
LOOKBACK = 10

# YE HAI TERE WIN TRADES KA MODE - SABSE ZYADA BAAR AAYA
MODE_RULES = {
    'Range_10D_Min': 8.0, # Range 8% se kam nahi
    'Range_10D_Max': 12.0, # Range 12% se zyada nahi
    'Vol_Ratio_Min': 0.8, # Vol Ratio 0.8 se kam nahi
    'Vol_Ratio_Max': 1.0, # Vol Ratio 1.0 se zyada nahi
    'Higher_Lows': 4, # Higher Lows exactly 4 - Most Frequent
    'Vol_Dry_Days': 3, # Vol Dry Days exactly 3 - Most Frequent
    'Green_Candles_Min': 3 # Green Candles min 3
}
# =======================================

print("=== MODE LOGIC BACKTEST - ALL STOCKS ===", flush=True)
print(f"Logic: Range {MODE_RULES['Range_10D_Min']}-{MODE_RULES['Range_10D_Max']}%, Vol {MODE_RULES['Vol_Ratio_Min']}-{MODE_RULES['Vol_Ratio_Max']}, HL={MODE_RULES['Higher_Lows']}, Dry={MODE_RULES['Vol_Dry_Days']}", flush=True)

# Google Sheets Setup
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

def get_or_create_ws(sh, title):
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=50000, cols=40)

# Watchlist se stocks uthao
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper().replace('.NS','') for s in stocks if s.strip()]

if not stocks:
    print("Watchlist khali hai. Pehle stock add kar.", flush=True)
    exit()

print(f"Stocks: {len(stocks)} | {stocks}", flush=True)

# Master Summary Sheet
ws_master = get_or_create_ws(sh, "MODE_LOGIC_SUMMARY")
master_header = ['Stock', 'Pattern_Count', 'Wins', 'Losses', 'Neutral', 'WinRate_%', 'Avg_Days_Held', 'Expectancy_%']
master_data = [master_header]

# HAR STOCK KE LIYE LOOP
for stock in stocks:
    print(f"\n--- {stock} ---", flush=True)

    try:
        # 1. DATA DOWNLOAD
        df = yf.download(f"{stock}.NS", period="max", progress=False, auto_adjust=True)
        if df.empty:
            print(f"{stock}: Data nahi mila", flush=True)
            master_data.append([stock, 0, 0, 0, 0, 0, 0, 0])
            continue

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        print(f"Data: {df.index[0].date()} to {df.index[-1].date()}", flush=True)

        # 2. MODE LOGIC BACKTEST
        all_trades = []
        pattern_count = 0
        i = LOOKBACK

        while i < len(df) - 1:
            window = df.iloc[i-LOOKBACK:i]
            today = df.iloc[i]

            if window['Volume'].mean() == 0:
                i += 1
                continue

            # DNA Calculate
            range_10d = (window['High'].max() - window['Low'].min()) / window['Low'].min() * 100
            vol_ratio = today['Volume'] / window['Volume'].mean()
            higher_lows = (window['Low'].diff() > 0).sum()
            vol_dry = (window['Volume'] < window['Volume'].mean() * 0.8).sum()
            green_candles = (window['Close'] > window['Open']).sum()

            # MODE LOGIC MATCH
            match = (
                MODE_RULES['Range_10D_Min'] <= range_10d <= MODE_RULES['Range_10D_Max'] and
                MODE_RULES['Vol_Ratio_Min'] <= vol_ratio <= MODE_RULES['Vol_Ratio_Max'] and
                higher_lows == MODE_RULES['Higher_Lows'] and
                vol_dry == MODE_RULES['Vol_Dry_Days'] and
                green_candles >= MODE_RULES['Green_Candles_Min']
            )

            if match:
                pattern_count += 1
                entry_price = today['Close']
                entry_date = df.index[i]

                result = 'NEUTRAL'
                exit_price = df['Close'].iloc[min(i + HOLD_DAYS, len(df)-1)]
                exit_date = df.index[min(i + HOLD_DAYS, len(df)-1)]
                days_held = HOLD_DAYS
                max_move = 0

                # Outcome Check - 20 din me SL ya Target
                for j in range(i+1, min(i+HOLD_DAYS+1, len(df))):
                    curr_high = df['High'].iloc[j]
                    curr_low = df['Low'].iloc[j]
                    max_move = max(max_move, (curr_high / entry_price - 1) * 100)

                    # SL Hit
                    if curr_low <= entry_price * (1 - STOP_LOSS_PCT/100):
                        result = 'LOSS'
                        exit_price = entry_price * (1 - STOP_LOSS_PCT/100)
                        exit_date = df.index[j]
                        days_held = j - i
                        break

                    # Target Hit
                    if curr_high >= entry_price * (1 + TARGET_PCT/100):
                        result = 'WIN'
                        exit_price = entry_price * (1 + TARGET_PCT/100)
                        exit_date = df.index[j]
                        days_held = j - i
                        break

                all_trades.append({
                    'Stock': stock,
                    'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                    'Entry_Price': round(entry_price, 2),
                    'Exit_Date': exit_date.strftime('%Y-%m-%d'),
                    'Exit_Price': round(exit_price, 2),
                    'Days_Held': days_held,
                    'Result': result,
                    'PnL_Pct': round((exit_price / entry_price - 1) * 100, 1),
                    'Max_Move': round(max_move, 1),
                    'Range_10D': round(range_10d, 1),
                    'Vol_Ratio': round(vol_ratio, 2),
                    'Higher_Lows': higher_lows,
                    'Vol_Dry': vol_dry
                })

                i = i + days_held + 1 # Overlap avoid
            else:
                i += 1

        # 3. STATS CALC
        df_trades = pd.DataFrame(all_trades)
        wins = len(df_trades[df_trades['Result'] == 'WIN']) if not df_trades.empty else 0
        losses = len(df_trades[df_trades['Result'] == 'LOSS']) if not df_trades.empty else 0
        neutrals = len(df_trades[df_trades['Result'] == 'NEUTRAL']) if not df_trades.empty else 0
        winrate = round(wins / pattern_count * 100, 1) if pattern_count else 0
        avg_days = round(df_trades['Days_Held'].mean(), 1) if not df_trades.empty else 0
        avg_loss = df_trades[df_trades['Result'] == 'LOSS']['PnL_Pct'].mean() if losses > 0 else -5.0
        expectancy = round((winrate/100 * TARGET_PCT) + ((100-winrate)/100 * avg_loss), 2)

        # 4. SAVE INDIVIDUAL SHEET
        ws_stock = get_or_create_ws(sh, f"{stock}_MODE_LOGIC")
        ws_stock.clear()
        if not df_trades.empty:
            ws_stock.update([df_trades.columns.values.tolist()] + df_trades.values.tolist())

        # 5. MASTER ME ADD KAR
        master_data.append([
            stock, pattern_count, wins, losses, neutrals,
            winrate, avg_days, expectancy
        ])

        print(f"{stock}: Patterns={pattern_count} | Wins={wins} | Loss={losses} | WinRate={winrate}% | Expectancy={expectancy}%", flush=True)

    except Exception as e:
        print(f"{stock}: ERROR - {str(e)}", flush=True)
        master_data.append([stock, 'ERROR', 0, 0, 0, 0, 0, 0])

# 6. MASTER SHEET SAVE
ws_master.clear()
ws_master.update(master_data)

print(f"\n=== COMPLETE ===", flush=True)
print(f"Master Sheet: MODE_LOGIC_SUMMARY", flush=True)
print(f"Har stock ki detail: STOCKNAME_MODE_LOGIC", flush=True)
