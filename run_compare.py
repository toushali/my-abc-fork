#!/usr/bin/env python3
"""Run exactly two P3DR ablations, sequentially, from the ABC build directory.

Abl1: purse -V 3 -E 3 (random ordering, E1 progress-based weights)
Abl2: purse -V 3 -E 5 (same random ordering, uniform weights)

Example:
  python3 -u run_compare.py --cpu 16 --b 10000 --p 100000 --time 3600 \
      --folder test-designs --out ablation_results

Writes separate per-mode CSVs/logs and a combined results_table.csv.
Completed successful runs are skipped when restarting with identical settings.
"""
import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

CONFIGS = (
    ("Abl1", "-V 3 -E 3"),
    ("Abl2", "-V 3 -E 5"),
)
RE_PROPS = re.compile(
    r"Properties:\s*All\s*=\s*(\d+)\.\s*Proved\s*=\s*(\d+)\.\s*"
    r"Disproved\s*=\s*(\d+)\.\s*Undecided\s*=\s*(\d+)", re.I
)
RE_TOTAL = re.compile(
    r"\[PURSE-TOTAL\]\s+variant=\d+\s+total_sat_calls=(\d+)\s+"
    r"undecided_sat_calls=(\d+)\s+time=([\d.]+)"
)
RE_SUMMARY = re.compile(
    r"\[PURSE-SUMMARY\]\s+po=\d+\s+final=(\w+)"
    r".*?\bsat_calls=(\d+)\b.*?\brounds=(\S+)"
)
RE_RSS = re.compile(r"\[RUNNER-RAM\]\s+max_rss_kb=(\d+)")
RE_ERR = re.compile(r"(?:^|\n)\s*(?:Error:|usage:\s*purse)", re.I)
RE_PO = re.compile(r"\bi/o\s*=\s*\d+\s*/\s*(\d+)")
RE_AND = re.compile(r"\band\s*=\s*(\d+)")
RE_LATCH = re.compile(r"\blat\s*=\s*(\d+)")
RE_ABL = re.compile(r"\[PURSE-ABLATION\]\s+E=(\d+)\s+random_order=1\s+weighted=(\d+)\s+seed=(\d+)")
FIELDS = (
    "Design", "PO", "AND", "Latches", "Variant", "CPU", "Status",
    "Proved", "Disproved", "Undecided", "Concluded", "Total_SAT",
    "Undecided_SAT", "Proved_SAT", "Rounds", "Time", "Wall_Time",
    "Peak_RAM_MB", "Log", "Seed"
)


