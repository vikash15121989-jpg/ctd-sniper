import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ========== CONFIG ==========
MOVE_PCT = 10.0
LOOKFORWARD_DAYS = 20
STOP_LOSS_PCT = 5.0
LOOKBACK_PATTERN = 10 # 10 DIN KA PATTERN
# ============================

print("=== ALL-IN-ONE STOCK DNA - 10 DAY PATTERN ===", flush=True)

# Google Sheets
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=50000, cols=40)

# Watchlist se stock uthao
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper().replace('.NS','') for s in stocks if s.strip()]
STOCK = stocks[0] if stocks else "RELIANCE"

print(f"Stock: {STOCK} | Pattern Days: {LOOKBACK_PATTERN}", flush=True)

# 1. DATA
df = yf.download(f"{STOCK}.NS", period="max", progress=False, auto_adjust=True)
if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
print(f"Data: {df.index[0].date()} to {df.index[-1].date()} | {len(df)} days", flush=True)

# 2. PART 1: SABHI 10% MOVES + UNKA 10 DIN KA PATTERN
success_moves = []
for i in range(LOOKBACK_PATTERN, len(df) - LOOKFORWARD_DAYS):
    entry_price = df['Close'].iloc[i]
    target_hit = False
    sl_hit = False
    days_to_target = None

    for j in range(i+1, i+LOOKFORWARD_DAYS+1):
        if df['Low'].iloc[j] <= entry_price * (1 - STOP_LOSS_PCT/100):
            sl_hit = True
            break
        if df['High'].iloc[j] >= entry_price * (1 + MOVE_PCT/100):
            target_hit = True
            days_to_target = j - i
            break

    if target_hit and not sl_hit:
        window = df.iloc[i-LOOKBACK_PATTERN:i] # AB 10 DIN
        today = df.iloc[i]

        success_moves.append({
            'Signal_Date': df.index[i].strftime('%Y-%m-%d'),
            'Entry_Close': round(entry_price, 2),
            'Days_to_10pct': days_to_target,
            'Move_Achieved': round((df['High'].iloc[i+1:i+LOOKFORWARD_DAYS+1].max() / entry_price - 1) * 100, 1),

            # 10 DAY PRICE PATTERN
            'High_10D': round(window['High'].max(), 2),
            'Low_10D': round(window['Low'].min(), 2),
            'Range_10D_Pct': round((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100, 2),
            'Close_10D_Change': round((window['Close'].iloc[-1] / window['Close'].iloc[0] - 1) * 100, 2),
            'Higher_Lows_10D': int((window['Low'].diff() > 0).sum()),
            'Higher_Highs_10D': int((window['High'].diff() > 0).sum()),
            'Green_Candles_10D': int((window['Close'] > window['Open']).sum()),
            'Days_Above_EMA20_10D': int((window['Close'] > window['Close'].ewm(span=20).mean()).sum()),

            # 10 DAY VOLUME PATTERN
            'Vol_Today': int(today['Volume']),
            'Vol_Avg_10D': int(window['Volume'].mean()),
            'Vol_Ratio_10D': round(today['Volume'] / window['Volume'].mean(), 2) if window['Volume'].mean() > 0 else 0,
            'Vol_Dry_Days_10D': int((window['Volume'] < window['Volume'].mean() * 0.8).sum()),
            'Vol_Increasing_10D': int((window['Volume'].diff() > 0).sum()),
            'Vol_Spike_10D': int(today['Volume'] > window['Volume'].max() * 1.2),
            'Vol_Below_Avg_10D': int((window['Volume'] < window['Volume'].mean()).sum()),

            # COMBINED 10 DAY
            'Tight_Range_10D': int(((window['High'].max() - window['Low'].min()) / window['Low'].min() * 100) < 5.0),
            'Consolidation_10D': int(window['Close'].std() / window['Close'].mean() * 100 < 2.0),
            'Breakout_10D': int(today['Close'] > window['High'].max()),
        })

df_success = pd.DataFrame(success_moves)
print(f"\n[PART 1] Total 10% moves found: {len(df_success)}", flush=True)

# 3. PART 2: COMMON PATTERN NIKALO
if not df_success.empty:
    pattern_summary = {
        'Range_10D_Min': df_success['Range_10D_Pct'].quantile(0.25),
        'Range_10D_Max': df_success['Range_10D_Pct'].quantile(0.75),
        'Vol_Ratio_10D_Min': df_success['Vol_Ratio_10D'].quantile(0.25),
        'Vol_Ratio_10D_Max': df_success['Vol_Ratio_10D'].quantile(0.75),
        'Higher_Lows_10D_Min': int(df_success['Higher_Lows_10D'].quantile(0.25)),
        'Green_Candles_10D_Min': int(df_success['Green_Candles_10D'].quantile(0.25)),
        'Vol_Dry_Days_10D_Min': int(df_success['Vol_Dry_Days_10D'].quantile(0.25)),
    }

    # 4. PART 3: POORE HISTORY ME BACKTEST
    backtest_trades = []
    for i in range(LOOKBACK_PATTERN, len(df) - LOOKFORWARD_DAYS):
        window = df.iloc[i-LOOKBACK_PATTERN:i]
        today = df.iloc[i]

        if window['Volume'].mean() == 0: continue

        range_10d = (window['High'].max() - window['Low'].min()) / window['Low'].min() * 100
        vol_ratio = today['Volume'] / window['Volume'].mean()
        higher_lows = (window['Low'].diff() > 0).sum()
        green_candles = (window['Close'] > window['Open']).sum()
        vol_dry = (window['Volume'] < window['Volume'].mean() * 0.8).sum()

        match = (
            pattern_summary['Range_10D_Min'] <= range_10d <= pattern_summary['Range_10D_Max'] and
            pattern_summary['Vol_Ratio_10D_Min'] <= vol_ratio <= pattern_summary['Vol_Ratio_10D_Max'] and
            higher_lows >= pattern_summary['Higher_Lows_10D_Min'] and
            green_candles >= pattern_summary['Green_Candles_10D_Min'] and
            vol_dry >= pattern_summary['Vol_Dry_Days_10D_Min']
        )

        if match:
            entry = today['Close']
            result = 'NEUTRAL'
            exit_price = df['Close'].iloc[i + LOOKFORWARD_DAYS]
            days_held = LOOKFORWARD_DAYS

            for j in range(i+1, i+LOOKFORWARD_DAYS+1):
                if df['Low'].iloc[j] <= entry * (1 - STOP_LOSS_PCT/100):
                    result = 'LOSS'
                    exit_price = entry * (1 - STOP_LOSS_PCT/100)
                    days_held = j - i
                    break
                if df['High'].iloc[j] >= entry * (1 + MOVE_PCT/100):
                    result = 'WIN'
                    exit_price = entry * (1 + MOVE_PCT/100)
                    days_held = j - i
                    break

            backtest_trades.append({
                'Signal_Date': df.index[i].strftime('%Y-%m-%d'),
                'Entry': round(entry, 2),
                'Exit': round(exit_price, 2),
                'Result': result,
                'PnL_Pct': round((exit_price / entry - 1) * 100, 1),
                'Days_Held': days_held,
                'Range_10D': round(range_10d, 1),
                'Vol_Ratio': round(vol_ratio, 2)
            })

df_backtest = pd.DataFrame(backtest_trades)

# 5. SAB SHEET ME SAVE KARO
ws_dna = get_or_create_ws(sh, f"{STOCK}_DNA_10D")
ws_backtest = get_or_create_ws(sh, f"{STOCK}_BACKTEST_10D")
ws_summary = get_or_create_ws(sh, f"{STOCK}_SUMMARY_10D")

ws_dna.clear()
ws_dna.update([df_success.columns.values.tolist()] + df_success.values.tolist())

ws_backtest.clear()
if not df_backtest.empty:
    ws_backtest.update([df_backtest.columns.values.tolist()] + df_backtest.values.tolist())

# 6. SUMMARY
wins = len(df_backtest[df_backtest['Result'] == 'WIN']) if not df_backtest.empty else 0
losses = len(df_backtest[df_backtest['Result'] == 'LOSS']) if not df_backtest.empty else 0
total = len(df_backtest) if not df_backtest.empty else 0
winrate = round(wins / total * 100, 1) if total else 0
avg_win = df_backtest[df_backtest['PnL_Pct'] > 0]['PnL_Pct'].mean() if not df_backtest.empty else 0
avg_loss = df_backtest[df_backtest['PnL_Pct'] < 0]['PnL_Pct'].mean() if not df_backtest.empty else 0
expectancy = round((winrate/100 * MOVE_PCT) + ((100-winrate)/100 * avg_loss), 2) if total else 0

summary_data = [
    ['Stock', STOCK],
    ['Pattern Period', '10 Days'],
    ['Total 10% Moves in History', len(df_success)],
    ['Pattern Range_10D', f"{pattern_summary['Range_10D_Min']:.1f}% - {pattern_summary['Range_10D_Max']:.1f}%"],
    ['Pattern Vol_Ratio_10D', f"{pattern_summary['Vol_Ratio_10D_Min']:.2f} - {pattern_summary['Vol_Ratio_10D_Max']:.2f}"],
    ['Pattern Higher_Lows_10D', f">= {pattern_summary['Higher_Lows_10D_Min']} days"],
    ['Pattern Green_Candles_10D', f">= {pattern_summary['Green_Candles_10D_Min']} days"],
    ['Pattern Vol_Dry_Days_10D', f">= {pattern_summary['Vol_Dry_Days_10D_Min']} days"],
    ['', ''],
    ['BACKTEST RESULTS', ''],
    ['Total Signals', total],
    ['Wins', wins],
    ['Losses', losses],
    ['WinRate', f"{winrate}%"],
    ['Avg Win', f"+{round(avg_win,1)}%"],
    ['Avg Loss', f"{round(avg_loss,1)}%"],
    ['Expectancy per Trade', f"{expectancy}%"],
]

ws_summary.clear()
ws_summary.update(summary_data)

print(f"\n[PART 2] 10-Day Pattern Extracted", flush=True)
print(f"[PART 3] Backtest: {total} signals | WinRate: {winrate}% | Expectancy: {expectancy}%", flush=True)
print(f"\n=== COMPLETE - 3 SHEETS CREATED ===", flush=True)
print(f"1. {STOCK}_DNA_10D - Date wise 10% moves + 10 day pattern", flush=True)
print(f"2. {STOCK}_BACKTEST_10D - Har signal ka result", flush=True)
print(f"3. {STOCK}_SUMMARY_10D - Final report", flush=True)
