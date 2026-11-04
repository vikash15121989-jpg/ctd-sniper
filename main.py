import numpy as np
import pandas as pd
import yfinance as yf

# Paracables ka monthly data download karein
ticker = "PARACABLES.NS"
df = yf.download(ticker, start="2012-01-01", interval="1mo", auto_adjust=False)

if isinstance(df.columns, pd.MultiIndex):
  df.columns = df.columns.get_level_values(0)

df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
highs = df["High"].values
n = len(df)

min_consolidation_months = 3
swing_points = []

# Code ka exact logic jo swing high detect karta hai
for i in range(min_consolidation_months, n - min_consolidation_months):
  current_high = highs[i]

  # Check pichle 3 mahine ka high
  is_higher_than_left = True
  for k in range(i - min_consolidation_months, i):
    if current_high <= highs[k]:
      is_higher_than_left = False
      break

  # Check agle 3 mahine ka high
  is_higher_than_right = True
  for k in range(i + 1, i + min_consolidation_months + 1):
    if current_high <= highs[k]:
      is_higher_than_right = False
      break

  if is_higher_than_left and is_higher_than_right:
    date_str = df.index[i].strftime("%Y-%m-%d")
    swing_points.append({"Date": date_str, "High_Price": current_high})

# Results print karein
res_df = pd.DataFrame(swing_points)
print(f"=== PARACABLES DETECTED SWING HIGHS ({len(res_df)} points) ===")
print(res_df)
