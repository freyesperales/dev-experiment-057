# Sample databases

**These are synthetic approximations, not IANA releases.** They are hand-built by
`build_samples.py` so that this repository stays small, ships no third-party
binaries, and runs on a machine with no timezone database at all. Offsets,
abbreviations and transition dates follow the real 2026 changes closely enough to
reproduce the failure modes being demonstrated, and no further. For real answers,
point `gnomon` at real trees.

Regenerate them with:

```bash
python build_samples.py      # from this directory
python samples/build_samples.py   # from the repository root
```

## The two trees

| | `trees/tzdata-2025b` | `trees/tzdata-2026d` |
|---|---|---|
| `+VERSION` | `2025b` | `2026d` |
| Role | the stale database your server was compiled against | current, authoritative |

## The zones, and what each one is for

| Zone | Change modelled | Exercises |
|---|---|---|
| `America/Vancouver` | British Columbia settles on permanent −07:00, relabelled MST | The headline case: an open-ended `future-divergence` driven almost entirely by a footer change (`PST8PDT,M3.2.0,M11.1.0` → `MST7`), invisible to a transition-table diff |
| `Canada/Pacific` | a link to Vancouver, identical rules | Alias collapsing: reported once, under the IANA area name, with the link named |
| `America/Edmonton` | Alberta settles on permanent −06:00 (CST) | Offsets agree all summer and split on 2026-11-01, so the report must separate a label-only interval from an offset interval |
| `Africa/Casablanca` | Morocco settles on permanent UTC on 2026-09-20; its Ramadan window also moves by a week between vintages | A divergence that has **already started**; negative DST; and readings that exist in one database but fall in a gap in the other (`gap-appears`, `gap-resolves`, `fold-appears`) |
| `Europe/Chisinau` | one historical fall-back hour differs (03:00 vs the EU's 04:00, pre-2023) | `historical-only`: a divergence exactly **one hour wide**, which a daily sampler would miss entirely. Also the `fold-appears` case in the past |
| `America/Whitehorse` | same fixed −07:00, relabelled from `-07` to `MST` | `abbrev-only`: byte-different, behaviourally identical. The control against false positives |
| `Europe/Madrid` | nothing | `identical`. The clean control |
| `America/Coyhaique` | present only in the newer tree | `only-in-right` / `zone-missing` |

Every generated file is internally consistent — its POSIX footer agrees with its
own final transition — because that is what real `zic` output looks like, and
`gnomon` reports the inconsistent case as a defect rather than treating it as
ordinary input. `tests/test_diverge.py::test_our_fixtures_are_internally_consistent`
enforces this.

## `scheduled.csv`

Fourteen wall-clock readings chosen so that all seven row classifications appear
at least once. Columns are `id`, `zone`, `local_time`, `note`; the `note` column is
ignored by `gnomon` and is there for the reader.

The readings are deliberately unzoned. A stamp carrying `Z` or a numeric offset is
rejected by `gnomon blast`, because accepting one would hide the very bug being
looked for.
