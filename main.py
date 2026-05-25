import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER V2: UNBROKEN SPRING + LIQUIDITY ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE UTHA LE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | Spring kabhi toota nahi + Liquidity 5Cr")

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
        last_candle = df.iloc[-1]

        # 4. LIQUIDITY CHECK - PEHLE HI KAR LO
        avg_turnover = last_candle['Vol_50'] * last_candle['Close']
        liquidity_ok = avg_turnover > 50000000 and last_candle['Vol_50'] > 100000
        if not liquidity_ok:
            print(f" ❌ {stock}: Liquidity low - {avg_turnover/10000000:.1f}Cr")
            continue

        # 5. SPRING DHOONDO - LAST 90 DIN KA LOWEST
        df_90d = df.tail(90).copy()
        spring_low = df_90d['Low'].min()
        spring_candle = df_90d.loc[df_90d['Low'] == spring_low].iloc[-1]
        spring_date = spring_candle.name
        spring_idx = df.index.get_loc(spring_date)

        # 6. SPRING KE BAAD LOW TOOTI YA NAHI? ← MAIN FILTER
        df_after_spring = df.iloc[spring_idx:] # Spring se aaj tak
        lowest_close_after_spring = df_after_spring['Close'].min()

        if lowest_close_after_spring <= spring_low:
            print(f" ❌ {stock}: Spring FAIL. Low toota: {lowest_close_after_spring:.2f} <= {spring_low:.2f}")
            continue

        # 7. CREEK DHOONDO - SPRING SE PEHLE KA HIGH
        df_before_spring = df.iloc[:spring_idx+1].tail(90)
        if df_before_spring.empty:
            continue
        creek_high = df_before_spring['High'].max()

        # 8. ABHI CREEK BREAK NAHI KIYA HONA CHAHIYE
        if last_candle['Close'] >= creek_high:
            print(f" ❌ {stock}: Pehle hi Creek break: {last_candle['Close']:.2f} >= {creek_high:.2f}")
            continue

        # 9. KITNE DIN PURANA SPRING HAI
        days_ago = len(df) - spring_idx - 1
        if days_ago > 90: # 90 din se zyada purana ignore
            print(f" ❌ {stock}: Spring bahut purana {days_ago} din")
            continue

        spring_vol_dry = spring_candle['Volume'] < spring_candle['Vol_50'] * 0.8
        spring_strength = 'STRONG' if spring_vol_dry else 'WEAK'

        signals.append({
            'Stock': stock,
            'Ref_Date': date_str,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Lowest_Close_After': round(lowest_close_after_spring, 2),
            'Spring_Strength': spring_strength,
            'Trading_Days_Ago': days_ago,
            'Creek_High': round(creek_high, 2),
            'CMP': round(last_candle['Close'], 2),
            'Distance_To_Creek_%': round((creek_high - last_candle['Close'])/last_candle['Close']*100, 1),
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ {stock}: Spring {days_ago} din pehle | Unbroken | Creek {creek_high:.2f}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 10. SHEET UPDATE
try:
    ws_output = sh.worksheet("SpringSetups")
    print("SpringSetups tab mil gayi")
except gspread.exceptions.WorksheetNotFound:
    print("SpringSetups tab nahi mili, nayi bana raha hu...")
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals).sort_values('Trading_Days_Ago')
    ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"\n=== SCAN COMPLETE: {len(signals)} UNBROKEN SPRING SETUPS FOUND ===")
else:
    ws_output.update([["Ref_Date", "Status"], [date_str, "No Unbroken Spring found"]])
    print("\n=== SCAN COMPLETE: 0 SETUPS FOUND ===")
