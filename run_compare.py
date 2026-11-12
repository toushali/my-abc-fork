#!/usr/bin/env python3
"""Compare four PURSE modes with standard multi-output PDR.

Run from the ABC directory:
    python3 run_compare.py

Outputs:
    results_table.csv
    logs/<design>_<mode>.log
    logs/<design>_stats.log
"""

import csv
import glob
import os
import re
import subprocess
import time
from pathlib import Path

# -------- Change these settings as needed --------
FOLDER = "test-designs"
B = 10000        # PURSE conflict budget per slice
P = 100000       # PURSE propagation budget per slice
T = 10         # seconds for each solver run

# All commands include: read_aiger; fold; strash
# PDR uses -a to solve all properties and -v to print the manager's SAT call count.
CONFIGS = [
    ("V2",        "purse", "-V 2"),
    ("V3_E1",     "purse", "-V 3 -E 1"),
    ("V3_E3",     "purse", "-V 3 -E 3"),
    ("V3_E4_K2",  "purse", "-V 3 -E 4 -k 2"),
    ("PDR",       "pdr",   "-a -v"),
]

# ABC prints this for PURSE and for 'pdr -a'.
PROPS_RE = re.compile(
    r"Properties:\s*All\s*=\s*(\d+)\.\s*"
    r"Proved\s*=\s*(\d+)\.\s*"
    r"Disproved\s*=\s*(\d+)\.\s*"
    r"Undecided\s*=\s*(\d+)", re.IGNORECASE
)
PURSE_TOTAL_RE = re.compile(
    r"\[PURSE-TOTAL\]\s+variant=\d+\s+"
    r"total_sat_calls=(\d+)\s+undecided_sat_calls=(\d+)\s+"
    r"time=([\d.]+)"
)
PURSE_SUMMARY_RE = re.compile(
    r"\[PURSE-SUMMARY\]\s+po=\d+\s+final=(\w+)"
    r".*?\bsat_calls=(\d+)\b.*?\brounds=(\S+)"
)
PDR_CALLS_RE = re.compile(r"\bBlock\s*=.*?\bCall\s*=\s*(\d+)")
ABC_TIME_RE = re.compile(
    r"Properties:.*?\bTime\s*=\s*([\d.]+)\s*sec", re.IGNORECASE
)


def last(regex, text):
    matches = list(regex.finditer(text))
    return matches[-1] if matches else None


def run_abc(command, log_path, timeout=None):
    """Run ABC with streaming log output. Returns (wall_seconds, run_state)."""
    start = time.monotonic()
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            result = subprocess.run(
                ["./abc", "-c", command],
                stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                check=False,
            )
        rc = result.returncode
        state = "OK" if rc == 0 else ("KILLED" if rc in (-9, 137) else f"EXIT_{rc}")
    except subprocess.TimeoutExpired:
        state = "TIMEOUT"
        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\n[RUNNER] Process exceeded external wall-clock timeout.\n")
    return time.monotonic() - start, state


def parse_properties(output):
    match = last(PROPS_RE, output)
    return (match.group(1), match.group(2), match.group(3), match.group(4)) if match else None


def run_solver(design, name, label, tool, flags):
    log_file = Path("logs") / f"{name}_{label}.log"
    prefix = f"read_aiger {design}; fold; strash; "
    if tool == "purse":
        command = prefix + f"purse -B {B} -P {P} -T {T} {flags}"
    else:
        command = prefix + f"pdr -T {T} {flags}"

    wall, state = run_abc(command, log_file, timeout=None)
    output = log_file.read_text(encoding="utf-8", errors="replace")
    props = parse_properties(output)

    # ABC sometimes reports an argument error and returns exit code 0.
    if state == "OK" and not props:
        state = "NO_SUMMARY"
    if re.search(r"(?:^|\n)\s*(?:Error:|usage:\s*(?:purse|pdr))", output, re.I):
        if state == "OK" or state == "NO_SUMMARY":
            state = "ABC_ERROR"

    result = {
        "status": state,
        "proved": props[1] if props else "",
        "disproved": props[2] if props else "",
        "undecided": props[3] if props else "",
        "total_sat": "",
        "undecided_sat": "",
        "proved_sat": "",
        "rounds": "",
        "time": "",          # ABC reported time (if available)
        "wall_time": f"{wall:.3f}",  # comparable wall time for every mode
    }

    if tool == "purse":
        total = last(PURSE_TOTAL_RE, output)
        if total:
            result["total_sat"] = total.group(1)
            result["undecided_sat"] = total.group(2)
            result["time"] = total.group(3)
        summaries = PURSE_SUMMARY_RE.findall(output)
        if summaries:
            result["proved_sat"] = sum(int(calls) for verdict, calls, _ in summaries
                                       if verdict == "SAFE")
            result["rounds"] = max(len(history) for _, _, history in summaries)
    else:
        # 'pdr -a -v' writes a single aggregate manager line:
        # Block = ... Oblig = ... Clause = ... Call = ...
        calls = last(PDR_CALLS_RE, output)
        if calls:
            result["total_sat"] = calls.group(1)
        # PDR has no PURSE per-property SAT/round reports.
        elapsed = last(ABC_TIME_RE, output)
        if elapsed:
            result["time"] = elapsed.group(1)

    return result


