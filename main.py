import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER: SPRING + DRY UP SCANNER START ===")

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y").strftime('%Y-%m-%d')
print(f"Backtest Date: {end_date} | Day: {datetime.strptime(end_date, '%Y-%m-%d').strftime('%A')}")

stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start="2023-01-01", end=end_date, interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 60: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df['IsGreen'] = df['Close'] > df['Open']

        bo_candle = df.iloc[-1] # Backtest date
        df_past = df.iloc[:-1] # BO se pehle ka data

        # FIX: Pehle Spring dhoondo, phir Creek
        df_90d = df_past.iloc[-90:].copy() # 90 din me dhoondo
        spring_low = df_90d['Low'].min()
        spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
        spring_idx = df_90d.index.get_loc(spring_candle.name)

        # Creek = Spring se pehle ka highest high
        df_before_spring = df_90d.iloc[:spring_idx+1]
        creek_high = df_before_spring['High'].max()

        print(f"DEBUG: Creek={creek_high:.2f} | Spring={spring_low:.2f} on {spring_candle.name.date()} | BO Close={bo_candle['Close']:.2f}")

        is_spring = spring_candle['IsGreen']
        vol_condition = bo_candle['Volume'] < bo_candle['Vol_50'] * 1.1 # 10% relaxation
        breakout = bo_candle['Close'] > creek_high

        if is_spring and vol_condition and breakout:
            signals.append({
                'Stock': stock, 'Status': 'READY',
                'SpringLow': round(spring_low, 2), 'CreekHigh': round(creek_high, 2),
                'Close': round(bo_candle['Close'], 2),
                'Volume': int(bo_candle['Volume']), 'Vol_50': int(bo_candle['Vol_50'])
            })
            print(f"[PASS] ✅ {stock}: READY")
        else:
            reason = []
            if not is_spring: reason.append("Spring red")
            if not vol_condition: reason.append(f"Vol high: {int(bo_candle['Volume'])} > {int(bo_candle['Vol_50']*1.1)}")
            if not breakout: reason.append(f"BO nahi: {bo_candle['Close']:.2f} < {creek_high:.2f}")
            print(f" ❌ {stock}: {', '.join(reason)}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

try:
    ws_output = sh.worksheet("LiveSignals")
    ws_output.clear()
    if signals:
        df_out = pd.DataFrame(signals)
        ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"\n=== SCAN COMPLETE: {len(signals)} SIGNALS FOUND ===")
    else:
        ws_output.update([["No READY signals found on this date"]])
        print("\n=== SCAN COMPLETE: 0 SIGNALS FOUND ===")
except Exception as e:
    print(f"Sheet Update Error: {e}")
