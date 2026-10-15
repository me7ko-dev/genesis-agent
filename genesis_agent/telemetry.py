import json
import logging
import os
import time

from genesis_agent.config import DATA_DIR

log = logging.getLogger("genesis.telemetry")

STATUS_FILE = os.environ.get("GENESIS_STATUS_FILE", str(DATA_DIR / "live_status.json"))

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
        log.debug("статусът не се записа", exc_info=True)
        print(f"[TELEMETRY-ERR]: {e}")
