import os, time, tempfile, uuid, logging, argparse
import pandas as pd, traceback, platform
from datetime import date
from pycaret.classification import setup, compare_models, finalize_model, save_model
from fsstate import read_status, update_status, now_iso, try_lock, unlock

from training.data_loader import load_data
from training.data_preprocessing import (
    handle_patient_birth_year,
    convert_dates,
    fix_data_types,
    fill_missing_text_fields,
    standardize_text
)
from training.feature_engineering import (
    create_days_until_appointment,
    add_time_period,
    add_day_of_week,
    add_children_holiday,
    calculate_patient_age,
    add_repeated_patient_features
)
from training.model_training import (
    setup_pycaret,
    train_best_model,
    tune_best_model
)
from training.csv_exporter import export_training_csv, export_profiling_csv
from profiling.pipeline import run_profiling_pipeline
from shared.distance_enrichment import enrich_distances, geocode_unknown_addresses
from shared.db import get_connection

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

MODEL_DIR = os.getenv("MODEL_DIR", "/app")
MODEL_BASENAME = os.getenv("MODEL_BASENAME", "model")
POLL = float(os.getenv("POLL_INTERVAL", "60.0"))
EXPORT_WINDOW_MONTHS = int(os.getenv("EXPORT_WINDOW_MONTHS", "8"))

# ---------------------------------------------------------------------------
# Monthly schedule helpers
# ---------------------------------------------------------------------------


def _is_first_saturday(d: date) -> bool:
    """True if *d* is the first Saturday of its month (day 1-7, weekday=5)."""
    return d.weekday() == 5 and d.day <= 7


def _is_sunday_after_first_saturday(d: date) -> bool:
    """True if *d* is the Sunday right after the first Saturday (day 2-8, weekday=6)."""
    return d.weekday() == 6 and 2 <= d.day <= 8


def _month_key(d: date) -> str:
    """Return 'YYYY-MM' string for idempotency checks."""
    return d.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Saturday: export CSVs from DB
# ---------------------------------------------------------------------------


def _run_monthly_export() -> bool:
    """Query the DB and write training.csv + profiling_input.csv.

    Returns True on success, False on failure.
    """
    training_path = os.path.join(MODEL_DIR, "training.csv")
    profiling_path = os.path.join(MODEL_DIR, "profiling_input.csv")

    print(f"[EXPORT] Connecting to DB (window={EXPORT_WINDOW_MONTHS} months)")
    with get_connection() as conn:
        n_train = export_training_csv(
            conn, training_path, window_months=EXPORT_WINDOW_MONTHS,
        )
        print(f"[EXPORT] training.csv written: {n_train} rows")

        n_prof = export_profiling_csv(
            conn, profiling_path, window_months=EXPORT_WINDOW_MONTHS,
        )
        print(f"[EXPORT] profiling_input.csv written: {n_prof} rows")

    return True


def _queue_training_job():
    """Write a training job ticket to status.json (same as POST /train)."""
    job_id = str(uuid.uuid4())
    csv_path = os.path.join(MODEL_DIR, "training.csv")

    def mut(s: dict):
        s.update({
            "job_id": job_id,
            "job_type": "training",
            "csv_path": csv_path,
            "target": "appointment_attended",
            "processing": now_iso(),
            "done": None,
            "reloaded": None,
            "stage": "queued",
            "error": None,
            "claimed_by": None,
        })

    update_status(mut)
    print(f"[SCHEDULER] Queued training job {job_id}")

def atomic_replace(src, dst): os.replace(src, dst)

def _run_profiling_only():
    """Run only the profiling pipeline using csv_path from status.json."""
    st = read_status()
    csv_path = st.get("csv_path") or os.path.join(MODEL_DIR, "profiling_input.csv")

    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Profiling CSV not found: {csv_path}. "
            "Export data first with 'python worker.py export'."
        )

    update_status(lambda s: s.update({"stage": "profiling"}))
    print(f"[PROFILING] Running profiling pipeline on {csv_path}")
    run_profiling_pipeline(csv_path=csv_path, model_dir=MODEL_DIR)
    update_status(lambda s: s.update({"stage": "done", "done": now_iso()}))
    print("[PROFILING] Profiling pipeline complete.")


def _train():
    start_total = time.time()
    st = read_status()
    csv = st["csv_path"]
    target = st["target"]

    update_status(lambda s: s.update({"stage": "loading_csv"}))
    df = pd.read_csv(csv)
    if target not in df.columns:
        raise ValueError(f"Missing target column '{target}'")

    update_status(lambda s: s.update({"stage": "training"}))
    
    # Run training and save model in same context
    run_training_and_save()

    update_status(lambda s: s.update({"stage": "done", "done": now_iso()}))
    total_time = time.time() - start_total
    print(f"Total pipeline time: {total_time:.2f}s")

