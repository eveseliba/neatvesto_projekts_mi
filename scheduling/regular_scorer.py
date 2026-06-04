"""Regular (recurring) appointment scorer for /schedule/regular.

Work-week based algorithm: divides [start_date, until_date] into
calendar work-weeks (Mon-Fri) and picks up to ``times_per_week``
visits per week with >=1 day spacing preference.
"""

import logging
from collections import defaultdict
from datetime import date as date_type, timedelta
from itertools import combinations

from scheduling.schedule_response import SlotSpecialistPair
from scheduling.regular_response import RegularSlotResult, RegularPlanVisit
from scheduling.patient_history import lookup_attendance
from scheduling.scorer import (
    _find_matching_window,
    _extract_tier_level,
    _combined_score,
    _APPOINTMENT_BUFFER_MIN,
    _PREFERRED_MAX_GAP,
)
from scheduling.multi_scorer import (
    _score_slot,
    _prune_candidates,
    _find_best_arrangement,
    _all_slot_ids_from_scored,
    _slot_group_conflicts,
    build_date_service_matrix,
)
from scheduling.slot_query import format_time, slot_hour

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Work-week grid builder
# ---------------------------------------------------------------------------

def _build_work_weeks(
    start_date: date_type, until_date: date_type,
) -> list[tuple[date_type, date_type]]:
    """Build a list of (Monday, Friday) work-week ranges.

    - First week starts on start_date (may be mid-week).
    - Last week ends on until_date (may be mid-week).
    - Skips Sat/Sun entirely.
    - Weekend start_date is advanced to the next Monday.
    """
    if start_date > until_date:
        return []

    # Advance weekend start to next Monday
    if start_date.weekday() >= 5:
        start_date = start_date + timedelta(days=(7 - start_date.weekday()))
        if start_date > until_date:
            return []

    weeks = []
    # Monday of start_date's week
    monday = start_date - timedelta(days=start_date.weekday())
    if monday < start_date:
        # start_date is mid-week — first partial week
        friday = monday + timedelta(days=4)
        weeks.append((start_date, min(friday, until_date)))
        monday += timedelta(weeks=1)

    while monday <= until_date:
        friday = monday + timedelta(days=4)
        weeks.append((monday, min(friday, until_date)))
        monday += timedelta(weeks=1)

    return weeks


# ---------------------------------------------------------------------------
# Specialist selection for regular appointments
# ---------------------------------------------------------------------------

def _select_best_specialists(
    all_slots: dict[int, list[dict]],
    start_date: date_type,
    until_date: date_type,
) -> dict[int, int]:
    """Pick the best specialist per service based on work-week coverage.

    For each service, groups slots by ``arsta_id`` and counts how many
    distinct work-weeks each specialist has at least one slot in.  The
    specialist with the highest week coverage wins; ties are broken by
    total slot count (more slots = more scheduling flexibility).

    Returns ``{svc_idx: arsta_id}`` for each service that has slots.
    """
    work_weeks = _build_work_weeks(start_date, until_date)
    if not work_weeks:
        return {}

    chosen: dict[int, int] = {}

    for svc_idx, slots in all_slots.items():
        if not slots:
            continue

        # Group slots by specialist
        by_specialist: dict[int, list[dict]] = defaultdict(list)
        for s in slots:
            by_specialist[s["arsta_id"]].append(s)

        best_arsta: int | None = None
        best_weeks = -1
        best_count = -1

        for arsta_id, arsta_slots in by_specialist.items():
            # Count distinct work-weeks this specialist covers
            week_hits = 0
            for wk_start, wk_end in work_weeks:
                if any(wk_start <= s["vizites_datums"] <= wk_end
                       for s in arsta_slots):
                    week_hits += 1

            if (week_hits > best_weeks
                    or (week_hits == best_weeks and len(arsta_slots) > best_count)):
                best_arsta = arsta_id
                best_weeks = week_hits
                best_count = len(arsta_slots)

        if best_arsta is not None:
            chosen[svc_idx] = best_arsta

    return chosen


# ---------------------------------------------------------------------------
# Scored-tuple → RegularSlotResult conversion
# ---------------------------------------------------------------------------