def last(regex, source):
    matches = list(regex.finditer(source))
    return matches[-1] if matches else None


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_csv(path, rows, fields):
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temp.open("w", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for name in sorted(rows):
            w.writerow(rows[name])
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read_csv(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf8") as f:
        return {row["Design"]: row for row in csv.DictReader(f)}


def run_abc(command, log_path, root):
    start = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf8", errors="replace") as log:
        proc = subprocess.Popen(
            ["/usr/bin/time", "-f", "[RUNNER-RAM] max_rss_kb=%M", "./abc", "-c", command],
            stdout=log, stderr=subprocess.STDOUT, cwd=root, start_new_session=True
        )
        try:
            rc = proc.wait()
        except BaseException:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            raise
    status = "OK" if rc == 0 else ("KILLED" if rc in (-9, 137) else f"EXIT_{rc}")
    return time.monotonic() - start, status


def parse(log, status, wall, expected_e):
    props, total, rss, abla = (last(x, log) for x in (RE_PROPS, RE_TOTAL, RE_RSS, RE_ABL))
    if status == "OK" and props is None:
        status = "NO_SUMMARY"
    if RE_ERR.search(log):
        status = "ABC_ERROR"
    if status == "OK" and (abla is None or int(abla.group(1)) != expected_e or
                           int(abla.group(2)) != int(expected_e == 3)):
        status = "INVALID_ABLATION_BINARY"
    proved = int(props.group(2)) if props else None
    disproved = int(props.group(3)) if props else None
    summaries = RE_SUMMARY.findall(log)
    return {
        "Status": status,
        "Proved": proved if proved is not None else "",
        "Disproved": disproved if disproved is not None else "",
        "Undecided": props.group(4) if props else "",
        "Concluded": proved + disproved if proved is not None and disproved is not None else "",
        "Total_SAT": total.group(1) if total else "",
        "Undecided_SAT": total.group(2) if total else "",
        "Proved_SAT": sum(int(calls) for res, calls, _ in summaries if res == "SAFE") if summaries else "",
        "Rounds": max((len(history) for _, _, history in summaries), default=""),
        "Time": total.group(3) if total else "",
        "Wall_Time": f"{wall:.3f}",
        "Peak_RAM_MB": f"{int(rss.group(1)) / 1024:.2f}" if rss else "",
        "Seed": abla.group(3) if abla else "",
    }


def refresh_combined(output, per_mode):
    # Separate metrics for both variants; no ambiguous slash-joined values.
    fields = ["Design", "PO", "AND", "Latches"]
    measurements = ["Status", "Proved", "Disproved", "Undecided", "Concluded",
                    "Total_SAT", "Undecided_SAT", "Proved_SAT", "Rounds",
                    "Time", "Wall_Time", "Peak_RAM_MB", "Seed"]
    for name, _ in CONFIGS:
        fields.extend(f"{m}_{name}" for m in measurements)
    all_designs = set().union(*(r.keys() for r in per_mode.values()))
    combined = {}
    for design in all_designs:
        row = {"Design": design}
        for name, _ in CONFIGS:
            if design in per_mode[name]:
                source = per_mode[name][design]
                for key in ("PO", "AND", "Latches"):
                    row[key] = source[key]
                for key in measurements:
                    row[f"{key}_{name}"] = source.get(key, "")
        combined[design] = row
    atomic_csv(output / "results_table.csv", combined, fields)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", default="test-designs")
    p.add_argument("--out", default="ablation_results")
    p.add_argument("--b", type=int, default=10000)
    p.add_argument("--p", type=int, default=100000)
    p.add_argument("--time", type=int, default=3600)
    p.add_argument("--cpu", type=int, default=None, help="Optional single Linux logical CPU ID, e.g. 16")
    p.add_argument("--rerun-all", action="store_true")
    args = p.parse_args()
    if min(args.b, args.p, args.time) <= 0:
        p.error("--b, --p, and --time must be positive")
    root = Path.cwd().resolve()
    if not (root / "abc").is_file() or not Path("/usr/bin/time").is_file():
        p.error("Requires ./abc and /usr/bin/time; invoke from the built ABC directory")
    folder = (root / args.folder).resolve()
    if not folder.is_dir():
        p.error(f"Design folder not found: {folder}")
    designs = sorted(list(folder.glob("*.aig")) + list(folder.glob("*.aag")))
    if not designs:
        p.error(f"No .aig/.aag designs in {folder}")
    names = [d.stem for d in designs]
    if len(names) != len(set(names)):
        p.error("Duplicate design stems (.aig/.aag) would overwrite results")
    if args.cpu is not None:
        if not hasattr(os, "sched_getaffinity") or args.cpu not in os.sched_getaffinity(0):
            p.error(f"CPU {args.cpu} not available to current process")
        os.sched_setaffinity(0, {args.cpu})
    output = (root / args.out).resolve()
    if output == root:
        p.error("--out must be a subdirectory, not the ABC root")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".run_compare.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"Another run is using {output}")
        manifest = {
            "abc_sha256": digest(root / "abc"), "b": args.b, "p": args.p, "time": args.time,
            "modes": [list(x) for x in CONFIGS],
            "designs": [{"name": d.name, "size": d.stat().st_size,
                         "mtime_ns": d.stat().st_mtime_ns} for d in designs]
        }
        config_path = output / "run_config.json"
        if config_path.exists():
            prev = json.loads(config_path.read_text(encoding="utf8"))
            if prev != manifest:
                raise SystemExit("Existing results use different ABC/designs/budgets/time. Choose a new --out.")
        elif any(output.glob("results_*.csv")):
            raise SystemExit("Existing results have no manifest. Choose a new --out.")
        else:
            config_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
        per_mode = {name: read_csv(output / f"results_{name}.csv") for name, _ in CONFIGS}
        print(f"Ablation evaluation: {len(designs)} designs; B={args.b}, P={args.p}, T={args.time}", flush=True)
        print("Modes: Abl1 (-E 3: random + weighted), Abl2 (-E 5: random + uniform)", flush=True)
        print(f"Results: {output}", flush=True)
        for idx, path in enumerate(designs, 1):
            name = path.stem
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                p.error(f"Design outside ABC root: {path}")
            if any(ch in rel for ch in (';', '"', '\n', '\r')):
                p.error(f"Unsafe design filename: {path}")
            print(f"[{idx}/{len(designs)}] {name}", flush=True)
            stats_log = output / "stats" / f"{name}_stats.log"
            if not stats_log.exists():
                _, state = run_abc(f"read_aiger {rel}; fold; strash; print_stats", stats_log, root)
                if state != "OK":
                    print(f"  warning: stats exited {state}", flush=True)
            stats = stats_log.read_text(errors="replace")
            po, gates, latch = (last(r, stats) for r in (RE_PO, RE_AND, RE_LATCH))
            metadata = {"Design": name, "PO": po.group(1) if po else "",
                        "AND": gates.group(1) if gates else "",
                        "Latches": latch.group(1) if latch else ""}
            for label, flags in CONFIGS:
                rows = per_mode[label]
                if not args.rerun_all and rows.get(name, {}).get("Status") == "OK":
                    print(f"  {label}: SKIP successful result", flush=True)
                    continue
                log_path = output / "logs" / label / f"{name}_{label}.log"
                command = f"read_aiger {rel}; fold; strash; purse -B {args.b} -P {args.p} -T {args.time} {flags}"
                print(f"  {label}: RUN {flags}", flush=True)
                wall, state = run_abc(command, log_path, root)
                result = parse(log_path.read_text(errors="replace"), state, wall, 3 if label == "Abl1" else 5)
                result.update(metadata)
                result.update({"Variant": label, "CPU": args.cpu if args.cpu is not None else "",
                               "Log": str(log_path.relative_to(output))})
                rows[name] = result
                atomic_csv(output / f"results_{label}.csv", rows, FIELDS)
                refresh_combined(output, per_mode)
                print(f"  {label}: {result['Status']} concluded={result['Concluded']} "
                      f"SAT={result['Total_SAT']} RSS={result['Peak_RAM_MB']} MB", flush=True)
        refresh_combined(output, per_mode)
        print(f"Done. CSV and logs saved in {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
