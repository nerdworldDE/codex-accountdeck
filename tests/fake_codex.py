#!/usr/bin/env python3
"""Local protocol fixture; never reads the real Codex home or calls a model."""

import json
from pathlib import Path
import subprocess
import sys


root = Path(__file__).parent
state_file = root / "threads.json"
log_file = root / "requests.jsonl"


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    with log_file.open("a") as log:
        log.write(json.dumps(request, ensure_ascii=False) + "\n")
    method = request.get("method")
    if "id" not in request:
        continue
    request_id = request["id"]
    params = request.get("params", {})
    state = json.loads(state_file.read_text())
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "test-codex"}})
    elif method == "thread/list":
        data = sorted(state["threads"], key=lambda t: t["updatedAt"], reverse=True)
        data = [t for t in data if t.get("archived", False) == params.get("archived", False)]
        data = [t for t in data if t.get("source", "cli") in params["sourceKinds"]]
        start = int(params.get("cursor", "0"))
        size = min(state.get("page_size", 100), params["limit"])
        end = start + size
        send({"id": request_id, "result": {
            "data": data[start:end], "nextCursor": str(end) if end < len(data) else None,
        }})
    elif method == "thread/name/set":
        if params["name"] == "ERROR":
            send({"id": request_id, "error": {"code": -32602, "message": "Testfehler beim Speichern"}})
            continue
        for thread in state["threads"]:
            if thread["id"] == params["threadId"]:
                thread["name"] = params["name"]
        state_file.write_text(json.dumps(state, ensure_ascii=False))
        # Notifications may arrive before the response to our own request.
        send({"method": "thread/name/updated", "params": params})
        send({"id": request_id, "result": {}})
    elif method == "test/timeout":
        continue
    elif method == "test/inherited-pipes":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (root / "descendant.pid").write_text(str(child.pid))
        continue
    elif method == "test/exit":
        sys.exit(1)
    elif method == "test/malformed":
        print("not json", flush=True)
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "Unknown method"}})
