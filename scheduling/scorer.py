"""Profile-based tier cascade filtering and slot ranking."""

import logging

from scheduling.slot_query import format_time, slot_hour
from scheduling.patient_history import (
    build_attendance_cache,
    compute_attendance_probability,
    lookup_attendance,
)
from scheduling.schedule_response import SlotResult, SlotSpecialistPair, ServiceResult

logger = logging.getLogger(__name__)

# Attendance defaults (matches multi_scorer.py)
_ATT_DEFAULT = 0.5


def _combined_score(profile_rate: float, att_prob: float | None) -> float:
    """Combined ranking score blending profile rate with attendance."""
    eff_att = att_prob if att_prob is not None else _ATT_DEFAULT
    return profile_rate * (0.3 + 0.7 * eff_att)


def _slot_to_result(
    slot: dict,
    score: float | None,
    profile_match: bool,
    profile_tier_level: int | None = None,
    profile_window_rate: float | None = None,
) -> SlotResult:
    """Convert a raw DB slot dict + scoring info into a SlotResult.

    Supports grouped slots produced by ``_group_slots()``: if the dict
    contains ``_group_slot_ids`` / ``_group_specialist_ids`` keys those
    lists are used directly; otherwise single-element lists are built
    from the slot's own ``id`` / ``arsta_id``.
    """
    raw_slot_ids = slot.get("_group_slot_ids", [slot["id"]])
    raw_specialist_ids = slot.get("_group_specialist_ids", [slot["arsta_id"]])
    pairs = [
        SlotSpecialistPair(slot_id=sid, specialist_id=spec)
        for sid, spec in zip(raw_slot_ids, raw_specialist_ids)
    ]
    return SlotResult(
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
        score=score,
        profile_match=profile_match,
        profile_tier_level=profile_tier_level,
        profile_window_rate=profile_window_rate,
    )


def _compute_attendance(
    patient_visits: list[dict] | None,
    weekday: int,
    hours: list[int],
    att_cache: dict | None = None,
) -> tuple[float | None, int]:
    """Compute attendance probability, returning (None, 0) if no visits."""
    if att_cache:
        return lookup_attendance(att_cache, weekday, hours)
    if patient_visits is None:
        return None, 0
    return compute_attendance_probability(patient_visits, weekday, hours)


_APPOINTMENT_BUFFER_MIN = 15  # minimum gap between appointments (minutes)
_PREFERRED_MAX_GAP = 120      # prefer combos where max gap between visits ≤ 2h


def _overlaps_any(
    start: int, end: int, occupied: list[tuple[int, int]]
) -> bool:
    """Check if [start, end) overlaps any interval in occupied (with buffer).

    The buffer is applied symmetrically: a candidate slot is rejected if it
    starts less than BUFFER minutes after an occupied slot ends, or ends
    less than BUFFER minutes before an occupied slot starts.
    """
    buf = _APPOINTMENT_BUFFER_MIN
    for occ_start, occ_end in occupied:
        if start < occ_end + buf and end + buf > occ_start:
            return True
    return False


def _extract_tier_level(window: dict | None) -> int | None:
    """Extract tier level from a day-time cell dict."""
    if window is None:
        return None
    tier = window.get("tier", window.get("tier_ci"))
    return int(tier) if tier is not None else None


def _build_earliest(
    slots: list[dict],
    day_time_cells: list[dict],
    patient_visits: list[dict] | None = None,
    att_cache: dict | None = None,
) -> SlotResult | None:
    """Build the earliest SlotResult from an ordered slot list."""
    if not slots:
        return None

    slot = slots[0]
    weekday = slot["vizites_datums"].isoweekday()
    hour = slot_hour(slot["sakuma_laiks"])

    window = _find_matching_window(day_time_cells, weekday, hour)
    att_prob = None
    if window:
        if att_cache:
            att_prob, _ = lookup_attendance(
                att_cache, weekday, window["hours"]
            )
        elif patient_visits is not None:
            att_prob, _ = compute_attendance_probability(
                patient_visits, weekday, window["hours"]
            )

    window_rate = window["rate"] if window else None
    score = round(_combined_score(window_rate, att_prob), 4) if window_rate is not None else None

    return _slot_to_result(
        slot,
        score=score,
        profile_match=window is not None,
        profile_tier_level=_extract_tier_level(window),
        profile_window_rate=window_rate,
    )


