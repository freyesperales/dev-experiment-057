# Development Experiment 057 — gnomon

**An offline, dependency-free (pure-stdlib) DIVERGENCE AND BLAST-RADIUS ENGINE for
compiled tz databases.** It answers the one question no tool inside a database can
answer about itself: *is the timezone data my server is using the data the world
is using, and if not, which rows have I already written down wrong?*

A gnomon is the part of a sundial that casts the shadow — the piece that turns the
passage of time into a reading you can act on.

The series' first entry in the **TIME-CORRECTNESS / DATABASE-INTERNALS** lane, and
its first whose subject is a *disagreement between two copies of the same
reference data* rather than a single artefact.

---

## Thesis

On 2026-09-28 the top of Hacker News carried a Postgres timezone footgun; behind
it sat a Supabase issue reporting that its Postgres image is compiled against
`tzdata-2025b` while the container ships `tzdata-2026d`. The consequence is
narrow and nasty. In 2026 British Columbia, Alberta, Manitoba and the Northwest
Territories stopped changing their clocks, and Morocco settled on permanent UTC.
A server built against the older data therefore converts local wall clocks with
rules that no longer exist — and `(date + time) AT TIME ZONE tz`, the documented
way to turn user-entered local time into a stored instant, is exactly that
conversion. It raises nothing. The wrong instant is materialised at write time,
so every downstream consumer inherits it: reminders, notifications, iCalendar
feeds. And `pg_timezone_names` is a view over the same stale directory, so
**nothing inside the database can reveal the skew.** For Canada the detonation
date is 2026-11-01, thirty-three days after this was built; for Morocco it has
already passed.

The insight is that the disagreement must be measured from *outside*, by reading
both databases' bytes directly, and that the interesting direction is the one
everybody's tooling omits: not "what offset applies at this instant" but its
**inverse** — "which instants does this local wall clock denote?" That inverse is
not a function. A reading can denote zero instants (it fell in a spring-forward
gap), one, or two (an autumn fold). Two databases can disagree not just about
*which* instant a reading means but about *how many* it has, and that is the
difference between a row that is an hour out and a row whose intent is no longer
recoverable at all.

The smallest thing that proves this is a TZif reader, an exact interval-wise
divergence engine over two zone trees, and the inverse map applied to a table of
the readings your users actually entered — classified, and reduced to SQL where
arithmetic can honestly repair it.

**Lane and differentiation.** The last ten entries are domain engines over a
single file format — spreadsheets, e-invoices, GTFS, subtitles, binary
containers. This one's unit of analysis is a *pair* of reference databases and
the silent divergence between them, and it is the first entry to ship a
**remediation** artefact (corrective SQL) alongside the analysis rather than a
verdict alone.

---

## Quick start

No dependencies. Python 3.11+ (developed and tested on 3.13). Nothing touches the
network, and nothing reads the host timezone database — it works on a machine
that has none.

```bash
python main.py                    # guided demonstration over the bundled samples
python -m pytest tests -q         # 156 tests
python samples/build_samples.py   # regenerate the synthetic sample databases
```

Equivalent `make` targets exist (`make run`, `make test`, `make samples`,
`make check`, `make clean`) — see *Assumptions and limitations* for their
verification status.

### Against your own server

```bash
# 1. Find the tree Postgres was actually compiled against.
pg_config --sharedir            # then look in <sharedir>/timezone
# In a container, the two trees are often both present but only one is used:
#   /usr/share/zoneinfo                     <- the OS copy (often current)
#   /nix/store/*-tzdata-*/share/zoneinfo    <- what the server was built against

# 2. Compare them. Exit code 2 means a future divergence exists.
python -m gnomon.cli diff /nix/store/…-tzdata-2025b/share/zoneinfo /usr/share/zoneinfo

# 3. Check one reading by hand.
python -m gnomon.cli resolve OLD NEW --zone America/Vancouver --wall "2026-11-15 09:30"

# 4. Find your affected rows and get SQL for them.
psql -Atc "COPY (SELECT id, tz AS zone, local_start AS local_time FROM bookings)
           TO STDOUT WITH CSV HEADER" > rows.csv
python -m gnomon.cli blast OLD NEW --data rows.csv \
    --sql fix.sql --table bookings --instant-column starts_at --id-column id
```

`gnomon zones TREE` lists what a tree contains, with each zone's POSIX footer —
useful on its own for confirming which tree a container is really serving.

