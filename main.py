from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf

# IST Timezone setup
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(f"=== [GNFC DEDICATED DEBUG] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

symbol = "GNFC.NS"
stock_clean = "GNFC"

data = yf.Ticker(symbol).history(period="2y", interval="1d")
if data.empty:
    print("❌ No data found for GNFC")
else:
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    # 1. Daily Liquidity Check (Last 20 sessions avg turnover)
    data['Turnover'] = data['Close'] * data['Volume']
    recent_daily_turnover = data['Turnover'].iloc[-20:].mean()
    print(f"📊 Recent 20-Day Avg Daily Turnover: ₹{recent_daily_turnover / 10000000:.2f} Cr")

    # Resample to Monthly Candles
    monthly = data.resample('ME').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()

    print("\n📅 Last 6 Monthly Candles Data:")
    print(monthly.tail(6))

    # Exclude current running month
    current_date = datetime.now(IST)
    first_day_current_month = current_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    completed_monthly = monthly[monthly.index < first_day_current_month]

    if len(completed_monthly) >= 5:
        setup_month = completed_monthly.iloc[-1]
        prev_month = completed_monthly.iloc[-2]
        prev_4_vols = completed_monthly['Volume'].iloc[-5:-1]

        print(f"\n--- RULE EVALUATION FOR GNFC ---")
        print(f"Setup Month (Completed): {setup_month.name.strftime('%Y-%m')}")
        print(f"Previous Month (Completed): {prev_month.name.strftime('%Y-%m')}")
        
        setup_close = float(setup_month['Close'])
        prev_high = float(prev_month['High'])
        cond_close = setup_close > prev_high
        print(f"1. Setup Close ({setup_close}) > Prev Month High ({prev_high})? --> {cond_close}")

        setup_vol = float(setup_month['Volume'])
        max_prev_vol = float(prev_4_vols.max())
        cond_vol = setup_vol > max_prev_vol
        print(f"2. Setup Volume ({setup_vol}) > Max Vol of Prev 4 Months ({max_prev_vol})? --> {cond_vol}")
        
        if cond_close and cond_vol:
            print("\n✅ RESULT: GNFC STRICTLY PASSES ALL RULES!")
        else:
            print("\n❌ RESULT: GNFC FAILS THE RULES.")
            
