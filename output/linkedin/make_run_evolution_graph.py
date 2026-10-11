"""Render a LinkedIn-ready summary of verified Atira run results."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


OUT = Path(__file__).with_name("atira-run-evolution.png")


def minutes(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def rounded_panel(ax, x: float, y: float, width: float, height: float, color: str) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y), width, height,
            boxstyle="round,pad=0.012,rounding_size=0.025",
            linewidth=0,
            facecolor=color,
            transform=ax.transAxes,
        )
    )


def main() -> None:
    # Verified run-time milestones only. No scenario answers or quality scores are embedded.
    labels = ["First run", "Later run", "Fastest final run"]
    seconds = [739, 310, 103]  # 12:19, 5:10, 1:43

    navy = "#0B1020"
    panel = "#151C31"
    pale = "#E9EDF5"
    muted = "#A6B1C8"
    teal = "#3DE1C3"
    gold = "#FFCB5B"
    blue = "#7AA7FF"
    lavender = "#C8B6FF"

    fig = plt.figure(figsize=(16, 9), dpi=150, facecolor=navy)
    canvas = fig.add_axes([0, 0, 1, 1])
    canvas.set_axis_off()

    rounded_panel(canvas, 0.07, 0.20, 0.86, 0.52, panel)

    fig.text(0.08, 0.90, "Run time reduced in the final push", color=pale,
             fontsize=31, fontweight="bold", family="DejaVu Sans")
    fig.text(0.08, 0.848,
             "The final push: from 12:19 to 1:43",
             color=muted, fontsize=14, family="DejaVu Sans")

    chart = fig.add_axes([0.15, 0.30, 0.70, 0.30], facecolor="none")
    xs = [0, 1, 2]
    bars = chart.bar(xs, seconds, color=[blue, lavender, teal], width=0.48)
    chart.set_ylim(0, 840)
    chart.set_yticks([0, 240, 480, 720])
    chart.set_yticklabels(["0:00", "4:00", "8:00", "12:00"], color=muted, fontsize=12)
    chart.set_xticks(xs, labels, color=pale, fontsize=14)
    chart.spines[["top", "right", "left"]].set_visible(False)
    chart.spines["bottom"].set_color("#35405C")
    chart.grid(axis="y", color="#2A344E", linewidth=0.8)
    chart.set_axisbelow(True)
    chart.set_title("RUN TIME", loc="left", color=pale,
                    fontsize=16, fontweight="bold", pad=16)
    for bar, run_seconds in zip(bars, seconds):
        chart.text(bar.get_x() + bar.get_width() / 2, run_seconds + 30, minutes(run_seconds),
                   ha="center", va="bottom", color=pale, fontsize=23, fontweight="bold")

    fig.text(0.08, 0.115, "86% LESS TIME", color=teal,
             fontsize=22, fontweight="bold", family="DejaVu Sans")
    fig.text(0.08, 0.065,
             "The fastest run completed in 1 minute 43 seconds — down from 12 minutes 19 seconds.",
             color=pale, fontsize=17, family="DejaVu Sans")

    fig.savefig(OUT, dpi=150, facecolor=navy, bbox_inches="tight", pad_inches=0.1)
    print(OUT)


if __name__ == "__main__":
    main()