---

## Sample run

Over the bundled synthetic databases, as of 2026-09-29:

```
  left  (assumed stale)  : 2025b (samples/trees/tzdata-2025b)
  right (authoritative)  : 2026d (samples/trees/tzdata-2026d)
  window                 : 2025-09-29 00:00:00Z .. 2029-09-28 00:00:00Z
  'now'                  : 2026-09-29 00:00:00Z

  FIRST WRONG ANSWER     : 2026-11-01 08:00:00Z (in 33 day(s)) in America/Edmonton
  worst severity         : CRITICAL
  verdicts               : 1 abbrev-only, 3 future-divergence, 2 identical

[CRITICAL] America/Edmonton  (future-divergence)
    footer MST7MDT,M3.2.0,M11.1.0 -> CST6
    -07:00 -> -06:00 (delta +1h)
    future  2026-03-08 09:00:00Z .. 2026-11-01 08:00:00Z  label MDT vs CST (same offset)
    future  2026-11-01 08:00:00Z .. 2027-03-14 09:00:00Z  MST -07:00 vs CST -06:00  delta +1h
    future  2027-03-14 09:00:00Z .. 2027-11-07 08:00:00Z  label MDT vs CST (same offset)
    future  2027-11-07 08:00:00Z .. 2028-03-12 09:00:00Z  MST -07:00 vs CST -06:00  delta +1h
    future  ... 3 more interval(s)

[CRITICAL] America/Vancouver  (future-divergence)
    aliases: Canada/Pacific
    footer PST8PDT,M3.2.0,M11.1.0 -> MST7
    -08:00 -> -07:00 (delta +1h)

[INFO    ] America/Whitehorse  (abbrev-only)
    footer <-07>7 -> MST7
    future  2025-09-29 00:00:00Z .. onwards  label -07 vs MST (same offset)
```

Note the shape of Edmonton: the label changes at the last spring-forward, but the
*offsets* only diverge each winter, from 2026-11-01 onwards, for ever. Note also
that `Canada/Pacific` is reported once, as an alias of `America/Vancouver`, rather
than as a separate finding.

And the blast radius over the bundled `samples/scheduled.csv`:

```
  rows analysed          : 14
  worst severity         : CRITICAL

       4  agree
       4  shifted        <- silent corruption: the row already holds the wrong value
       1  gap-appears    <- the reading no longer exists; Postgres shifts it, silently
       1  gap-resolves
       2  fold-appears   <- the reading is now ambiguous; half of these are wrong
       1  fold-resolves
       1  zone-missing

  bkg-1001  2026-12-15 09:00:00  America/Vancouver  [CRITICAL] shifted
      stored 2026-12-15 17:00:00Z (-08:00 PST) -> correct 2026-12-15 16:00:00Z (-07:00 MST)
      correction -1h

NEEDS A HUMAN, NOT ARITHMETIC (4)
    bkg-1007  2026-03-29 02:30:00  Africa/Casablanca  (gap-appears)
    bkg-1013  2027-03-01 09:00:00  America/Coyhaique  (zone-missing)
    bkg-1012  2022-10-30 03:30:00  Europe/Chisinau    (fold-appears)
    bkg-1009  2026-09-20 01:30:00  Africa/Casablanca  (fold-appears)

CORRECTIONS NEEDED
       4 row(s)  -1h  (-3600 seconds)
       2 row(s)  +1h  (+3600 seconds)
```

With `--sql`:

```sql
-- Stored values are assumed to have been written through: 2025b (…/tzdata-2025b)
-- Authoritative database: 2026d (…/tzdata-2026d)
-- This repairs rows that were already written. It does not fix the cause:
-- rebuild or repoint the server at current tzdata, or every future write
-- reintroduces the same error.
BEGIN;
-- 4 row(s) need -1h (-3600 seconds); classes: fold-appears, shifted
UPDATE bookings SET starts_at = starts_at + INTERVAL '-3600 seconds'
  WHERE booking_id IN ('bkg-1001', 'bkg-1004', 'bkg-1005', 'bkg-1012');
-- 2 row(s) need +1h (+3600 seconds); classes: gap-appears, shifted
UPDATE bookings SET starts_at = starts_at + INTERVAL '3600 seconds'
  WHERE booking_id IN ('bkg-1006', 'bkg-1007');
-- Verify the row count matches the comment above, then:
-- COMMIT;   (or ROLLBACK; if it does not)
```

