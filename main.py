import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import time
import warnings
warnings.filterwarnings('ignore')

print("=== SWING SNIPER V16.0 - 15-60 DAY HOLDS ===", flush=True)

# 1. SETUP
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

end_date = datetime.now()
start_date = end_date - timedelta(days=600) # 600 din - swing ko data chahiye
lookback_days = 15 # 15 trading days check karo

print(f"Backfill: {lookback_days} trading days till {end_date.date()}", flush=True)

# 2. NIFTY REGIME CHECK - WEEKLY
nifty = yf.download("^NSEI", start=start_date - timedelta(days=400), end=end_date + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)
if nifty.empty or len(nifty) < 250:
    raise ValueError("Nifty data nahi mila")

nifty['200DMA'] = nifty['Close'].rolling(200).mean()
nifty['50DMA'] = nifty['Close'].rolling(50).mean()

close_now = float(nifty['Close'].iloc[-1])
dma200_now = float(nifty['200DMA'].iloc[-1])
dma50_now = float(nifty['50DMA'].iloc[-1])

is_bull = close_now > dma200_now and dma50_now > dma200_now
regime = "BULL" if is_bull else "BEAR"
print(f"Market Regime: {regime}", flush=True)

# 3. SWING RULES V16.0
R = {
    'min_price': 50, 'min_daily_value_cr': 1.0, 'min_vol_shares': 200000, # Swing me liquidity zyada
    # SWING PARAMS
    'swing_lookback': 90, # 90 din me major CHOCH
    'swing_pullback_min': 15.0, # Min 15% pullback from high
    'swing_pullback_max': 35.0, # Max 35% pullback
    'swing_vol_dry_pct': 0.15, # Volume 15% se kam
    'swing_sl_buffer': 0.08, # 8% buffer SL
    'swing_target_r': 3.0, # Min 1:3 RR
    'swing_min_rr_pct': 15.0, # Min 15% target
    'swing_max_risk_pct': 12.0, # Max 12% risk
    'swing_50wma_filter': True, # 50 Week MA filter
    'choch_zone_pct': 5.0, # BOS ke 5% paas - swing wide
}

def add_indicators(df):
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['50DMA'] = df['Close'].rolling(50).mean()
    df['200DMA'] = df['Close'].rolling(200).mean()
    df['Range'] = df['High'] - df['Low']
    # WEEKLY DATA
    df_weekly = df.resample('W-FRI').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'})
    df_weekly['50WMA'] = df_weekly['Close'].rolling(50).mean()
    df_weekly['200WMA'] = df_weekly['Close'].rolling(200).mean()
    df['50WMA'] = df_weekly['50WMA'].reindex(df.index, method='ffill')
    df['200WMA'] = df_weekly['200WMA'].reindex(df.index, method='ffill')
    return df

def check_liquidity(df, idx):
    try:
        close = df['Close'].iloc[idx]
        vol_20ma = df['Vol_20MA'].iloc[idx]
        daily_val = df['Daily_Value_20MA'].iloc[idx]
        if pd.isna(close) or close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

# ===== SWING CHOCH DETECT =====
def detect_swing_choch(df, idx):
    if idx < 120: return False, {}
    lookback = R['swing_lookback']
    recent = df.iloc[idx-lookback:idx]
    if len(recent) < 60: return False, {}

    # 1. Major downtrend: 25%+ drop hona chahiye
    high_90d = recent['High'].max()
    low_90d = recent['Low'].min()
    drop_pct = (high_90d - low_90d) / high_90d * 100
    if drop_pct < 25: return False, {}

    # 2. Last Major Lower High - 10 day swing
    swing_highs = []
    for i in range(10, len(recent)-10):
        if recent['High'].iloc[i] == recent['High'].iloc[i-10:i+11].max():
            swing_highs.append(i)
    if len(swing_highs) < 1: return False, {}

    last_lh_idx = swing_highs[-1]
    last_lh_price = recent['High'].iloc[last_lh_idx]

    # 3. CHOCH: Last LH ko 2% upar close se toda
    choch_idx = None
    for i in range(last_lh_idx + 5, len(recent)):
        if recent['Close'].iloc[i] > last_lh_price * 1.02:
            choch_idx = i
            break
    if choch_idx is None: return False, {}

    # 4. CHOCH ke baad 10%+ rally = Confirmation
    after_choch_high = recent.iloc[choch_idx:]['High'].max()
    if after_choch_high < last_lh_price * 1.10: return False, {}

    return True, {
        'choch_date': recent.index[choch_idx].strftime('%Y-%m-%d'),
        'bos_level': round(last_lh_price, 2),
        'choch_idx': idx - lookback + choch_idx,
        'choch_high': round(after_choch_high, 2),
        'choch_vol': recent['Volume'].iloc[choch_idx:choch_idx+5].mean()
    }

