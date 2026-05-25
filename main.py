import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER: CREEK = CLOSE BASED SWING HIGH ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE - BAS REFERENCE KE LIYE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
print(f"Reference Date: {date_str}")

# 3. STOCK LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 100:
            print(f" ❌ Data kam hai: {len(df)} rows")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        actual_last_date = df.index[-1].strftime('%d/%m/%Y')

        # 4. LIQUIDITY CHECK
        avg_turnover = last_candle['Vol_50'] * last_candle['Close']
        if avg_turnover < 50000000 or last_candle['Vol_50'] < 100000:
            print(f" ❌ Liquidity low - {avg_turnover/10000000:.1f}Cr")
            continue

        # 5. SPRING DHOONDO - MINIMUM 2 DIN PURANA
        df_check = df.iloc[:-2]
        if len(df_check) < 20: continue
        df_check_rev = df_check.iloc[::-1]

        spring_low = None
        spring_date = None
        spring_candle = None

        for idx, row in df_check_rev.iterrows():
            current_low = row['Low']
            after_idx = df.index.get_loc(idx) + 1
            df_after = df.iloc[after_idx:]
            if df_after.empty: continue
            if df_after['Close'].min() > current_low:
                spring_low = current_low
                spring_date = idx
                spring_candle = row
                break

        if spring_low is None:
            print(f" ❌ Koi Unbroken Spring nahi mila")
            continue

        spring_idx = df.index.get_loc(spring_date)

        # 6. CREEK = CLOSE BASED SWING HIGH - 3 LEFT 3 RIGHT ✅
        df_before_spring = df.iloc[:spring_idx]
        if len(df_before_spring) < 20:
            print(f" ❌ Spring se pehle data kam")
            continue

        # Last 60 din me dhoondo
        df_recent = df_before_spring.tail(60).copy()

        # ← YAHAN BADLAAV: CLOSE BASED SWING HIGH
        # Left ke 3 close aur Right ke 3 close se bada hona chahiye
        df_recent['Max_Left_3'] = df_recent['Close'].shift(1).rolling(3).max()
        df_recent['Max_Right_3'] = df_recent['Close'].shift(-1).rolling(3).max()

        df_recent['IsSwingHigh'] = (
            (df_recent['Close'] > df_recent['Max_Left_3']) &
            (df_recent['Close'] > df_recent['Max_Right_3'])
        )

        swing_highs = df_recent[df_recent['IsSwingHigh']]

        if swing_highs.empty:
            # Agar swing nahi mila to last 60D ka Highest Close
            creek_high = df_recent['Close'].max()
            creek_date = df_recent['Close'].idxmax().strftime('%d/%m/%Y')
            creek_type = 'Max Close Last 60D'
        else:
            # Spring ke sabse najdik wala Swing High
            creek_high = swing_highs['Close'].iloc[-1] # Close use kar rahe hain
            creek_date = swing_highs.index[-1].strftime('%d/%m/%Y')
            creek_type = 'Nearest Close Swing High'

        # 7. CREEK BREAK NAHI HONA CHAHIYE - CLOSE SE CHECK
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
            'Data_Till': actual_last_date,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Spring_Strength': spring_strength,
            'Trading_Days_Ago': days_ago,
            'Creek_High_Close': round(creek_high, 2), # ← Close based hai ab
            'Creek_Date': creek_date,
            'Creek_Type': creek_type,
            'CMP': round(last_candle['Close'], 2),
            'Distance_To_Creek_%': round((creek_high - last_candle['Close'])/last_candle['Close']*100, 1),
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ Spring {days_ago} din pehle | Creek Close {creek_high:.2f} on {creek_date}")

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