---

## Architecture

```
                 bytes on disk                          your data
                       │                                    │
        ┌──────────────┴──────────────┐                     │
        │  tzif.py     RFC 8536 reader│                     │
        │  posixtz.py  footer parser  │                     │
        └──────────────┬──────────────┘                     │
                       ▼                                    │
              rules.py  ZoneRules                           │
        forward:  instant  -> offset                        │
        inverse:  wall     -> {0,1,2} instants  ◄────────────┤
        breakpoints: where the offset can change             │
                       │                                    │
        ┌──────────────┴───────────┐          ┌──────────────┴─────────────┐
        │ tree.py   zone discovery │          │ blast.py  per-row verdicts │
        │ diverge.py interval diff │          │           + corrective SQL │
        └──────────────┬───────────┘          └──────────────┬─────────────┘
                       └───────────────┬──────────────────────┘
                                       ▼
                        report.py  text + JSON   ·   cli.py  exit codes
                                       ▲
                        tzifwrite.py  TZif writer (samples, fixtures)
```

**Data flow.** `tree.py` walks a directory, sniffs each file for the TZif magic
(so an unfamiliar layout loses zones rather than crashing), and parses the ones
that qualify. `diverge.py` compares two zones; `blast.py` compares two inverse
maps against caller data. `report.py` renders; `cli.py` decides the exit code.

### The one idea worth stating

**Sampling is the obvious approach and it is wrong.** Walk a day at a time
comparing offsets and you will find the permanent shifts — and miss every
disagreement narrower than your step. A fall-back moved by one hour produces a
divergence exactly one hour wide, and a daily sampler walks straight past it. The
bundled `Europe/Chisinau` is precisely this case.

So instead: between two consecutive *breakpoints* of either database, both
offsets are constant **by construction**. Taking the union of both zones'
breakpoints partitions the window into segments on which a single comparison is
exact, and coalescing adjacent equal verdicts turns "seventeen one-hour
disagreements" back into "every winter from 2026-11-01 onwards". Exact, and
linear in the number of transitions rather than in the length of the window.

### Two approaches considered

| | **A. Sample the forward map** | **B. Interval-wise over breakpoints** (chosen) |
|---|---|---|
| Correctness | Misses any divergence narrower than the step | Exact: no divergence can hide between breakpoints |
| CPU | O(window / step) — 4 years at 1h is ~35k probes per zone, ×600 zones | O(transitions in window) — tens per zone |
| Memory | Trivial | Holds both transition tables, a few hundred KB for a full tree |
| Output shape | A cloud of points needing post-hoc grouping | Maximal intervals directly, which is what a human reads |
| Maintenance | Tempting to "fix" by shrinking the step, which never terminates | Requires a correct breakpoint generator, footer included |

B costs one real piece of work — the POSIX footer must be *evaluated*, not just
compared as a string, to generate breakpoints beyond the transition table. That
work is unavoidable anyway: modern `zic` defaults to *slim* output, so a zone
that abolished DST expresses the change almost entirely as a new footer
(`PST8PDT,M3.2.0,M11.1.0` → `MST7`) with no new transition records at all. A tool
that diffed only transition tables would see two nearly identical files and
report nothing while every future answer disagreed.

### Why not use `zoneinfo`?

Three reasons, each disqualifying on its own. It exposes no transition table, so
the interval-wise method is impossible. It reads whichever database the *host*
ships, and the whole point is to compare databases the host is not using — the
tree inside a container image or a Nix store. And a CPython build with no system
tzdata and no `tzdata` wheel has no database at all, which is the machine this was
developed on (`zoneinfo.TZPATH` is empty there).

`zoneinfo` does make an excellent oracle, though. `tests/test_tzif.py`
cross-validates this parser against CPython's entirely independent one over
thousands of instants across four zone shapes, including the footer-governed
region beyond the last transition, and cross-validates the inverse map against
`datetime`. Two implementations of the same RFC agreeing on every sampled instant
is stronger evidence than any hand-written expectation table.

### Severity model

