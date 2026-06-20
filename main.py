import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ========== HIGH WINRATE PATTERN CONFIG ==========
TARGET_PCT = 10.0
STOP_LOSS_PCT = 5.0
HOLD_DAYS = 20
LOOKBACK = 10

# TIGHT RULES - HIGH WINRATE DNA
RULES = {
    'Range_10D_Max': 6.0, # 10 din range 6% se kam
    'Vol_Ratio_Max': 0.7, # Volume avg se 30% kam
    'Higher_Lows_Min': 6, # 10 din me 6+ Higher Lows
    'Vol_Dry_Days_Min': 5, # 10 din me 5+ din volume sukha
    'Green_Candles_Min': 5 # 10 din me 5+ green
}
# ==================================================

print("=== MULTI-STOCK HIGH WINRATE BACKTEST ===", flush=True)

# Google Sheets
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=50000, cols=40)

# Watchlist se sab stocks uthao
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper().replace('.NS','') for s in stocks if s.strip()]

if not stocks:
    print("Watchlist khali hai bhai", flush=True)
    exit()

print(f"Stocks Found: {len(stocks)} | {stocks}", flush=True)

# Master summary sheet
ws_master = get_or_create_ws(sh, "WINRATE_SUMMARY_ALL")
master_data = [['Stock', 'Pattern_Count', 'Wins', 'Losses', 'Neutral', 'WinRate_%', 'Expectancy_%', 'Avg_Max_Move_%']]

# HAR STOCK KE LIYE LOOP
for stock in stocks:
    print(f"\n--- Processing {stock} ---", flush=True)

    try:
        # 1. DATA
        df = yf.download(f"{stock}.NS", period="max", progress=False, auto_adjust=True)
        if df.empty:
            print(f"{stock}: No data", flush=True)
            master_data.append([stock, 0, 0, 0, 0, 0, 0, 0])
            continue

        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

        # 2. PATTERN SCAN + BACKTEST
        all_trades = []
        pattern_count = 0
        i = LOOKBACK

        while i < len(df) - 1:
            window = df.iloc[i-LOOKBACK:i]
            today = df.iloc[i]

            if window['Volume'].mean() == 0:
                i += 1
                continue

            # Calculate DNA factors
            range_10d = (window['High'].max() - window['Low'].min()) / window['Low'].min() * 100
            vol_ratio = today['Volume'] / window['Volume'].mean()
            higher_lows = (window['Low'].diff() > 0).sum()
            vol_dry = (window['Volume'] < window['Volume'].mean() * 0.8).sum()
            green_candles = (window['Close'] > window['Open']).sum()

            # CHECK PATTERN MATCH
            match = (
                range_10d <= RULES['Range_10D_Max'] and
                vol_ratio <= RULES['Vol_Ratio_Max'] and
                higher_lows >= RULES['Higher_Lows_Min'] and
                vol_dry >= RULES['Vol_Dry_Days_Min'] and
                green_candles >= RULES['Green_Candles_Min']
            )

            if match:
                pattern_count += 1
                entry_price = today['Close']
                entry_date = df.index[i]
                result = 'NEUTRAL'
                exit_price = df['Close'].iloc[min(i + HOLD_DAYS, len(df)-1)]
                days_held = HOLD_DAYS
                max_favorable = 0

                # Check outcome
                for j in range(i+1, min(i+HOLD_DAYS+1, len(df))):
                    current_high = df['High'].iloc[j]
                    current_low = df['Low'].iloc[j]
                    max_favorable = max(max_favorable, (current_high / entry_price - 1) * 100)

                    if current_low <= entry_price * (1 - STOP_LOSS_PCT/100):
                        result = 'LOSS'
                        exit_price = entry_price * (1 - STOP_LOSS_PCT/100)
                        days_held = j - i
                        break
                    if current_high >= entry_price * (1 + TARGET_PCT/100):
                        result = 'WIN'
                        exit_price = entry_price * (1 + TARGET_PCT/100)
                        days_held = j - i
                        break

                all_trades.append({
                    'Stock': stock,
                    'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                    'Entry_Price': round(entry_price, 2),
                    'Days_Held': days_held,
                    'Result': result,
                    'PnL_Pct': round((exit_price / entry_price - 1) * 100, 1),
                    'Max_Move': round(max_favorable, 1),
                    'Range_10D': round(range_10d, 1),
                    'Vol_Ratio': round(vol_ratio, 2),
                    'Higher_Lows': higher_lows
                })

                i = i + days_held + 1
            else:
                i += 1

        # 3. CALC STATS FOR THIS STOCK
        df_trades = pd.DataFrame(all_trades)
        wins = len(df_trades[df_trades['Result'] == 'WIN']) if not df_trades.empty else 0
        losses = len(df_trades[df_trades['Result'] == 'LOSS']) if not df_trades.empty else 0
        neutrals = len(df_trades[df_trades['Result'] == 'NEUTRAL']) if not df_trades.empty else 0
        winrate = round(wins / pattern_count * 100, 1) if pattern_count else 0
        avg_loss = df_trades[df_trades['Result'] == 'LOSS']['PnL_Pct'].mean() if losses > 0 else -5.0
        expectancy = round((winrate/100 * TARGET_PCT) + ((100-winrate)/100 * avg_loss), 2)
        avg_max_move = df_trades['Max_Move'].mean() if not df_trades.empty else 0

        # 4. SAVE INDIVIDUAL SHEET
        ws_stock = get_or_create_ws(sh, f"{stock}_WINRATE")
        ws_stock.clear()
        if not df_trades.empty:
            ws_stock.update([df_trades.columns.values.tolist()] + df_trades.values.tolist())

        # 5. ADD TO MASTER
        master_data.append([
            stock, pattern_count, wins, losses, neutrals,
            winrate, expectancy, round(avg_max_move, 1)
        ])

        print(f"{stock}: Patterns={pattern_count} | Wins={wins} | Loss={losses} | WinRate={winrate}%", flush=True)

    except Exception as e:
        print(f"{stock}: Error - {str(e)}", flush=True)
        master_data.append([stock, 'ERROR', 0, 0, 0, 0, 0, 0])

# 6. SAVE MASTER SUMMARY
ws_master.clear()
ws_master.update(master_data)

print(f"\n=== ALL STOCKS COMPLETE ===", flush=True)
print(f"Master Sheet: WINRATE_SUMMARY_ALL", flush=True)