# ===== VALID RETRACEMENT CHECK =====
def check_valid_retracement(df, idx, bos_level):
    if idx < 3: return False, {}
    row = df.iloc[idx]
    prev1 = df.iloc[idx-1]
    prev2 = df.iloc[idx-2]

    # Cond A: Bearish Engulfing
    cond_engulf = False
    if row['Close'] < row['Open']:
        body_prev = abs(prev1['Close'] - prev1['Open'])
        body_curr = abs(row['Close'] - row['Open'])
        if body_curr > body_prev * 0.8:
            if row['Open'] > prev1['Close'] and row['Close'] < prev1['Open']:
                cond_engulf = True

    # Cond B: 2 candle ka low break
    cond_break_2low = False
    if row['Low'] < prev1['Low'] and row['Low'] < prev2['Low']:
        cond_break_2low = True

    if not (cond_engulf or cond_break_2low):
        return False, {}

    # Cond C: Body BOS zone me - wick nahi
    zone_low = bos_level * (1 - R['choch_zone_pct']/100)
    zone_high = bos_level * (1 + R['choch_zone_pct']/100)

    if not (zone_low <= row['Close'] <= zone_high):
        return False, {}

    # Body ka 60% hissa zone me ho
    candle_range = row['High'] - row['Low']
    if candle_range == 0: return False, {}
    body_top = max(row['Open'], row['Close'])
    body_bot = min(row['Open'], row['Close'])
    body_in_zone = min(body_top, zone_high) - max(body_bot, zone_low)
    body_pct_in_zone = body_in_zone / (body_top - body_bot + 0.01) * 100

    if body_pct_in_zone < 60: return False, {}

    return True, {
        'retrace_type': 'ENGULF' if cond_engulf else '2LOW_BREAK',
        'body_in_zone_%': round(body_pct_in_zone, 1)
    }