def _pick_best_slot_on_date(
    date_slots: list[dict],
    day_time_cells: list[dict],
    patient_visits: list[dict] | None,
    occupied: list[tuple[int, int]] | None = None,
    att_cache: dict | None = None,
) -> SlotResult:
    """Pick the best slot from slots on a single date, using profile scoring.

    Strategy: run tier cascade on the date's slots, pick the best match.
    Falls back to earliest slot on that date if no profile match.

    If *occupied* is provided, slots overlapping those time ranges are
    excluded before scoring (cross-service time-conflict avoidance).
    """
    # Filter out slots that overlap with already-occupied time ranges
    if occupied:
        available = [
            s for s in date_slots
            if not _overlaps_any(s["sakuma_laiks"], s["beigu_laiks"], occupied)
        ]
        if not available:
            # All slots overlap — fall back to earliest on this date
            slot = date_slots[0]
            weekday = slot["vizites_datums"].isoweekday()
            hour = slot_hour(slot["sakuma_laiks"])
            window = _find_matching_window(day_time_cells, weekday, hour)
            att_prob, _ = _compute_attendance(
                patient_visits, weekday, window["hours"] if window else [],
                att_cache=att_cache,
            )
            window_rate = window["rate"] if window else None
            score = round(_combined_score(window_rate, att_prob), 4) if window_rate is not None else None
            return _slot_to_result(
                slot,
                score=score,
                profile_match=window is not None,
                profile_tier_level=_extract_tier_level(window),
                profile_window_rate=window_rate,
            )
        date_slots = available

    if day_time_cells:
        matches = _match_slots_per_tier(date_slots, day_time_cells)
        if matches:
            matches.sort(key=lambda x: (x[2], -x[1]["rate"]))
            slot, cell, tier_num = matches[0]
            weekday = slot["vizites_datums"].isoweekday()
            att_prob, _ = _compute_attendance(
                patient_visits, weekday, cell["hours"],
                att_cache=att_cache,
            )
            window_rate = cell["rate"]
            score = round(_combined_score(window_rate, att_prob), 4)
            return _slot_to_result(
                slot,
                score=score,
                profile_match=True,
                profile_tier_level=tier_num,
                profile_window_rate=window_rate,
            )

    # Fallback: earliest slot on that date
    slot = date_slots[0]
    weekday = slot["vizites_datums"].isoweekday()
    hour = slot_hour(slot["sakuma_laiks"])
    window = _find_matching_window(day_time_cells, weekday, hour)
    att_prob, _ = _compute_attendance(
        patient_visits, weekday, window["hours"] if window else [],
    )
    window_rate = window["rate"] if window else None
    score = round(_combined_score(window_rate, att_prob), 4) if window_rate is not None else None
    return _slot_to_result(
        slot,
        score=score,
        profile_match=window is not None,
        profile_tier_level=_extract_tier_level(window),
        profile_window_rate=window_rate,
    )


