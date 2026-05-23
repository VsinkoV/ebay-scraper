#!/usr/bin/env python3
"""
Price comparison visualiser.
Reads listing CSVs for a search query and renders:
  1. Bar chart  — median price per platform (with min/max whiskers)
  2. Box plot   — full price distribution per platform
  3. Horizontal reference line for eBay sold median (50% bargain threshold)

Usage:
  python3 visualise.py
  python3 visualise.py "air force 1"   # skip query prompt
"""

import csv
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")   # headless — saves to file; remove for interactive window
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

DATASETS_DIR = Path("datasets")

PLATFORM_COLOURS = {
    "ebay":   "#E53238",
    "vinted": "#007782",
    "depop":  "#FF4040",
}

PLATFORM_LABELS = {
    "ebay":   "eBay",
    "vinted": "Vinted",
    "depop":  "Depop",
}


# ── helpers ───────────────────────────────────────────────────────────────────
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_price(val) -> float | None:
    try:
        return float(str(val or "").replace("£", "").replace(",", "").strip())
    except ValueError:
        return None


def load_prices(query: str, platform: str) -> list[float]:
    path = DATASETS_DIR / f"{_slug(query)}_{platform}_new.csv"
    if not path.exists():
        return []
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price(row.get("price", ""))
            if p is not None and p > 0:
                prices.append(p)
    return prices


def load_ebay_sold_median(query: str) -> float | None:
    path = DATASETS_DIR / f"{_slug(query)}_sold.csv"
    if not path.exists():
        return None
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price(row.get("price", ""))
            if p is not None and p > 0:
                prices.append(p)
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]