# ===== SWING PULLBACK CHECK =====
def check_swing_pullback(df, idx, choch_data):
    bos_level = choch_data['bos_level']
    choch_high = choch_data['choch_high']
    choch_vol = choch_data['choch_vol']

    if idx <= choch_data['choch_idx'] + 10: return False, {}
    row = df.iloc[idx]

    # 1. Pullback depth: 15-35%
    pullback_pct = (choch_high - row['Low']) / choch_high * 100
    if not (R['swing_pullback_min'] <= pullback_pct <= R['swing_pullback_max']):
        return False, {}

    # 2. Support: BOS ya 50DMA
    support1 = bos_level
    support2 = df['50DMA'].iloc[idx]
    main_support = max(support1, support2)
    zone_low = main_support * 0.95
    zone_high = main_support * 1.05
    if not (zone_low <= row['Low'] <= zone_high):
        return False, {}

    # 3. Valid Retracement
    is_valid_retrace, retrace_data = check_valid_retracement(df, idx, bos_level)
    if not is_valid_retrace: return False, {}

    # 4. Volume DRY < 15%
    if row['Volume'] > choch_vol * R['swing_vol_dry_pct']:
        return False, {}

    # 5. Bullish reversal candle
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    upper_wick = row['High'] - max(row['Open'], row['Close'])
    is_hammer = lower_wick > body * 2 and upper_wick < body * 0.5
    is_bullish = row['Close'] > row['Open'] and body > row['Range'] * 0.6
    if not (is_hammer or is_bullish): return False, {}

    # 6. 50WMA filter
    if R['swing_50wma_filter'] and row['Close'] < df['50WMA'].iloc[idx]:
        return False, {}

    # 7. Higher Low vs previous
    if idx > 0 and row['Low'] <= df['Low'].iloc[idx-1]: return False, {}

    # 8. SL & Target Calc
    swing_low = df['Low'].iloc[idx-20:idx+1].min()
    sl_price = swing_low * (1 - R['swing_sl_buffer'])
    sl_200dma = df['200DMA'].iloc[idx] * 0.98
    final_sl = min(sl_price, sl_200dma)

    risk = row['Close'] - final_sl
    risk_pct = risk / row['Close'] * 100
    if risk_pct > R['swing_max_risk_pct']: return False, {}

    target_1r = row['Close'] + risk * R['swing_target_r']
    target_choch = choch_high * 1.1
    final_target = min(target_1r, target_choch)
    target_pct = (final_target - row['Close']) / row['Close'] * 100
    if target_pct < R['swing_min_rr_pct']: return False, {}

    return True, {
        'Date': df.index[idx].strftime('%Y-%m-%d'), 'Stock': '',
        'Type': 'SWING_PB', 'BOS_Level': round(bos_level, 2),
        'Entry_Zone': round(row['Low'], 2), 'Entry': round(row['Close'], 2),
        'SL': round(final_sl, 2), 'Target': round(final_target, 2),
        'Risk_%': round(risk_pct, 1), 'Reward_%': round(target_pct, 1),
        'RR': round(target_pct / risk_pct, 1),
        'Pullback_%': round(pullback_pct, 1),
        'Volume_vs_CHC_%': round(row['Volume'] / choch_vol * 100, 1),
        'Retrace_Type': retrace_data['retrace_type'],
        'Body_In_Zone_%': retrace_data['body_in_zone_%'],
        'Hold_Days': '15-60'
    }

# ===== MAIN SCAN =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
swing_list = []

print(f"Scanning {len(stocks)} stocks for {lookback_days} days...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)}", flush=True)

        df = yf.download(f"{stock}.NS", start=start_date, end=end_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 300 or df['Close'].isna().all():
            continue

        df = add_indicators(df)

        for day_offset in range(lookback_days):
            idx = len(df) - 1 - day_offset
            if idx < 300: continue
            if not check_liquidity(df, idx): continue

            # SWING LOGIC
            is_swing_choch, swing_choch_data = detect_swing_choch(df, idx)
            if is_swing_choch:
                is_swing_pb, swing_data = check_swing_pullback(df, idx, swing_choch_data)
                if is_swing_pb:
                    swing_data['Stock'] = stock
                    swing_data['CHOCH_Date'] = swing_choch_data['choch_date']
                    swing_list.append(swing_data)

        time.sleep(0.2)
    except Exception as e:
        continue

# ===== UPDATE SHEET =====
def update_sheet_final(sheet_name, data_list, date_col='Date'):
    try:
        ws = sh.worksheet(sheet_name)
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=5000, cols=25)
    ws.clear()
    if data_list:
        df_out = pd.DataFrame(data_list)
        df_out = df_out.drop_duplicates(subset=['Stock', date_col], keep='last')
        df_out = df_out.sort_values([date_col, 'Stock'], ascending=[False, True])
        payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
        ws.update('A1', payload)
        return len(df_out)
    else:
        ws.update('A1', [[f"No swing setups for last {lookback_days} trading days"]])
        return 0

count = update_sheet_final('SWING_SETUPS', swing_list, 'Date')

print(f"\n=== DONE V16.0 SWING ===", flush=True)
print(f"SWING_SETUPS: {count} - 15-60 day holds", flush=True)
print(f"Market Regime: {regime}", flush=True)
