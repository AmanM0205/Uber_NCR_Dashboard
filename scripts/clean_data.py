"""Clean the Uber NCR 2024 ride bookings dataset for the Tableau dashboard.

Input : data/raw/ncr_ride_bookings.csv   (150,000 bookings, Kaggle / GitHub mirror)
Output: data/clean/uber_rides_clean.csv

Run from the project root:  python scripts/clean_data.py
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "ncr_ride_bookings.csv"
OUT = ROOT / "data" / "clean" / "uber_rides_clean.csv"


def time_slot(hour: int) -> str:
    if 5 <= hour < 9:
        return "1 Early Morning (5-9)"
    if 9 <= hour < 12:
        return "2 Morning (9-12)"
    if 12 <= hour < 17:
        return "3 Afternoon (12-17)"
    if 17 <= hour < 21:
        return "4 Evening (17-21)"
    return "5 Night (21-5)"


def main() -> None:
    df = pd.read_csv(RAW, na_values=["null"])

    # IDs are wrapped in literal quotes ("""CNR5884300""") in the source file.
    for col in ["Booking ID", "Customer ID"]:
        df[col] = df[col].str.strip('"')

    # Booking IDs are not unique (1,233 collisions between genuinely different
    # trips), so every row gets its own key.
    df.insert(0, "Ride Key", range(1, len(df) + 1))

    ts = pd.to_datetime(df["Date"] + " " + df["Time"])
    df["Booking Datetime"] = ts.dt.strftime("%Y-%m-%d %H:%M:%S")
    df["Month Num"] = ts.dt.month
    df["Month"] = ts.dt.strftime("%m-%b")          # sortable label, e.g. 03-Mar
    df["Weekday Num"] = ts.dt.dayofweek + 1         # 1 = Monday
    df["Weekday"] = ts.dt.strftime("%a")
    df["Hour"] = ts.dt.hour
    df["Time Slot"] = ts.dt.hour.map(time_slot)
    df["Is Weekend"] = (ts.dt.dayofweek >= 5).map({True: "Weekend", False: "Weekday"})

    # One flag per outcome so Tableau can SUM them for rates.
    status = df["Booking Status"]
    df["Ride Count"] = 1
    df["Is Completed"] = (status == "Completed").astype(int)
    df["Is Cancelled Customer"] = (status == "Cancelled by Customer").astype(int)
    df["Is Cancelled Driver"] = (status == "Cancelled by Driver").astype(int)
    df["Is No Driver"] = (status == "No Driver Found").astype(int)
    df["Is Incomplete"] = (status == "Incomplete").astype(int)

    # Revenue counts only completed rides; incomplete rides also carry a fare.
    df["Revenue"] = df["Booking Value"].where(status == "Completed", 0).fillna(0)

    df["Failure Reason"] = (
        df["Reason for cancelling by Customer"]
        .fillna(df["Driver Cancellation Reason"])
        .fillna(df["Incomplete Rides Reason"])
        .fillna(status.where(status == "No Driver Found"))
    )

    df["Distance Band"] = pd.cut(
        df["Ride Distance"],
        bins=[0, 5, 10, 20, 35, 50],
        labels=["0-5 km", "5-10 km", "10-20 km", "20-35 km", "35-50 km"],
    ).astype("string")

    df["Payment Method"] = df["Payment Method"].fillna("Not Paid")

    df = df.drop(columns=[
        "Cancelled Rides by Customer", "Cancelled Rides by Driver", "Incomplete Rides",
    ])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"Wrote {len(df):,} rows x {df.shape[1]} cols -> {OUT}")


if __name__ == "__main__":
    main()
