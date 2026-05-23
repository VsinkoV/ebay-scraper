"""
eBay Sold Listings — Analysis & Graphs
Run: python3 analyse.py
Outputs a multi-page PDF and prints a summary to the terminal.
"""

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
from pathlib import Path
from matplotlib.backends.backend_pdf import PdfPages

CSV_FILE = "sold_listings.csv"
OUTPUT_PDF = "analysis.pdf"
TARGET_PRICE = 50  # £ — shown as a reference line on price charts

sns.set_theme(style="whitegrid", palette="muted")
TITLE = "All Saints Leather Jacket — eBay UK Sold Listings"


# ── Load & clean ─────────────────────────────────────────────────────────────

def remove_outliers(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Remove rows outside 1.5×IQR on the given column."""
    q1 = df[col].quantile(0.25)
    q3 = df[col].quantile(0.75)
    iqr = q3 - q1
    return df[(df[col] >= q1 - 1.5 * iqr) & (df[col] <= q3 + 1.5 * iqr)]


def load(path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (df_clean, df_all) — clean has outliers removed."""
    df = pd.read_csv(path)

    # Drop placeholder rows
    df = df[~df["title"].astype(str).str.contains("Shop on eBay", na=True)].copy()

    # Price → float
    df["price_num"] = (
        df["price"].astype(str)
        .str.replace(r"[£$,]", "", regex=True)
        .str.strip()
        .pipe(pd.to_numeric, errors="coerce")
    )

    # Date sold → datetime
    df["date_dt"] = pd.to_datetime(df["date_sold"], dayfirst=True, errors="coerce")

    # Condition — strip trailing " ·  Size: X" etc.
    df["condition_clean"] = (
        df["condition"].astype(str)
        .str.replace(r"\s*·.*$", "", regex=True)
        .str.strip()
    )

    # days_ago → int
    df["days_ago"] = pd.to_numeric(df["days_ago"], errors="coerce")

    # Shipping bucket
    df["free_shipping"] = df["shipping"].astype(str).str.lower().str.contains("free")

    df = df.dropna(subset=["price_num"])
    df_clean = remove_outliers(df, "price_num")
    return df_clean, df


# ── Helpers ───────────────────────────────────────────────────────────────────

def target_line(ax: plt.Axes, orientation: str = "h"):
    """Draw a TARGET_PRICE reference line."""
    if orientation == "h":
        ax.axhline(TARGET_PRICE, color="gold", linewidth=1.8, linestyle="--",
                   label=f"Target £{TARGET_PRICE}")
    else:
        ax.axvline(TARGET_PRICE, color="gold", linewidth=1.8, linestyle="--",
                   label=f"Target £{TARGET_PRICE}")


# ── Plots ─────────────────────────────────────────────────────────────────────

def plot_price_distribution(df: pd.DataFrame, ax: plt.Axes):
    sns.histplot(df["price_num"], bins=30, kde=True, ax=ax, color="steelblue")
    median = df["price_num"].median()
    ax.axvline(median, color="tomato", linestyle="--", linewidth=1.5,
               label=f"Median £{median:.0f}")
    target_line(ax, "v")
    ax.set_title("Price Distribution (outliers removed)")
    ax.set_xlabel("Sale Price (£)")
    ax.set_ylabel("Count")
    ax.legend()


def plot_price_by_condition(df: pd.DataFrame, ax: plt.Axes):
    order = (df.groupby("condition_clean")["price_num"]
               .median().sort_values(ascending=False).index)
    sns.boxplot(data=df, x="condition_clean", y="price_num",
                order=order, ax=ax, palette="Set2")
    target_line(ax, "h")
    ax.set_title("Price by Condition (outliers removed)")
    ax.set_xlabel("Condition")
    ax.set_ylabel("Sale Price (£)")
    ax.tick_params(axis="x", rotation=20)
    ax.legend(fontsize=8)


def plot_sales_over_time(df: pd.DataFrame, ax: plt.Axes):
    daily = (df.dropna(subset=["date_dt"])
               .set_index("date_dt")["price_num"]
               .resample("D")
               .agg(["count", "median"]))
    ax2 = ax.twinx()
    ax.bar(daily.index, daily["count"], color="lightsteelblue",
           label="# Sales", width=0.8)
    ax2.plot(daily.index, daily["median"], color="tomato",
             linewidth=2, label="Median Price (£)")
    ax2.axhline(TARGET_PRICE, color="gold", linewidth=1.8, linestyle="--",
                label=f"Target £{TARGET_PRICE}")
    ax.set_title("Sales Volume & Median Price Over Time")
    ax.set_xlabel("Date")
    ax.set_ylabel("Number of Sales")
    ax2.set_ylabel("Median Price (£)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30)
    lines = ax.get_legend_handles_labels()
    lines2 = ax2.get_legend_handles_labels()
    ax.legend(lines[0] + lines2[0], lines[1] + lines2[1], loc="upper left", fontsize=8)


def plot_sales_recency(df: pd.DataFrame, ax: plt.Axes):
    """
    Bar chart of number of sales per week bucket (days_ago).
    Proxy for stock turnover speed — how quickly listings are selling.
    Note: true 'time to sell' (listing date → sold date) requires
    scraping individual item pages.
    """
    plot_df = df.dropna(subset=["days_ago"]).copy()
    plot_df["week_bucket"] = (plot_df["days_ago"] // 7).astype(int)
    counts = plot_df["week_bucket"].value_counts().sort_index()
    # Label as "0–7d", "8–14d" etc.
    labels = [f"{w*7}–{w*7+6}d" for w in counts.index]
    ax.bar(range(len(counts)), counts.values, color="mediumseagreen")
    ax.set_xticks(range(len(counts)))
    ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8)
    ax.set_title("Sales Recency — Items Sold per Week\n(proxy for turnover speed)")
    ax.set_xlabel("Sold How Long Ago")
    ax.set_ylabel("Number of Sales")


def plot_top_models(df: pd.DataFrame, ax: plt.Axes):
    model_df = df[df["model"].notna() & (df["model"] != "")]
    if model_df.empty:
        ax.text(0.5, 0.5, "No model data", ha="center", va="center")
        ax.set_title("Top Models by Sales Count")
        return
    counts = model_df["model"].value_counts().head(12)
    sns.barplot(x=counts.values, y=counts.index, ax=ax, palette="viridis")
    ax.set_title("Top Models by Sales Count")
    ax.set_xlabel("Number Sold")
    ax.set_ylabel("Model")


def plot_model_median_price(df: pd.DataFrame, ax: plt.Axes):
    model_df = df[df["model"].notna() & (df["model"] != "")]
    if model_df.empty:
        ax.text(0.5, 0.5, "No model data", ha="center", va="center")
        ax.set_title("Median Sale Price by Model")
        return
    medians = (model_df.groupby("model")["price_num"]
                       .median()
                       .sort_values(ascending=False)
                       .head(12))
    sns.barplot(x=medians.values, y=medians.index, ax=ax, palette="rocket")
    ax.axvline(TARGET_PRICE, color="gold", linewidth=1.8, linestyle="--",
               label=f"Target £{TARGET_PRICE}")
    ax.set_title("Median Sale Price by Model")
    ax.set_xlabel("Median Price (£)")
    ax.set_ylabel("Model")
    ax.legend(fontsize=8)


def plot_shipping(df: pd.DataFrame, ax: plt.Axes):
    counts = df["free_shipping"].value_counts()
    labels = ["Free Shipping", "Paid Shipping"]
    ax.pie(counts.values, labels=labels, autopct="%1.0f%%",
           colors=["#4CAF50", "#FF7043"], startangle=90)
    ax.set_title("Free vs Paid Shipping")


def plot_price_vs_days_ago(df: pd.DataFrame, ax: plt.Axes):
    plot_df = df.dropna(subset=["days_ago"])
    sns.scatterplot(data=plot_df, x="days_ago", y="price_num",
                    hue="condition_clean", alpha=0.6, ax=ax, palette="Set1")
    target_line(ax, "h")
    ax.set_title("Sale Price vs Days Since Sale")
    ax.set_xlabel("Days Ago")
    ax.set_ylabel("Sale Price (£)")
    ax.legend(title="Condition", fontsize=8)


def plot_price_range_velocity(df: pd.DataFrame, ax: plt.Axes) -> pd.DataFrame:
    """
    Bar chart: price band vs median days_ago.
    Lower = items in that price range appear more recently in sold results
    = faster turnover. Best proxy available without listing start dates.
    Returns the summary table for printing.
    """
    plot_df = df.dropna(subset=["days_ago"]).copy()

    bins   = [0, 25, 50, 75, 100, 150, 200, float("inf")]
    labels = ["£0–25", "£25–50", "£50–75", "£75–100", "£100–150", "£150–200", "£200+"]
    plot_df["price_band"] = pd.cut(plot_df["price_num"], bins=bins, labels=labels)

    summary = (
        plot_df.groupby("price_band", observed=True)["days_ago"]
        .agg(count="count", median_days="median", mean_days="mean")
        .reset_index()
        .dropna()
    )

    colors = ["#2ecc71" if m <= 14 else "#f39c12" if m <= 30 else "#e74c3c"
              for m in summary["median_days"]]
    bars = ax.bar(summary["price_band"], summary["median_days"], color=colors, edgecolor="white")

    # Annotate bars with count
    for bar, cnt in zip(bars, summary["count"]):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5, f"n={int(cnt)}",
                ha="center", va="bottom", fontsize=8)

    ax.axhline(TARGET_PRICE * 0, color="none")  # just for spacing
    ax.set_title(
        "Price Band vs Sales Velocity\n"
        "(lower bar = sold more recently = faster turnover)\n"
        "Green ≤14d · Orange ≤30d · Red >30d",
        fontsize=9
    )
    ax.set_xlabel("Price Band")
    ax.set_ylabel("Median Days Since Sale")
    ax.tick_params(axis="x", rotation=20)

    return summary


# ── Summary ───────────────────────────────────────────────────────────────────

def print_velocity_table(df_clean: pd.DataFrame):
    plot_df = df_clean.dropna(subset=["days_ago"]).copy()
    bins   = [0, 25, 50, 75, 100, 150, 200, float("inf")]
    labels = ["£0–25", "£25–50", "£50–75", "£75–100", "£100–150", "£150–200", "£200+"]
    plot_df["price_band"] = pd.cut(plot_df["price_num"], bins=bins, labels=labels)
    summary = (
        plot_df.groupby("price_band", observed=True)["days_ago"]
        .agg(count="count", median_days="median")
        .reset_index()
        .dropna()
    )
    print("  Price band → sales velocity (lower days = faster turnover):")
    print(f"  {'Band':<12} {'# Sold':>8} {'Median days ago':>16}  Speed")
    print("  " + "-" * 46)
    for _, row in summary.iterrows():
        speed = "Fast" if row.median_days <= 14 else "Medium" if row.median_days <= 30 else "Slow"
        print(f"  {str(row.price_band):<12} {int(row['count']):>8} {row.median_days:>16.0f}  {speed}")
    print()


def print_summary(df_clean: pd.DataFrame, df_all: pd.DataFrame):
    removed = len(df_all) - len(df_clean)
    pct_at_target = (df_clean["price_num"] <= TARGET_PRICE).mean() * 100
    print("\n── Summary ──────────────────────────────────────────")
    print(f"  Total listings:        {len(df_all)}  ({removed} outliers removed)")
    print(f"  Date range:            {df_clean['date_dt'].min().date()} → {df_clean['date_dt'].max().date()}")
    print(f"  Min price:             £{df_clean['price_num'].min():.2f}")
    print(f"  Max price:             £{df_clean['price_num'].max():.2f}")
    print(f"  Mean price:            £{df_clean['price_num'].mean():.2f}")
    print(f"  Median price:          £{df_clean['price_num'].median():.2f}")
    print(f"  Target price (£{TARGET_PRICE}):    {pct_at_target:.0f}% of listings sold at or below target")
    print(f"  Free shipping:         {df_clean['free_shipping'].sum()} ({df_clean['free_shipping'].mean()*100:.0f}%)")
    print()
    print_velocity_table(df_clean)
    print("  Top conditions:")
    for cond, cnt in df_clean["condition_clean"].value_counts().head(5).items():
        print(f"    {cond}: {cnt}")
    print()
    top_models = df_clean[df_clean["model"] != ""]["model"].value_counts().head(5)
    if not top_models.empty:
        print("  Top models:")
        for model, cnt in top_models.items():
            print(f"    {model}: {cnt}")
    print("─────────────────────────────────────────────────────\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    csv_path = Path(CSV_FILE)
    if not csv_path.exists():
        print(f"File not found: {CSV_FILE}")
        return

    print(f"Loading {CSV_FILE}...")
    df_clean, df_all = load(CSV_FILE)
    print(f"Loaded {len(df_all)} listings → {len(df_clean)} after outlier removal.")

    print_summary(df_clean, df_all)

    with PdfPages(OUTPUT_PDF) as pdf:

        # Page 1: price distribution + condition
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_price_distribution(df_clean, axes[0])
        plot_price_by_condition(df_clean, axes[1])
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

        # Page 2: sales over time
        fig, ax = plt.subplots(figsize=(14, 5))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_sales_over_time(df_clean, ax)
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

        # Page 3: sales recency + price vs days ago
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_sales_recency(df_clean, axes[0])
        plot_price_vs_days_ago(df_clean, axes[1])
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

        # Page 3b: price band velocity chart
        fig, ax = plt.subplots(figsize=(12, 6))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_price_range_velocity(df_clean, ax)
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

        # Page 4: model counts + model median price
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_top_models(df_clean, axes[0])
        plot_model_median_price(df_clean, axes[1])
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

        # Page 5: shipping pie + blank space for notes
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(TITLE, fontsize=13, fontweight="bold")
        plot_shipping(df_clean, axes[0])
        # Right panel: target price summary text
        axes[1].axis("off")
        median = df_clean["price_num"].median()
        pct = (df_clean["price_num"] <= TARGET_PRICE).mean() * 100
        summary_text = (
            f"Target Price: £{TARGET_PRICE}\n\n"
            f"Median sold price:  £{median:.2f}\n"
            f"Mean sold price:    £{df_clean['price_num'].mean():.2f}\n\n"
            f"{pct:.0f}% of sales were at or\nbelow your target of £{TARGET_PRICE}\n\n"
            f"Outliers removed: {len(df_all) - len(df_clean)}\n"
            f"(IQR method, 1.5× fence)"
        )
        axes[1].text(0.1, 0.5, summary_text, transform=axes[1].transAxes,
                     fontsize=13, verticalalignment="center",
                     bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.8))
        plt.tight_layout()
        pdf.savefig(fig); plt.close(fig)

    print(f"Saved {OUTPUT_PDF}")
    import subprocess
    subprocess.run(["open", OUTPUT_PDF])


if __name__ == "__main__":
    main()
