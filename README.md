# drone-rid

Every FAA **UAS Declaration of Compliance** (Remote ID and Operations Over
People) as a flat table: make, model, created date, tracking number and serial
range, plus every individual serial number the declarants listed.

Source: https://uasdoc.faa.gov/listDocs. Snapshot taken 2026-10-12.

**Lookup page: https://i-infra.github.io/drone-rid/** — type a serial number
from a Remote ID broadcast and watch it narrow to the declared make and model
with each character.

| | |
|---|---|
| documents | 507 (478 Remote ID, 29 Operations Over People) |
| distinct makes | 165 |
| distinct make + model | 425 |
| individual serial values | 10,259,255 |

## Data

`data/faa_uasdoc_map.csv` — one row per declaration.

| column | meaning |
|---|---|
| `make`, `model`, `series` | as declared |
| `doc_type` | `rid` (Part 89 Remote ID) or `oop` (Part 107 Operations Over People) |
| `category` | OOP category, or "Unmanned Aircraft" / "Broadcast Module" for RID |
| `status` | `accepted` or `rescinded` |
| `created_at` | when the declaration was first submitted |
| `revision_created_at` | when the currently public revision was submitted |
| `updated_at` | the date the site's table shows |
| `tracking_number` | `RID…` / `OOP…` identifier; the public page is `https://uasdoc.faa.gov/listDocs/<tracking_number>` |
| `rev_number` | revision count of the public revision |
| `serial_range` | the serial range, see below |
| `serial_range_source` | where `serial_range` came from |
| `serial_start`, `serial_end` | the declarant's stated range, when they filled it in |
| `serial_total` | number of serial rows the site holds across all revisions |
| `serial_types` | formats seen: `ANSI/CTA-2063-A`, `RANGE`, `NO_FORMAT` |

The serial range lives in three different places on the site, and the
`serial_range_source` column says which one each row used:

- `declared` — the detail record's `serialNumberStart`/`serialNumberEnd` (102 documents)
- `range_rows` — one or more `start-end` rows on the public revision (191)
- `listed` — the declarant listed every serial; shown as `lowest..highest (N listed)` (214)

`data/faa_uasdoc_map.json` — the same rows with the raw detail fields
(`serial_description`, `fcc_identifier`, `revisions`, …).

`data/serials.csv.xz` — every serial value (10.26M rows): tracking number,
make, model, the revision it belongs to, whether that revision is the one
currently public, and the value. A `RANGE` row reads `start-end`. Older
revisions are included because they sometimes use a different scheme than the
current one.

`data/listing.json`, `data/dom_page1.json`, `data/listDocs.png` — the raw
listing as the API returned it, the first page of the rendered table compared
against it (8/8 rows matched), and a screenshot of the page as the browser saw
it.

## Tool

`tools/faa_doc_map.py` collects all of this. It drives a real browser (Brave)
through [friendly-arsenic](https://github.com/itdaniher/friendly-arsenic):
the browser opens `listDocs`, the script reads the rendered table from the
DOM as a cross-check, then has the page call its own JSON API
(`/api/v1/publicDOCRev`, `/api/v1/publicDOCRev/<tn>`, `/api/v1/serialNumbers`)
with `fetch()` from inside the document. Field provenance and the two traps
(serial lists of 100k+ rows; the range living in three places) are documented
at the top of the script.

Run it inside an arsenic image, with the script mounted over the toolkit's
`scripts/` directory:

```sh
docker run --rm --init --network host --shm-size=1g \
    --add-host notselenium.duckdns.org:127.0.0.1 \
    -v /path/to/friendly-arsenic:/app \
    -v $PWD/tools/faa_doc_map.py:/app/scripts/faa_doc_map.py:ro \
    -v $PWD/out:/out \
    --entrypoint python3 arsenic-brave \
    scripts/faa_doc_map.py --browser brave -o /out/faa_uasdoc
```

A full run takes about 35 minutes and is resumable (`docs.jsonl`).
`--rebuild` rewrites the CSV/JSON from `docs.jsonl` without a browser.

## Lookup site

`site/index.html` is a single static page. `tools/build_index.py` turns the
dataset into `site/index.json`: every declaration's serials collapsed into
`[low, high]` intervals (declared ranges, `start-end` rows, runs of
consecutive listed serials, and spans of listed serials that share all but
their last three characters). A typed prefix `q` is consistent with an
interval iff `low[:len(q)] <= q <= high[:len(q)]`; that one comparison over
~15k intervals is the whole search. The page says `exact` only when the full
serial lies inside a lossless interval; a hit inside a span means a serial of
that shape was declared, not necessarily this one.

The GitHub Actions workflow in `.github/workflows/pages.yml` rebuilds the index
and deploys `site/` to GitHub Pages on every push that touches the data, the
page or the builder. To build locally:

```sh
python3 tools/build_index.py --stats
python3 -m http.server -d site 8000   # then open http://localhost:8000/
```

## Provenance

Public records published by the FAA. Fetched once, as a browser would, with a
one-second pause between batches.
