# parse_purse.py
# Runs PURSE (V2, V3 with E1/E2/E3, 100 s) on ONE design, stores the logs,
# then turns the logs into one Excel file:
#   sheet "100s"   : one row per slice (property, round) for all four runs
#   sheet "Solved" : when each property was solved, with a running count
#
# Usage:  python3 parse_purse.py        (run from the ABC folder)
# Needs:  pip install pandas openpyxl

import re
import subprocess
import pandas as pd

# ---- 1. Settings -----------------------------------------------------------
DESIGN = "test-designs/bob12m16m.aig"
B, P, T = 1000, 100000, 100
RUN_ABC = True      # set to False to only re-parse existing logs

# label -> (log file, extra purse flags)
RUNS = {
    "V2":    ("v2_100.log",    "-V 2"),
    "V3_E1": ("v3_e1_100.log", "-V 3 -E 1"),
    "V3_E3": ("v3_e3_100.log", "-V 3 -E 3"),
}
OUT = "bob12m16m_trace.xlsx"


# ---- 2. Run ABC and store the logs ----------------------------------------
def run_all():
    for label, (log, flags) in RUNS.items():
        cmd = f"read {DESIGN}; strash; purse -B {B} -P {P} -T {T} {flags}"
        print("Running", label, ":", cmd)
        try:
            r = subprocess.run(["./abc", "-c", cmd], capture_output=True,
                               text=True, timeout=T + 120)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        with open(log, "w") as f:
            f.write(out)


# ---- 3. Small helper: "a=1 b=PAUSED" -> {"a": "1", "b": "PAUSED"} -----------
def fields(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


# ---- 4. Read one log file -------------------------------------------------
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
            # V3 only: decided after this round, used in the next one
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
            "Variant":       variant,
            "PO":            int(f["po"]),
            "Result":        "SAFE" if f["res"] == "1" else "UNSAFE",
            "Round":         int(f["round"]),
            "Time":          float(f["time"]),
            "Solved_so_far": i,
        })

    return rows, done


# ---- 5. Read all logs and write the Excel file -----------------------------
if RUN_ABC:
    run_all()

all_rows, all_solved = [], []
for variant, (log, _) in RUNS.items():
    r, d = read_log(log, variant)
    all_rows += r
    all_solved += d

with pd.ExcelWriter(OUT) as xls:
    pd.DataFrame(all_rows).to_excel(xls, sheet_name="100s", index=False)
    cols = ["Variant", "PO", "Result", "Round", "Time", "Solved_so_far"]
    pd.DataFrame(all_solved, columns=cols).to_excel(xls, sheet_name="Solved", index=False)

print("Wrote", OUT)