def _scored_to_regular(
    scored: tuple,
    label: str,
    days_since_previous: int | None,
) -> RegularSlotResult:
    """Convert an internal scored-slot tuple to a RegularSlotResult."""
    _svc_idx, slot, ps, att_prob, _att_count, pm, tier_level, window_rate = scored

    combined = round(_combined_score(ps, att_prob), 4) if ps > 0 else None

    raw_slot_ids = slot.get("_group_slot_ids", [slot["id"]])
    raw_specialist_ids = slot.get("_group_specialist_ids", [slot["arsta_id"]])
    pairs = [
        SlotSpecialistPair(slot_id=sid, specialist_id=spec)
        for sid, spec in zip(raw_slot_ids, raw_specialist_ids)
    ]

    return RegularSlotResult(
        slots=pairs,
        date=str(slot["vizites_datums"]),
        start_time=slot["sakuma_laiks"],
        end_time=slot["beigu_laiks"],
        start_time_formatted=format_time(slot["sakuma_laiks"]),
        end_time_formatted=format_time(slot["beigu_laiks"]),
        specialty_id=slot["specialitates_id"],
        hospital_id=slot["slimnicas_id"],
        service_id=slot.get("pakalpojuma_id"),
        appointment_type=slot.get("vizites_veids"),
        score=combined,
        profile_match=pm,
        profile_tier_level=tier_level,
        profile_window_rate=window_rate,
        label=label,
        days_since_previous=days_since_previous,
    )


# ---------------------------------------------------------------------------
# Single-service scoring — week slot picker
# ---------------------------------------------------------------------------

def _pick_week_slots_single(
    candidates: list[dict],
    times_per_week: int,
    day_time_cells: list[dict],
    att_cache: dict | None,
    previous_visit_date: date_type | None,
) -> list[tuple[tuple, str]]:
    """Pick up to times_per_week slots from a single week's candidates.

    Enforces >=1 day gap between picks (relaxed if impossible).
    Returns list of (scored_tuple, label) pairs.
    """
    # Score all candidates
    scored = []
    for slot in candidates:
        scores = _score_slot(slot, day_time_cells, att_cache)
        cs = _combined_score(scores[0], scores[1])
        scored.append((slot, scores, cs))

    # Group by date
    scored_by_date: dict[date_type, list[tuple]] = defaultdict(list)
    for slot, scores, cs in scored:
        scored_by_date[slot["vizites_datums"]].append((slot, scores, cs))

    available_dates = sorted(scored_by_date.keys())
    picks: list[tuple[tuple, str]] = []
    last_pick_date = previous_visit_date

    for _i in range(times_per_week):
        if not available_dates:
            break

        # Filter dates with >=1 day gap from last pick
        if last_pick_date is not None:
            valid_dates = [d for d in available_dates
                           if abs((d - last_pick_date).days) >= 2]
        else:
            valid_dates = list(available_dates)

        # Fallback: if no valid dates after gap filter, allow all remaining
        if not valid_dates:
            valid_dates = list(available_dates)

        # For each valid date, find best slot
        best_slot_per_date = []
        for d in valid_dates:
            date_scored = scored_by_date[d]
            matched = [(s, sc, cs) for s, sc, cs in date_scored if sc[3]]
            unmatched = [(s, sc, cs) for s, sc, cs in date_scored if not sc[3]]

            if matched:
                best = max(matched, key=lambda item: item[2])
                best_slot_per_date.append((d, best, "individual_best"))
            elif unmatched:
                best = min(unmatched, key=lambda item: (
                    item[0]["vizites_datums"], item[0]["sakuma_laiks"],
                ))
                best_slot_per_date.append((d, best, "earliest"))

        if not best_slot_per_date:
            break

        # Pick best: profile-matched slots first (by score), then unmatched (by date)
        matched_dates = [(d, b, l) for d, b, l in best_slot_per_date
                         if l == "individual_best"]
        unmatched_dates = [(d, b, l) for d, b, l in best_slot_per_date
                           if l != "individual_best"]

        if matched_dates:
            chosen_d, chosen_best, chosen_label = max(
                matched_dates, key=lambda item: item[1][2],
            )
        else:
            chosen_d, chosen_best, chosen_label = min(
                unmatched_dates, key=lambda item: item[0],
            )

        slot, scores, _cs = chosen_best
        scored_tuple = (0, slot, *scores)
        picks.append((scored_tuple, chosen_label))
        last_pick_date = chosen_d
        available_dates.remove(chosen_d)

    return picks


