#!/usr/bin/env python3
"""faa_doc_map.py -- map make/model -> created date, tracking number, serial range
from the FAA UAS Declaration of Compliance site (https://uasdoc.faa.gov/listDocs).

    # one shot inside an arsenic image (Brave or Chromium)
    scripts/faa_doc_map.py --browser brave -o /out/faa_uasdoc

    # a few documents, to check a layout or API change
    scripts/faa_doc_map.py --browser brave -o /out/faa_uasdoc --limit 5

    # against a daemon already running (see download_apk.py --daemon)
    scripts/faa_doc_map.py --session chrome -o /out/faa_uasdoc

    # rewrite the CSV/JSON outputs from docs.jsonl (no browser, no network)
    scripts/faa_doc_map.py --rebuild -o /out/faa_uasdoc

Outputs, under -o:
    docs.jsonl              one record per document, written as fetched (resumable)
    faa_uasdoc_map.json     the finished mapping, one object per document
    faa_uasdoc_map.csv      flat: make, model, series, type, status, created,
                            tracking number, serial range, serial count
    serials.csv             every individual serial-number row the site holds,
                            keyed by tracking number and revision
    dom_page1.json          what the rendered table showed on the first page,
                            and whether it agreed with the API
    listDocs.png            screenshot of the page as the browser saw it

How the data is reached, and why this way
-----------------------------------------
listDocs is a React app over a JSON API at /api/v1. The table is paginated
client-side (25 rows a page, 500+ documents), and the serial range is not in
the table at all: it lives on each document's detail view, and the individual
serial numbers behind the "Serial Numbers" button come from yet another call.
Clicking through 500 detail pages and 500 modals is slow and fragile; the
page's own three calls are not:

    GET /api/v1/publicDOCRev?itemsPerPage=N&pageIndex=K   the listing
    GET /api/v1/publicDOCRev/<trackingNumber>             detail, incl.
                                                          serialNumberStart/End
    GET /api/v1/serialNumbers?findBy=docTrackingNumber&docTrackingNumber=<tn>
        &formatForDocView=true&itemsPerPage=50&pageIndex=0  row count + types
    GET /api/v1/serialNumbers?findBy=docRevIds&docRevIds[]=<rev>&limit=-1
        (Accept: text/csv)                                 every serial value
                                                           of one revision

The CSV export is what the page's "Download CSV" button calls, and it matters:
some documents carry tens of thousands of individually listed serials
(OOP000000079: 58,955 rows), and the JSON endpoint pages those at ~400 bytes a
row. The CSV is one value per line, so the same list is ~1MB instead of ~24MB
spread over 59 round trips through the control channel.

So, as genius_corpus.py does for annotations, the *page* makes those calls:
`fetch()` evaluated in the document's own world, carrying the site's cookies,
UA and origin exactly as its React code would. The browser is a real Brave on
a real display; nothing here speaks to the server except through it.

The rendered table is still read once, from the DOM, and compared against the
API: it is the proof that the API answers describe the page a person would see.

Field provenance (so nobody has to re-derive it from the minified bundle):
  created_at            document `createdAt` (first submission)
  revision_created_at   `publicRevision.createdAt` (the revision shown publicly)
  updated_at            listing `updatedAt` (what the table's date column shows)
  serial_start/_end     `publicRevision.serialNumberStart/End`
  serial_total          /serialNumbers totalItems (rows across all revisions)
  serial_types          the `type` values seen on the first 50 rows: NO_FORMAT,
                        RANGE or ANSI/CTA-2063-A
  serials_by_revision   {docRevId: [value, ...]} from the CSV export, one call
                        per revision; a RANGE row reads "start-end"
  serial_range          derived, for the flat CSV: serialNumberStart..End when
                        the detail carries them (102 of 507 documents do);
                        otherwise the public revision's "start-end" rows joined
                        with " | "; otherwise, for a plain list, its lowest and
                        highest value. `serial_range_source` says which.
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genius_frames as F  # noqa: E402  (frame selection is site-agnostic)

ORIGIN = "uasdoc.faa.gov"
LIST_URL = f"https://{ORIGIN}/listDocs"
API = "/api/v1"
LIST_PAGE = 200      # listing page size; the server honoured 1000, this is polite
SERIAL_SAMPLE = 50   # JSON rows fetched per document, for count and types
CHUNK = 200_000      # characters per eval when pulling a response back
TABLE_READY = "document.querySelector('table tbody tr td') !== null"


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# the page fetches for us
# --------------------------------------------------------------------------

def page_fetch_many(b, urls, timeout=120, poll=0.5, accept="application/json"):
    """Have the current document fetch `urls` (same-origin) and hand back
    {url: {"status": int, "text": str} | {"error": str}}.

    `eval` on the control channel is synchronous and returns a JSON-able value,
    so the fetch is kicked off, its result parked on a window global under a
    one-off key, and polled. The shim's eval does unwrap a returned promise,
    but a slow answer would then collide with the channel's own timeout; the
    park-and-poll shape never blocks the channel for longer than one round trip.

    Only the counters are polled, and each body is pulled back once, in slices.
    The first version serialized the whole slot on every poll; on a document
    with 59k serial rows that meant re-sending megabytes twice a second and
    the run stalled for ten minutes on one batch.
    """
    key = "f" + uuid.uuid4().hex
    js = (
        "(function(){"
        "window.__faa=window.__faa||{};"
        f"var slot=window.__faa[{key!r}]={{done:0,n:{len(urls)},r:{{}}}};"
        f"{json.dumps(urls)}.forEach(function(u){{"
        f"fetch(u,{{credentials:'same-origin',headers:{{accept:{accept!r}}}}})"
        ".then(function(res){return res.text().then(function(t){"
        "slot.r[u]={status:res.status,text:t};slot.done++;});})"
        ".catch(function(e){slot.r[u]={error:String(e)};slot.done++;});"
        "});return true;})()"
    )
    if b.eval(js).get("value") is not True:
        raise RuntimeError("page refused to start the fetch (eval returned no value)")
    slot = f"window.__faa[{key!r}]"
    deadline = time.time() + timeout
    try:
        while True:
            got = b.eval(f"{slot}.done + '/' + {slot}.n").get("value")
            if got and got.split("/")[0] == got.split("/")[1]:
                break
            if time.time() > deadline:
                raise TimeoutError(f"page fetch of {len(urls)} urls did not finish in {timeout}s")
            time.sleep(poll)
        out = {}
        for u in urls:
            r = f"{slot}.r[{json.dumps(u)}]"
            meta = b.eval(f"JSON.stringify({{status:{r}.status,error:{r}.error,len:({r}.text||'').length}})").get("value")
            meta = json.loads(meta)
            if meta.get("error"):
                out[u] = {"error": meta["error"]}
                continue
            parts, pos, n = [], 0, int(meta["len"])
            while pos < n:
                piece = b.eval(f"{r}.text.slice({pos},{pos + CHUNK})").get("value")
                if not isinstance(piece, str):
                    raise RuntimeError(f"{u}: could not read response slice at {pos}")
                parts.append(piece)
                pos += CHUNK
            out[u] = {"status": meta["status"], "text": "".join(parts)}
        return out
    finally:
        b.eval(f"delete {slot}")


def api_json(result, url):
    """Unwrap one page_fetch_many result into the API's `data` object."""
    if "error" in result:
        raise RuntimeError(f"{url}: {result['error']}")
    if result["status"] != 200:
        raise RuntimeError(f"{url}: HTTP {result['status']}: {result['text'][:200]}")
    body = json.loads(result["text"])
    err = body.get("error") or {}
    if err.get("message") or err.get("errors"):
        raise RuntimeError(f"{url}: API error {err}")
    return body["data"]