def score_slots_for_service(
    slots: list[dict],
    day_time_cells: list[dict],
    patient_visits: list[dict] | None,
    service_id: int | None,
    specialist_id: int | None,
    earliest_slots: list[dict] | None = None,
) -> ServiceResult:
    """Run the profile-first funnel for one service's slots.

    1. Identify earliest slot (chronologically first, regardless of profile)
    2. Tier cascade: filter slots by profile tiers, highest rate first
    3. Score matched slots with attendance probability
    4. Return earliest + top 3 recommended
    """
    total_slots = len(slots)
    earliest_source = earliest_slots if earliest_slots is not None else slots

    if total_slots == 0 and not earliest_source:
        return ServiceResult(
            service_id=service_id,
            specialist_id=specialist_id,
            earliest=None,
            recommended=[],
            total_slots_evaluated=0,
            tier_used=None,
            profile_match=False,
            profile_match_reason="No available slots found",
        )

    # --- Build attendance cache once ---
    att_cache = build_attendance_cache(
        patient_visits or [], day_time_cells,
    )

    # --- Earliest slot (from earliest_slots if provided, else from slots) ---
    earliest_result = _build_earliest(earliest_source, day_time_cells, att_cache=att_cache)

    # --- Per-slot tier cascade ---
    # Each slot independently finds its best (lowest) matching tier.
    if not day_time_cells:
        # No profile data — earliest already covers the chronological view;
        # recommended stays empty because there is nothing to score.
        return ServiceResult(
            service_id=service_id,
            specialist_id=specialist_id,
            earliest=earliest_result,
            recommended=[],
            total_slots_evaluated=total_slots,
            tier_used=None,
            profile_match=False,
            profile_match_reason="No profile data available",
        )

    # Build per-slot matches: for each slot, find its best tier
    slot_matches = _match_slots_per_tier(slots, day_time_cells)
    # slot_matches: list of (slot, cell, tier_num) for matched slots

    if not slot_matches:
        logger.warning(
            "No tier matched for service_id=%s specialist_id=%s (total_slots=%d)",
            service_id, specialist_id, total_slots,
        )
        recommended = _build_fallback_recommended(slots[:3])
        return ServiceResult(
            service_id=service_id,
            specialist_id=specialist_id,
            earliest=earliest_result,
            recommended=recommended,
            total_slots_evaluated=total_slots,
            tier_used=None,
            profile_match=False,
            profile_match_reason="No profile tier matched any available slots",
        )

    # --- Score matched slots with attendance-aware ranking ---
    # Compute attendance for every matched slot so it can influence the
    # sort order.  The combined score weighs profile rate and attendance:
    #   combined = rate * (0.3 + 0.7 * att_prob)
    # This allows slots with better attendance to outrank higher-rate
    # slots in the same or even a better tier when attendance is poor.
    scored_matches: list[tuple[dict, dict, int, float | None, float]] = []
    for slot, cell, tier_num in slot_matches:
        weekday = slot["vizites_datums"].isoweekday()
        att_prob = None
        if att_cache:
            att_prob, _ = lookup_attendance(
                att_cache, weekday, cell["hours"]
            )
        combined = _combined_score(cell["rate"], att_prob)
        scored_matches.append((slot, cell, tier_num, att_prob, combined))

    scored_matches.sort(key=lambda x: -x[4])
    top_3 = scored_matches[:3]
    best_tier = top_3[0][2]

    recommended = []
    for slot, cell, tier_num, att_prob, combined in top_3:
        recommended.append(
            _slot_to_result(
                slot,
                score=round(combined, 4),
                profile_match=True,
                profile_tier_level=tier_num,
                profile_window_rate=cell["rate"],
            )
        )

    return ServiceResult(
        service_id=service_id,
        specialist_id=specialist_id,
        earliest=earliest_result,
        recommended=recommended,
        total_slots_evaluated=total_slots,
        tier_used=best_tier,
        profile_match=True,
        profile_match_reason=None,
    )