# ---------------------------------------------------------------------------
# Single-service scoring — main function
# ---------------------------------------------------------------------------

def score_regular_single(
    all_slots: list[dict],
    day_time_cells: list[dict],
    times_per_week: int,
    total_times: int,
    until_date: date_type,
    excluded_dates: list[str] | None = None,
    att_cache: dict | None = None,
    start_date: date_type | None = None,
) -> list[RegularPlanVisit]:
    """Score a single service across work-weeks.

    Returns a chronological list of ``RegularPlanVisit`` (up to
    ``total_times`` visits).
    """
    if start_date is None:
        start_date = date_type.today() + timedelta(days=1)

    excluded_set: set[date_type] = set()
    if excluded_dates:
        excluded_set = {date_type.fromisoformat(d) for d in excluded_dates}

    work_weeks = _build_work_weeks(start_date, until_date)
    visits: list[RegularPlanVisit] = []
    last_service_date: date_type | None = None
    previous_visit_date: date_type | None = None

    for week_start, week_end in work_weeks:
        if len(visits) >= total_times:
            break

        candidates = [
            s for s in all_slots
            if week_start <= s["vizites_datums"] <= week_end
            and s["vizites_datums"] not in excluded_set
        ]
        if not candidates:
            continue

        remaining = total_times - len(visits)
        week_tpw = min(times_per_week, remaining)

        week_picks = _pick_week_slots_single(
            candidates, week_tpw, day_time_cells,
            att_cache, previous_visit_date,
        )

        # Sort picks chronologically by date
        week_picks.sort(key=lambda p: p[0][1]["vizites_datums"])

        for scored_tuple, label in week_picks:
            slot = scored_tuple[1]
            slot_date = slot["vizites_datums"]
            days_gap = (slot_date - last_service_date).days if last_service_date else None
            last_service_date = slot_date
            previous_visit_date = slot_date

            visits.append(RegularPlanVisit(
                date=str(slot_date),
                slots=[_scored_to_regular(scored_tuple, label, days_gap)],
            ))

    return visits[:total_times]


# ---------------------------------------------------------------------------
# Multi-service scoring — helpers
# ---------------------------------------------------------------------------

def _build_period_combos(
    period_slots: dict[int, list[dict]],
    services_with_slots: set[int],
    day_time_cells: list[dict],
    att_cache: dict | None,
    ideal_date: date_type | None,
) -> list[tuple]:
    """Generate and sort combinations for a single period."""
    date_matrix = build_date_service_matrix(period_slots)

    # Pre-prune per (date, service)
    pruned: dict[date_type, dict[int, list[dict]]] = {}
    for d in sorted(date_matrix.keys()):
        pruned[d] = {}
        for idx in date_matrix[d]:
            if idx in services_with_slots:
                pruned[d][idx] = _prune_candidates(
                    date_matrix[d][idx], day_time_cells,
                )

    combos: list[tuple] = []
    for d in sorted(pruned.keys()):
        available = sorted(pruned[d].keys())
        if len(available) < 2:
            continue
        for combo_size in range(len(available), 1, -1):
            for subset in combinations(available, combo_size):
                svc_slots = {idx: pruned[d][idx] for idx in subset}
                arrangement = _find_best_arrangement(
                    svc_slots, day_time_cells, att_cache=att_cache,
                )
                if arrangement is None:
                    continue
                total_score = sum(_combined_score(s[2], s[3]) for s in arrangement)
                avg_score = total_score / len(arrangement)
                sorted_arr = sorted(arrangement,
                                    key=lambda s: s[1]["sakuma_laiks"])
                max_gap = 0
                for gi in range(1, len(sorted_arr)):
                    gap = (sorted_arr[gi][1]["sakuma_laiks"]
                           - sorted_arr[gi - 1][1]["beigu_laiks"])
                    if gap > max_gap:
                        max_gap = gap
                combos.append((
                    str(d), combo_size, avg_score, max_gap,
                    arrangement, frozenset(subset), d,
                ))

    if ideal_date is not None:
        combos.sort(key=lambda x: (
            -x[1],
            abs((x[6] - ideal_date).days),
            x[3] > _PREFERRED_MAX_GAP,
            -x[2],
            x[3],
        ))
    else:
        combos.sort(key=lambda x: (-x[1], x[3] > _PREFERRED_MAX_GAP, -x[2], x[3]))

    return combos


