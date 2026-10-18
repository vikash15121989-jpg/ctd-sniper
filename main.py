import warnings
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

def run_historical_backtest(stock_tickers, index_ticker="^NSEI", start_date="2024-01-01", end_date="2026-10-01"):
    print(f"Downloading Benchmark Index ({index_ticker}) from {start_date} to {end_date}...")
    nifty_df = yf.download(index_ticker, start=start_date, end=end_date, progress=False)
    if isinstance(nifty_df.columns, pd.MultiIndex):
        nifty_df = nifty_df.xs(index_ticker, level=1, axis=1)

    trades = []

    for ticker in stock_tickers:
        formatted_ticker = ticker if ticker.endswith(".NS") else f"{ticker}.NS"
        print(f"Backtesting historical data for: {ticker}...")

        try:
            # CRITICAL: auto_adjust=False for unadjusted raw prices matching charts
            df = yf.download(formatted_ticker, start=start_date, end=end_date, auto_adjust=False, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            if df.empty or len(df) < 150:
                continue

            # Daily Technical Indicators
            df['20_EMA'] = df['Close'].ewm(span=20, adjust=False).mean()

            # Resample to Monthly Candles using raw prices
            monthly = df.resample('ME').agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            }).dropna()

            if len(monthly) < 6:
                continue

            # Align with Nifty for Mansfield RS Calculation
            combined = pd.DataFrame({
                'Stock': df['Close'],
                'Nifty': nifty_df['Close']
            }).dropna()
            rel_ratio = combined['Stock'] / combined['Nifty']
            base_ma = rel_ratio.rolling(window=50).mean()
            df['RS'] = ((rel_ratio / base_ma) - 1) * 100

            # Loop through past monthly candles to find historical setups
            for m_idx in range(5, len(monthly) - 1):
                setup_month = monthly.iloc[m_idx]
                prev_month = monthly.iloc[m_idx - 1]
                prev_4_vols = monthly['Volume'].iloc[m_idx - 5 : m_idx]

                cond_close_above_high = setup_month['Close'] > prev_month['High']
                cond_volume_surge = setup_month['Volume'] > prev_4_vols.max()

                m_date = setup_month.name
                if m_date not in df.index:
                    continue

                sub_df = df.loc[:m_date]
                if len(sub_df) < 20:
                    continue

                latest_rs = sub_df['RS'].iloc[-1]
                recent_lows = sub_df['Low'].iloc[-10:]
                recent_emas = sub_df['20_EMA'].iloc[-10:]
                had_shakeout = any(recent_lows <= recent_emas * 1.01)

                # STRICT BACKTEST CONDITIONS
                if cond_close_above_high and cond_volume_surge and (latest_rs > 0) and had_shakeout:
                    # Find next day entry
                    future_days = df.loc[df.index > m_date]
                    if len(future_days) == 0:
                        continue

                    entry_date = future_days.index[0]
                    entry_price = df.loc[entry_date, 'Open']

                    # Stoploss & Targets Calculation
                    shakeout_sl = min(setup_month['Low'], sub_df['20_EMA'].iloc[-1] * 0.98)
                    stop_loss = round(max(shakeout_sl, entry_price * 0.95), 2)
                    target_6_pct = round(entry_price * 1.06, 2)
                    target_10_pct = round(entry_price * 1.10, 2)

                    # Forward Simulation (Max 30 trading days hold)
                    future_df = df.loc[entry_date:].iloc[1:31]
                    if future_df.empty:
                        continue

                    trade_result = "TIME_EXIT"
                    exit_price = future_df['Close'].iloc[-1]
                    exit_reason = "Time Exit"

                    for f_idx in range(len(future_df)):
                        day_low = future_df['Low'].iloc[f_idx]
                        day_high = future_df['High'].iloc[f_idx]

                        if day_low <= stop_loss:
                            trade_result = "STOP_LOSS"
                            exit_price = stop_loss
                            exit_reason = "SL Hit"
                            break
                        elif day_high >= target_10_pct:
                            trade_result = "WIN_10%"
                            exit_price = target_10_pct
                            exit_reason = "10% Target Hit"
                            break
                        elif day_high >= target_6_pct:
                            trade_result = "WIN_6%"
                            exit_price = target_6_pct
                            exit_reason = "6% Target Hit"
                            break

                    pnl_pct = round(((exit_price - entry_price) / entry_price) * 100, 2)

                    trades.append({
                        "Ticker": ticker,
                        "Entry_Date": entry_date.strftime('%Y-%m-%d'),
                        "Entry_Price": round(entry_price, 2),
                        "Exit_Price": round(exit_price, 2),
                        "Result": exit_reason,
                        "PnL_Pct": pnl_pct
                    })

        except Exception as e:
            continue

    return pd.DataFrame(trades)

# --- EXECUTION ---
if __name__ == "__main__":
    backtest_universe = ["POLYCAB", "TRENT", "BEL", "HAL", "PERSISTENT", "DIXON", "MCX", "VOLTAS", "CUMMINSIND", "KEI", "TATASTEEL", "RELIANCE", "BHEL", "COFORGE"]

    results = run_historical_backtest(backtest_universe, index_ticker="^NSEI", start_date="2024-01-01", end_date="2026-10-01")

    if not results.empty:
        total_trades = len(results)
        winning_trades = len(results[results['PnL_Pct'] > 0])
        losing_trades = len(results[results['PnL_Pct'] <= 0])
        win_rate = round((winning_trades / total_trades) * 100, 2)
        total_return = round(results['PnL_Pct'].sum(), 2)
        avg_pnl = round(results['PnL_Pct'].mean(), 2)

        print("\n" + "=" * 60)
        print("          HISTORICAL BACKTEST RESULTS (RS + SHAKEOUT)")
        print("=" * 60)
        print(f"Total Trades Taken      : {total_trades}")
        print(f"Winning Trades          : {winning_trades}")
        print(f"Losing Trades           : {losing_trades}")
        print(f"Overall Win Rate (%)    : {win_rate}%")
        print(f"Total Cumulative PnL (%) : {total_return}%")
        print(f"Avg Return per Trade    : {avg_pnl}%")
        print("=" * 60)
        print("\nDetailed Trade Log:")
        print(results.to_string(index=False))
    else:
        print("No historical trades matched the strict criteria.")
        