def score_slots_cross_service(
    all_slots: dict[int, list[dict]],
    day_time_cells: list[dict],
    patient_visits: list[dict] | None,
    service_ids: list[int | None],
    specialist_ids: list[int | None],
) -> list[ServiceResult] | None:
    """Cross-service combination scoring with partial combination support.

    Finds dates where 2+ services have available slots and builds
    date-aligned recommendations with time-overlap avoidance.  Unlike the
    previous all-or-nothing approach, this supports partial combinations:
    if only a subset of services share a date, those services are combined
    while remaining services fall back to independent scoring.

    Returns a list of ServiceResult (one per service) or None if no
    cross-service combinations were found (caller should fall back to
    per-service profiling).
    """
    n_services = len(service_ids)

    # Phase A: Early exits
    if not day_time_cells:
        return None

    # Build attendance cache once for the entire cross-service scoring
    att_cache = build_attendance_cache(
        patient_visits or [], day_time_cells,
    )

    # Phase B: Build date indexes for all services
    # date_slots_index[service_idx][date] → list of slots
    date_slots_index: dict[int, dict] = {}
    # date_services[date] → set of service indices with slots on that date
    date_services: dict = {}

    for idx in range(n_services):
        date_map: dict = {}
        for slot in all_slots.get(idx, []):
            d = slot["vizites_datums"]
            date_map.setdefault(d, []).append(slot)
            date_services.setdefault(d, set()).add(idx)
        date_slots_index[idx] = date_map

    # Phase C: Find and rank combination dates (2+ services)
    # Compute tier info per service for ranking
    service_tier_matches: dict[int, list[tuple]] = {}
    for idx in range(n_services):
        slots = all_slots.get(idx, [])
        if slots:
            service_tier_matches[idx] = _match_slots_per_tier(
                slots, day_time_cells,
            )
        else:
            service_tier_matches[idx] = []

    # Build per-date scoring: best tier and rate among participating services
    date_tier_info: dict = {}  # date → (best_tier, best_rate)
    for idx, matches in service_tier_matches.items():
        for slot, cell, tier_num in matches:
            d = slot["vizites_datums"]
            current = date_tier_info.get(d, (999, 0.0))
            if tier_num < current[0] or (
                tier_num == current[0] and cell["rate"] > current[1]
            ):
                date_tier_info[d] = (tier_num, cell["rate"])

    combo_candidates = [
        (d, svc_set)
        for d, svc_set in date_services.items()
        if len(svc_set) >= 2
    ]
    if not combo_candidates:
        return None

    # Rank: most services first, then best tier, best rate, earliest date
    def _date_sort_key(item):
        d, svc_set = item
        best_tier, best_rate = date_tier_info.get(d, (999, 0.0))
        return (-len(svc_set), best_tier, -best_rate, d)

    combo_candidates.sort(key=_date_sort_key)

    # Phase D: Greedy group selection (up to 3)
    # First prioritise dates that cover uncovered services, then dates that
    # add more date-group options for already-covered services (up to 3
    # recommended slots per service).
    selected_groups: list[tuple] = []  # (date, set_of_service_indices)
    covered: set[int] = set()
    service_rec_count: dict[int, int] = {i: 0 for i in range(n_services)}

    for d, svc_set in combo_candidates:
        # Accept this date if any participating service still needs recs
        has_capacity = any(service_rec_count[i] < 3 for i in svc_set)
        if has_capacity and len(svc_set) >= 2:
            selected_groups.append((d, svc_set))
            covered |= svc_set
            for i in svc_set:
                service_rec_count[i] += 1
        if len(selected_groups) >= 3:
            break

    if not selected_groups:
        return None

    # Phase E: Build combination results per group
    # service_recommended[idx] accumulates SlotResults across groups
    service_recommended: dict[int, list[SlotResult]] = {
        idx: [] for idx in range(n_services)
    }

    # Precompute per-service per-date best tier (for ordering within group)
    def _best_tier_on_date(svc_idx, d):
        """Return (tier, -rate) for a service on a date, or fallback."""
        for slot, cell, tier_num in service_tier_matches.get(svc_idx, []):
            if slot["vizites_datums"] == d:
                return (tier_num, -cell["rate"])
        return (999, 0.0)  # no tier match — goes last

    for group_date, group_services in selected_groups:
        # Order services: best tier match first gets first time pick
        ordered = sorted(group_services, key=lambda i: _best_tier_on_date(i, group_date))

        occupied: list[tuple[int, int]] = []

        for idx in ordered:
            if len(service_recommended[idx]) >= 3:
                continue  # already has 3 recommendations
            date_slots = date_slots_index[idx].get(group_date, [])
            if not date_slots:
                continue

            result = _pick_best_slot_on_date(
                date_slots, day_time_cells, patient_visits,
                occupied=occupied, att_cache=att_cache,
            )
            occupied.append((result.start_time, result.end_time))
            service_recommended[idx].append(result)

    # Phase F: Assemble final results
    results: list[ServiceResult] = [None] * n_services  # type: ignore[list-item]

    for idx in range(n_services):
        recommended = service_recommended[idx]
        slots = all_slots.get(idx, [])

        if recommended:
            # Service participated in at least one combination group
            # Determine tier_used from the best recommendation
            tier_used = None
            for slot_result in recommended:
                if slot_result.profile_match and slot_result.profile_tier_level is not None:
                    t = slot_result.profile_tier_level
                    if tier_used is None or t < tier_used:
                        tier_used = t

            results[idx] = ServiceResult(
                service_id=service_ids[idx],
                specialist_id=specialist_ids[idx],
                earliest=_build_earliest(
                    slots, day_time_cells, att_cache=att_cache,
                ),
                recommended=recommended[:3],
                total_slots_evaluated=len(slots),
                tier_used=tier_used,
                profile_match=any(r.profile_match for r in recommended),
            )
        else:
            # Service not in any combination — independent scoring
            results[idx] = score_slots_for_service(
                slots=slots,
                day_time_cells=day_time_cells,
                patient_visits=patient_visits,
                service_id=service_ids[idx],
                specialist_id=specialist_ids[idx],
            )

    combo_count = len(selected_groups)
    combined_services = sum(1 for r in service_recommended.values() if r)
    logger.info(
        "Cross-service scoring: %d groups, %d/%d services combined",
        combo_count, combined_services, n_services,
    )
    return results