def _pick_individual_for_service(
    slots: list[dict],
    svc_idx: int,
    day_time_cells: list[dict],
    att_cache: dict | None,
    ideal_date: date_type | None,
    excluded_ids: set[int] | None = None,
) -> tuple | None:
    """Pick the best individual slot for one service with spacing pref."""
    if excluded_ids is None:
        excluded_ids = set()

    available = [s for s in slots if not _slot_group_conflicts(s, excluded_ids)]
    if not available:
        available = slots
    if not available:
        return None

    scored_list = []
    for slot in available:
        scores = _score_slot(slot, day_time_cells, att_cache)
        cs = _combined_score(scores[0], scores[1])
        scored_list.append((slot, scores, cs))

    matched = [(s, sc, cs) for s, sc, cs in scored_list if sc[3]]
    unmatched = [(s, sc, cs) for s, sc, cs in scored_list if not sc[3]]

    if matched:
        if ideal_date is not None:
            matched.sort(key=lambda item: (
                abs((item[0]["vizites_datums"] - ideal_date).days),
                -item[2],
            ))
        else:
            matched.sort(key=lambda item: -item[2])
        best_slot, best_scores, _ = matched[0]
    elif unmatched:
        unmatched.sort(key=lambda item: (
            item[0]["vizites_datums"], item[0]["sakuma_laiks"],
        ))
        best_slot, best_scores, _ = unmatched[0]
    else:
        return None

    return (svc_idx, best_slot, *best_scores)


# ---------------------------------------------------------------------------
# Multi-service scoring — week planner
# ---------------------------------------------------------------------------

