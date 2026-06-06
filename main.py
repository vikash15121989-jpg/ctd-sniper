import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== OBV SQUEEZE SCANNER V8.0 - HSCL JAISE SETUP ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. OBV CALCULATOR
def calculate_obv(df):
    obv = [0]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            obv.append(obv[-1] + df['Volume'].iloc[i])
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            obv.append(obv[-1] - df['Volume'].iloc[i])
        else:
            obv.append(obv[-1])
    df['OBV'] = obv
    return df

# 3. SQUEEZE FINDER
def find_obv_squeeze(df):
    if len(df) < 100:
        return None

    df = calculate_obv(df)
    df['OBV_20MA'] = df['OBV'].rolling(20).mean()
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()

    today = df.iloc[-1]
    week_ago = df.iloc[-6]

    # CONDITION 1: BASE CHECK - 60 din range
    lookback = df.iloc[-60:]
    base_high = lookback['High'].max()
    base_low = lookback['Low'].min()
    base_range_pct = (base_high - base_low) / base_low * 100

    if base_range_pct > 18 or base_range_pct < 3:
        return None # Base nahi hai

    # CONDITION 2: OBV SQUEEZE - Cross ke kareeb
    if pd.isna(today['OBV_20MA']) or today['OBV_20MA'] == 0:
        return None

    obv_vs_ma = (today['OBV'] / today['OBV_20MA'] - 1) * 100
    if not (-5 <= obv_vs_ma <= 2): # -5% niche se +2% upar tak
        return None

    # CONDITION 3: OBV UPAR MUD RAHA
    if today['OBV'] <= week_ago['OBV']:
        return None

    # CONDITION 4: PRICE BASE KE TOP PE
    if today['Close'] < base_high * 0.95:
        return None

    # CONDITION 5: VOLUME JAGRAHA
    vol_ratio = today['Volume'] / today['Vol_20MA']
    if vol_ratio < 1.2:
        return None

    return {
        'date': today.name.strftime('%Y-%m-%d'),
        'close': round(today['Close'], 2),
        'base_high': round(base_high, 2),
        'base_low': round(base_low, 2),
        'base_range_pct': round(base_range_pct, 1),
        'obv': int(today['OBV']),
        'obv_20ma': int(today['OBV_20MA']),
        'obv_vs_ma_pct': round(obv_vs_ma, 1),
        'vol_ratio': round(vol_ratio, 1),
        'distance_to_bo': round((base_high - today['Close']) / today['Close'] * 100, 1)
    }

# 4. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="6mo", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue

        setup = find_obv_squeeze(df)
        if setup is None:
            print("No Squeeze")
            continue

        print(f" ✅ SQUEEZE | Base:{setup['base_range_pct']}% | OBV:{setup['obv_vs_ma_pct']}% vs MA | Vol:{setup['vol_ratio']}x")
        signals.append({'Stock': stock, **setup})
        time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 5. SHEET UPDATE
try:
    ws_output = sh.worksheet("OBV_Squeeze_Setups")
except:
    ws_output = sh.add_worksheet(title="OBV_Squeeze_Setups", rows=1000, cols=12)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('obv_vs_ma_pct', ascending=False) # Jo sabse close hai cross ke

    def convert_to_native(val):
        if isinstance(val, (np.integer, np.int64)): return int(val)
        elif isinstance(val, (np.floating, np.float64)): return float(val)
        else: return val

    df_out = df_out.applymap(convert_to_native)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {len(signals)} SQUEEZE SETUPS MIL GAYE ===")
    print("Sheet 'OBV_Squeeze_Setups' check kar. Jo top pe hai wo HSCL jaisa hai.")
else:
    ws_output.update('A1', [["No Squeeze Setups Right Now"]])
    print("\n=== DONE: 0 SETUPS - MARKET ME ABHI BASE NAI BAN RAHE ===")
