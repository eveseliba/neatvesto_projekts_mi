"""Combination-based scorer for /schedule/multi.

Explores all valid date×service combinations and builds genuinely diverse
plans.  Each plan is a list of visits (days) with concrete slot assignments.
"""

import logging
from collections import defaultdict
from datetime import date as date_type
from itertools import combinations

from scheduling.schedule_response import SlotResult
from scheduling.patient_history import (
    build_attendance_cache,
    lookup_attendance,
)
from scheduling.scorer import (
    _slot_to_result,
    _match_slots_per_tier,
    _build_earliest,
    _find_matching_window,
    _extract_tier_level,
    _combined_score,
    _APPOINTMENT_BUFFER_MIN,
    _PREFERRED_MAX_GAP,
)
from scheduling.slot_query import format_time, slot_hour

logger = logging.getLogger(__name__)

# Pruning constants
_MAX_CANDIDATES_PER_SERVICE = 15
_TOP_PLANS = 3


# ---------------------------------------------------------------------------
# Slot grouping
# ---------------------------------------------------------------------------

def _group_slots(slots: list[dict], include_end_time: bool = True) -> list[dict]:
    """Merge slots sharing the same grouping key into groups.

    Each group is a superset of a normal slot dict with two extra keys:
    ``_group_slot_ids`` and ``_group_specialist_ids``.

    When *include_end_time* is True (default), the grouping key is
    (date, start, end, service_id).  When False, end_time is excluded
    so that slots at the same start time from different specialists
    (which may have different durations) are merged into a single entry;
    the representative's ``beigu_laiks`` is set to the maximum across all
    members.
    """
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for slot in slots:
        if include_end_time:
            key = (
                slot["vizites_datums"],
                slot["sakuma_laiks"],
                slot["beigu_laiks"],
                slot["pakalpojuma_id"],
            )
        else:
            key = (
                slot["vizites_datums"],
                slot["sakuma_laiks"],
                slot["pakalpojuma_id"],
            )
        buckets[key].append(slot)

    groups: list[dict] = []
    for members in buckets.values():
        rep = dict(members[0])
        # Flatten existing group IDs so re-grouping is idempotent
        all_ids: list[int] = []
        all_specs: list[int] = []
        for m in members:
            all_ids.extend(m.get("_group_slot_ids", [m["id"]]))
            all_specs.extend(m.get("_group_specialist_ids", [m["arsta_id"]]))
        rep["_group_slot_ids"] = all_ids
        rep["_group_specialist_ids"] = all_specs
        if not include_end_time and len(members) > 1:
            rep["beigu_laiks"] = max(m["beigu_laiks"] for m in members)
        groups.append(rep)
    return groups


def group_slots_by_start(slots: list[dict]) -> list[dict]:
    """Merge slots sharing the same (date, start_time, service) into groups.

    Convenience wrapper around ``_group_slots(include_end_time=False)``.
    """
    return _group_slots(slots, include_end_time=False)


def group_all_slots(
    all_slots: dict[int, list[dict]],
) -> dict[int, list[dict]]:
    """Apply ``_group_slots`` per service index."""
    return {
        svc_idx: _group_slots(slots)
        for svc_idx, slots in all_slots.items()
    }


# ---------------------------------------------------------------------------
# Adaptive slot cap
# ---------------------------------------------------------------------------

_SLOT_CAP = 2500


def apply_adaptive_cap(
    all_slots: dict[int, list[dict]],
    slot_cap: int = _SLOT_CAP,
) -> dict[int, list[dict]]:
    """Trim slots to a date cutoff when total exceeds *slot_cap*.

    Walks dates chronologically and cuts at the day **before** the
    cumulative count crosses the cap, preserving the nearest dates.
    Returns the (possibly trimmed) dict unchanged if under cap.
    """
    from datetime import timedelta

    total_slots = sum(len(slots) for slots in all_slots.values())
    if total_slots <= slot_cap:
        return all_slots

    all_dates = sorted({
        s["vizites_datums"]
        for slots in all_slots.values()
        for s in slots
    })

    cumulative = 0
    cutoff_date = all_dates[-1]
    for d in all_dates:
        day_count = sum(
            1 for slots in all_slots.values()
            for s in slots if s["vizites_datums"] == d
        )
        cumulative += day_count
        if cumulative >= slot_cap:
            cutoff_date = d - timedelta(days=1)
            break

    return {
        idx: [s for s in slots if s["vizites_datums"] <= cutoff_date]
        for idx, slots in all_slots.items()
    }


