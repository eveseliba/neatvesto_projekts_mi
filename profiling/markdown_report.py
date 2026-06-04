"""Generate PROFILES.md and pretty-print profile summaries."""

import os

AGE_GROUP_ORDER = [
    ("toddler", "Toddlers (0-6)"),
    ("school_age", "School-age (7-18)"),
    ("adult", "Adults (18+)"),
]
DISTANCE_ORDER = ["0-10km", "10-30km", "30-100km", "100+km"]
DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri"]
TIER_LABELS = {
    1: "T1 (Best)",
    2: "T2 (Good)",
    3: "T3 (Average)",
    4: "T4 (Below avg)",
    5: "T5 (Worst)",
}


class MarkdownReportGenerator:
    """Builds the PROFILES.md markdown from pipeline output."""

    def __init__(self, profiles: dict, day_time_interactions: dict):
        self.profiles = profiles
        self.day_time_interactions = day_time_interactions
        self._summary_rows: list[tuple] = []

    def generate(self, output_path: str):
        """Generate and write the full markdown report."""
        lines = self._build_header()
        lines += self._build_segment_tables()
        lines += self._build_summary_table()

        content = "\n".join(lines)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"  Profiles markdown saved to: {output_path}")

    def _build_header(self) -> list[str]:
        meta = self.profiles.get("metadata", {})
        trained_at = meta.get("trained_at", "N/A")
        total = meta.get("total_records_used", 0)
        dist_feat = meta.get("distance_feature_used", "N/A")
        accuracy = meta.get("model_accuracy", {})
        dt_auc = accuracy.get("decision_tree", {}).get("cv_auc", "N/A")
        rf_auc = accuracy.get("random_forest", {}).get("cv_auc", "N/A")

        return [
            "# Day x Time Interaction Profiles",
            "",
            'Filter: `pakalpojuma_sveids = "0001"`',
            f"Trained at: {trained_at} | Total records: {total:,} | Distance feature: {dist_feat}",
            f"DT CV AUC: {dt_auc} | RF CV AUC: {rf_auc}",
            "Time windows: 8-9, 10-11, 12-13, 14-15, 16-17",
            "Tier strategy: CI Overlap",
            "",
            "Each table = one age group + distance segment. Rows = weekdays, columns = tiers.",
            "Cells show time windows sorted by attendance rate (highest first).",
            r"Format: window (rate%, sample_size). \* = low sample size (n < 30).",
            "",
        ]

    def _build_segment_tables(self) -> list[str]:
        lines = []
        self._summary_rows = []

        for age_prefix, age_title in AGE_GROUP_ORDER:
            lines += ["---", "", f"## {age_title}", ""]

            for dist in DISTANCE_ORDER:
                seg_key = f"{age_prefix}_{dist}"
                cells = self.day_time_interactions.get(seg_key)
                if not cells:
                    continue

                lines += self._build_one_segment(cells, age_title, dist)

        return lines

    def _build_one_segment(self, cells: list, age_title: str, dist: str) -> list[str]:
        max_tier = max(c.get("tier_ci", 1) for c in cells)

        tier_headers = [TIER_LABELS.get(t, f"T{t}") for t in range(1, max_tier + 1)]
        header = "| Day | " + " | ".join(tier_headers) + " |"
        separator = "|---" + "|---" * max_tier + "|"

        lines = [f"### {age_title} / {dist}", "", header, separator]

        for day_name in DAY_ORDER:
            day_cells = sorted(
                [c for c in cells if c["day_name"] == day_name],
                key=lambda c: -c["rate"],
            )
            tier_groups: dict[int, list] = {}
            for c in day_cells:
                t = c.get("tier_ci", 1)
                tier_groups.setdefault(t, []).append(c)

            tier_strs = []
            for t in range(1, max_tier + 1):
                group = tier_groups.get(t, [])
                if group:
                    tier_strs.append(
                        ", ".join(_format_dti_cell(c) for c in group)
                    )
                else:
                    tier_strs.append("-")

            lines.append(f"| **{day_name}** | " + " | ".join(tier_strs) + " |")

        lines.append("")

        # Collect summary stats
        total_n = sum(c["n"] for c in cells)
        rates = [c["rate"] for c in cells]
        spread_pp = (max(rates) - min(rates)) * 100
        num_tiers = max_tier
        best = max(cells, key=lambda c: c["rate"])
        worst = min(cells, key=lambda c: c["rate"])
        best_str = (
            f"{best['day_name']} {best['window']} "
            f"({best['rate'] * 100:.1f}%)"
        )
        worst_star = "*" if worst.get("low_sample", False) else ""
        worst_str = (
            f"{worst['day_name']} {worst['window']} "
            f"({worst['rate'] * 100:.1f}%){worst_star}"
        )
        self._summary_rows.append(
            (f"{age_title} / {dist}", total_n, spread_pp, num_tiers,
             best_str, worst_str)
        )

        return lines

    def _build_summary_table(self) -> list[str]:
        lines = [
            "---",
            "",
            "## Summary",
            "",
            "| Profile | N | Spread (pp) | Tiers (CI) | Best slot | Worst slot |",
            "|---|---:|---:|---:|---|---|",
        ]
        for label, n, spread, tiers, best, worst in self._summary_rows:
            lines.append(
                f"| {label} | {n:,} | {spread:.1f} | {tiers} | {best} | {worst} |"
            )
        lines.append("")
        return lines


