import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER FINAL: TESTED & WORKING ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE UTHA LE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
end_date = datetime.strptime(date_str, "%d/%m/%Y")
end_date_str = end_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str}")

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
            print(f" ❌ Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]

        # 4. LIQUIDITY CHECK
        avg_turnover = last_candle['Vol_50'] * last_candle['Close']
        if avg_turnover < 50000000 or last_candle['Vol_50'] < 100000:
            print(f" ❌ Liquidity low - {avg_turnover/10000000:.1f}Cr")
            continue

        # 5. SPRING DHOONDO - MINIMUM 2 DIN PURANA HONA CHAHIYE
        df_90d = df.tail(92).copy() # 92 isliye ki last 2 din chhod ke 90 check karenge
        if len(df_90d) < 92:
            print(f" ❌ 90 din ka data nahi")
            continue
            
        df_check = df_90d.iloc[:-2] # Aaj aur kal ko hata diya. Ab 90 din bache
        df_check_rev = df_check.iloc[::-1] # Ulta kar diya
        
        spring_low = None
        spring_date = None
        spring_candle = None
        
        for idx, row in df_check_rev.iterrows():
            current_low = row['Low']
            after_idx = df.index.get_loc(idx) + 1
            df_after = df.iloc[after_idx:]
            
            lowest_close_after = df_after['Close'].min()
            
            if lowest_close_after > current_low:
                spring_low = current_low
                spring_date = idx
                spring_candle = row
                break

        if spring_low is None:
            print(f" ❌ Koi Unbroken Spring nahi mila")
            continue

        spring_idx = df.index.get_loc(spring_date)

        # 6. CREEK DHOONDO - SPRING SE PEHLE KA HIGH
        df_before_spring = df.iloc[:spring_idx]
        if len(df_before_spring) < 20:
            print(f" ❌ Spring se pehle data kam")
            continue
        creek_high = df_before_spring['High'].tail(90).max()

        # 7. CREEK BREAK NAHI HONA CHAHIYE
        if last_candle['Close'] >= creek_high:
            print(f" ❌ Creek break ho gaya: {last_candle['Close']:.2f} >= {creek_high:.2f}")
            continue

        # 8. KITNE DIN PURANA
        days_ago = len(df) - spring_idx - 1
        if days_ago > 120:
            print(f" ❌ Spring bahut purana {days_ago} din")
            continue

        # 9. VOLUME CHECK
        spring_vol_dry = spring_candle['Volume'] < spring_candle['Vol_50'] * 0.8 if pd.notna(spring_candle['Vol_50']) else False
        spring_strength = 'STRONG' if spring_vol_dry else 'WEAK'

        signals.append({
            'Stock': stock,
            'Ref_Date': date_str,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Spring_Strength': spring_strength,
            'Trading_Days_Ago': days_ago,
            'Creek_High': round(creek_high, 2),
            'CMP': round(last_candle['Close'], 2),
            'Distance_To_Creek_%': round((creek_high - last_candle['Close'])/last_candle['Close']*100, 1),
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ Spring {days_ago} din pehle | Creek {creek_high:.2f}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 10. SHEET UPDATE
try:
    ws_output = sh.worksheet("SpringSetups")
except:
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals).sort_values('Trading_Days_Ago')
    ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"\n=== DONE: {len(signals)} SETUPS MIL GAYE ===")
else:
    ws_output.update([["Ref_Date", "Status"], [date_str, "No Setups"]])
    print("\n=== DONE: 0 SETUPS ===")