# ---------------------------------------------------------------------------
# Date-service availability matrix
# ---------------------------------------------------------------------------

def build_date_service_matrix(
    all_slots: dict[int, list[dict]],
) -> dict[date_type, dict[int, list[dict]]]:
    """Build mapping: date -> {service_idx -> [slots on that date]}."""
    matrix: dict[date_type, dict[int, list[dict]]] = defaultdict(dict)
    for svc_idx, slots in all_slots.items():
        by_date: dict[date_type, list[dict]] = defaultdict(list)
        for slot in slots:
            by_date[slot["vizites_datums"]].append(slot)
        for d, date_slots in by_date.items():
            matrix[d][svc_idx] = date_slots
    return dict(matrix)


# ---------------------------------------------------------------------------
# Slot scoring helpers
# ---------------------------------------------------------------------------

def _score_slot(
    slot: dict,
    day_time_cells: list[dict],
    att_cache: dict | None = None,
) -> tuple[float, float | None, int, bool, int | None, float | None]:
    """Score a single slot with profile and attendance probability.

    Returns (profile_score, att_prob, att_count, profile_match,
    tier_level, window_rate).
    """
    weekday = slot["vizites_datums"].isoweekday()
    hour = slot_hour(slot["sakuma_laiks"])
    window = _find_matching_window(day_time_cells, weekday, hour)

    profile_score = window["rate"] if window else 0.0
    profile_match = window is not None
    tier_level = _extract_tier_level(window)
    window_rate = window["rate"] if window else None

    att_prob = None
    att_count = 0
    if window and att_cache:
        att_prob, att_count = lookup_attendance(
            att_cache, weekday, window["hours"]
        )

    return profile_score, att_prob, att_count, profile_match, tier_level, window_rate


# ---------------------------------------------------------------------------
# Candidate pruning
# ---------------------------------------------------------------------------

def _prune_candidates(
    slots: list[dict],
    day_time_cells: list[dict],
    max_candidates: int = _MAX_CANDIDATES_PER_SERVICE,
) -> list[dict]:
    """Keep top-N slots by profile tier match, falling back to earliest."""
    if len(slots) <= max_candidates:
        return slots

    if not day_time_cells:
        return slots[:max_candidates]

    matched = _match_slots_per_tier(slots, day_time_cells)
    matched.sort(key=lambda x: (x[2], -x[1]["rate"]))
    result = [s for s, _, _ in matched[:max_candidates]]

    if len(result) < max_candidates:
        matched_ids = {id(s) for s, _, _ in matched}
        for s in slots:
            if id(s) not in matched_ids:
                result.append(s)
                if len(result) >= max_candidates:
                    break

    return result


# ---------------------------------------------------------------------------
# Group-aware helpers
# ---------------------------------------------------------------------------

def _all_slot_ids_from_scored(scored_slots: list[tuple]) -> set[int]:
    """Expand group IDs from scored slot tuples into a flat set."""
    ids: set[int] = set()
    for _svc_idx, slot, *_ in scored_slots:
        ids.update(slot.get("_group_slot_ids", [slot["id"]]))
    return ids


def _slot_group_conflicts(slot: dict, excluded_ids: set[int]) -> bool:
    """Check if any ID in a slot group is in the excluded set."""
    group_ids = slot.get("_group_slot_ids", [slot["id"]])
    return bool(set(group_ids) & excluded_ids)


# ---------------------------------------------------------------------------
# Arrangement finding (backtracking)
# ---------------------------------------------------------------------------