def _group_by_tier(cells: list[dict]) -> dict[int, list[dict]]:
    """Group day-time cells by tier, sorted by rate descending within each tier."""
    tiers: dict[int, list[dict]] = {}
    for cell in cells:
        tier = cell.get("tier", cell.get("tier_ci", 999))
        tiers.setdefault(tier, []).append(cell)

    # Sort each tier's cells by rate descending
    for tier_cells in tiers.values():
        tier_cells.sort(key=lambda c: c["rate"], reverse=True)

    return tiers


def _match_slots_per_tier(
    slots: list[dict], day_time_cells: list[dict]
) -> list[tuple[dict, dict, int]]:
    """Per-slot tier cascade: each slot finds its best (lowest) matching tier.

    Returns list of (slot, matching_cell, tier_num) tuples.
    Only includes slots that matched at least one tier.
    """
    tiers = _group_by_tier(day_time_cells)
    tier_order = sorted(tiers.keys())

    # Build per-tier lookups: (weekday, hour) → cell
    tier_lookups: list[tuple[int, dict[tuple[int, int], dict]]] = []
    for tier_num in tier_order:
        lookup: dict[tuple[int, int], dict] = {}
        for cell in tiers[tier_num]:
            for h in cell["hours"]:
                lookup[(cell["day_of_week"], h)] = cell
        tier_lookups.append((tier_num, lookup))

    matched = []
    for slot in slots:
        weekday = slot["vizites_datums"].isoweekday()
        hour = slot_hour(slot["sakuma_laiks"])
        key = (weekday, hour)
        for tier_num, lookup in tier_lookups:
            if key in lookup:
                matched.append((slot, lookup[key], tier_num))
                break  # best tier found for this slot

    return matched


def _find_matching_window(
    cells: list[dict], weekday: int, hour: int
) -> dict | None:
    """Find the day-time cell that matches a given weekday + hour."""
    for cell in cells:
        if cell["day_of_week"] == weekday and hour in cell["hours"]:
            return cell
    return None


def _build_fallback_recommended(
    slots: list[dict],
) -> list[SlotResult]:
    """Build recommended list from earliest slots when no tier matches."""
    results = []
    for slot in slots:
        results.append(
            _slot_to_result(
                slot,
                score=None,
                profile_match=False,
            )
        )
    return results
