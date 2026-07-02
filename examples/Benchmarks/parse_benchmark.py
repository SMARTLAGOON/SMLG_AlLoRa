"""Aggregate AlLoRa benchmark serial logs into per-RF-config throughput numbers.

Runs on the laptop (CPython only — no device imports). The Source benchmark main
prints one `BENCH,...` line per file it serves; capture the Source's serial output
to a file and feed it here:

    python3 parse_benchmark.py v2_baseline_sf7.log
    cat *.log | python3 parse_benchmark.py

Produces the v2 baseline table the v3 wire-format decision is gated on — so the
+1-header-byte / DATA-index calls are made against measured throughput, not arithmetic.

Line format emitted by the Source main:
    BENCH,sf=7,bw=125,cr=1,name=8.bin,bytes=8192,sec=12.340,retx=3,ok=1
(Other lines — BENCH_HEADER, BENCH_DONE, debug — are ignored.)
"""
import statistics


def parse_line(line):
    """Parse one `BENCH,k=v,...` line into a dict, or None if it isn't one."""
    line = line.strip()
    if not line.startswith("BENCH,"):
        return None
    fields = {}
    for kv in line[len("BENCH,"):].split(","):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        fields[k.strip()] = v.strip()
    return fields


def parse_benchmark(lines):
    """Aggregate BENCH lines into {(sf, bw, cr): summary}.

    `lines` is any iterable of strings (a file, stdin, a list). Throughput is
    bytes*8/sec per file; only completed transfers (ok=1) count toward throughput.
    """
    groups = {}
    for line in lines:
        f = parse_line(line)
        if not f:
            continue
        try:
            rec = {
                "sf": int(f["sf"]),
                "bw": int(f.get("bw", 125)),
                "cr": int(f.get("cr", 1)),
                "bytes": int(f["bytes"]),
                "sec": float(f["sec"]),
                "ok": int(f.get("ok", 1)),
                "retx": int(f.get("retx", 0)),
            }
        except (KeyError, ValueError):
            continue  # malformed line — skip, don't crash a long capture
        rec["bps"] = (rec["bytes"] * 8.0 / rec["sec"]) if rec["sec"] > 0 else 0.0
        groups.setdefault((rec["sf"], rec["bw"], rec["cr"]), []).append(rec)

    summary = {}
    for key, recs in groups.items():
        oks = [r for r in recs if r["ok"]]
        bps = [r["bps"] for r in oks if r["bps"] > 0]
        summary[key] = {
            "files": len(recs),
            "ok": len(oks),
            "success_rate": len(oks) / len(recs) if recs else 0.0,
            "total_bytes": sum(r["bytes"] for r in oks),
            "mean_bps": statistics.mean(bps) if bps else 0.0,
            "median_bps": statistics.median(bps) if bps else 0.0,
            "max_bps": max(bps) if bps else 0.0,
            "mean_retx": statistics.mean([r["retx"] for r in oks]) if oks else 0.0,
        }
    return summary


def format_summary(summary):
    """Render the aggregate as a fixed-width table sorted by (sf, bw, cr)."""
    header = "SF   BW    CR   files  ok    succ%   mean_bps   median_bps  max_bps    mean_retx"
    rows = [header]
    for (sf, bw, cr) in sorted(summary):
        s = summary[(sf, bw, cr)]
        rows.append("{:<4} {:<5} {:<4} {:<6} {:<5} {:<7.1f} {:<10.1f} {:<11.1f} {:<10.1f} {:<.2f}".format(
            sf, bw, cr, s["files"], s["ok"], s["success_rate"] * 100,
            s["mean_bps"], s["median_bps"], s["max_bps"], s["mean_retx"]))
    return "\n".join(rows)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as fh:
            data = fh.read().splitlines()
    else:
        data = sys.stdin.read().splitlines()
    print(format_summary(parse_benchmark(data)))
