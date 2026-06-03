import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== ZIGZAG WYCKOFF SCANNER ===")

# 1. ZIGZAG FUNCTION - SWING POINTS NIKALNE KA
def get_zigzag_swings(df, pct=3.0):
    """ZigZag swing high/low nikalta hai. pct = 3% default"""
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values

    swing_highs = [] # (index, price)
    swing_lows = [] # (index, price)

    last_high_idx = 0
    last_low_idx = 0
    trend = 0 # 0=none, 1=up, -1=down

    for i in range(1, len(df)):
        # Current swing se kitna move hua
        if trend >= 0: # Up trend ya start
            if closes[i] > highs[last_high_idx]:
                last_high_idx = i
            elif (highs[last_high_idx] - lows[i]) / highs[last_high_idx] * 100 >= pct:
                swing_highs.append((last_high_idx, highs[last_high_idx]))
                trend = -1
                last_low_idx = i
        else: # Down trend
            if closes[i] < lows[last_low_idx]:
                last_low_idx = i
            elif (highs[i] - lows[last_low_idx]) / lows[last_low_idx] * 100 >= pct:
                swing_lows.append((last_low_idx, lows[last_low_idx]))
                trend = 1
                last_high_idx = i

    # Last point add karo
    if trend >= 0:
        swing_highs.append((last_high_idx, highs[last_high_idx]))
    else:
        swing_lows.append((last_low_idx, lows[last_low_idx]))

    return swing_highs, swing_lows

# 2. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 3. DATE SETUP
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
try:
    ref_date = datetime.strptime(date_raw, '%Y-%m-%d')
except ValueError:
    ref_date = datetime.strptime(date_raw, '%d/%m/%Y')

end_date = ref_date
start_date = ref_date - timedelta(days=60)
print(f"Backtest Period: {start_date.date()} to {end_date.date()}")

# 4. SCAN ALL STOCKS
stocks = [s.strip().upper() for s in ws_watchlist.col_values(1)[1:] if s.strip()]
all_signals = []

for i, stock in enumerate(stocks):
    try:
        df = yf.download(f"{stock}.NS", start=start_date - timedelta(days=150),
                         end=end_date + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue
        df = df[df.index <= end_date]

        # ===== ZIGZAG SWING NIKALO =====
        swing_highs, swing_lows = get_zigzag_swings(df, pct=3.0)

        # Minimum 2 swing high + 2 swing low chahiye
        if len(swing_highs) < 2 or len(swing_lows) < 2: continue

        # ===== HAR DIN CHECK KARO =====
        for j in range(50, len(df)):
            today = df.iloc[j]
            signal_date = today.name

            # ===== STAGE 1: HH-HL STRUCTURE CHECK KARO =====
            # Aaj se pehle ke last 2 swing high/low lo
            past_sh = [sh for sh in swing_highs if sh[0] < j]
            past_sl = [sl for sl in swing_lows if sl[0] < j]

            if len(past_sh) < 2 or len(past_sl) < 2: continue

            sh1_idx, sh1_price = past_sh[-2] # Pehla swing high
            sh2_idx, sh2_price = past_sh[-1] # Dusra swing high

            sl1_idx, sl1_price = past_sl[-2] # Pehla swing low
            sl2_idx, sl2_price = past_sl[-1] # Dusra swing low

            # Higher High: Dusra swing high > Pehla swing high
            if sh2_price <= sh1_price: continue

            # Higher Low: Dusra swing low > Pehla swing low
            if sl2_price <= sl1_price: continue

            # Structure confirm hua - Ab footprint check

            # ===== STAGE 2: FOOTPRINT - TERE RULE =====
            prev_10 = df.iloc[j-10:j]
            if len(prev_10) < 10: continue

            ten_day_high = prev_10['High'].max()
            ten_day_max_vol = prev_10['Volume'].max()

            # Condition 1: Aaj ka Volume > Pichle 10 din ka Max Volume
            if today['Volume'] <= ten_day_max_vol: continue

            # Condition 2: Aaj ka High < Pichle 10 din ka Max High
            if today['High'] >= ten_day_high: continue

            # ===== STAGE 3: LIQUIDITY CHECK =====
            avg_vol_20 = df['Volume'].iloc[j-20:j].mean()
            if avg_vol_20 < 150000: continue
            vol_multiple = today['Volume'] / avg_vol_20

            # ===== SAB PASS ✅ SIGNAL BANAO =====
            hh_pct = round((sh2_price/sh1_price - 1) * 100, 1)
            hl_pct = round((sl2_price/sl1_price - 1) * 100, 1)

            # Creek = 10 din ka high jo toda nahi
            creek = ten_day_high
            entry = creek * 1.001

            # 15 DIN AAGE RESULT
            future_data = df.iloc[j+1:j+16]
            status = "INTACT"
            max_profit = 0

            if len(future_data) > 0:
                max_high_15d = future_data['High'].max()
                if max_high_15d > entry:
                    max_profit = round((max_high_15d - entry) / entry * 100, 1)
                    if max_profit >= 6.0: status = "BREAKOUT"
                    elif max_profit >= 3.0: status = "BREAKOUT_WEAK"
                    else: status = "BREAKOUT_SMALL"

            all_signals.append({
                'Date': signal_date.strftime('%Y-%m-%d'),
                'Stock': stock,
                'Close': round(today['Close'], 2),
                'Creek': round(creek, 2),
                'HH%': hh_pct,
                'HL%': hl_pct,
                'SH1': round(sh1_price, 2),
                'SH2': round(sh2_price, 2),
                'SL1': round(sl1_price, 2),
                'SL2': round(sl2_price, 2),
                'Vol': f"{vol_multiple:.1f}x",
                'Status': status,
                'Max%': max_profit
            })
            print(f"💎 {signal_date.date()} | {stock} | HH:{hh_pct}% HL:{hl_pct}% | Vol:{vol_multiple:.1f}x | {status} {max_profit}%")

        time.sleep(0.05)
    except Exception as e:
        print(f"Error {stock}: {str(e)[:40]}")
        pass

# 5. OUTPUT TO SHEET
try:
    ws_output = sh.worksheet("WyckoffSignals")
except:
    ws_output = sh.add_worksheet(title="WyckoffSignals", rows=2000, cols=20)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    total = len(df_out)
    breakout = len(df_out[df_out['Status'] == 'BREAKOUT'])

    summary = [
        ["ZIGZAG WYCKOFF SIGNALS", ""],
        ["Logic: ZigZag HH+HL + Vol>10D MaxVol + High<10D High", ""],
        ["Period", f"{start_date.date()} to {end_date.date()}"],
        ["Total Signals", total],
        ["Breakout 6%+", breakout],
        ["Success Rate 6%+", f"{round(breakout/total*100,1)}%" if total > 0 else "0%"],
        ["", ""],
    ]
    payload = summary + [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {total} SIGNALS | {breakout} BREAKOUT 6%+ ===")
else:
    ws_output.update('A1', [["Status", "No ZigZag Signals Found"]])
    print("\n=== DONE: 0 SIGNALS ===")
