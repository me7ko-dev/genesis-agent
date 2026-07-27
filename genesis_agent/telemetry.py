import json
import os
import time
from pathlib import Path

# Пътят до live status файла — относителен спрямо проекта, с env override.
# (Беше хардкоднат Windows път C:\Users\roika\..., мъртъв код на Linux.)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = os.environ.get("GENESIS_STATUS_FILE", str(_PROJECT_ROOT / "live_status.json"))

def report_thought(thought: str):
    """Writes a thought to the live status file for the Operator to see."""
    try:
        data = {
            "last_thought": thought,
            "timestamp": time.time(),
            "status": "active"
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            print(f"[GENESIS]: {thought}")
        except UnicodeEncodeError:
            print(f"[GENESIS]: {thought.encode('ascii', 'ignore').decode('ascii')}")
    except Exception as e:
        print(f"[TELEMETRY-ERR]: {e}")

def clear_status():
    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)
