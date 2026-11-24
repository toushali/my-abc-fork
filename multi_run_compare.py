#!/usr/bin/env python3
"""Run five ABC/PURSE variants on five distinct Linux physical CPU cores.

Default assignment (all five solver variants run concurrently):
    CPU #1: V2
    CPU #2: V3_E1
    CPU #3: V3_E3
    CPU #4: V3_E4_K2
    CPU #5: PDR

Every mode has a separate results CSV
and a separate log directory. Existing successful results are skipped on rerun.

From the ABC directory:
    python3 run_compare_5cores.py --list-cores
    python3 run_compare_5cores.py --time 3600 --out runs_server54
    python3 run_compare_5cores.py --cores 16,18,20,22,24 --time 3600 --out runs_server54

This script requires Linux CPU affinity, GNU /usr/bin/time, and ./abc.
It pins one solver process per selected physical core.
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
from collections import Counter
from multiprocessing import Process
from pathlib import Path

CONFIGS = {
    "V2": ("purse", "-V 2"),
    "V3_E1": ("purse", "-V 3 -E 1"),
    "V3_E3": ("purse", "-V 3 -E 3"),
    "V3_E4_K2": ("purse", "-V 3 -E 4 -k 2"),
    "PDR": ("pdr", "-a -v"),
}

# One worker per physical core: each variant runs concurrently.
GROUPS = (("V2",), ("V3_E1",), ("V3_E3",), ("V3_E4_K2",), ("PDR",))

PROPS_RE = re.compile(
    r"Properties:\s*All\s*=\s*(\d+)\s*\.\s*"
    r"Proved\s*=\s*(\d+)\s*\.\s*"
    r"Disproved\s*=\s*(\d+)\s*\.\s*"
    r"Undecided\s*=\s*(\d+)", re.IGNORECASE
)
PURSE_TOTAL_RE = re.compile(
    r"\[PURSE-TOTAL\]\s+variant=\d+\s+"
    r"total_sat_calls=(\d+)\s+undecided_sat_calls=(\d+)\s+time=([\d.]+)"
)
PURSE_SUMMARY_RE = re.compile(
    r"\[PURSE-SUMMARY\]\s+po=\d+\s+final=(\w+)"
    r".*?\bsat_calls=(\d+)\b.*?\brounds=(\S+)"
)
PDR_CALLS_RE = re.compile(r"\bBlock\s*=.*?\bCall\s*=\s*(\d+)")
ABC_TIME_RE = re.compile(r"Properties:.*?\bTime\s*=\s*([\d.]+)\s*sec", re.I)
PEAK_RAM_RE = re.compile(r"\[RUNNER-RAM\]\s+max_rss_kb=(\d+)")
ABC_ERROR_RE = re.compile(r"(?:^|\n)\s*(?:Error:|usage:\s*(?:purse|pdr))", re.I)
STATS_PO_RE = re.compile(r"\bi/o\s*=\s*\d+\s*/\s*(\d+)")
STATS_AND_RE = re.compile(r"\band\s*=\s*(\d+)")
STATS_LAT_RE = re.compile(r"\blat\s*=\s*(\d+)")

FIELDS = [
    "Design", "PO", "AND", "Latches", "Size_AND_plus_Latches",
    "Variant", "CPU", "Status", "Proved", "Disproved", "Undecided",
    "Decided", "Total_SAT", "Undecided_SAT", "Proved_SAT",
    "Rounds", "Time", "Wall_Time", "Peak_RAM_MB", "Log",
]


def last(regex, text):
    matches = list(regex.finditer(text))
    return matches[-1] if matches else None


def physical_key(cpu):
    """Return (socket, physical core) for an allowed Linux logical CPU."""
    topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
    try:
        package = (topology / "physical_package_id").read_text().strip()
        core = (topology / "core_id").read_text().strip()
        return (package, core)
    except (OSError, ValueError):
        return None


def available_cpus():
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        raise SystemExit("Linux sched_getaffinity/sched_setaffinity are required.")
    return sorted(os.sched_getaffinity(0))


def choose_cpus(requested):
    allowed = available_cpus()
    if requested:
        try:
            chosen = [int(x.strip()) for x in requested.split(",")]
        except ValueError:
            raise SystemExit("--cores must be five comma-separated CPU IDs, e.g. 16,18,20,22,24")
        if len(chosen) != 5 or len(set(chosen)) != 5:
            raise SystemExit("Specify exactly five *distinct* CPU IDs with --cores.")
        if any(cpu not in allowed for cpu in chosen):
            raise SystemExit(f"Requested CPU not permitted. Allowed: {allowed}")
    else:
        chosen = []
        seen = set()
        for cpu in allowed:
            key = physical_key(cpu)
            # If topology unavailable, treat the CPU as a separate logical core.
            key = key if key is not None else ("logical", cpu)
            if key not in seen:
                chosen.append(cpu)
                seen.add(key)
            if len(chosen) == 5:
                break
        if len(chosen) != 5:
            raise SystemExit(
                "Need at least five distinct CPU cores available to this process. "
                f"Allowed logical CPU IDs: {allowed}"
            )

    known = [physical_key(c) for c in chosen]
    if all(k is not None for k in known) and len(set(known)) != 5:
        raise SystemExit(
            "Selected CPU IDs include hyperthread siblings of the same physical core. "
            "Choose five different physical cores (see --list-cores)."
        )
    return chosen


def print_cores():
    allowed = available_cpus()
    keys = [physical_key(x) for x in allowed]
    known = sum(k is not None for k in keys)
    print(f"Allowed logical CPUs (affinity/cgroup): {len(allowed)} — IDs: {allowed}")
    print(f"os.cpu_count() (host-visible logical CPUs): {os.cpu_count()}")
    if known == len(allowed):
        print(f"Distinct physical cores in allowed set: {len(set(keys))}")
    print("CPU ID    Physical socket/core")
    for cpu, key in zip(allowed, keys):
        print(f"{cpu:<9} {key if key is not None else 'unknown'}")


def run_abc(command, log_path, abc_root, external_timeout=None):
    """Run ABC with a private log. Kill the whole ABC/time process group on timeout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8", errors="replace") as f:
        proc = subprocess.Popen(
            ["/usr/bin/time", "-f", "[RUNNER-RAM] max_rss_kb=%M",
             "./abc", "-c", command],
            cwd=abc_root, stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=external_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except BaseException:
            # On interruption, do not leave a solver running in the background.
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    if timed_out:
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n[RUNNER] External subprocess timeout exceeded; process group killed.\n")
        status = "EXTERNAL_TIMEOUT"
    else:
        status = "OK" if rc == 0 else ("KILLED" if rc in (-9, 137) else f"EXIT_{rc}")
    return time.monotonic() - start, status


def write_csv_atomic(path, rows, fields):
    """One owner per CSV, updated via atomic rename after every completed design."""
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with temp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for name in sorted(rows):
            writer.writerow(rows[name])
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def previous_rows(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["Design"]: row for row in csv.DictReader(f)}


def parse_result(text, tool, wall, status):
    props = last(PROPS_RE, text)
    if status == "OK" and props is None:
        status = "NO_SUMMARY"
    if ABC_ERROR_RE.search(text):
        status = "ABC_ERROR"

    p = int(props.group(2)) if props else None
    d = int(props.group(3)) if props else None
    ram = last(PEAK_RAM_RE, text)
    result = {
        "Status": status,
        "Proved": str(p) if p is not None else "",
        "Disproved": str(d) if d is not None else "",
        "Undecided": props.group(4) if props else "",
        "Decided": str(p + d) if p is not None and d is not None else "",
        "Total_SAT": "", "Undecided_SAT": "", "Proved_SAT": "",
        "Rounds": "", "Time": "", "Wall_Time": f"{wall:.3f}",
        "Peak_RAM_MB": f"{int(ram.group(1)) / 1024:.2f}" if ram else "",
    }
    if tool == "purse":
        total = last(PURSE_TOTAL_RE, text)
        if total:
            result["Total_SAT"] = total.group(1)
            result["Undecided_SAT"] = total.group(2)
            result["Time"] = total.group(3)
        summaries = PURSE_SUMMARY_RE.findall(text)
        if summaries:
            result["Proved_SAT"] = str(sum(int(calls) for verdict, calls, _ in summaries if verdict == "SAFE"))
            result["Rounds"] = str(max(len(history) for _, _, history in summaries))
    else:
        calls = last(PDR_CALLS_RE, text)
        if calls:
            result["Total_SAT"] = calls.group(1)
        t = last(ABC_TIME_RE, text)
        if t:
            result["Time"] = t.group(1)
    return result


def prepare_metadata(designs, out, root):
    """Collect circuit statistics once, before any solver workers start."""
    stats_dir = out / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    meta = {}
    for idx, design in enumerate(designs, 1):
        name = Path(design).stem
        log = stats_dir / f"{name}_stats.log"
        if not log.exists():
            _, state = run_abc(f"read_aiger {design}; fold; strash; print_stats", log, root)
            if state != "OK":
                print(f"Warning: stats for {name} exited {state}", flush=True)
        output = log.read_text(encoding="utf-8", errors="replace")
        po = last(STATS_PO_RE, output)
        gates = last(STATS_AND_RE, output)
        latches = last(STATS_LAT_RE, output)
        a = int(gates.group(1)) if gates else None
        l = int(latches.group(1)) if latches else None
        meta[name] = {
            "Design": name,
            "PO": po.group(1) if po else "",
            "AND": str(a) if a is not None else "",
            "Latches": str(l) if l is not None else "",
            "Size_AND_plus_Latches": str(a + l) if a is not None and l is not None else "",
        }
        print(f"[stats {idx}/{len(designs)}] {name}: PO={meta[name]['PO'] or '?'}"
              f" AND={meta[name]['AND'] or '?'}", flush=True)
    write_csv_atomic(out / "metadata.csv", meta,
                     ["Design", "PO", "AND", "Latches", "Size_AND_plus_Latches"])
    return meta


def worker(core, modes, designs, meta, out, root, budget_b, budget_p, timeout_t,
           external_timeout, rerun_all):
    try:
        # This worker and all subprocesses spawned by it inherit this one-CPU affinity.
        os.sched_setaffinity(0, {core})
        for label in modes:
            csv_path = out / f"results_{label}.csv"
            rows = previous_rows(csv_path)
            tool, flags = CONFIGS[label]
            for index, design in enumerate(designs, 1):
                name = Path(design).stem
                if not rerun_all and rows.get(name, {}).get("Status") == "OK":
                    print(f"[CPU {core}][{label}][{index}/{len(designs)}] SKIP {name}", flush=True)
                    continue
                log = out / "logs" / label / f"{name}_{label}.log"
                prefix = f"read_aiger {design}; fold; strash; "
                if tool == "purse":
                    cmd = prefix + f"purse -B {budget_b} -P {budget_p} -T {timeout_t} {flags}"
                else:
                    cmd = prefix + f"pdr -T {timeout_t} {flags}"
                print(f"[CPU {core}][{label}][{index}/{len(designs)}] RUN {name}", flush=True)
                wall, status = run_abc(cmd, log, root, external_timeout)
                output = log.read_text(encoding="utf-8", errors="replace")
                result = parse_result(output, tool, wall, status)
                result.update(meta[name])
                result.update({"Variant": label, "CPU": str(core),
                               "Log": str(log.relative_to(out))})
                rows[name] = result
                write_csv_atomic(csv_path, rows, FIELDS)
                print(f"[CPU {core}][{label}][{index}/{len(designs)}] "
                      f"{result['Status']} decided={result['Decided'] or '?'} "
                      f"SAT={result['Total_SAT'] or '?'} "
                      f"wall={result['Wall_Time']}s", flush=True)
        print(f"[CPU {core}] Group complete: {', '.join(modes)}", flush=True)
    except Exception as exc:
        print(f"[CPU {core}] FATAL: {exc}", file=sys.stderr, flush=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-cores", action="store_true", help="Show allowed logical CPUs and physical-core topology; do not run ABC")
    parser.add_argument("--cores", help="Exactly five distinct physical-core CPU IDs, e.g. 16,18,20,22,24; selected automatically if omitted")
    parser.add_argument("--folder", default="test-designs", help="Circuit directory relative to ABC root (default: test-designs)")
    parser.add_argument("--out", default="runs_5cores", help="Output directory (default: runs_5cores)")
    parser.add_argument("--b", type=int, default=10000, help="PURSE conflict budget (default: 10000)")
    parser.add_argument("--p", type=int, default=100000, help="PURSE propagation budget (default: 100000)")
    parser.add_argument("--time", type=int, default=10, help="ABC timeout in seconds PER RUN (default: 10)")
    parser.add_argument("--external-timeout", type=int, default=None,
                        help="Optional external safety timeout; leave unset for normal experiment")
    parser.add_argument("--rerun-all", action="store_true", help="Rerun even successful designs (replace each row, preserving unique CSVs)")
    args = parser.parse_args()
    if args.list_cores:
        print_cores()
        return 0
    if args.b <= 0 or args.p <= 0 or args.time <= 0:
        parser.error("--b, --p, and --time must be positive integers")
    root = Path.cwd().resolve()
    if not (root / "abc").is_file():
        parser.error("Cannot find ./abc. Run from your built ABC directory")
    if not Path("/usr/bin/time").is_file():
        parser.error("GNU /usr/bin/time not found. Install `time` (e.g., sudo apt install time)")
    folder = root / args.folder
    if not folder.is_dir():
        parser.error(f"Circuit folder does not exist: {folder}")
    designs_abs = sorted(list(folder.glob("*.aig")) + list(folder.glob("*.aag")))
    if not designs_abs:
        parser.error(f"No .aig or .aag circuits in {folder}")
    names = [path.stem for path in designs_abs]
    dupes = [name for name, count in Counter(names).items() if count > 1]
    if dupes:
        parser.error(f"Duplicate circuit stems would overwrite results/logs: {dupes}")
    # Paths are relative to ABC's working directory.
    designs = [str(path.relative_to(root)) for path in designs_abs]
    for design in designs:
        if any(ch in design for ch in (';', '\n', '\r', '"')):
            parser.error(f"Unsafe special character in design filename: {design!r}")
    cores = choose_cpus(args.cores)
    print_cores()
    print("\nCPU assignments (one variant per distinct physical core; all concurrent):")
    for cpu, modes in zip(cores, GROUPS):
        print(f"  logical CPU {cpu}, physical={physical_key(cpu)}: {' -> '.join(modes)}")
    print(f"  ABC: {root / 'abc'}")
    print(f"  budgets: B={args.b} P={args.p} T={args.time}s")

    out = (root / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # A nonblocking exclusive lock prevents accidentally starting a second run
    # that would update the same output CSV/log paths.
    lockfile = (out / ".run_compare.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another run already owns output directory {out}. Choose a new --out.")
    lockfile.write(f"pid={os.getpid()}\n")
    lockfile.flush()

    # Refuse to resume an existing output folder if run settings have changed.
    # This prevents accidental mixtures of T=10 tests with T=3600 paper runs.
    abc_hash = hashlib.sha256()
    with (root / "abc").open("rb") as af:
        for chunk in iter(lambda: af.read(1048576), b""):
            abc_hash.update(chunk)
    manifest = {
        "abc_sha256": abc_hash.hexdigest(),
        "b": args.b, "p": args.p, "time": args.time,
        "external_timeout": args.external_timeout,
        "designs": [{"path": d, "size": (root / d).stat().st_size,
                     "mtime_ns": (root / d).stat().st_mtime_ns} for d in designs],
        "cores": cores,
        "configs": {name: list(definition) for name, definition in CONFIGS.items()},
        "groups": [list(group) for group in GROUPS],
    }
    manifest_path = out / "run_config.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old != manifest:
            differences = sorted(k for k in manifest if old.get(k) != manifest[k])
            raise SystemExit(
                f"Existing result folder has different configuration: {differences}. "
                "Use a NEW --out folder (recommended). Results are unchanged."
            )
    elif list(out.glob("results_*.csv")):
        raise SystemExit(
            f"Output directory {out} contains results but no run_config.json; "
            "use a new --out folder to avoid mixing experiments."
        )
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    meta = prepare_metadata(designs, out, root)

    children = []
    for cpu, modes in zip(cores, GROUPS):
        proc = Process(target=worker, args=(
            cpu, modes, designs, meta, out, root,
            args.b, args.p, args.time, args.external_timeout, args.rerun_all
        ))
        proc.start()
        children.append(proc)
    for proc in children:
        proc.join()
    failed = [proc.exitcode for proc in children if proc.exitcode != 0]
    if failed:
        print(f"One or more groups failed: exit codes {failed}; completed CSV rows are preserved", file=sys.stderr)
        return 1
    print("\nDone. Independent files are in:", out)
    for label in CONFIGS:
        print(" -", out / f"results_{label}.csv")
    print(" -", out / "metadata.csv")
    print(" -", out / "logs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