def _plan_week_multi(
    period_slots: dict[int, list[dict]],
    times_per_week: int,
    service_count: int,
    day_time_cells: list[dict],
    att_cache: dict | None,
    last_pick_date: date_type | None,
) -> list[tuple[int, tuple, str]]:
    """Build up to times_per_week visit-days for multiple services in one week.

    Returns list of (svc_idx, scored_tuple, phase) entries.
    """
    services_with_slots = set(period_slots.keys())
    if not services_with_slots:
        return []

    # Collect all available dates across all services
    all_dates: set[date_type] = set()
    for svc_idx, slots in period_slots.items():
        for s in slots:
            all_dates.add(s["vizites_datums"])
    available_dates = sorted(all_dates)

    day_plans: list[list[tuple[int, tuple, str]]] = []

    for _i in range(times_per_week):
        if not available_dates:
            break

        # Gap filter: dates with >=1 day gap from last pick
        if last_pick_date is not None:
            valid_dates = [d for d in available_dates
                           if abs((d - last_pick_date).days) >= 2]
        else:
            valid_dates = list(available_dates)

        # Fallback: allow adjacent if no valid dates
        if not valid_dates:
            valid_dates = list(available_dates)

        # For each valid date, build a plan and rank
        date_plans = []
        for d in valid_dates:
            # Filter slots to this date only
            date_period_slots: dict[int, list[dict]] = {}
            for svc_idx, slots in period_slots.items():
                date_slots = [s for s in slots if s["vizites_datums"] == d]
                if date_slots:
                    date_period_slots[svc_idx] = date_slots

            if not date_period_slots:
                continue

            date_services = set(date_period_slots.keys())
            plan_entries: list[tuple[int, tuple, str]] = []
            plan_slot_ids: set[int] = set()
            remaining = set(date_services)

            # Phase 1: Combinations
            if len(date_services) >= 2:
                combos = _build_period_combos(
                    date_period_slots, date_services, day_time_cells,
                    att_cache, None,
                )
                for combo in combos:
                    if not remaining:
                        break
                    _date_str, _combo_size, _avg_score, _time_span, scored_slots, svc_set, _d = combo
                    if not svc_set.issubset(remaining):
                        continue
                    combo_ids = _all_slot_ids_from_scored(scored_slots)
                    if combo_ids & plan_slot_ids:
                        continue
                    for ss in scored_slots:
                        plan_entries.append((ss[0], ss, "combo"))
                    plan_slot_ids.update(combo_ids)
                    remaining -= svc_set

            # Phase 2: Individual fill — exclude time-overlapping slots
            # Collect occupied time ranges from combo phase
            occupied_ranges: list[tuple[int, int]] = []
            for _pe_idx, pe_scored, _pe_phase in plan_entries:
                occupied_ranges.append((
                    pe_scored[1]["sakuma_laiks"],
                    pe_scored[1]["beigu_laiks"],
                ))

            for svc_idx in sorted(remaining):
                svc_candidates = date_period_slots.get(svc_idx, [])
                if occupied_ranges:
                    svc_candidates = [
                        s for s in svc_candidates
                        if all(
                            s["sakuma_laiks"] >= occ_end + _APPOINTMENT_BUFFER_MIN
                            or s["beigu_laiks"] + _APPOINTMENT_BUFFER_MIN <= occ_start
                            for occ_start, occ_end in occupied_ranges
                        )
                    ]
                result = _pick_individual_for_service(
                    svc_candidates,
                    svc_idx, day_time_cells, att_cache, None,
                    excluded_ids=plan_slot_ids,
                )
                if result is not None:
                    plan_entries.append((svc_idx, result, "individual"))
                    slot_ids = set(result[1].get("_group_slot_ids", [result[1]["id"]]))
                    plan_slot_ids.update(slot_ids)
                    occupied_ranges.append((
                        result[1]["sakuma_laiks"],
                        result[1]["beigu_laiks"],
                    ))

            if plan_entries:
                services_covered = len(set(pe[0] for pe in plan_entries))
                total_score = sum(
                    _combined_score(pe[1][2], pe[1][3])
                    for pe in plan_entries
                )
                date_plans.append((d, plan_entries, services_covered, total_score))

        if not date_plans:
            break

        # Rank: most services covered first, then highest total score
        date_plans.sort(key=lambda x: (-x[2], -x[3]))
        best_date, best_entries, _, _ = date_plans[0]

        day_plans.append(best_entries)
        last_pick_date = best_date
        available_dates.remove(best_date)

    # Flatten all day plans
    result: list[tuple[int, tuple, str]] = []
    for entries in day_plans:
        result.extend(entries)
    return result


# ---------------------------------------------------------------------------
# Multi-service scoring — main function
# ---------------------------------------------------------------------------

