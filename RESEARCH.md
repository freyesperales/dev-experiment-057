# Research log — Development Experiment 057

Sweep run 2026-09-29. Signals are grouped by lane; the ones that actually drove
the build are marked **[SEED]**.

## Lane 1 — data, databases, time correctness  *(chosen lane)*

1. **[SEED]** [Footguns with Postgres "at time zone 'UTC'"](https://news.ycombinator.com/item?id=49865312)
   — on the Hacker News front page, 2026-09-28, 166 points. Postgres coerces
   `timestamp` to `timestamptz` implicitly, and whether the session `TimeZone`
   participates is not obvious from the expression. Established that timezone
   handling was live on the front page that day, and pointed at signal 2.

2. **[SEED]** [Postgres is built against tzdata 2025b while the image ships tzdata 2026d — supabase/postgres#2465](https://github.com/supabase/postgres/issues/2465)
   — *the* seed. The image carries two zoneinfo databases: `/usr/share/zoneinfo`
   is Alpine's current `tzdata-2026d`, while the server was built
   `--with-system-tzdata` pointing at a Nix-provided `tzdata-2025b`. Named
   consequences: `America/Vancouver`, `Canada/Pacific`, `America/Edmonton`,
   `Canada/Mountain`, `America/Yellowknife` and `America/Inuvik` off by an hour
   every winter from 2026-11-01; `Africa/Casablanca` and `Africa/El_Aaiun` off
   nearly year-round from 2026-09-20; 26 further zone files differing only
   historically. Why it matters, in the issue's own words: it fails silently, and
   `pg_timezone_names` is a view over the same stale directory, **so nothing
   inside the database can reveal the skew**. That last clause is the entire
   justification for an external tool.

3. **[SEED]** [Canada is about to break your scheduler](https://dev.to/vatsalpatel/canada-is-about-to-break-your-scheduler-2817)
   — the practitioner write-up. Frames 2026-11-01 as "a quiet data integrity bomb
   with a detonation date" and supplies the failure path that became `blast`:
   applications convert local wall clock to an instant server-side with
   `(date + time) AT TIME ZONE tz` — the documented way to schedule from user
   input — and the value is *materialised at write time*, so every downstream
   consumer inherits it: reminders, notification scheduling, iCalendar feeds.

4. **[SEED]** [IANA tz database releases 2026a–2026d](https://data.iana.org/time-zones/tzdb/NEWS)
   / [tz-announce](https://lists.iana.org/hyperkitty/list/tz-announce@iana.org/latest)
   — the primary source for what actually changed. British Columbia to permanent
   −07:00 on 2026-03-09; Alberta to permanent −06:00 on 2026-06-18; the Northwest
   Territories to permanent −06:00, effective 2026-11-01 02:00; Morocco to
   permanent UTC on 2026-09-20 02:00; `Europe/Chisinau` removed from
   `zonenow.tab` as it now agrees with `Europe/Athens` for future timestamps
   (Moldova has used EU transition *times* since 2022 — 03:00/04:00, not
   02:00/03:00). Every sample zone is modelled on one of these.

5. [Adopting permanent daylight saving time — BC Gov News](https://news.gov.bc.ca/releases/2026AG0013-000209)
   and [Daylight saving time in Canada](https://en.wikipedia.org/wiki/Daylight_saving_time_in_Canada)
   — confirms the legislative side and the *relabelling*: Vancouver becomes MST,
   Edmonton becomes CST. Relabelling is why `gnomon` distinguishes an
   abbreviation change from an offset change instead of treating any difference as
   a finding; `America/Whitehorse` in the samples is that control.

6. [Update British Columbia DST Changes — moment/moment-timezone#1141](https://github.com/moment/moment-timezone/issues/1141)
   — the same staleness one layer up the stack, in a JS bundle rather than a
   container. Evidence the problem is not Postgres-specific, which is why the
   engine takes two *trees* and knows nothing about databases.

7. [Recurring all-day iCal holidays stored one day early east of UTC — zammad#6398](https://github.com/zammad/zammad/issues/6398)
   and [recurring events crossing the November 2026 fall-back omitted — open-webui#31605](https://github.com/open-webui/open-webui/issues/31605)
   — independent 2026 bug reports where the *inverse* map is what breaks, and the
   November 2026 transition is the trigger in the second. Pushed the design from
   "diff offsets" to "diff the inverse map, including multiplicity", which is
   where `gap-appears` and `fold-appears` came from.

8. [Time zone management in PostgreSQL — Cybertec](https://www.cybertec-postgresql.com/en/time-zone-management-in-postgresql/)
   — background on `--with-system-tzdata` versus the bundled tree, and the
   `$PGSHAREDIR/timezone` layout. Source for the README's "find the tree your
   server was compiled against" recipe.

## Lane 2 — AI, agents, models

9. [Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5) — HN front page,
   866 points. Noted and deliberately not built on: a model release is not a
   buildable offline prototype.

10. [Jeff — Jev-compatible 0.8B decision models, trained at home, ~30 ms](https://github.com/firelex/jeff)
    (561 points) and [MicroLLM Lab — 7 tiny LLMs in the browser](https://stateofutopia.com/experiments/microllmlab/)
    (272 points), alongside WebLLM 0.2.85 shipping Qwen3.5 0.8B–9B configs and
    Chrome's built-in `LanguageModel` interface stable since Chrome 148. A strong
    lane, **rejected**: every candidate needs model weights and a network fetch,
    so it cannot be a self-contained offline prototype with a passing test suite.

11. [Nvidia wants to put a watchdog chip next to every AI agent](https://www.cnbc.com/2026/09/28/nvidia-releases.html)
    (220 points) — interesting, but the buildable slice is a policy simulator,
    and experiments 032/035/036/040 already cover agent permission and hook
    planes.

## Lane 3 — developer tools and infrastructure

12. [cf: The Agentic CLI for the Cloudflare API](https://blog.cloudflare.com/cloudflare-cf-cli-launch/)
    (164 points) — needs an account and credentials. Rejected on the
    no-external-API constraint.

13. [There is more to code review than (automatable) detection](https://www.adaptivecapacitylabs.com/2026/08/24/there-is-more-to-code-review-than-automatable-detection/)
    (162 points) and [Coding is not solved](https://blog.alexewerlof.com/p/coding-is-not-solved)
    (542 points) — read as an argument against shipping yet another linter, which
    reinforced the decision to make this entry emit a *repair* (corrective SQL)
    rather than only a verdict.

14. [Barriers to Using Static Application Security Testing Tools: a literature review (arXiv 2609.01669)](https://arxiv.org/pdf/2609.01669)
    — false-positive fatigue is the dominant adoption barrier for static tools.
    Directly shaped the severity model: an abbreviation-only change is INFO and a
    historical-only change is LOW, so the CRITICAL bucket stays small enough to
    act on, and `--fail-on` defaults to `critical`.

## Lane 4 — emerging tech

15. [OpenArm](https://github.com/topics/urdf), RoboTwin 2.0 (ICML 2026), and the
    ESP32-P4 robotic-arm work on Espressif's developer blog — a genuinely open
    lane. **Rejected on novelty against utility**: an offline URDF
    forward-kinematics and reachability checker is buildable pure-stdlib, but
    Pinocchio and MoveIt already do it well, and the 2026-11-01 deadline in lane 1
    was both more novel and more urgent.

16. [World Labs is joining AMD](https://www.worldlabs.ai/blog/amd-announcement)
    (301 points) — consolidation news, no buildable slice.

## Lane 5 — compliance and business

17. [EU Data Act, 12 September 2026](https://www.wsgr.com/en/insights/eu-data-act-september-2026-deadline-what-businesses-need-to-know.html)
    and the [CRA reporting obligations of 11 September 2026](https://www.veracode.com/blog/eu-cyber-resilience-act-2026-compliance/)
    — both live, both fresh. **Rejected as a repeat**: experiment 052 (`tocsin`)
    already ships a multi-regime incident-notification deadline engine covering
    CRA, NIS2, GDPR and DORA.

---

## Candidates scored

Three candidates from three different lanes, scored on novelty, real-world
utility, and buildability as a working prototype in one unattended session.

| Candidate | Lane | Novelty | Utility | Buildable | Verdict |
|---|---|---|---|---|---|
| **tz-database divergence + blast radius** | data / time correctness | High — no tool does semantic TZif *tree* diffing, and none does the inverse-map multiplicity diff | High and **time-boxed**: 33 days to 2026-11-01; Morocco already live | High — pure stdlib; TZif is a documented binary format; `zoneinfo` is an independent oracle | **Chosen** |
| In-browser tiny-LLM router | AI / on-device | Medium | Medium | **No** — needs weights and network | Rejected |
| Offline URDF reachability checker | robotics | Medium — Pinocchio/MoveIt cover it | Niche | High | Rejected |

**Novelty check against the full ledger.** No prior entry among 001–056 touches
timezones, TZif, tzdata, calendars, or database reference data; the nearest
neighbours are 051 (`headway`, GTFS — transit schedules, not zone rules) and 052
(`tocsin`, legal deadlines — durations, not offsets). The recent ten-entry streak
is *domain engines over one file format*; this entry's unit of analysis is a
**pair** of reference databases and the divergence between them, and it is the
first in the series to emit a remediation artefact alongside the analysis.

## Specification sources

- [RFC 8536 — The Time Zone Information Format (TZif)](https://datatracker.ietf.org/doc/html/rfc8536)
  — the wire format: header counts, the 32-bit and 64-bit data blocks, the
  footer, and §3.3.1 for the extended POSIX rule-time range of ±167 hours.
- [POSIX.1 TZ environment variable](https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap08.html)
  — the footer grammar and, critically, the sign convention: a POSIX offset is
  added to local time to obtain UTC, so it is the negation of the conventional
  UTC offset.
- [PostgreSQL: Date/Time Types](https://www.postgresql.org/docs/current/datatype-datetime.html)
  and [POSIX Time Zone Specifications](https://www.postgresql.org/docs/current/datetime-posix-timezone-specs.html)
  — confirms `AT TIME ZONE` semantics and that Postgres shifts rather than raises
  on a nonexistent local time, which is the behaviour `resolve_wall` reproduces.