def q(path, **params):
    return f"{API}{path}?" + urllib.parse.urlencode(params)


# --------------------------------------------------------------------------
# the three calls
# --------------------------------------------------------------------------

def fetch_listing(b):
    """Every row of the public listing, paged."""
    items, page, total = [], 0, None
    while total is None or len(items) < total:
        url = q("/publicDOCRev", itemsPerPage=LIST_PAGE, pageIndex=page)
        data = api_json(page_fetch_many(b, [url])[url], url)
        got = data.get("items") or []
        total = int(data.get("totalItems") or 0)
        if not got:
            break
        items.extend(got)
        log(f"[list] page {page}: {len(got)} rows ({len(items)}/{total})")
        page += 1
    # The server is authoritative about the count; a duplicate would mean it
    # re-sorted between pages, which is worth knowing rather than hiding.
    seen = {}
    for it in items:
        seen.setdefault(it["trackingNumber"], it)
    if len(seen) != len(items):
        log(f"[list] WARNING: {len(items) - len(seen)} duplicate rows across pages")
    return list(seen.values()), total


def fetch_docs(b, tracking_numbers):
    """Detail + serial sample for a few documents in one round trip, then the
    CSV export of every serial value, one call per revision."""
    urls = {}
    for tn in tracking_numbers:
        urls[tn] = (
            q(f"/publicDOCRev/{tn}"),
            q("/serialNumbers", findBy="docTrackingNumber", docTrackingNumber=tn,
              formatForDocView="true", itemsPerPage=SERIAL_SAMPLE, pageIndex=0),
        )
    flat = [u for pair in urls.values() for u in pair]
    results = page_fetch_many(b, flat)
    out = {}
    for tn, (durl, surl) in urls.items():
        try:
            detail = api_json(results[durl], durl)
            sample = api_json(results[surl], surl)
            items = detail.get("items") or []
            revs = [r.get("docRevId") for r in ((items[0] if items else {}).get("revisions") or [])]
            revs = [r for r in revs if r is not None]
            csv_urls = {rev: f"{API}/serialNumbers?findBy=docRevIds&docRevIds%5B%5D={rev}&limit=-1"
                        for rev in revs}
            by_rev = {}
            if csv_urls:
                got = page_fetch_many(b, list(csv_urls.values()), accept="text/csv")
                for rev, url in csv_urls.items():
                    r = got[url]
                    if "error" in r or r["status"] != 200:
                        raise RuntimeError(f"{url}: {r.get('error') or 'HTTP ' + str(r['status'])}")
                    by_rev[str(rev)] = [ln.strip() for ln in r["text"].splitlines() if ln.strip()]
        except Exception as e:  # one bad document must not sink the batch
            out[tn] = {"error": str(e)}
            continue
        out[tn] = {
            "detail": detail,
            "serial_sample": list(sample.get("items") or []),
            "serial_total": int(sample.get("totalItems") or 0),
            "serials_by_revision": by_rev,
        }
    return out