def run_training_and_save():
    # 1. Load data
    st = read_status()
    csv_path = st.get("csv_path")
    print(f"1. Load data from {csv_path}")
    df = load_data(csv_path)
    if df is None:
        return

    # 2. Preprocess
    print("2. Preprocess")
    df = handle_patient_birth_year(df)
    df = convert_dates(df, ['appointment_date_registration', 'appointment_date'])
    df = fix_data_types(df)
    df = fill_missing_text_fields(df, ['appointment_type', 'service'])
    df = standardize_text(df, ['appointment_type', 'service'])

    # 2b. Geocode unknown addresses + enrich NULL distances
    print("2b. Geocode unknown addresses")
    geocode_unknown_addresses(df)
    print("2c. Distance enrichment")
    df = enrich_distances(df)

    # 3. Feature engineering
    print("3. Feature engineering")
    df = create_days_until_appointment(df)
    df = add_time_period(df)
    df = add_day_of_week(df)
    df = add_repeated_patient_features(df)
    df = add_children_holiday(df)
    df = calculate_patient_age(df)

    df_encoded = df.copy()
    train_df = df_encoded

    # 4. Model training
    print("4. Model training")
    setup_pycaret(
        df=train_df,
        target='appointment_attended',
        categorical_features=[
            'appointment_day_of_week',
            'appointment_time_period',
            'appointment_type',
            'service',
            'doctor_type_id'
        ],
        ignore_features=[
            'appointment_date_registration',
            'appointment_date',
            'appointment_status',
            'appointment_approved',
            'patient_id',
            'doctor_id',
            'service_type_id',
            'patient_birth_year',
            'patient_address',
            'facility_name'
        ]
    )
    best = train_best_model()

    # 5. Model tuning
    print("5. Model tuning")
    tuned_model = tune_best_model(best)
    
    # 6. Finalize and save model
    print("6. Finalize and save model")
    final_model = finalize_model(tuned_model)
    
    with tempfile.TemporaryDirectory(dir=MODEL_DIR) as tmpd:
        tmp_base = os.path.join(tmpd, MODEL_BASENAME)
        save_model(final_model, tmp_base)  # writes tmp_base.pkl
        atomic_replace(f"{tmp_base}.pkl", os.path.join(MODEL_DIR, f"{MODEL_BASENAME}.pkl"))

    # 7. Run profiling pipeline (fault-isolated)
    try:
        update_status(lambda s: s.update({"stage": "profiling"}))
        print("7. Profiling pipeline")
        profiling_csv = os.path.join(MODEL_DIR, "profiling_input.csv")
        if os.path.exists(profiling_csv):
            run_profiling_pipeline(csv_path=profiling_csv, model_dir=MODEL_DIR)
        else:
            print(f"Profiling skipped: {profiling_csv} not found")
    except Exception as e:
        print(f"Profiling failed (non-fatal): {e}")
        # Profiling failure should NOT block the main prediction model


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------


def _cli_export(args):
    """CLI handler: export CSVs from DB."""
    print("[CLI] Starting CSV export from database...")
    try:
        _run_monthly_export()
        mk = _month_key(date.today())
        update_status(lambda s: s.update({
            "last_export_month": mk,
            "export_done": now_iso(),
        }))
        print(f"[CLI] Export complete for {mk}")
    except Exception:
        print(f"[CLI] Export FAILED:\n{traceback.format_exc()}")
        raise SystemExit(1)


def _cli_train(args):
    """CLI handler: queue and immediately run a training job."""
    csv_path = os.path.join(MODEL_DIR, "training.csv")
    if not os.path.exists(csv_path):
        print(f"[CLI] ERROR: {csv_path} not found. Run 'export' first.")
        raise SystemExit(1)

    print("[CLI] Queueing training job...")
    _queue_training_job()

    print("[CLI] Running training now...")
    if not try_lock():
        print("[CLI] ERROR: Could not acquire lock. Is another worker running?")
        raise SystemExit(1)
    try:
        update_status(lambda s: s.update({"claimed_by": platform.node()}))
        _train()
        mk = _month_key(date.today())
        update_status(lambda s: s.update({"last_train_month": mk}))
        print("[CLI] Training complete.")
    except Exception:
        update_status(lambda s: s.update({
            "stage": "failed", "error": traceback.format_exc(),
        }))
        print(f"[CLI] Training FAILED:\n{traceback.format_exc()}")
        raise SystemExit(1)
    finally:
        unlock()


