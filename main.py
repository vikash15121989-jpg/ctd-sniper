import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER: A1 DATE SE 20 DIN PEECHE ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE UTHA LE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | Spring check karega: {(end_date - timedelta(days=20)).strftime('%d/%m/%Y')} se {date_str} tak")

# 3. STOCK LIST - COLUMN A SE A2 se neeche
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        # 4. DATA DOWNLOAD - A1 ki date tak ka data
        df = yf.download(f"{stock}.NS", start="2023-01-01", end=end_date_str, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 100:
            print(f" ❌ {stock}: Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()

        # 5. LIQUIDITY CHECK - A1 ki date pe
        last_candle = df.iloc[-1] # Ye A1 wali date ki candle hai
        avg_turnover = last_candle['Vol_50'] * last_candle['Close']
        liquidity_ok = avg_turnover > 50000000 and last_candle['Vol_50'] > 100000 # 5Cr + 1Lakh shares
        if not liquidity_ok:
            print(f" ❌ {stock}: Liquidity low - {avg_turnover/10000000:.1f}Cr")
            continue

        # 6. SPRING DHOONDO - Pichle 90 din ka Lowest Low
        df_90d = df.tail(90).copy()
        spring_low = df_90d['Low'].min()
        spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
        spring_date = spring_candle.name

        # 7. KYA SPRING A1 DATE SE PICHLE 20 DIN ME BANA?
        days_diff = (end_date - spring_date).days
        if days_diff > 20 or days_diff < 0:
            print(f" ❌ {stock}: Spring {spring_date.strftime('%d/%m/%Y')} ko bana - Range ke bahar")
            continue

        # 8. VOLUME DRYNESS CHECK - Spring wale din
        spring_vol_dry = spring_candle['Volume'] < spring_candle['Vol_50'] * 0.8
        spring_strength = 'STRONG' if spring_vol_dry else 'WEAK'

        # 9. CREEK DHOONDO - Spring se pehle ka Highest High
        spring_idx = df.index.get_loc(spring_date)
        df_before_spring = df.iloc[:spring_idx+1].tail(90)
        if df_before_spring.empty:
            print(f" ❌ {stock}: Spring se pehle data nahi")
            continue
        creek_high = df_before_spring['High'].max()
        creek_date = df_before_spring['High'].idxmax()

        signals.append({
            'Stock': stock,
            'Ref_Date': date_str,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Spring_Strength': spring_strength,
            'Spring_Vol_%': round((spring_candle['Volume']/spring_candle['Vol_50'])*100, 0),
            'Creek_Date': creek_date.strftime('%d/%m/%Y'),
            'Creek_High': round(creek_high, 2),
            'Close_on_RefDate': round(last_candle['Close'], 2),
            'Days_Ago': days_diff,
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ {stock}: Spring {days_diff} din pehle | {spring_strength} | Creek={creek_high:.2f}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 10. SHEET UPDATE KARO
try:
    ws_output = sh.worksheet("SpringSetups")
    ws_output.clear()
    if signals:
        df_out = pd.DataFrame(signals).sort_values('Days_Ago')
        ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
        print(f"\n=== SCAN COMPLETE: {len(signals)} SPRING SETUPS FOUND ===")
    else:
        ws_output.update([["No Spring found in last 20 days from " + date_str]])
        print("\n=== SCAN COMPLETE: 0 SETUPS FOUND ===")
except Exception as e:
    print(f"Sheet Update Error: {e}")