def same_or_list(values):
    # Display a single value when all runs agree, otherwise slash-join
    # in the same order as CONFIGS. Missing values remain visible as blanks.
    values = [str(v) for v in values]
    return values[0] if len(set(values)) == 1 else "/".join(values)


def main():
    designs = sorted(glob.glob(f"{FOLDER}/*.aig") + glob.glob(f"{FOLDER}/*.aag"))
    if not os.path.isfile("./abc"):
        raise SystemExit("Cannot find ./abc. Run this script from the built ABC directory.")
    if not designs:
        raise SystemExit(f"No .aig or .aag designs found in {FOLDER!r}.")

    Path("logs").mkdir(exist_ok=True)
    labels = [label for label, _, _ in CONFIGS]
    columns = ["Design", "PO", "AND", "Proved", "Disproved", "Undecided"]
    for group in (
        "Status", "Total_SAT", "Undecided_SAT", "Proved_SAT",
        "Rounds", "Time", "Wall_Time",
    ):
        columns.extend(f"{group}_{label}" for label in labels)

    with open("results_table.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()

        for number, design in enumerate(designs, 1):
            name = Path(design).stem
            print(f"\n[{number}/{len(designs)}] {design}", flush=True)

            stats_path = Path("logs") / f"{name}_stats.log"
            _, stats_state = run_abc(
                f"read_aiger {design}; fold; strash; print_stats",
                stats_path,
            )
            stats = stats_path.read_text(encoding="utf-8", errors="replace")
            po = last(re.compile(r"\bi/o\s*=\s*\d+\s*/\s*(\d+)"), stats)
            ands = last(re.compile(r"\band\s*=\s*(\d+)"), stats)

            results = {}
            for label, tool, flags in CONFIGS:
                print(f"  Running {label} ...", flush=True)
                results[label] = run_solver(design, name, label, tool, flags)
                r = results[label]
                print(
                    f"    {r['status']}: proved={r['proved'] or '?'} "
                    f"disproved={r['disproved'] or '?'} "
                    f"undecided={r['undecided'] or '?'} "
                    f"wall={r['wall_time']}s",
                    flush=True,
                )

            row = {
                "Design": name,
                "PO": po.group(1) if po else "",
                "AND": ands.group(1) if ands else "",
            }
            for key, column in (
                ("proved", "Proved"),
                ("disproved", "Disproved"),
                ("undecided", "Undecided"),
            ):
                row[column] = same_or_list([results[label][key] for label in labels])
            for key, group in (
                ("status", "Status"),
                ("total_sat", "Total_SAT"),
                ("undecided_sat", "Undecided_SAT"),
                ("proved_sat", "Proved_SAT"),
                ("rounds", "Rounds"),
                ("time", "Time"),
                ("wall_time", "Wall_Time"),
            ):
                for label in labels:
                    row[f"{group}_{label}"] = results[label][key]

            writer.writerow(row)
            file.flush()  # save intermediate progress after every design
            if stats_state != "OK":
                print(f"  Warning: print_stats exited with {stats_state}", flush=True)

    print("\nDone. Saved results_table.csv and logs/", flush=True)


if __name__ == "__main__":
    main()
