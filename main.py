import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER: 20 TRADING DAYS + AUTO TAB CREATE ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE UTHA LE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | Spring check: Last 20 TRADING DAYS")

# 3. STOCK LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", start="2023-01-01", end=end_date_str, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 100:
            print(f" ❌ {stock}: Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()

        # 4. LIQUIDITY CHECK
        last_candle = df.iloc[-1]
        avg_turnover = last_candle['Vol_50'] * last_candle['Close']
        liquidity_ok = avg_turnover > 50000000 and last_candle['Vol_50'] > 100000
        if not liquidity_ok:
            print(f" ❌ {stock}: Liquidity low - {avg_turnover/10000000:.1f}Cr")
            continue

        # 5. SPRING DHOONDO
        df_90d = df.tail(90).copy()
        spring_low = df_90d['Low'].min()
        spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
        spring_date = spring_candle.name

        # 6. KYA SPRING LAST 20 TRADING DAYS ME BANA?
        df_last_20 = df.tail(20)
        if spring_date not in df_last_20.index:
            print(f" ❌ {stock}: Spring 20 trading days ke bahar")
            continue

        days_ago = len(df) - df.index.get_loc(spring_date) - 1
        spring_vol_dry = spring_candle['Volume'] < spring_candle['Vol_50'] * 0.8
        spring_strength = 'STRONG' if spring_vol_dry else 'WEAK'

        # 7. CREEK DHOONDO
        spring_idx = df.index.get_loc(spring_date)
        df_before_spring = df.iloc[:spring_idx+1].tail(90)
        if df_before_spring.empty:
            continue
        creek_high = df_before_spring['High'].max()

        signals.append({
            'Stock': stock,
            'Ref_Date': date_str,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Spring_Strength': spring_strength,
            'Trading_Days_Ago': days_ago,
            'Creek_High': round(creek_high, 2),
            'Close_on_RefDate': round(last_candle['Close'], 2),
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ {stock}: Spring {days_ago} trading days pehle | {spring_strength}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 8. SHEET UPDATE - AUTO CREATE WALA LOGIC ← YAHAN DHYAN DE
try:
    # Pehle tab dhundne ki koshish kar
    ws_output = sh.worksheet("SpringSetups")
    print("SpringSetups tab mil gayi")
except gspread.exceptions.WorksheetNotFound:
    # Nahi mili to bana de
    print("SpringSetups tab nahi mili, nayi bana raha hu...")
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=20)

# Ab data daal
ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals).sort_values('Trading_Days_Ago')
    ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"\n=== SCAN COMPLETE: {len(signals)} SPRING SETUPS FOUND ===")
else:
    ws_output.update([["Ref_Date", "Status"], [date_str, "No Spring found in last 20 trading days"]])
    print("\n=== SCAN COMPLETE: 0 SETUPS FOUND ===")