def stats(prices: list[float]) -> dict:
    if not prices:
        return {}
    arr = sorted(prices)
    n   = len(arr)
    return {
        "min":    arr[0],
        "q1":     arr[n // 4],
        "median": arr[n // 2],
        "q3":     arr[(3 * n) // 4],
        "max":    arr[-1],
        "mean":   sum(arr) / n,
        "count":  n,
    }


# ── plotting ──────────────────────────────────────────────────────────────────
def plot(query: str, platforms: list[str], output: Path) -> None:
    data = {}
    for p in platforms:
        prices = load_prices(query, p)
        if prices:
            data[p] = prices

    if not data:
        print("  No listing data found. Run the watcher first to seed CSVs.")
        return

    sold_median = load_ebay_sold_median(query)
    bargain_threshold = round(sold_median * 0.50, 2) if sold_median else None

    fig, (ax1, ax2) = plt.subplots(
        1, 2,
        figsize=(13, 6),
        gridspec_kw={"width_ratios": [1.2, 1]},
    )
    fig.suptitle(
        f"Price comparison — \"{query}\"",
        fontsize=14, fontweight="bold", y=1.01,
    )

    # ── chart 1: median bar + min/max whiskers ────────────────────────────────
    xs       = list(range(len(data)))
    labels   = [PLATFORM_LABELS[p] for p in data]
    colours  = [PLATFORM_COLOURS[p] for p in data]
    medians  = []
    means    = []
    mins     = []
    maxs     = []
    q1s      = []
    q3s      = []
    counts   = []

    for p in data:
        s = stats(data[p])
        medians.append(s["median"])
        means.append(s["mean"])
        mins.append(s["min"])
        maxs.append(s["max"])
        q1s.append(s["q1"])
        q3s.append(s["q3"])
        counts.append(s["count"])

    bars = ax1.bar(
        xs, medians,
        color=colours, alpha=0.85, width=0.5, zorder=3,
        label="Median price",
    )

    # IQR box (Q1→Q3) overlaid on bar
    for i, (q1, q3, col) in enumerate(zip(q1s, q3s, colours)):
        ax1.bar(
            i, q3 - q1,
            bottom=q1,
            color=col, alpha=0.35, width=0.5, zorder=4,
        )

    # Min/max whiskers
    for i, (mn, mx) in enumerate(zip(mins, maxs)):
        ax1.plot([i, i], [mn, mx], color="black", linewidth=1.2, zorder=5)
        ax1.plot([i - 0.1, i + 0.1], [mn, mn], color="black", linewidth=1.2, zorder=5)
        ax1.plot([i - 0.1, i + 0.1], [mx, mx], color="black", linewidth=1.2, zorder=5)

    # Mean dot
    ax1.scatter(xs, means, color="white", edgecolors="black", s=55, zorder=6, label="Mean price")

    # Annotate medians on bars
    for bar, med, count in zip(bars, medians, counts):
        ax1.text(
            bar.get_x() + bar.get_width() / 2, med + 0.5,
            f"£{med:.0f}\n(n={count})",
            ha="center", va="bottom", fontsize=9, fontweight="bold",
        )

    # eBay sold median reference lines
    if sold_median:
        ax1.axhline(sold_median, color="gold", linewidth=1.8, linestyle="--", zorder=2,
                    label=f"eBay sold median £{sold_median:.0f}")
    if bargain_threshold:
        ax1.axhline(bargain_threshold, color="limegreen", linewidth=1.5, linestyle=":", zorder=2,
                    label=f"Bargain threshold £{bargain_threshold:.0f} (50%)")

    # Shade bargain zone
    if bargain_threshold:
        ax1.axhspan(0, bargain_threshold, alpha=0.07, color="limegreen", zorder=1)

    ax1.set_xticks(xs)
    ax1.set_xticklabels(labels, fontsize=11)
    ax1.set_ylabel("Price (£)", fontsize=10)
    ax1.set_title("Median price by platform\n(bar=IQR, whiskers=min/max, dot=mean)", fontsize=10)
    ax1.legend(fontsize=8)
    ax1.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax1.set_axisbelow(True)

    # Highlight cheapest platform
    if medians:
        cheapest_idx = int(np.argmin(medians))
        ax1.bar(
            [cheapest_idx], [medians[cheapest_idx]],
            color=colours[cheapest_idx], alpha=1.0, width=0.5, zorder=3,
            edgecolor="gold", linewidth=2.5,
        )
        ax1.text(
            cheapest_idx, -max(medians) * 0.06,
            "CHEAPEST",
            ha="center", va="top", fontsize=7.5, color="goldenrod", fontweight="bold",
        )

    # ── chart 2: box plot distribution ───────────────────────────────────────
    box_data    = [data[p] for p in data]
    box_colours = [PLATFORM_COLOURS[p] for p in data]

    bp = ax2.boxplot(
        box_data,
        patch_artist=True,
        medianprops={"color": "white", "linewidth": 2},
        whiskerprops={"color": "black"},
        capprops={"color": "black"},
        flierprops={"marker": ".", "markersize": 4, "alpha": 0.5},
        widths=0.5,
    )
    for patch, col in zip(bp["boxes"], box_colours):
        patch.set_facecolor(col)
        patch.set_alpha(0.80)

    if sold_median:
        ax2.axhline(sold_median, color="gold", linewidth=1.8, linestyle="--",
                    label=f"eBay sold median £{sold_median:.0f}")
    if bargain_threshold:
        ax2.axhline(bargain_threshold, color="limegreen", linewidth=1.5, linestyle=":",
                    label=f"Bargain threshold £{bargain_threshold:.0f}")
        ax2.axhspan(0, bargain_threshold, alpha=0.07, color="limegreen")

    ax2.set_xticks(range(1, len(data) + 1))
    ax2.set_xticklabels(labels, fontsize=11)
    ax2.set_ylabel("Price (£)", fontsize=10)
    ax2.set_title("Price distribution by platform\n(box=IQR, line=median)", fontsize=10)
    ax2.legend(fontsize=8)
    ax2.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax2.set_axisbelow(True)

    # Colour-coded legend patches
    legend_patches = [
        mpatches.Patch(color=PLATFORM_COLOURS[p], label=PLATFORM_LABELS[p])
        for p in data
    ]
    fig.legend(
        handles=legend_patches,
        loc="lower center",
        ncol=len(data),
        fontsize=9,
        framealpha=0.8,
        bbox_to_anchor=(0.5, -0.04),
    )

    plt.tight_layout()
    fig.savefig(output, dpi=150, bbox_inches="tight")
    print(f"\n  Chart saved → {output}")
    plt.close(fig)

    # ── terminal summary table ────────────────────────────────────────────────
    print(f"\n  {'Platform':<10}  {'Count':>5}  {'Min':>7}  {'Q1':>7}  "
          f"{'Median':>7}  {'Mean':>7}  {'Q3':>7}  {'Max':>7}")
    print("  " + "─" * 72)
    for p in data:
        s = stats(data[p])
        cheapest_marker = "  ◀ cheapest" if s["median"] == min(stats(data[pp])["median"] for pp in data) else ""
        print(f"  {PLATFORM_LABELS[p]:<10}  {s['count']:>5}  "
              f"£{s['min']:>6.2f}  £{s['q1']:>6.2f}  "
              f"£{s['median']:>6.2f}  £{s['mean']:>6.2f}  "
              f"£{s['q3']:>6.2f}  £{s['max']:>6.2f}{cheapest_marker}")
    if sold_median:
        print(f"\n  eBay sold median : £{sold_median:.2f}")
    if bargain_threshold:
        print(f"  Bargain threshold: £{bargain_threshold:.2f}  (50% of sold median)")
        for p in data:
            below = [x for x in data[p] if x < bargain_threshold]
            if below:
                pct = len(below) / len(data[p]) * 100
                print(f"    {PLATFORM_LABELS[p]}: {len(below)} of {len(data[p])} listings below threshold ({pct:.0f}%)")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("  Search query: ").strip()

    if not query:
        print("  No query entered.")
        return

    platforms = ["ebay", "vinted", "depop"]

    # Only include platforms that have data
    available = [p for p in platforms if (DATASETS_DIR / f"{_slug(query)}_{p}_new.csv").exists()]
    if not available:
        print(f"\n  No CSV data found for '{query}'. Run the watcher first.\n"
              f"  Expected files like: datasets/{_slug(query)}_vinted_new.csv")
        return

    print(f"\n  Platforms with data: {', '.join(available)}")

    output = DATASETS_DIR / f"{_slug(query)}_price_comparison.png"
    plot(query, available, output)


if __name__ == "__main__":
    main()
