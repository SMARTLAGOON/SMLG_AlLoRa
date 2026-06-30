"""Tests for the benchmark log parser (CPython-side, ADR 0003 decision gate)."""
from examples.Benchmarks.parse_benchmark import parse_line, parse_benchmark

SAMPLE = """\
BENCH_HEADER sf=7 bw=125 cr=1 cks=243
BENCH,sf=7,bw=125,cr=1,name=1.bin,bytes=1024,sec=1.0,retx=0,ok=1
BENCH,sf=7,bw=125,cr=1,name=2.bin,bytes=2048,sec=2.0,retx=1,ok=1
some unrelated debug line that should be ignored
BENCH,sf=12,bw=125,cr=1,name=1.bin,bytes=1024,sec=8.0,retx=2,ok=1
BENCH,sf=12,bw=125,cr=1,name=2.bin,bytes=2048,sec=20.0,retx=5,ok=0
BENCH_DONE
""".splitlines()


def test_parse_line_ignores_non_bench_lines():
    assert parse_line("BENCH_HEADER sf=7") is None
    assert parse_line("random text") is None
    assert parse_line("BENCH,sf=7,bytes=10,sec=1.0")["sf"] == "7"


def test_parse_benchmark_aggregates_per_rf_config():
    summary = parse_benchmark(SAMPLE)

    # Two RF configs seen.
    assert set(summary) == {(7, 125, 1), (12, 125, 1)}

    sf7 = summary[(7, 125, 1)]
    assert sf7["files"] == 2 and sf7["ok"] == 2
    assert sf7["success_rate"] == 1.0
    # 1024*8/1.0 == 8192 and 2048*8/2.0 == 8192 -> mean/median both 8192.
    assert sf7["mean_bps"] == 8192.0
    assert sf7["median_bps"] == 8192.0
    assert sf7["mean_retx"] == 0.5

    sf12 = summary[(12, 125, 1)]
    assert sf12["files"] == 2 and sf12["ok"] == 1      # the ok=0 transfer excluded from throughput
    assert sf12["success_rate"] == 0.5
    assert sf12["mean_bps"] == 1024.0                  # only the completed 1024 B / 8 s file
    assert sf12["mean_retx"] == 2.0                    # retx averaged over completed transfers only


def test_parse_benchmark_handles_empty_and_garbage():
    assert parse_benchmark([]) == {}
    assert parse_benchmark(["", "noise", "BENCH,sf=bad,bytes=x,sec=y"]) == {}