def _find_best_arrangement(
    service_slots: dict[int, list[dict]],
    day_time_cells: list[dict],
    excluded_ids: set[int] | None = None,
    att_cache: dict | None = None,
) -> list[tuple] | None:
    """Find the best valid arrangement for services on ONE date.

    Uses backtracking: each arrangement has exactly one slot per service
    with 15-min gaps between consecutive appointments.

    Returns scored_slots list or None if no valid arrangement exists.
    """
    if not service_slots:
        return None

    svc_indices = sorted(service_slots.keys())
    if excluded_ids is None:
        excluded_ids = set()

    # Build unified pool: (start_time, end_time, svc_idx, slot)
    pool: list[tuple[int, int, int, dict]] = []
    for idx in svc_indices:
        for slot in service_slots[idx]:
            if not _slot_group_conflicts(slot, excluded_ids):
                pool.append((slot["sakuma_laiks"], slot["beigu_laiks"], idx, slot))
    pool.sort(key=lambda x: (x[0], x[1], x[2]))

    n_services = len(svc_indices)
    svc_set = set(svc_indices)

    best: list[tuple] | None = None
    best_score = -1.0
    best_span = float("inf")

    def _backtrack(
        pos: int,
        assigned: dict[int, dict],
        last_end: int,
    ):
        nonlocal best, best_score, best_span

        if len(assigned) == n_services:
            scored = []
            total = 0.0
            for idx in svc_indices:
                slot = assigned[idx]
                scores = _score_slot(slot, day_time_cells, att_cache)
                scored.append((idx, slot, *scores))
                total += _combined_score(scores[0], scores[1])

            # Compute max gap between consecutive appointments
            sorted_slots = sorted(assigned.values(),
                                  key=lambda s: s["sakuma_laiks"])
            max_gap = 0
            for gi in range(1, len(sorted_slots)):
                gap = (sorted_slots[gi]["sakuma_laiks"]
                       - sorted_slots[gi - 1]["beigu_laiks"])
                if gap > max_gap:
                    max_gap = gap

            within_pref = max_gap <= _PREFERRED_MAX_GAP
            best_within = best_span <= _PREFERRED_MAX_GAP
            if (within_pref and not best_within) or (
                within_pref == best_within and (
                    total > best_score
                    or (total == best_score and max_gap < best_span)
                )
            ):
                best = scored
                best_score = total
                best_span = max_gap
            return

        remaining = svc_set - set(assigned.keys())
        for i in range(pos, len(pool)):
            start, end, svc_idx, slot = pool[i]
            if svc_idx not in remaining:
                continue
            if start < last_end + _APPOINTMENT_BUFFER_MIN:
                continue
            assigned[svc_idx] = slot
            _backtrack(i + 1, assigned, end)
            del assigned[svc_idx]

    _backtrack(0, {}, -_APPOINTMENT_BUFFER_MIN)
    return best


# ---------------------------------------------------------------------------
# Combination generation
# ---------------------------------------------------------------------------

def _generate_all_combinations(
    date_matrix: dict[date_type, dict[int, list[dict]]],
    services_with_slots: set[int],
    day_time_cells: list[dict],
    att_cache: dict | None = None,
) -> list[tuple]:
    """For each date, try all service subsets of size >= 2.

    Returns list of (date_str, combo_size, avg_score, time_span,
    scored_slots, svc_frozenset) sorted by (-combo_size, -avg_score,
    +time_span).
    """
    all_combos: list[tuple] = []

    # Pre-prune: compute pruned slots per (date, service) once
    pruned_matrix: dict[date_type, dict[int, list[dict]]] = {}
    for d in sorted(date_matrix.keys()):
        pruned_matrix[d] = {}
        for idx in date_matrix[d]:
            if idx in services_with_slots:
                pruned_matrix[d][idx] = _prune_candidates(
                    date_matrix[d][idx], day_time_cells,
                )

    for d in sorted(pruned_matrix.keys()):
        available = sorted(pruned_matrix[d].keys())
        if len(available) < 2:
            continue

        for combo_size in range(len(available), 1, -1):
            for subset in combinations(available, combo_size):
                svc_slots = {idx: pruned_matrix[d][idx] for idx in subset}

                arrangement = _find_best_arrangement(
                    svc_slots, day_time_cells, att_cache=att_cache,
                )
                if arrangement is None:
                    continue

                total_score = sum(
                    _combined_score(s[2], s[3]) for s in arrangement
                )
                avg_score = total_score / len(arrangement)

                sorted_arr = sorted(arrangement,
                                    key=lambda s: s[1]["sakuma_laiks"])
                max_gap = 0
                for gi in range(1, len(sorted_arr)):
                    gap = (sorted_arr[gi][1]["sakuma_laiks"]
                           - sorted_arr[gi - 1][1]["beigu_laiks"])
                    if gap > max_gap:
                        max_gap = gap

                all_combos.append((
                    str(d),
                    combo_size,
                    avg_score,
                    max_gap,
                    arrangement,
                    frozenset(subset),
                ))

    all_combos.sort(key=lambda x: (-x[1], x[3] > _PREFERRED_MAX_GAP, -x[2], x[3]))
    return all_combos