def _format_dti_cell(cell: dict) -> str:
    """Format a day-time interaction cell: 'window (rate%, n)' with * for low sample."""
    pct = round(cell["rate"] * 100)
    star = "*" if cell.get("low_sample", False) else ""
    return f"{cell['window']} ({pct}%, {cell['n']}){star}"


def print_profiles(profiles: dict):
    """Pretty-print the generated profiles."""
    print()
    print("=" * 70)
    print("  GENERATED PROFILES")
    print("=" * 70)

    meta = profiles.get("metadata", {})
    print(f"\n  Data range:      {meta.get('data_range', 'N/A')}")
    print(f"  Total records:   {meta.get('total_records_used', 'N/A'):,}")
    print(f"  Trained at:      {meta.get('trained_at', 'N/A')}")
    accuracy = meta.get("model_accuracy", {})
    dt_auc = accuracy.get("decision_tree", {}).get("cv_auc", "N/A")
    rf_auc = accuracy.get("random_forest", {}).get("cv_auc", "N/A")
    print(f"  DT CV-AUC:       {dt_auc}")
    print(f"  RF CV-AUC:       {rf_auc}")
    dist = meta.get("distance_feature_used", "N/A")
    print(f"  Distance feature: {dist}")

    all_profiles = profiles.get("profiles", {})
    print(f"\n  {len(all_profiles)} profiles discovered:")
    print("-" * 70)

    for pkey in sorted(all_profiles.keys()):
        pval = all_profiles[pkey]
        age = pval.get("age_group", "?")
        dist_grp = pval.get("distance_group", "?")
        recs = pval.get("recommendations", [])

        print(f"\n  [{pkey}]  age={age}  distance={dist_grp}")
        print(f"  {'Rank':<5} {'Window':<10} {'Days':<40} {'Score':<8} {'Lift':<8} {'Samples':<8}")
        for rec in recs:
            days_str = ", ".join(d[:3] for d in rec.get("days", []))
            window = f"{rec['start_hour']}-{rec['end_hour']}"
            score = f"{rec['score']:.4f}"
            lift = f"{rec['lift']:+.4f}"
            n = rec.get("sample_size", 0)
            print(f"  {rec['rank']:<5} {window:<10} {days_str:<40} {score:<8} {lift:<8} {n:<8}")

    print()
    print("=" * 70)