def score_regular_multi(
    all_slots: dict[int, list[dict]],
    day_time_cells: list[dict],
    times_per_week: int,
    total_times: int,
    until_date: date_type,
    service_count: int,
    excluded_dates: list[str] | None = None,
    att_cache: dict | None = None,
    start_date: date_type | None = None,
) -> list[RegularPlanVisit]:
    """Score multiple services across work-weeks.

    Returns a chronological list of ``RegularPlanVisit``.
    """
    if start_date is None:
        start_date = date_type.today() + timedelta(days=1)

    excluded_set: set[date_type] = set()
    if excluded_dates:
        excluded_set = {date_type.fromisoformat(d) for d in excluded_dates}

    all_service_indices = set(range(service_count))
    work_weeks = _build_work_weeks(start_date, until_date)

    all_visits: list[RegularPlanVisit] = []
    missing_services: set[int] = set()
    last_service_dates: dict[int, date_type] = {}   # svc_idx → last date
    previous_plan_last_date: date_type | None = None
    service_visit_counts: dict[int, int] = defaultdict(int)

    for week_idx, (week_start, week_end) in enumerate(work_weeks):
        # Stop when all services with available slots have reached total_times
        active_services = set(all_slots.keys())
        if active_services and all(
            service_visit_counts[idx] >= total_times
            for idx in active_services
        ):
            break

        # 1. Filter slots per service to this week
        period_slots: dict[int, list[dict]] = {}
        for svc_idx in range(service_count):
            if service_visit_counts[svc_idx] >= total_times:
                continue
            svc_slots = [
                s for s in all_slots.get(svc_idx, [])
                if week_start <= s["vizites_datums"] <= week_end
                and s["vizites_datums"] not in excluded_set
            ]
            if svc_slots:
                period_slots[svc_idx] = svc_slots

        services_with_slots = set(period_slots.keys())

        # 2. Visit-days per week
        week_tpw = times_per_week

        # 3. Build plan for this week
        plan_entries: list[tuple[int, tuple, str]] = []

        if services_with_slots:
            plan_entries = _plan_week_multi(
                period_slots, week_tpw, service_count,
                day_time_cells, att_cache, previous_plan_last_date,
            )

        # 4. Collect raw entries: (svc_idx, date, scored_tuple, label)
        raw_entries: list[tuple[int, date_type, tuple, str]] = []

        for svc_idx, scored, phase in plan_entries:
            slot = scored[1]
            slot_date = slot["vizites_datums"]
            label = "combination" if phase == "combo" else "individual_best"
            raw_entries.append((svc_idx, slot_date, scored, label))

        # 5. Carry-forward earliest for missing services from PREVIOUS week
        if missing_services and week_idx > 0:
            for svc_idx in sorted(missing_services):
                svc_slots = period_slots.get(svc_idx, [])
                if not svc_slots:
                    continue

                earliest = min(
                    svc_slots,
                    key=lambda s: (s["vizites_datums"], s["sakuma_laiks"]),
                )
                earliest_date = earliest["vizites_datums"]

                # Find plan date for same service (if any)
                plan_date_for_svc = None
                for pe_svc, pe_date, _, _ in raw_entries:
                    if pe_svc == svc_idx:
                        plan_date_for_svc = pe_date
                        break

                if plan_date_for_svc is None:
                    scores = _score_slot(earliest, day_time_cells, att_cache)
                    scored_tuple = (svc_idx, earliest, *scores)
                    raw_entries.append((svc_idx, earliest_date, scored_tuple, "earliest"))

        # 6. Sort entries chronologically, then compute days_since_previous
        raw_entries.sort(key=lambda x: (x[1], x[0]))

        period_slot_results: list[tuple[int, date_type, RegularSlotResult]] = []
        # Use a copy of last_service_dates for within-week tracking
        period_dates = dict(last_service_dates)

        for svc_idx, slot_date, scored, label in raw_entries:
            days_gap = None
            if svc_idx in period_dates:
                days_gap = (slot_date - period_dates[svc_idx]).days
            period_dates[svc_idx] = slot_date

            period_slot_results.append((
                svc_idx,
                slot_date,
                _scored_to_regular(scored, label, days_gap),
            ))

        # 7. Update last_service_dates from week results
        for svc_idx, slot_date, sr in period_slot_results:
            if svc_idx not in last_service_dates or slot_date > last_service_dates[svc_idx]:
                last_service_dates[svc_idx] = slot_date
            service_visit_counts[svc_idx] += 1

        # 8. Update previous_plan_last_date
        if period_slot_results:
            last = max(d for _, d, _ in period_slot_results)
            previous_plan_last_date = last

        # 9. Update missing_services (exclude satisfied services)
        active = {idx for idx in all_service_indices
                  if service_visit_counts[idx] < total_times}
        covered_indices = {svc_idx for svc_idx, _, _ in period_slot_results}
        missing_services = active - covered_indices

        # 10. Merge same-date visits and append
        by_date: dict[str, list[RegularSlotResult]] = defaultdict(list)
        for _, slot_date, sr in period_slot_results:
            by_date[str(slot_date)].append(sr)

        for d in sorted(by_date.keys()):
            all_visits.append(RegularPlanVisit(date=d, slots=by_date[d]))

    return all_visits
