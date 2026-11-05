# parse_purse.py
# Turns PURSE logs (V2 and V3-N, at 10 s and 100 s) for ONE design
# into one Excel file:
#   sheet "10s"    : one row per slice (property, round) for V2 and V3-N
#   sheet "100s"   : same, for the 100 s runs
#   sheet "Solved" : when each property was solved, with a running count
#
# Usage:  python3 parse_purse.py
# Needs:  pip install pandas openpyxl

import re
import pandas as pd

# ---- 1. Set your file names here ------------------------------------------
LOGS = {
    "10s":  {"V2": "v2_10.log",  "V3N": "v3_10.log"},
    "100s": {"V2": "v2_100.log", "V3N": "v3_100.log"},
}
OUT = "bob12m16m_trace.xlsx"


# ---- 2. Small helper: "a=1 b=PAUSED" -> {"a": "1", "b": "PAUSED"} -----------
def fields(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


# ---- 3. Read one log file -------------------------------------------------
def read_log(path, variant):
    sched = []      # [PURSE-SCHED] lines, in the order they ran
    alloc = {}      # [PURSE-ALLOC-N] lines, looked up by (round, po)
    solved = []     # [PURSE-SOLVED] lines

    for line in open(path):
        f = fields(line)
        if "[PURSE-SCHED]" in line:
            sched.append(f)
        elif "[PURSE-ALLOC-N]" in line:
            alloc[(f["round"], f["po"])] = f
        elif "[PURSE-SOLVED]" in line:
            solved.append(f)

    # one row per slice
    rows = []
    last_frame = {}     # last frame seen for each property
    turn = {}           # position of the slice inside its round
    for f in sched:
        po, rnd = f["po"], f["round"]

        frame = int(f["frame"])
        dF = frame - last_frame.get(po, 0)
        last_frame[po] = frame

        turn[rnd] = turn.get(rnd, 0) + 1

        conf = int(f["conf_incr"])
        prop = int(f["prop_incr"])
        over_conf = int(f["over_conf"])
        over_prop = int(f["over_prop"])
        paused = f["result"] == "PAUSED"

        a = alloc.get((rnd, po), {})    # empty for V2

        rows.append({
            "Variant":        variant,
            "Round":          int(rnd),
            "Turn":           turn[rnd],          # 1 = ran first in this round
            "PO":             int(po),
            "Result":         f["result"],
            "dF":             dF,
            "dTC":            int(f["sat_calls_incr"]),
            # budget given for this slice (only known when it paused)
            "Assigned_conf":  conf - over_conf if paused else None,
            "Assigned_prop":  prop - over_prop if paused else None,
            # work actually done in this slice
            "Actual_conf":    conf,
            "Actual_prop":    prop,
            "Over_conf":      over_conf if paused else None,
            "Over_prop":      over_prop if paused else None,
            # V3-N only: decided after this round, used in the next one
            "Eff":            a.get("eff"),
            "Weight_next":    a.get("w"),
            "Conf_next":      a.get("conf"),
            "Prop_next":      a.get("prop"),
            "Slice_time":     float(f["time"]),
        })

    # solved list with a running count
    done = []
    for i, f in enumerate(solved, start=1):
        done.append({
            "Variant":      variant,
            "PO":           int(f["po"]),
            "Result":       "SAFE" if f["res"] == "1" else "UNSAFE",
            "Round":        int(f["round"]),
            "Time":         float(f["time"]),
            "Solved_so_far": i,
        })

    return rows, done


# ---- 4. Read all logs and write the Excel file -----------------------------
all_solved = []
with pd.ExcelWriter(OUT) as xls:
    for timeout, files in LOGS.items():
        rows = []
        for variant, path in files.items():
            r, d = read_log(path, variant)
            rows += r
            for x in d:
                x["Timeout"] = timeout
            all_solved += d
        pd.DataFrame(rows).to_excel(xls, sheet_name=timeout, index=False)

    cols = ["Timeout", "Variant", "PO", "Result", "Round", "Time", "Solved_so_far"]
    pd.DataFrame(all_solved)[cols].to_excel(xls, sheet_name="Solved", index=False)

print("Wrote", OUT)