Future offset divergence is CRITICAL because it silently corrupts rows written
*today* for dates that have not happened yet, and nothing inside the database can
detect it. A reading that stops existing (`gap-appears`) is HIGH: Postgres does
not raise, it shifts the value. A reading that becomes ambiguous
(`fold-appears`) is MEDIUM — the conversion picks one of two, so about half are
wrong. Purely historical divergence is LOW: it changes how old instants
*display*, which is visible and fixable. An abbreviation change with identical
offsets is INFO.

---

## Assumptions and limitations

**Stated assumptions.**

- **Left is stale, right is authoritative.** Every correction is computed as
  `right − left` and assumes the stored value was written through the left
  database. Applying the SQL to rows written by some *third* database moves them
  to a third wrong answer. The SQL says so in its own header.
- **Gap and fold policy mirrors Postgres.** For a fold, the earliest candidate
  wins (also Python's `fold=0` default). For a gap, the reading is pushed forward
  by the width of the gap. Other stacks differ — Java's `ZonedDateTime` also
  pushes forward, but `pytz`'s `localize(is_dst=None)` raises. If your writer
  raises or rejects instead, `gap-appears` rows are errors in your logs rather
  than silent corruption, which is a better position to be in.
- **Rows supplied to `blast` are unzoned wall clocks.** A stamp carrying `Z` or a
  numeric offset is rejected rather than accepted, because silently accepting one
  would hide the very bug being hunted.
- **`--now` defaults to today, UTC**, and only partitions findings into future and
  past. Pass it explicitly for reproducible output.

**Limitations.**

- **The bundled sample databases are synthetic approximations, not IANA
  releases.** They were hand-built so the repository stays small, ships no
  third-party binaries, and runs where no tz database exists. Offsets,
  abbreviations and dates follow the real 2026 changes closely enough to
  reproduce the failure modes, and no further. `samples/README.md` documents each
  zone and what it is for. For real answers, point `gnomon` at real trees.
- **Leap-second records are parsed but not used.** TZif carries them; no
  mainstream consumer applies them to civil time, and neither does this.
- **The `right/` and `posix/` subtrees are skipped by default** (`--include-legacy`
  to include them). They duplicate zones found elsewhere and would triple the
  report.
- **Zone *aliasing* changes are not themselves diffed.** If a release re-points
  `Canada/Pacific` at different rules, that shows as a divergence on the zone;
  if it merely re-links two zones whose rules are identical, it is invisible under
  the default collapsing. `--no-collapse-aliases` reports every name separately.
- **`blast` requires zoneinfo trees, not single files**, because it matches rows
  by IANA zone name and a lone file has only a filename. It says so and exits 1.
  `diff` and `resolve` do accept two single files and pair them positionally.
- **No database connection.** By design: `gnomon` reads files and CSV. Extracting
  the rows is one `COPY … TO STDOUT WITH CSV HEADER`, shown above. This keeps the
  tool credential-free and safe to run anywhere.
- **`make` was not available on the development machine**, so the Makefile's
  targets were not executed. Each one is a one-line wrapper around a `python`
  command that *was* run and is printed by `make help`; those python commands are
  the verified path.
- **Only the `Mm.w.d`, `Jn` and `n` POSIX rule forms are supported** — which is
  all of them. Rule times in the extended TZif v3 range (±167 h) are accepted.

---

## Verification

```
python -m pytest tests -q   →  156 passed
python main.py              →  exit 0 (asserts the demo's own exit codes are 2, 0, 2)
```

The suite covers: the POSIX grammar including every rule form, sign inversion,
southern-hemisphere wrap-around and thirteen malformed specs; TZif parsing plus
eight classes of corrupt input (bad magic, unknown version, truncation,
non-ascending transitions, dangling type indices, malformed footers); the
forward map, the breakpoint generator, and the inverse map's gap, fold and
boundary cases both inside and beyond the transition table; interval coalescing
and the one-hour-wide divergence that defeats sampling; all seven row
classifications; CSV parsing including ragged rows, BOMs and zoned stamps; SQL
grouping, quoting and chunking; and every CLI subcommand, output mode and
documented exit code.

Two tests are load-bearing beyond their names. `test_agrees_with_stdlib_zoneinfo`
is the independent-oracle check described above. `test_our_fixtures_are_internally_consistent`
guards the guard: it asserts that no test fixture has a footer contradicting its
own transition table — a mistake made and caught during development, which had
quietly invalidated four assertions before it was found.

---

*Built as Development Experiment 057. Research signals in [RESEARCH.md](RESEARCH.md).*
