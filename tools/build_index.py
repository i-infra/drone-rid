#!/usr/bin/env python3
"""build_index.py -- turn the dataset into the search index the site loads.

    tools/build_index.py                # data/ -> site/index.json
    tools/build_index.py --stats        # also print what it collapsed

The site answers one question: "I am typing a serial number from a Remote ID
broadcast; which make/model could it be?" That has to work on a static page,
so the 10.26M individual serial values are collapsed into intervals a browser
can scan on every keystroke:

  * a declared range   (serialNumberStart .. serialNumberEnd on the detail)
  * a RANGE row        ("start-end" in the serial list)
  * a run              consecutive listed serials that share a prefix and
                       count up in their trailing digits, e.g.
                       1875BF7GB2000001 .. 1875BF7GB2100000 (100,000 values)
  * a span             listed serials that are in no run, grouped by all but
                       their last `SPAN_TAIL` characters: [lowest, highest] of
                       the group, with the count. Skydio, AgEagle and Autel
                       list tens of thousands of serials that count up in
                       base-36 with gaps (X10PCH2234, X10PCH2238, ...); a
                       span says "a serial of this shape is one of theirs",
                       which is the question the page answers. Lossy by
                       design: a gap inside a span matches too.
  * a single           a listed serial that is in no run and alone in its span

Runs that share a span prefix are merged into that span as well, so a
document never contributes more than one interval per distinct
`len - SPAN_TAIL` prefix. That is what keeps the index small enough to ship.

Every interval is [low, high] over equal-length strings, so a typed prefix q
is consistent with it iff low[:len(q)] <= q <= high[:len(q)]. That one
comparison is the whole search; the page does nothing cleverer.

Runs are a lossless encoding of what was listed. A declared range or RANGE
row is the declarant's claim and is kept as stated. Intervals from older
revisions are kept too, flagged, because a serial issued under an earlier
revision is still that aircraft.
"""
import argparse
import collections
import json
import lzma
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RANGE_RE = re.compile(r"^([^-\s]+)-([^-\s]+)$")
SPAN_TAIL = 3
TRAIL_DIGITS = re.compile(r"^(.*?)(\d+)$")


def norm(v):
    return (v or "").strip().upper()


def load_docs(path):
    docs = json.load(open(path))
    docs.sort(key=lambda r: ((r["make"] or "").lower(), (r["model"] or "").lower(), r["tracking_number"]))
    return docs


def collapse(values):
    """values -> (intervals, singles). A run needs >= 2 consecutive members."""
    groups = collections.defaultdict(list)
    singles = []
    for v in values:
        m = TRAIL_DIGITS.match(v)
        if not m:
            singles.append(v)
            continue
        prefix, digits = m.group(1), m.group(2)
        groups[(prefix, len(digits))].append(int(digits))
    runs = []
    for (prefix, width), nums in groups.items():
        nums = sorted(set(nums))
        start = prev = nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
                continue
            runs.append((prefix, width, start, prev))
            start = prev = n
        runs.append((prefix, width, start, prev))
    # Everything in one span group collapses to one interval. A group with a
    # single lossless run keeps the "run" label (every value inside is
    # listed); any other group is a "span" (some values inside may not be).
    groups = collections.defaultdict(lambda: {"lo": None, "hi": None, "count": 0, "runs": 0, "members": 0})
    def add(lo, hi, count, is_run):
        g = groups[(len(lo), lo[:-SPAN_TAIL] if len(lo) > SPAN_TAIL else "")]
        g["lo"] = lo if g["lo"] is None or lo < g["lo"] else g["lo"]
        g["hi"] = hi if g["hi"] is None or hi > g["hi"] else g["hi"]
        g["count"] += count
        g["runs"] += is_run
        g["members"] += 1
    for prefix, width, a, b in runs:
        add(f"{prefix}{a:0{width}d}", f"{prefix}{b:0{width}d}", b - a + 1, 1)
    for v in singles:
        add(v, v, 1, 0)
    intervals = []
    for g in groups.values():
        if g["members"] == 1:
            kind = "run" if g["runs"] and g["count"] > 1 else "single"
        else:
            kind = "span"
        intervals.append((kind, g["lo"], g["hi"], g["count"]))
    return intervals


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "site", "index.json"))
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    docs = load_docs(os.path.join(args.data, "faa_uasdoc_map.json"))
    idx = {d["tracking_number"]: i for i, d in enumerate(docs)}

    # serials.csv: tracking_number, make, model, doc_rev_id, is_public_revision, value
    per = collections.defaultdict(lambda: {"ranges": [], "values": []})
    n_rows = 0
    with lzma.open(os.path.join(args.data, "serials.csv.xz"), "rt") as fh:
        header = fh.readline()
        assert header.startswith("tracking_number,"), header
        for line in fh:
            n_rows += 1
            tn, _make, _model, rev, is_pub, value = line.rstrip("\n").split(",", 5)
            key = (tn, rev, is_pub == "True")
            v = norm(value)
            m = RANGE_RE.match(v)
            if m:
                per[key]["ranges"].append((m.group(1), m.group(2)))
            else:
                per[key]["values"].append(v)

    out_docs, intervals = [], []
    kinds = collections.Counter()
    for d in docs:
        out_docs.append({
            "tn": d["tracking_number"],
            "make": d["make"],
            "model": d["model"],
            "series": d.get("series") or "",
            "type": d["doc_type"],
            "status": d["status"],
            "created": (d.get("created_at") or "")[:10],
            "category": d.get("category") or "",
        })
        i = idx[d["tracking_number"]]
        if d.get("serial_start") or d.get("serial_end"):
            lo, hi = norm(d.get("serial_start")), norm(d.get("serial_end"))
            if lo and hi:
                intervals.append([i, lo, hi, "declared", 1, 0])
            elif lo or hi:
                intervals.append([i, lo or hi, lo or hi, "declared", 1, 1])
            kinds["declared"] += 1

    # Revisions re-list what the previous one listed, so the same interval
    # shows up once per revision; keep one, preferring the public revision.
    seen = {}
    for (tn, rev, is_pub), got in sorted(per.items(), key=lambda kv: not kv[0][2]):
        i = idx[tn]
        pub = 1 if is_pub else 0
        found = [("range", lo, hi, 0) for lo, hi in got["ranges"]] + collapse(got["values"])
        for kind, lo, hi, count in found:
            key = (i, lo, hi)
            if key in seen:
                seen[key][5] = max(seen[key][5], count)
                continue
            seen[key] = [i, lo, hi, kind, pub, count]
            kinds[kind] += 1
    intervals.extend(seen.values())

    # Stable order: by low bound, so the page's scan is cache-friendly and the
    # diff of a rebuild is readable.
    intervals.sort(key=lambda t: (t[1], t[2], t[0]))
    payload = {
        "built_from": "uasdoc.faa.gov snapshot 2026-10-12",
        "docs": out_docs,
        "intervals": intervals,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))

    size = os.path.getsize(args.out)
    print(f"{n_rows} serial rows -> {len(intervals)} intervals ({dict(kinds)}) "
          f"for {len(docs)} documents; {size/1e6:.2f} MB -> {args.out}")
    if args.stats:
        per_doc = collections.Counter(t[0] for t in intervals)
        worst = per_doc.most_common(5)
        for i, n in worst:
            print(f"  {docs[i]['tracking_number']} {docs[i]['make']} {docs[i]['model']}: {n} intervals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