def _cli_full(args):
    """CLI handler: export + train (full pipeline)."""
    _cli_export(args)
    _cli_train(args)


def _cli_profiling(args):
    """CLI handler: run only the profiling pipeline on existing profiling_input.csv."""
    csv_path = getattr(args, "csv_path", None) or os.path.join(MODEL_DIR, "profiling_input.csv")
    if not os.path.exists(csv_path):
        print(f"[CLI] ERROR: {csv_path} not found. Run 'export' first.")
        raise SystemExit(1)

    job_id = str(uuid.uuid4())

    def mut(s: dict):
        s.update({
            "job_id": job_id,
            "job_type": "profiling",
            "csv_path": csv_path,
            "target": None,
            "processing": now_iso(),
            "done": None,
            "reloaded": None,
            "stage": "queued",
            "error": None,
            "claimed_by": None,
        })

    update_status(mut)
    print(f"[CLI] Queueing profiling job {job_id} for {csv_path}")

    if not try_lock():
        print("[CLI] ERROR: Could not acquire lock. Is another worker running?")
        raise SystemExit(1)
    try:
        update_status(lambda s: s.update({"claimed_by": platform.node()}))
        _run_profiling_only()
        print("[CLI] Profiling complete.")
    except Exception:
        update_status(lambda s: s.update({
            "stage": "failed", "error": traceback.format_exc(),
        }))
        print(f"[CLI] Profiling FAILED:\n{traceback.format_exc()}")
        raise SystemExit(1)
    finally:
        unlock()


def _cli_daemon(args):
    """CLI handler: run the normal polling loop (default)."""
    while True:
        today = date.today()
        st = read_status()

        # ---- Saturday: monthly CSV export -----------------------------------
        if _is_first_saturday(today):
            mk = _month_key(today)
            if st.get("last_export_month") != mk:
                print(f"[SCHEDULER] First Saturday detected ({today}), starting CSV export")
                try:
                    _run_monthly_export()
                    update_status(lambda s: s.update({
                        "last_export_month": mk,
                        "export_done": now_iso(),
                    }))
                    print(f"[SCHEDULER] Export complete for {mk}")
                except Exception:
                    print(f"[SCHEDULER] Export FAILED:\n{traceback.format_exc()}")

        # ---- Sunday: auto-trigger training ----------------------------------
        if _is_sunday_after_first_saturday(today):
            st = read_status()  # re-read after possible export update
            mk = _month_key(today)
            already_running = (
                st.get("processing") and not st.get("done") and not st.get("error")
            )
            trained_this_month = (
                st.get("last_train_month") == mk
            )
            export_ready = (st.get("last_export_month") == mk)

            if export_ready and not already_running and not trained_this_month:
                print(f"[SCHEDULER] Sunday after first Saturday ({today}), queueing training")
                _queue_training_job()
                update_status(lambda s: s.update({"last_train_month": mk}))

        # ---- Normal job processing (manual or scheduled) --------------------
        st = read_status()
        if st.get("processing") and not st.get("done") and not st.get("error"):
            if try_lock():
                try:
                    update_status(lambda s: s.update({"claimed_by": platform.node()}))
                    job_type = st.get("job_type", "training")
                    if job_type == "profiling":
                        _run_profiling_only()
                    else:
                        _train()
                except Exception:
                    update_status(lambda s: s.update({
                        "stage": "failed", "error": traceback.format_exc(),
                    }))
                finally:
                    unlock()

        time.sleep(POLL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BKUS ML Worker - training pipeline & scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python worker.py                          # start the polling daemon (default)
  python worker.py export                   # export CSVs from DB right now
  python worker.py train                    # run training on existing training.csv
  python worker.py full                     # export + train in one go
  python worker.py profiling                # run only the profiling pipeline
  python worker.py profiling --csv-path /path/profiling_input.csv
        """,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon",    help="Run the polling loop (default)")
    sub.add_parser("export",    help="Export training.csv + profiling_input.csv from DB")
    sub.add_parser("train",     help="Run training on existing training.csv")
    sub.add_parser("full",      help="Export from DB, then train (full pipeline)")
    prof_cmd = sub.add_parser("profiling", help="Run only the profiling pipeline on existing profiling_input.csv")
    prof_cmd.add_argument("--csv-path", dest="csv_path", default=None, help="Override CSV path")

    args = parser.parse_args()

    handlers = {
        "export":    _cli_export,
        "train":     _cli_train,
        "full":      _cli_full,
        "profiling": _cli_profiling,
        "daemon":    _cli_daemon,
        None:        _cli_daemon,  # no subcommand -> default to daemon
    }
    handlers[args.command](args)
