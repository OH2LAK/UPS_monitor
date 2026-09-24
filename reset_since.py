#!/usr/bin/env python3
"""
One-off: resets every UPS's "since" timestamp in state.json to right now,
so the --status "Tilassa" column stops showing durations that predate a
juggler reboot / power outage. Current "status" values are left untouched
(they're correct - upsc still reads live) - only the "how long has it been
in that status" clock is rezeroed. last_outage / charge_recovery_pending
(if any) are also left untouched.

Run this ONLY while ups-monitor.service is stopped, otherwise the running
process's in-memory state will just overwrite your edit again on its next
save.
"""
import json
import sys
import time

path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/ups_monitor/state.json"

with open(path, "r", encoding="utf-8") as f:
    state = json.load(f)

now = time.time()
for name, entry in state.items():
    old_since = entry.get("since")
    entry["since"] = now
    print(f"{name}: since {old_since} -> {now} (now)")

with open(path, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)

print(f"\nWrote {path}")