# ---------------------------------------------------------------------------
# Single-service slot picker
# ---------------------------------------------------------------------------

def _pick_best_slot(
    svc_idx: int,
    all_slots_4month: dict[int, list[dict]],
    day_time_cells: list[dict],
    excluded_ids: set[int],
    att_cache: dict | None = None,
) -> tuple[str, list[tuple]] | None:
    """Pick the best available slot for a single service.

    If ALL slots for this service are excluded, allows reuse (picks best
    overall).
    """
    slots = all_slots_4month.get(svc_idx, [])
    if not slots:
        return None

    # Try non-excluded slots first
    available = [
        s for s in slots if not _slot_group_conflicts(s, excluded_ids)
    ]

    # Reuse exception: if no non-excluded slots exist, allow reuse
    if not available:
        available = slots

    best_slot = None
    best_score = -1.0
    best_scored = None
    for slot in available:
        scores = _score_slot(slot, day_time_cells, att_cache)
        cs = _combined_score(scores[0], scores[1])
        if cs > best_score or best_slot is None:
            best_score = cs
            best_slot = slot
            best_scored = (svc_idx, slot, *scores)

    if best_scored is None:
        return None

    return (str(best_slot["vizites_datums"]), [best_scored])


# ---------------------------------------------------------------------------
# Greedy plan building
# ---------------------------------------------------------------------------

def _build_one_plan(
    all_combos: list[tuple],
    services_with_slots: set[int],
    all_slots_4month: dict[int, list[dict]],
    day_time_cells: list[dict],
    date_matrix: dict[date_type, dict[int, list[dict]]],
    global_used: set[int],
    att_cache: dict | None = None,
) -> tuple[float, list[tuple[str, list[tuple]]], set[int]] | None:
    """Build a single plan greedily.

    Returns (total_score, visit_list, slot_ids_used) or None.
    """
    remaining = set(services_with_slots)
    plan_visits: list[tuple[str, list[tuple]]] = []
    plan_slot_ids: set[int] = set()
    # Phase 1: pick combos from sorted list
    for combo in all_combos:
        if not remaining:
            break
        date_str, combo_size, avg_score, time_span, scored_slots, svc_set = combo

        # Skip if combo services aren't a subset of remaining
        if not svc_set.issubset(remaining):
            continue

        # Check slot conflicts with previously used slot IDs
        combo_ids = _all_slot_ids_from_scored(scored_slots)
        has_conflict = bool(combo_ids & global_used)

        if has_conflict:
            # Re-run arrangement excluding used slots
            d = None
            for dt in date_matrix:
                if str(dt) == date_str:
                    d = dt
                    break
            if d is None:
                continue

            svc_slots = {}
            for idx in svc_set:
                raw = date_matrix[d].get(idx, [])
                svc_slots[idx] = _prune_candidates(raw, day_time_cells)

            new_arrangement = _find_best_arrangement(
                svc_slots, day_time_cells, excluded_ids=global_used,
                att_cache=att_cache,
            )
            if new_arrangement is None:
                # Reuse exception: if every service has no other slots
                # outside excluded set, allow reuse
                all_reusable = True
                for idx in svc_set:
                    svc_slots_all = all_slots_4month.get(idx, [])
                    non_excluded = [
                        s for s in svc_slots_all
                        if not _slot_group_conflicts(s, global_used)
                    ]
                    if non_excluded:
                        all_reusable = False
                        break

                if all_reusable:
                    new_arrangement = scored_slots
                else:
                    continue

            scored_slots = new_arrangement
            combo_ids = _all_slot_ids_from_scored(scored_slots)

        plan_visits.append((date_str, scored_slots))
        plan_slot_ids.update(combo_ids)
        remaining -= svc_set

    # Phase 2: fill remaining services individually
    for svc_idx in sorted(remaining):
        result = _pick_best_slot(
            svc_idx, all_slots_4month, day_time_cells, global_used,
            att_cache,
        )
        if result is not None:
            date_str, scored = result
            plan_visits.append((date_str, scored))
            plan_slot_ids.update(_all_slot_ids_from_scored(scored))

    if not plan_visits:
        return None

    all_scores = [
        _combined_score(s[2], s[3]) for _, scored in plan_visits for s in scored
    ]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    return avg_score, plan_visits, plan_slot_ids


