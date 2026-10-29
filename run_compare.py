#!/usr/bin/env python3
# Run purse -V 1, -V 2, -V 3 on every design in a folder and write ONE table.
#
# How to use (from the ABC folder):
#     python3 run_compare.py
#
# Change the settings below if you need to.
# Output: results_table.csv (one row per design). Raw ABC output goes to logs/.

import csv, glob, os, re, subprocess

FOLDER = "test-designs"   # folder with the .aig / .aag designs
VARIANTS = [1, 2, 3]      # values of -V to compare
B = 1000                  # -B  conflict budget per slice
P = 50000                 # -P  propagation budget per slice
T = 100                    # -T  time limit per run, in seconds


def run_abc(command, timeout):
    """Run one ABC command and return everything it printed."""
    try:
        r = subprocess.run(["./abc", "-c", command], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return ""


def last(pattern, text):
    """Return the groups of the last match of a pattern, or None."""
    found = re.findall(pattern, text)
    return found[-1] if found else None


def run_variant(design, name, v):
    """Run purse with -V v and pull the numbers we need out of its output."""
    out = run_abc(f"read_aiger {design}; fold; strash; purse -B {B} -P {P} -T {T} -V {v}", T + 120)
    with open(f"logs/{name}_V{v}.log", "w") as f:
        f.write(out)

    # "Properties: All = 46. Proved = 45. Disproved = 1. Undecided = 0."
    props = last(r"All\s*=\s*\d+\.\s*Proved\s*=\s*(\d+)\.\s*Disproved\s*=\s*(\d+)\.\s*Undecided\s*=\s*(\d+)", out)
    # "c [PURSE-TOTAL] variant=3 total_sat_calls=... undecided_sat_calls=... time=..."
    total = last(r"PURSE-TOTAL\]\s+variant=\d+\s+total_sat_calls=(\d+)\s+undecided_sat_calls=(\d+)\s+time=([\d.]+)", out)
    # one "c [PURSE-SUMMARY] po=.. final=.. ... sat_calls=.. ... rounds=.." line per property
    summary = re.findall(r"PURSE-SUMMARY\]\s+po=\d+\s+final=(\w+).*?sat_calls=(\d+).*?rounds=(\S+)", out)

    return {
        "proved": props[0] if props else "",
        "disproved": props[1] if props else "",
        "undecided": props[2] if props else "",
        "total_sat": total[0] if total else "",
        "undecided_sat": total[1] if total else "",
        "time": total[2] if total else "",
        "proved_sat": sum(int(s[1]) for s in summary if s[0] == "SAFE") if summary else "",
        "rounds": max(len(s[2]) for s in summary) if summary else "",
    }


def same_or_list(values):
    """One value if all variants agree, otherwise the values joined like 45/44/45."""
    values = [str(x) for x in values]
    return values[0] if len(set(values)) == 1 else "/".join(values)


def main():
    designs = sorted(glob.glob(f"{FOLDER}/*.aig") + glob.glob(f"{FOLDER}/*.aag"))
    os.makedirs("logs", exist_ok=True)

    # column names
    columns = ["Design", "PO", "AND", "Proved", "Disproved", "Undecided"]
    for group in ["Total_SAT", "Undecided_SAT", "Proved_SAT", "Rounds", "Time"]:
        columns += [f"{group}_V{v}" for v in VARIANTS]

    with open("results_table.csv", "w", newline="") as f:
        table = csv.DictWriter(f, fieldnames=columns)
        table.writeheader()

        for design in designs:
            name = os.path.splitext(os.path.basename(design))[0]

            # PO = number of properties, AND = number of AND gates
            stats = run_abc(f"read_aiger {design}; fold; strash; print_stats", 300)
            io = last(r"i/o\s*=\s*\d+\s*/\s*(\d+)", stats)
            gates = last(r"\band\s*=\s*(\d+)", stats)

            results = {v: run_variant(design, name, v) for v in VARIANTS}

            row = {"Design": name, "PO": io or "", "AND": gates or ""}
            for key, column in [("proved", "Proved"), ("disproved", "Disproved"), ("undecided", "Undecided")]:
                row[column] = same_or_list([results[v][key] for v in VARIANTS])
            for key, group in [("total_sat", "Total_SAT"), ("undecided_sat", "Undecided_SAT"),
                               ("proved_sat", "Proved_SAT"), ("rounds", "Rounds"), ("time", "Time")]:
                for v in VARIANTS:
                    row[f"{group}_V{v}"] = results[v][key]

            table.writerow(row)
            f.flush()


main()