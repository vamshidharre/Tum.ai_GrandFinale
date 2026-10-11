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
    # Verified, high-level run facts only. No scenario answers are embedded.
    milestone_labels = ["Initial\npublic test", "First full-quality\npublic run"]
    milestone_quality = [10.0, 100.0]
    milestone_seconds = [739, 310]  # 12:19 and 5:10
    final_labels = ["Private\nrun 1", "Private\nrun 2", "Private\nrun 3"]
    final_quality = [66.3, 66.3, 66.3]
    final_seconds = [125, 103, 165]  # 2:05, 1:43, 2:45

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

    # Background sections.
    rounded_panel(canvas, 0.035, 0.19, 0.44, 0.57, panel)
    rounded_panel(canvas, 0.505, 0.19, 0.46, 0.57, panel)

    fig.text(0.05, 0.90, "We failed better—then we got faster.", color=pale,
             fontsize=31, fontweight="bold", family="DejaVu Sans")
    fig.text(0.05, 0.848,
             "Atira RFQ-to-Bid Challenge  •  verified run history  •  October 2026",
             color=muted, fontsize=14, family="DejaVu Sans")

    # Left milestone plot: quality bars plus times.
    left = fig.add_axes([0.065, 0.30, 0.38, 0.32], facecolor="none")
    positions = [0, 1]
    bars = left.bar(positions, milestone_quality, color=[blue, teal], width=0.52)
    left.set_ylim(0, 116)
    left.set_yticks([0, 50, 100])
    left.set_yticklabels(["0", "50", "100"], color=muted, fontsize=11)
    left.set_xticks(positions, milestone_labels, color=pale, fontsize=12)
    left.spines[["top", "right", "left"]].set_visible(False)
    left.spines["bottom"].set_color("#35405C")
    left.grid(axis="y", color="#2A344E", linewidth=0.8)
    left.set_axisbelow(True)
    left.set_title("QUALITY BREAKTHROUGH", loc="left", color=pale,
                   fontsize=16, fontweight="bold", pad=16)
    left.text(0, 111, "public-dataset evaluation score", color=muted, fontsize=10)
    for bar, quality, seconds in zip(bars, milestone_quality, milestone_seconds):
        left.text(bar.get_x() + bar.get_width() / 2, quality + 4,
                  f"{quality:.0f}/100  •  {minutes(seconds)}", ha="center", va="bottom",
                  color=pale, fontsize=15, fontweight="bold")
    fig.text(0.065, 0.235,
             "Quality rose 10× while runtime fell from 12:19 to 5:10 (−58%).",
             color=gold, fontsize=12.5, family="DejaVu Sans")

    # Right final-runs plot: runtime with quality labels to show repeatability.
    right = fig.add_axes([0.54, 0.30, 0.39, 0.32], facecolor="none")
    xs = [0, 1, 2]
    bars = right.bar(xs, final_seconds, color=[lavender, teal, lavender], width=0.54)
    right.set_ylim(0, 195)
    right.set_yticks([0, 60, 120, 180])
    right.set_yticklabels(["0:00", "1:00", "2:00", "3:00"], color=muted, fontsize=11)
    right.set_xticks(xs, final_labels, color=pale, fontsize=12)
    right.spines[["top", "right", "left"]].set_visible(False)
    right.spines["bottom"].set_color("#35405C")
    right.grid(axis="y", color="#2A344E", linewidth=0.8)
    right.set_axisbelow(True)
    right.set_title("FINAL PRIVATE RUNS", loc="left", color=pale,
                    fontsize=16, fontweight="bold", pad=16)
    right.text(0, 187, "same package • all eligible • same score", color=muted, fontsize=10)
    for bar, seconds, quality in zip(bars, final_seconds, final_quality):
        right.text(bar.get_x() + bar.get_width() / 2, seconds + 8, minutes(seconds),
                   ha="center", va="bottom", color=pale, fontsize=17, fontweight="bold")
        right.text(bar.get_x() + bar.get_width() / 2, max(seconds * 0.43, 19),
                   f"{quality:.1f}", ha="center", va="center",
                   color=navy, fontsize=15, fontweight="bold")
    fig.text(0.54, 0.235,
             "Fastest valid private run: 1:43 — 86% less time than the initial test.",
             color=teal, fontsize=12.5, family="DejaVu Sans")

    # Bottom facts anchor the story and preserve the quality / speed distinction.
    fig.text(0.05, 0.115, "WHAT THE FINAL HOUR PROVED", color=muted,
             fontsize=11, fontweight="bold", family="DejaVu Sans")
    fig.text(0.05, 0.068,
             "Correctness first: a 100-quality public run. Then repeatability: 3/3 eligible private runs in under 3 minutes.",
             color=pale, fontsize=15, family="DejaVu Sans")
    fig.text(0.05, 0.030,
             "Evaluation quality and runtime are separate metrics; the graphic reports both rather than hiding the trade-off.",
             color=muted, fontsize=11, family="DejaVu Sans")

    fig.savefig(OUT, dpi=150, facecolor=navy, bbox_inches="tight", pad_inches=0.1)
    print(OUT)


if __name__ == "__main__":
    main()