def build_record(listing_row, fetched):
    items = fetched["detail"].get("items") or []
    doc = items[0] if items else {}
    rev = doc.get("publicRevision") or {}
    types = sorted({r.get("type") or "" for r in fetched["serial_sample"]})
    return {
        "tracking_number": listing_row["trackingNumber"],
        "doc_type": listing_row.get("docType"),
        "category": listing_row.get("categoryDeclarationFor"),
        "make": rev.get("makeName") or listing_row.get("makeName"),
        "model": rev.get("modelName") or listing_row.get("modelName"),
        "series": rev.get("series") if rev.get("series") is not None else listing_row.get("series"),
        "status": listing_row.get("status"),
        "created_at": doc.get("createdAt"),
        "revision_created_at": rev.get("createdAt"),
        "updated_at": listing_row.get("updatedAt"),
        "rev_number": rev.get("revNumber", listing_row.get("revNumber")),
        "public_doc_rev_id": rev.get("docRevId"),
        "serial_start": rev.get("serialNumberStart"),
        "serial_end": rev.get("serialNumberEnd"),
        "serial_description": rev.get("serialNumberDescription"),
        "fcc_identifier": rev.get("fccIdentifier"),
        "serial_total": fetched["serial_total"],
        "serial_types": types,
        "serial_csv_rows": sum(len(v) for v in fetched["serials_by_revision"].values()),
        "serials_by_revision": fetched["serials_by_revision"],
        "revisions": doc.get("revisions") or [],
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --------------------------------------------------------------------------
# the rendered table, for the cross-check
# --------------------------------------------------------------------------

DOM_ROWS_JS = """JSON.stringify(Array.from(document.querySelectorAll('table tbody tr'))
    .map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())))"""
DOM_HEAD_JS = """JSON.stringify(Array.from(document.querySelectorAll('table thead th'))
    .map(th => th.innerText.trim()))"""


def read_table(b):
    heads = json.loads(F._value(b, DOM_HEAD_JS) or "[]")
    rows = json.loads(F._value(b, DOM_ROWS_JS) or "[]")
    return heads, rows


def cross_check(heads, rows, by_tn):
    """Does every row the page rendered appear in the API with the same make
    and model? Tracking numbers are unambiguous tokens (RID…/OOP…), so find
    the cell by shape rather than trusting a column index."""
    report = {"headers": heads, "rows": len(rows), "matched": 0, "mismatched": []}
    for cells in rows:
        tn = next((c for c in cells if c[:3] in ("RID", "OOP") and c[3:].isdigit()), None)
        if not tn:
            report["mismatched"].append({"cells": cells, "why": "no tracking number cell"})
            continue
        api = by_tn.get(tn)
        if not api:
            report["mismatched"].append({"cells": cells, "why": "not in API listing"})
            continue
        ok = api.get("makeName") in cells and api.get("modelName") in cells
        if ok:
            report["matched"] += 1
        else:
            report["mismatched"].append({"cells": cells, "api": {
                "make": api.get("makeName"), "model": api.get("modelName")}})
    return report


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

MAP_COLUMNS = [
    "make", "model", "series", "doc_type", "category", "status", "created_at",
    "revision_created_at", "updated_at", "tracking_number", "rev_number",
    "serial_range", "serial_range_source", "serial_start", "serial_end",
    "serial_total", "serial_types",
]

RANGE_RE = re.compile(r"^[^-\s]+-[^-\s]+$")


def derive_range(r):
    """(serial_range, source) -- see the field notes in the module docstring.

    The detail's serialNumberStart/End are the declarant's own statement of
    the range and win when present. Most documents leave them blank and
    express the range as RANGE rows ("start-end") instead, or list every
    serial; a plain list is summarized as lowest..highest with the count
    alongside so nobody mistakes it for a range. Only the public revision's
    rows are used: older revisions can hold a different scheme entirely
    (RID000002616 lists 9,999 serials on its first revision and one RANGE on
    the current one), so the first-50-rows `serial_types` sample, which spans
    revisions, is deliberately not consulted here.
    """
    if r.get("serial_start") or r.get("serial_end"):
        return f"{r.get('serial_start') or ''}..{r.get('serial_end') or ''}", "declared"
    by_rev = r.get("serials_by_revision") or {}
    values = by_rev.get(str(r.get("public_doc_rev_id"))) or []
    if not values:  # no rows on the public revision; fall back to any revision
        values = [v for vs in by_rev.values() for v in vs]
    ranges = [v for v in values if RANGE_RE.match(v)]
    listed = sorted(v for v in values if not RANGE_RE.match(v))
    parts, source = [], []
    if ranges:
        parts.append(" | ".join(ranges))
        source.append("range_rows")
    if listed:
        parts.append(f"{listed[0]}..{listed[-1]} ({len(listed)} listed)")
        source.append("listed")
    return " ; ".join(parts), "+".join(source) or "none"


def write_outputs(outdir, records):
    records = sorted(records, key=lambda r: ((r["make"] or "").lower(), (r["model"] or "").lower(),
                                             r["tracking_number"]))
    for r in records:
        r["serial_range"], r["serial_range_source"] = derive_range(r)
    with open(os.path.join(outdir, "faa_uasdoc_map.json"), "w") as fh:
        json.dump(records, fh, indent=1)
    with open(os.path.join(outdir, "faa_uasdoc_map.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MAP_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["serial_types"] = "|".join(r["serial_types"])
            row["serial_range"], row["serial_range_source"] = derive_range(r)
            w.writerow(row)
    with open(os.path.join(outdir, "serials.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tracking_number", "make", "model", "doc_rev_id", "is_public_revision", "value"])
        for r in records:
            for rev, values in r["serials_by_revision"].items():
                for v in values:
                    w.writerow([r["tracking_number"], r["make"], r["model"], rev,
                                str(rev) == str(r["public_doc_rev_id"]), v])


def load_done(path):
    done = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    done[rec["tracking_number"]] = rec
    return done


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

def bring_up(args):
    import download_apk as D
    if args.session:
        b, _st = D.attach_session(args.session)
        return b, None
    b, fields = D.launch_session(args.browser, profile_dir=args.profile_dir, detached=False)
    for _ in range(40):
        try:
            if b.get_uids():
                return b, fields
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("browser came up with no frames")


def open_listing(b, outdir):
    log(f"[page] {LIST_URL}")
    uid = F.goto(b, LIST_URL, origin=ORIGIN, timeout=60, settle=3.0, want=TABLE_READY)
    if not uid:
        shot = b.get_screenshot()
        shutil.move(shot, os.path.join(outdir, "listDocs_failed.png"))
        raise RuntimeError("listDocs never rendered a table row (see listDocs_failed.png)")
    shot = b.get_screenshot()
    shutil.move(shot, os.path.join(outdir, "listDocs.png"))
    log(f"[page] top-level document selected (uid {uid}); screenshot saved")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--browser", default="brave", choices=["brave", "chromium", "firefox"])
    ap.add_argument("--session", help="attach to a running daemon instead of launching")
    ap.add_argument("--profile-dir")
    ap.add_argument("-o", "--outdir", default="./faa_uasdoc")
    ap.add_argument("--limit", type=int, help="stop after N documents")
    ap.add_argument("--batch", type=int, default=3, help="documents per round trip")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between batches")
    ap.add_argument("--refetch", action="store_true", help="ignore docs.jsonl")
    ap.add_argument("--rebuild", action="store_true",
                    help="rewrite the outputs from docs.jsonl; no browser, no network")
    args = ap.parse_args()

    if args.rebuild:
        done = load_done(os.path.join(args.outdir, "docs.jsonl"))
        write_outputs(args.outdir, list(done.values()))
        log(f"[rebuild] {len(done)} documents -> {args.outdir}")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    jsonl_path = os.path.join(args.outdir, "docs.jsonl")
    done = {} if args.refetch else load_done(jsonl_path)
    if done:
        log(f"[resume] {len(done)} documents already on disk")

    b, _fields = bring_up(args)
    rc = 0
    try:
        open_listing(b, args.outdir)

        heads, rows = read_table(b)
        log(f"[dom] table: {len(rows)} rows, columns {heads}")

        listing, total = fetch_listing(b)
        log(f"[list] {len(listing)} documents (server says {total})")
        by_tn = {r["trackingNumber"]: r for r in listing}
        with open(os.path.join(args.outdir, "listing.json"), "w") as fh:
            json.dump(listing, fh, indent=1)

        check = cross_check(heads, rows, by_tn)
        with open(os.path.join(args.outdir, "dom_page1.json"), "w") as fh:
            json.dump({"check": check, "rows": rows}, fh, indent=1)
        log(f"[dom] cross-check: {check['matched']}/{check['rows']} rendered rows match the API"
            + (f"; {len(check['mismatched'])} MISMATCHED" if check["mismatched"] else ""))

        todo = [r for r in listing if r["trackingNumber"] not in done]
        if args.limit:
            todo = todo[: args.limit]
        log(f"[docs] fetching {len(todo)} documents, {args.batch} per round trip")
        failures = []
        with open(jsonl_path, "a" if not args.refetch else "w") as out:
            for i in range(0, len(todo), args.batch):
                batch = todo[i:i + args.batch]
                tns = [r["trackingNumber"] for r in batch]
                try:
                    got = fetch_docs(b, tns)
                except Exception as e:
                    log(f"  ! batch {tns} failed: {type(e).__name__}: {e}")
                    failures.extend(tns)
                    time.sleep(args.delay * 3)
                    continue
                for r in batch:
                    tn = r["trackingNumber"]
                    f = got.get(tn) or {"error": "missing from batch"}
                    if "error" in f:
                        log(f"  ! {tn}: {f['error']}")
                        failures.append(tn)
                        continue
                    rec = build_record(r, f)
                    done[tn] = rec
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                n = min(i + args.batch, len(todo))
                if n % 30 < args.batch or n == len(todo):
                    last = done.get(tns[-1]) or {}
                    log(f"  {n}/{len(todo)}  {tns[-1]} {last.get('make')} {last.get('model')} "
                        f"serials={last.get('serial_total')}/{last.get('serial_csv_rows')} "
                        f"range={last.get('serial_start')}..{last.get('serial_end')}")
                time.sleep(args.delay)

        write_outputs(args.outdir, list(done.values()))
        log(f"[done] {len(done)} documents in {args.outdir}; "
            f"{sum(r['serial_csv_rows'] for r in done.values())} serial values")
        if failures:
            log(f"[done] {len(failures)} failed: {failures}")
            with open(os.path.join(args.outdir, "failures.json"), "w") as fh:
                json.dump(failures, fh)
            rc = 2
    except Exception as e:
        log(f"[fatal] {type(e).__name__}: {e}")
        try:
            shutil.move(b.get_screenshot(), os.path.join(args.outdir, "fatal.png"))
        except Exception:
            pass
        rc = 1
    finally:
        if not args.session:
            try:
                b.terminate()
            except Exception as e:
                log(f"[teardown] {type(e).__name__}: {e}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
