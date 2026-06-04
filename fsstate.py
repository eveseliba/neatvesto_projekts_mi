import os, json, tempfile, time

STATUS_PATH = os.getenv("STATUS_PATH", "/app/status.json")
LOCK_PATH = STATUS_PATH + ".lock"

def now_iso():
    import datetime as dt
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def atomic_write_json(path: str, obj: dict):
    d = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=d) as tmp:
        json.dump(obj, tmp)
        tmp.flush(); os.fsync(tmp.fileno())
        tmp_name = tmp.name
    # On Windows, target file must not exist for replace to work
    # Remove if exists, then replace
    try:
        if os.path.exists(path):
            os.remove(path)
        os.replace(tmp_name, path)
    except Exception:
        # Cleanup temp file if replace fails
        try:
            os.remove(tmp_name)
        except:
            pass
        raise

def read_status() -> dict:
    if not os.path.exists(STATUS_PATH):
        return {
            "job_id": None, "csv_path": None, "target": None,
            "processing": None, "done": None, "reloaded": None,
            "stage": None, "error": None, "claimed_by": None, "rev": 0,
            "last_export_month": None, "export_done": None,
            "last_train_month": None,
        }
    with open(STATUS_PATH, "r") as f:
        return json.load(f)

def update_status(mutator):
    st = read_status()
    before_rev = st.get("rev", 0)
    mutator(st)
    st["rev"] = before_rev + 1
    atomic_write_json(STATUS_PATH, st)
    return st

def try_lock(timeout=3.0):
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            return True
        except FileExistsError:
            if time.time() > deadline:
                return False
            time.sleep(0.05)

def unlock():
    try: os.remove(LOCK_PATH)
    except FileNotFoundError: pass