# Maximum attempts *per plan slot* to find a unique fingerprint
_MAX_ATTEMPTS_PER_PLAN = 30


def _build_plans_greedy(
    all_combos: list[tuple],
    services_with_slots: set[int],
    all_slots_4month: dict[int, list[dict]],
    day_time_cells: list[dict],
    date_matrix: dict[date_type, dict[int, list[dict]]],
    att_cache: dict | None = None,
) -> list[tuple[float, list[tuple[str, list[tuple]]]]]:
    """Build up to 3 plans greedily from the sorted combo list.

    For each plan slot, keeps trying until a plan with a *new* unique
    fingerprint is found.  When a duplicate fingerprint is produced,
    the existing plan is replaced if the new one is better (higher
    score or tighter schedule).

    Two exclusion sets are maintained:

    * ``committed_used`` — slot IDs from accepted plans (permanently
      excluded so different plans don't recommend the same slot).
    * ``attempt_used`` — slot IDs from discarded duplicate attempts
      (excluded only while searching for the current plan slot, then
      reset so those slots remain available for plans with a different
      fingerprint).

    Returns list of (total_score, [(date_str, scored_slots), ...]).
    """
    plans: list[tuple[float, list[tuple[str, list[tuple]]]]] = []
    fp_to_idx: dict[frozenset, int] = {}
    committed_used: set[int] = set()
    zero_combo = len(all_combos) == 0

    while len(plans) < _TOP_PLANS:
        found_new = False
        attempt_used: set[int] = set()

        for _attempt in range(_MAX_ATTEMPTS_PER_PLAN):
            result = _build_one_plan(
                all_combos, services_with_slots, all_slots_4month,
                day_time_cells, date_matrix,
                committed_used | attempt_used,
                att_cache,
            )
            if result is None:
                return plans

            total_score, plan_visits, plan_slot_ids = result
            fp = _plan_fingerprint(plan_visits)

            if fp in fp_to_idx and not zero_combo:
                # Duplicate fingerprint — replace existing if better
                idx = fp_to_idx[fp]
                existing_score, existing_visits = plans[idx]
                new_gap = _plan_total_gap(plan_visits)
                old_gap = _plan_total_gap(existing_visits)
                if total_score > existing_score or (
                    total_score == existing_score and new_gap < old_gap
                ):
                    logger.debug(
                        "Plan %d replaced: score %.2f->%.2f, gap %d->%d",
                        idx + 1, existing_score, total_score,
                        old_gap, new_gap,
                    )
                    plans[idx] = (total_score, plan_visits)
                # Exclude these slots only for this plan-slot search
                attempt_used.update(plan_slot_ids)
                continue  # keep trying for a new fingerprint

            # New unique fingerprint (or zero-combo mode) — accept as next plan
            fp_to_idx[fp] = len(plans)
            plans.append((total_score, plan_visits))
            committed_used.update(plan_slot_ids)
            found_new = True
            break

        if not found_new:
            break  # exhausted attempts for this plan slot

    return plans


# ---------------------------------------------------------------------------
# Plan deduplication
# ---------------------------------------------------------------------------

def _plan_fingerprint(
    visit_list: list[tuple[str, list[tuple]]],
) -> frozenset:
    """Compute date+service fingerprint for a plan.

    Merges same-date entries so that a combo of 4 services plus an
    individual pick on the same date produces the same fingerprint as
    all 5 in a single combo.
    """
    by_date: dict[str, set[int]] = defaultdict(set)
    for date_str, scored_slots in visit_list:
        for s in scored_slots:
            by_date[date_str].add(s[0])
    return frozenset(
        (date_str, frozenset(svc_indices))
        for date_str, svc_indices in by_date.items()
    )


def _plan_total_gap(
    visit_list: list[tuple[str, list[tuple]]],
) -> int:
    """Compute total gap between consecutive same-day appointments."""
    by_date: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for date_str, scored_slots in visit_list:
        for _svc_idx, slot, *_ in scored_slots:
            by_date[date_str].append(
                (slot["sakuma_laiks"], slot["beigu_laiks"])
            )

    total = 0
    for times in by_date.values():
        times.sort()
        for i in range(len(times) - 1):
            gap = times[i + 1][0] - times[i][1]
            total += max(0, gap)
    return total


