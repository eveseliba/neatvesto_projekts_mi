# Scheduling API

Appointment scheduling endpoints that find optimal appointment slots using patient profile matching and historical attendance data.

---

## Endpoints

| Method | Path              | Description                                      |
| ------ | ----------------- | ------------------------------------------------ |
| POST   | `/schedule`         | Single-service slot recommendations                |
| POST   | `/schedule/multi`   | Multi-service combination plans (1-5 services)     |
| POST   | `/schedule/regular` | Recurring appointments over a time span            |

All endpoints require a connection to the scheduling database (PostgreSQL). If the database is unavailable, they return **503 Service Unavailable**.

All endpoints require Bearer token authentication (`MAIN_API_TOKEN`). Include `Authorization: Bearer <token>` in all requests.

---

## POST `/schedule`

Finds the **earliest available** slot and up to **3 recommended** slots for a single service, scored by patient profile match and attendance history.

### Request

| Field              | Type      | Required | Default  | Description                                                           |
| ------------------ | --------- | -------- | -------- | --------------------------------------------------------------------- |
| `patient_age`      | int       | yes      |          | Patient's age in years                                                |
| `patient_address`  | string    | yes      |          | Patient's address (city/district)                                     |
| `hospital_id`      | int       | no       | `1`      | Facility ID (1=Torņakalns, 2=Gaiļezers, 4=Rehab, 6=Daugavpils, 8=Valmiera) |
| `service_id`       | int       | cond.    |          | Service ID (at least one of `service_id` / `specialist_id` required)  |
| `specialist_id`    | int       | cond.    |          | Specialist ID                                                         |
| `patient_id`       | int       | no       |          | Patient ID — enables attendance history lookup for personalized scoring |
| `appointment_type` | list[int] | no       |          | Appointment type code filter — see [Appointment Type Codes](#appointment-type-codes-vizites_veids). Single int also accepted (auto-coerced to list); empty list treated as None. SQL uses `IN (...)` for multiple values. |
| `slot_filter`      | string    | no       | `"free"` | `"free"` or `"free_and_reserved"`                                     |
| `reception_mode`  | string \| list[string] | no | `"0001"` | Reception mode code (`pakalpojuma_sveids`). Accepts a single code or a list. Both zero-padded (`"0002"`) and bare (`"2"`) formats are expanded automatically. Default is `"0001"` (ambulatory). Known values: `"0001"`, `"0002"`, `"0003"`, `"0004"`. |
| `paid_visit`       | bool      | no       |          | Filter by paid (`true`) or free (`false`) visits                      |
| `excluded_dates`   | list[str] | no       | `[]`     | Dates to skip, format `"YYYY-MM-DD"`                                  |

### Example

```bash
curl -X POST http://localhost:8000/schedule \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 5,
    "patient_address": "Rīga",
    "service_id": 138
  }'
```

### Response

```json
{
  "patient_profile": "toddler_0-10km",
  "results": [
    {
      "service_id": 138,
      "specialist_id": null,
      "earliest": { "...SlotResult..." },
      "recommended": [ "...up to 3 SlotResults..." ],
      "total_slots_evaluated": 85,
      "tier_used": 1,
      "profile_match": true,
      "profile_match_reason": "Tier 1 match (Fri 08:00-09:00, rate 0.743)"
    }
  ]
}
```

**Response fields:**

| Field                  | Type              | Description                                                   |
| ---------------------- | ----------------- | ------------------------------------------------------------- |
| `patient_profile`      | string            | Resolved profile key (e.g. `"toddler_0-10km"`)               |
| `results`              | list[ServiceResult] | One entry per service (single for `/schedule`)              |
| `results[].earliest`   | SlotResult\|null  | Earliest available slot (no scoring applied)                  |
| `results[].recommended`| list[SlotResult]  | Top 3 scored slots matching the patient profile               |
| `results[].tier_used`  | int\|null         | Best profile tier matched (1=best .. 5=worst)                |
| `results[].profile_match` | bool           | Whether any profile window matched                            |
| `results[].profile_match_reason` | string\|null | Human-readable tier match description                  |
| `results[].total_slots_evaluated` | int       | Total slots considered                                      |

---

## POST `/schedule/multi`

Finds optimal plans that **combine multiple services into as few hospital visits as possible**. Returns up to **3 ranked plans** plus the earliest slot per service.

### Request

| Field              | Type                | Required | Default  | Description                                   |
| ------------------ | ------------------- | -------- | -------- | --------------------------------------------- |
| `patient_age`      | int                 | yes      |          | Patient's age in years                        |
| `patient_address`  | string              | yes      |          | Patient's address (city/district)             |
| `hospital_id`      | int                 | no       | `1`      | Facility ID                                   |
| `services`         | list[ServiceItem]   | yes      |          | 1-5 services to schedule                      |
| `patient_id`       | int                 | no       |          | Patient ID for attendance history              |
| `slot_filter`      | string              | no       | `"free"` | `"free"` or `"free_and_reserved"`             |
| `reception_mode`  | string \| list[string] | no    | `"0001"` | Reception mode code (`pakalpojuma_sveids`). Accepts a single code or a list. Both zero-padded (`"0002"`) and bare (`"2"`) formats are expanded automatically. Default is `"0001"` (ambulatory). Known values: `"0001"`, `"0002"`, `"0003"`, `"0004"`. |
| `appointment_type` | list[int]           | no       |          | Appointment type code filter — see [Appointment Type Codes](#appointment-type-codes-vizites_veids). Single int also accepted (auto-coerced to list); empty list treated as None. SQL uses `IN (...)` for multiple values. |
| `paid_visit`       | bool                | no       |          | Filter by paid (`true`) or free (`false`) visits |
| `excluded_dates`   | list[str]           | no       | `[]`     | Dates to skip                                 |

**ServiceItem:**

| Field           | Type    | Required | Description         |
| --------------- | ------- | -------- | ------------------- |
| `service_id`    | int     | yes      | Service ID          |
| `specialist_id` | int     | no       | Preferred specialist |

### Example

```bash
curl -X POST http://localhost:8000/schedule/multi \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 10,
    "patient_address": "Jelgava",
    "services": [
      {"service_id": 138},
      {"service_id": 200},
      {"service_id": 310}
    ]
  }'
```

### Response

```json
{
  "patient_profile": "school_age_10-30km",
  "earliest": [
    { "...SlotResult for service 138..." },
    { "...SlotResult for service 200..." },
    null
  ],
  "plans": [
    {
      "rank": 1,
      "score": 2.15,
      "visits": [
        {
          "date": "2026-03-10",
          "slots": [
            { "...SlotResult for service 138..." },
            { "...SlotResult for service 200..." }
          ]
        },
        {
          "date": "2026-03-12",
          "slots": [
            { "...SlotResult for service 310..." }
          ]
        }
      ]
    }
  ]
}
```

**Response fields:**

| Field              | Type                | Description                                              |
| ------------------ | ------------------- | -------------------------------------------------------- |
| `patient_profile`  | string              | Resolved profile key                                     |
| `earliest`         | list[SlotResult\|null] | One per service, earliest available regardless of score |
| `plans`            | list[Plan]          | Up to 3 ranked plans                                     |
| `plans[].rank`     | int                 | Plan rank (1 = best)                                     |
| `plans[].score`    | float               | Combined score across all slots                          |
| `plans[].visits`   | list[PlanVisit]     | Grouped by date, sorted chronologically                  |

**Plan ranking:** Plans are sorted by **fewest visits first** (fewer hospital trips is better), then by total score descending. Slots on the same date are grouped into a single visit with a minimum **15-minute gap** between appointments.

---

## POST `/schedule/regular`

Schedules **recurring appointments** over a computed time span. Given `start_date`, `times_per_week`, and `total_times`, the API divides the date range into calendar work-weeks (Mon-Fri) and picks up to `times_per_week` slots per week using the patient's profile, with ≥1 day spacing preference between visits. The initial search window covers `ceil(total_times / times_per_week)` weeks (no buffer). If not enough visits are found, the endpoint **iteratively extends** the window by `ceil(max_missing / times_per_week) + 4 weeks` and makes additional DB queries, repeating until all visits are found or the 1-year limit is reached. Supports both single-service and multi-service scheduling with carry-forward logic.

### Request

| Field             | Type              | Required | Default  | Description                                                              |
| ----------------- | ----------------- | -------- | -------- | ------------------------------------------------------------------------ |
| `patient_age`     | int               | yes      |          | Patient's age in years                                                   |
| `patient_address` | string            | yes      |          | Patient's address (city/district)                                        |
| `hospital_id`     | int               | no       | `1`      | Facility ID                                                              |
| `services`        | list[ServiceItem] | yes      |          | 1-5 services to schedule                                                 |
| `start_date`      | string            | yes      |          | Start date `YYYY-MM-DD` — must be today or in the future                 |
| `times_per_week`  | int               | yes      |          | Number of appointments per week (1-5, workdays only)                     |
| `total_times`     | int               | yes      |          | Total appointments to schedule (1 to `times_per_week × 52`)             |
| `patient_id`      | int               | no       |          | Patient ID for attendance history                                        |
| `slot_filter`     | string            | no       | `"free"` | `"free"` or `"free_and_reserved"`                                        |
| `reception_mode` | string \| list[string] | no  | `"0001"` | Reception mode code (`pakalpojuma_sveids`). Accepts a single code or a list. Both zero-padded (`"0002"`) and bare (`"2"`) formats are expanded automatically. Default is `"0001"` (ambulatory). Known values: `"0001"`, `"0002"`, `"0003"`, `"0004"`. |
| `appointment_type`| list[int]         | no       |          | Appointment type code filter — see [Appointment Type Codes](#appointment-type-codes-vizites_veids). Single int also accepted (auto-coerced to list); empty list treated as None. SQL uses `IN (...)` for multiple values. |
| `paid_visit`      | bool              | no       |          | Filter by paid (`true`) or free (`false`) visits                         |
| `excluded_dates`  | list[str]         | no       | `[]`     | Dates to skip                                                            |

**ServiceItem** is the same as in `/schedule/multi` (`service_id` required, `specialist_id` optional).

### Example

```bash
curl -X POST http://localhost:8000/schedule/regular \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 7,
    "patient_address": "Riga",
    "services": [{"service_id": 138}],
    "start_date": "2026-03-31",
    "times_per_week": 1,
    "total_times": 4
  }'
```

### Response

```json
{
  "patient_profile": "school_age_0-10km",
  "start_date": "2026-03-31",
  "times_per_week": 1,
  "total_times": 4,
  "total_visits": 4,
  "visits": [
    {
      "date": "2026-04-01",
      "slots": [
        {
          "slots": [{"slot_id": 501, "specialist_id": 10}],
          "date": "2026-04-01",
          "start_time": 721,
          "end_time": 740,
          "start_time_formatted": "12:01",
          "end_time_formatted": "12:20",
          "specialty_id": 4,
          "hospital_id": 1,
          "service_id": 138,
          "appointment_type": 6,
          "score": 0.5504,
          "profile_match": true,
          "profile_tier_level": 1,
          "profile_window_rate": 0.64,
          "label": "individual_best",
          "days_since_previous": null
        }
      ]
    },
    {
      "date": "2026-04-08",
      "slots": [
        {
          "...same SlotResult fields...",
          "label": "individual_best",
          "days_since_previous": 7
        }
      ]
    }
  ]
}
```

**Response fields:**

| Field              | Type                    | Description                                        |
| ------------------ | ----------------------- | -------------------------------------------------- |
| `patient_profile`  | string                  | Resolved profile key                               |
| `start_date`       | string                  | Echoed start date from request                     |
| `times_per_week`   | int                     | Echoed times per week from request                 |
| `total_times`      | int                     | Echoed total times requested                       |
| `total_visits`     | int                     | Actual visits scheduled (may be < `total_times` if slots are sparse) |
| `visits`           | list[RegularPlanVisit]  | Chronological list of scheduled visits              |

Each slot in `visits[].slots` is a standard `SlotResult` extended with two additional fields:

| Field                 | Type       | Description                                                                |
| --------------------- | ---------- | -------------------------------------------------------------------------- |
| `label`               | string     | How this slot was placed (see Labels below)                                |
| `days_since_previous` | int\|null  | Calendar days since the last visit for the same `service_id`; `null` for the first occurrence |

### Labels

| Label              | Meaning                                                             |
| ------------------ | ------------------------------------------------------------------- |
| `combination`      | Placed as part of a multi-service same-day arrangement              |
| `individual_best`  | Best profile-matched slot (single service or Phase 2 fill)          |
| `earliest`         | Fallback when no profile match available (carry-forward catch-up)   |

### Spacing Preference

Within each work-week, the algorithm prefers ≥1 day gap between visits:

- **Primary:** picks slots with ≥1 calendar day gap from the previous pick in the same week
- **Fallback:** if no gapped slot exists, picks adjacent days (no empty week)
- **Cross-week:** the last pick of week N influences the first pick of week N+1

Only applies to profile-matched slots; unmatched slots fall back to the earliest available.

### Time-Gap Preference

For multi-service same-day arrangements, the algorithm prefers combinations where the **max gap between any two consecutive appointments** is ≤ 120 minutes. This ensures patients don't wait hours between visits. The check is per-gap (not total span), so 5 services scheduled back-to-back are fine even if the total duration exceeds 120 min.

### Specialist Lock-in

After the first slot fetch, the algorithm auto-selects one specialist per service based on work-week coverage (how many weeks the specialist has slots). This specialist is used for all subsequent weeks, ensuring patients see the same doctor consistently. Does **not** apply when `specialist_id` is explicitly provided in the request.

### Overlap Prevention

Individual fill (Phase 2) prevents time overlaps between slots placed on the same date by tracking occupied time ranges with a 15-minute buffer between appointments.

### Constraints

| Constraint          | Value                                            |
| ------------------- | ------------------------------------------------ |
| `start_date`        | Must be today or in the future                   |
| `times_per_week`    | 1-5 (workdays only)                              |
| `total_times`       | 1 to `times_per_week × 52`                      |
| Search window       | `ceil(total_times / times_per_week) + 10` weeks, capped at 52 weeks |
| Visit trimming      | After scoring, visits are trimmed to `total_times` |
| Services            | 1-5                                              |
| Partial results     | Returned when insufficient slots found (`total_visits < total_times`) |

---

## SlotResult Model

Every slot (in `earliest`, `recommended`, and `plans`) uses this format:

| Field                  | Type                      | Description                                        |
| ---------------------- | ------------------------- | -------------------------------------------------- |
| `slots`                | list[{slot_id, specialist_id}] | Available slot/specialist pairs for this time  |
| `date`                 | string                    | Appointment date (`"YYYY-MM-DD"`)                  |
| `start_time`           | int                       | Start time (minutes since midnight)                |
| `end_time`             | int                       | End time (minutes since midnight)                  |
| `start_time_formatted` | string                    | Start time (`"HH:MM"`)                             |
| `end_time_formatted`   | string                    | End time (`"HH:MM"`)                               |
| `specialty_id`         | int                       | Medical specialty ID                               |
| `hospital_id`          | int                       | Hospital facility ID                               |
| `service_id`           | int\|null                 | Service ID                                         |
| `appointment_type`     | int\|null                 | Appointment type code                              |
| `score`                | float\|null               | Combined scoring value (null for earliest slots)   |
| `profile_match`        | bool                      | Whether the slot matched a patient profile window  |
| `profile_tier_level`   | int\|null                 | Profile tier (1=best .. 5=worst)                  |
| `profile_window_rate`  | float\|null               | Raw attendance rate for the matched profile window  |

---

## How Scoring Works

### Patient Profile Resolution

Based on `patient_age` and `patient_address`, the API resolves a patient profile:

1. **Age group:** `toddler` (0-6), `school_age` (7-18), `adult` (18+)
2. **Distance group:** `0-10km`, `10-30km`, `30-100km`, `100+km` (calculated from address to hospital via coordinates lookup)
3. **Profile key:** `{age_group}_{distance_group}` (e.g. `"toddler_0-10km"`)

Each profile contains 25 day-time cells (5 weekdays x 5 time windows) with attendance rates and tiers from the profiling pipeline.

### Slot Scoring

Each slot is scored using:

```
score = profile_score × (0.3 + 0.7 × attendance_probability)
```

- **`profile_score`** — attendance rate from the patient's profile for this day/time window (0-1)
- **`attendance_probability`** — empirical Bayes estimate from the patient's visit history (requires `patient_id`; defaults to 0.5 when unavailable)

The `profile_window_rate` field in the response contains the raw profile score; `score` is the combined value used for ranking.

### Tier Matching

Profile windows are assigned tiers (1-5) via statistical confidence interval overlap:
- **Tier 1 (Best)** — statistically highest attendance rate
- **Tier 2 (Good)** — high attendance, overlapping CI with Tier 1
- **Tier 3 (Average)** — moderate attendance
- **Tier 4 (Below avg)** — below average attendance
- **Tier 5 (Worst)** — lowest attendance or low-sample windows

The scoring algorithm cascades through tiers: it tries the best available tier first and falls back to lower tiers if no higher-tier slots are available.

---

## Configuration

### Database

The scheduling endpoints connect to a PostgreSQL database containing appointment slot data.

**Environment variables:**

| Variable                | Default     | Description                  |
| ----------------------- | ----------- | ---------------------------- |
| `SCHEDULING_DB_HOST`    | `localhost` | Database host                |
| `SCHEDULING_DB_PORT`    | `5432`      | Database port                |
| `SCHEDULING_DB_NAME`    | `bkus`      | Database name                |
| `SCHEDULING_DB_USER`    | `postgres`  | Database user                |
| `SCHEDULING_DB_PASSWORD`| `postgres`  | Database password            |

Alternatively, create a `db_config.json` file and set `SCHEDULING_DB_CONFIG` to its path. Full example (`db_config.json.template`):

```json
{
  "host": "localhost",
  "port": 5432,
  "dbname": "bkus",
  "user": "postgres",
  "password": "postgres",
  "minconn": 2,
  "maxconn": 10,
  "statement_timeout_ms": 5000,
  "search_months": 4,
  "max_slots_per_query": 5000,
  "tables": {
    "slots": "saule.vizisu_laiki",
    "visits_history": [
      "bkus.vizites_un_dzestas_23_24",
      "bkus.vizites_un_dzestas_25"
    ]
  }
}
```

**Config priority:** Environment variables > `db_config.json` > built-in defaults. All fields are optional — only override what differs from defaults.

### Database Tables

| Table                              | Purpose                                   |
| ---------------------------------- | ----------------------------------------- |
| `saule.vizisu_laiki`               | Available appointment slots               |
| `bkus.vizites_un_dzestas_23_24`    | Visit history (2023-2024)                 |
| `bkus.vizites_un_dzestas_25`       | Visit history (2025)                      |

Table names are configurable via `db_config.json`.

### Key Parameters

| Parameter       | Default | Description                                           |
| --------------- | ------- | ----------------------------------------------------- |
| `search_months` | 4       | How many months ahead to search for available slots   |
| `max_slots_per_query` | 5000 | Maximum slots returned per SQL query               |
| `statement_timeout_ms` | 5000 | SQL statement timeout (milliseconds)              |

---

## Scheduling Rules & Constraints

### Slot Filtering

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **Status filter (free)** | `"0001"`, `"1"` | Default — only free slots |
| **Status filter (free+reserved)** | `"0001"`, `"0002"`, `"1"`, `"2"` | When `slot_filter: "free_and_reserved"` |
| **Reception mode** | `pakalpojuma_sveids IN (...)` | Controlled by `reception_mode` parameter (default `"0001"` — ambulatory). Accepts a single code or a list; both zero-padded and bare formats expanded automatically. NULL excluded |
| **Past-slot buffer** | 60 minutes | Slots on today's date must start at least 1 hour from now (Europe/Riga timezone) |
| **Excluded dates** | YYYY-MM-DD | Slots on these dates are excluded from results |

### Search Window

| Rule | Value | Configurable |
| ---- | ----- | ------------ |
| **`/schedule` earliest** | Today + 4 months (with 60-min buffer) | Yes — `search_months` in `db_config.json` |
| **`/schedule` recommended** | Tomorrow + 4 months | Yes — `search_months` |
| **`/schedule/multi` recommended** | Tomorrow + 4 months | Yes — `search_months` |
| **`/schedule/multi` earliest** | Today + 4 months | Yes — `search_months` |
| **`/schedule/regular`** | `start_date` → computed `until_date` | No — derived from `start_date`, `times_per_week`, `total_times` |
| **Max slots per SQL query** | 5,000 | Yes — `max_slots_per_query` in `db_config.json` |

### Slot Grouping & Caps

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **Slot grouping** | `(date, start_time, end_time, service_id)` | Slots from different specialists at the same time are merged into a single group |
| **Adaptive slot cap** | 2,500 grouped slots | When exceeded, the date window is trimmed to preserve nearest dates |
| **Pre-pruning** | Top 15 per (date, service) | Before combination generation, only the best 15 candidates per service per date are kept |

### Appointment Gap Validation

| Rule | Value |
| ---- | ----- |
| **Minimum gap between appointments** | 15 minutes |
| **Applied to** | Same-date appointments in multi-service plans |
| **Validation** | Backtracking ensures no overlapping or too-close appointments |

### Plan Building (Multi-Service)

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **Maximum plans** | 3 | Up to 3 distinct plans are returned |
| **Maximum attempts per plan slot** | 30 | Retries to find a plan with a unique fingerprint |
| **Minimum services per combination** | 2 | At least 2 services must overlap on a date for a combination |
| **Plan fingerprint** | `(date, service_indices)` | Plans with the same services on the same dates are considered duplicates |
| **Zero-combo fallback** | Fingerprint dedup bypassed | When no dates have 2+ services, multiple plans are generated with different slots (same structure) |
| **Phase 2 fill** | Individual best slot | Services not covered by any combination get their individually best slot |
| **Plan sorting** | Fewest visits first, then score descending | Fewer hospital trips is preferred over higher scores |

### Scoring & Recommendations

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **Combined score formula** | `profile_rate × (0.3 + 0.7 × att_prob)` | Balances profile match with attendance history |
| **Default attendance probability** | 0.5 | Used when `patient_id` not provided or no history found |
| **Top recommended slots** | 3 | Per service in `/schedule` response |
| **Tier cascade** | 1 → 2 → 3 → 4 → 5 | Tries best tier first, falls back to lower tiers |

### Patient Attendance History

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **History lookback** | 2 years | Only visits from the last 2 years are considered |
| **Shrinkage parameter (k)** | 5 | ~5 observations needed before trusting a specific rate |
| **Three-level hierarchy** | Overall → Day → Day+Time | Each level borrows strength from the level above |
| **No history fallback** | `att_prob = None` → default 0.5 | Applied in the combined score formula |

#### Why Empirical Bayes Shrinkage

**The problem:** A patient with 2 past visits — both attended on Mondays at 10:00 — would naively get a 100% attendance probability for that time slot. But with only 2 observations, that estimate is unreliable. Conversely, a patient with 50 visits has enough data that their specific rate is trustworthy. The challenge is knowing when to trust the specific data and when to fall back to broader averages.

**The solution:** Empirical Bayes hierarchical shrinkage automatically blends specific observations with broader priors. With few observations, the estimate is "pulled" (shrunk) toward the population average. As evidence accumulates, the estimate gradually shifts toward the patient's actual rate. The shrinkage parameter `k=5` controls the tradeoff: ~5 observations at a given level are needed before the specific rate begins to dominate.

**Three-level hierarchy:**

```
Level 1 (prior):     overall_rate = attended_total / visits_total
Level 2 (day):       day_rate     = (attended_day + 5 × overall_rate) / (visits_day + 5)
Level 3 (day+time):  probability  = (attended_dt  + 5 × day_rate)     / (visits_dt  + 5)
```

Each level borrows strength from the one above. A patient with no Monday visits still gets a reasonable Monday estimate from their overall rate. A patient with no 10:00 visits on Mondays still gets a Monday-morning estimate from their overall Monday rate.

**Real-world precedent:** This technique is well-established in domains with sparse, hierarchical data:

- **Baseball analytics** — The canonical use case. Batting averages for players with few at-bats are shrunk toward the league average. A player hitting .400 in 10 at-bats isn't estimated as a .400 hitter; the estimate is pulled toward ~.260. This was popularized by Bradley Efron and Carl Morris in their 1975 paper *"Data Analysis Using Stein's Estimating Procedure"* and is now standard in sabermetrics.

- **Healthcare & clinical trials** — Hospital performance metrics (mortality rates, readmission rates) use empirical Bayes to fairly compare small rural hospitals against large urban ones. CMS (Centers for Medicare & Medicaid Services) uses hierarchical models for their Hospital Compare star ratings. Without shrinkage, a small hospital with 3 cases and 0 deaths would appear to have 0% mortality — clearly unreliable.

- **Insurance & actuarial science** — Known as "credibility theory" (Bühlmann model). Premium estimates for small groups are blended with portfolio-wide rates. The principle is identical: with limited claims history, trust the group average; with extensive history, trust the individual rate.

- **Recommendation systems** — Movie rating platforms (Netflix Prize) use similar hierarchical Bayesian approaches to estimate ratings for users/items with few observations, shrinking toward genre or user-group averages.

**Why it fits this use case:** Hospital appointment attendance is a natural hierarchical data problem:
- Most patients have relatively few visits (sparse data)
- Attendance patterns genuinely vary by day and time of day (hierarchy exists)
- The overall attendance rate provides a sensible prior (stable base rate)
- We need reliable estimates even for patients with 2-3 total visits

The alternative — using raw frequencies — would produce extreme and unstable estimates for most patients. Empirical Bayes gives stable, sensible estimates across the full range of patient history sizes, automatically adapting its confidence as data accumulates.

### Patient Profile Resolution

| Step | Logic |
| ---- | ----- |
| **Age group** | `toddler` (0-6), `school_age` (7-18), `adult` (18+) |
| **Distance calculation** | In-memory coordinates lookup → Nominatim geocoding fallback |
| **Distance group** | `0-10km`, `10-30km`, `30-100km`, `100+km` |
| **Distance fallback** | Same city as hospital → `0-10km`; different city → `30-100km` |
| **Profile fallback** | If exact profile not found, try any profile with same age group |

### Database Resilience

| Rule | Value | Description |
| ---- | ----- | ----------- |
| **Connection pool** | 2-10 connections | Configurable via `minconn`/`maxconn` |
| **Pool timeout** | 5 seconds | Max wait for available connection |
| **Statement timeout** | 5 seconds | Per-query timeout (configurable) |
| **Retry logic** | 3 retries, exponential backoff | 0.5s, 1s, 2s delays on connection errors |
| **Health check** | Every 30 seconds | Background thread attempts reconnection if pool is down |
| **Graceful degradation** | `/schedule` returns 503 | Other endpoints (`/predict`, `/profile`) remain available |

### Hardcoded vs Configurable Summary

| Parameter | Default | Configurable? |
| --------- | ------- | ------------- |
| Search months | 4 | Yes (`db_config.json`) |
| Max slots per query | 5,000 | Yes (`db_config.json`) |
| Statement timeout | 5,000ms | Yes (`db_config.json`) |
| Pool min/max connections | 2/10 | Yes (`db_config.json`) |
| DB tables | `saule.vizisu_laiki`, `bkus.vizites_un_dzestas_*` | Yes (`db_config.json`) |
| Appointment buffer | 15 min | No |
| Slot cap | 2,500 | No |
| Candidates per service | 15 | No |
| Max plans | 3 | No |
| Recommended slots | 3 | No |
| Attendance default | 0.5 | No |
| History lookback | 2 years | No |
| Shrinkage parameter | 5 | No |
| Past-slot buffer | 60 min | No |
| Max services | 5 | No |
| Retry max | 3 | No |

---

## Appointment Type Codes (`vizites_veids`)

Client-side codelist for the `appointment_type` request filter and the `appointment_type` response field. Values correspond to the `vizites_veids` column in the scheduling database.

| Code | Label (LV)                                          | Description                                          |
|------|------------------------------------------------------|------------------------------------------------------|
| 1    | Ar laiku un vizītes ilgumu                           | With time & visit duration (default). State-funded, typically first-time patients |
| 2    | Ar laiku un vizīšu skaitu                            | With time & visit count                              |
| 3    | Bez laika ar vizītes ilgumu                          | Without time, with visit duration                    |
| 4    | Bez laika ar vizīšu skaitu                           | Without time, with visit count                       |
| 5    | Rezervētais laiks ar vizītes ilgumu                  | Reserved time with visit duration                    |
| 6    | Rezervētais laiks ar vizīšu skaitu                   | Reserved time with visit count                       |
| 7    | Hronisks pacients ar vizītes ilgumu                  | Chronic patient with visit duration                  |
| 8    | Hronisks pacients ar vizīšu skaitu                   | Chronic patient with visit count                     |
| 9    | Prioritāras konsultācijas pacients ar vizītes ilgumu | Priority consultation patient with visit duration    |
| 10   | Prioritāras konsultācijas pacients ar vizīšu skaitu  | Priority consultation patient with visit count       |
| 11   | Attālināta konsultācija ar vizītes ilgumu            | Remote consultation with visit duration              |
| 12   | Attālināta konsultācija ar vizīšu skaitu             | Remote consultation with visit count                 |

**Example:** `"appointment_type": [5, 6, 7]` selects reserved-time and chronic-patient slots.

---

## Module Structure

```
scheduling/
├── db.py               # Database connection pool, config loading, health checks
├── validators.py       # Shared Pydantic validators (excluded_dates, identifiers)
├── schedule_request.py # Request models for /schedule (ScheduleRequest, ServiceRequest)
├── schedule_response.py# Response models for /schedule (SlotResult, ServiceResult, etc.)
├── multi_request.py    # Request models for /schedule/multi
├── multi_response.py   # Response models for /schedule/multi (Plan, PlanVisit)
├── regular_request.py  # Request models for /schedule/regular
├── regular_response.py # Response models for /schedule/regular
├── health.py           # HealthResponse model
├── slot_query.py       # SQL query builder for available slots
├── scorer.py           # Single-service scoring and ranking
├── multi_scorer.py     # Multi-service combination scheduling algorithm
├── regular_scorer.py   # Recurring appointment scheduling algorithm
└── patient_history.py  # Patient visit history, empirical Bayes attendance
```

### Dependencies

```
shared/
├── coordinates.py      # Address resolution, haversine distance, Nominatim geocoding
├── distance_enrichment.py  # DataFrame-level distance enrichment
└── profile.py          # Patient profile resolution (age group + distance → profile key)
```

---

## Startup Behavior

On API startup:

1. Loads `day_time_interactions.json` (patient profiles from training pipeline)
2. Parses `latvian_places_with_coordinates_v2.sql` into in-memory coordinate lookup
3. Creates database connection pool from config
4. Starts background health-check thread (auto-reconnect if DB goes down)

If the scheduling database is unavailable at startup, the API still starts — prediction and profiling endpoints work normally. Scheduling endpoints return **503** until the database becomes available.

---

## Error Handling

| HTTP Code | Condition                                              |
| --------- | ------------------------------------------------------ |
| 200       | Slots found and scored successfully                    |
| 401       | Missing or invalid Bearer token                        |
| 422       | Invalid request (missing required fields, bad format)  |
| 500       | Internal error (query failure, unexpected exception)   |
| 503       | Scheduling database not connected                      |