def _deduplicate_plans(
    raw_plans: list[tuple[float, list[tuple[str, list[tuple]]]]],
) -> list[tuple[float, list[tuple[str, list[tuple]]]]]:
    """Filter plans, keeping the tightest per fingerprint."""
    seen: dict[frozenset, tuple[int, float, int]] = {}
    # Map fingerprint -> (index, score, gap)

    for i, (score, visit_list) in enumerate(raw_plans):
        fp = _plan_fingerprint(visit_list)
        gap = _plan_total_gap(visit_list)

        if fp not in seen or gap < seen[fp][2]:
            seen[fp] = (i, score, gap)

    keep_indices = {v[0] for v in seen.values()}
    return [raw_plans[i] for i in sorted(keep_indices)]


# ---------------------------------------------------------------------------
# Scored-tuple to SlotResult conversion
# ---------------------------------------------------------------------------

def _scored_to_slot_result(scored_slots: list[tuple]) -> list[SlotResult]:
    """Convert scored slot tuples to SlotResult objects.

    Delegates the core slot→SlotResult conversion to ``_slot_to_result``
    from ``scorer.py``, adding the combined score computation on top.
    """
    results = []
    for svc_idx, slot, ps, att_prob, att_count, pm, tier_level, window_rate in scored_slots:
        combined = round(_combined_score(ps, att_prob), 4) if ps > 0 else None
        results.append(_slot_to_result(
            slot,
            score=combined,
            profile_match=pm,
            profile_tier_level=tier_level,
            profile_window_rate=window_rate,
        ))
    return results


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def score_multi_schedule(
    all_slots_4month: dict[int, list[dict]],
    all_slots_earliest: dict[int, list[dict]],
    day_time_cells: list[dict],
    patient_visits: list[dict] | None,
    service_ids: list[int],
    excluded_dates: list[str],
) -> dict:
    """Score and group services into complete plans for /schedule/multi.

    Returns dict with keys ``earliest`` (list) and ``plans`` (list of
    dicts with rank, score, visits).
    """
    n_services = len(service_ids)

    # --- Build attendance cache once (O(1) lookups instead of per-slot) ---
    att_cache = build_attendance_cache(
        patient_visits or [], day_time_cells,
    )

    # --- Group slots before anything else ---
    all_slots_4month = group_all_slots(all_slots_4month)
    all_slots_earliest = group_all_slots(all_slots_earliest)

    # --- Earliest per service (from unbounded query) ---
    earliest_list: list[SlotResult | None] = []
    for idx in range(n_services):
        slots = all_slots_earliest.get(idx, [])
        earliest_list.append(
            _build_earliest(slots, day_time_cells, att_cache=att_cache)
        )

    # --- Build date-service matrix from 4-month slots ---
    date_matrix = build_date_service_matrix(all_slots_4month)

    # Determine which services have any slots in the 4-month window
    services_with_slots = {
        idx for idx in range(n_services) if all_slots_4month.get(idx)
    }

    # --- Generate all combinations ---
    all_combos = _generate_all_combinations(
        date_matrix, services_with_slots, day_time_cells, att_cache,
    )

    # --- Build greedy plans ---
    raw_plans = _build_plans_greedy(
        all_combos, services_with_slots, all_slots_4month,
        day_time_cells, date_matrix, att_cache,
    )

    # --- Deduplicate plans (safety net) — skip when zero combos (plans
    #     intentionally share fingerprints in zero-combo mode) ---
    if all_combos:
        raw_plans = _deduplicate_plans(raw_plans)

    # --- Sort by fewest visits first, then highest score ---
    def _plan_sort_key(p):
        score, visit_list = p
        visit_count = len({d for d, _ in visit_list})
        return (visit_count, -score)

    raw_plans.sort(key=_plan_sort_key)

    plans = []
    for rank, (total_score, visit_list) in enumerate(raw_plans, 1):
        # Merge visit entries that share the same date
        by_date: dict[str, list[tuple]] = defaultdict(list)
        for date_str, scored_slots in visit_list:
            by_date[date_str].extend(scored_slots)
        visits = []
        for date_str in sorted(by_date):
            visits.append({
                "date": date_str,
                "slots": _scored_to_slot_result(by_date[date_str]),
            })
        plans.append({
            "rank": rank,
            "score": round(total_score, 2),
            "visits": visits,
        })

    return {"earliest": earliest_list, "plans": plans}
