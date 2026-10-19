# Exercise catalog attribution and origin

Exercise names, categories, muscle classifications and media references originate
from Hevy (https://hevy.com/). Hevy retains its rights; no license to media or
endorsement is implied. Only public exercise definitions and URL strings are
included. No images, video, account data, JS bundle or HAR is redistributed.

The bundled snapshot has 451 distinct built-in exercises, captured 2026-09-09.
It was imported offline on 2026-10-17 from the existing
`health-fitness-system/data/hevy_exercise_catalog.json` CatalogStore export
(git commit `0b56fd4`, 2026-09-12). Its metadata identifies
`https://hevy.com/_next/static/chunks/pages/_app-5fa98d71810f0958.js`.
This is an existing export, not a new network capture or independent proof of
current completeness. The original JS/HAR is unavailable; its checksum cannot
be recovered and is explicitly null. The export's SHA-256 and the exact bundled
JSON's SHA-256 are recorded separately in `exercise_catalog.meta.json`.

Acquisition/inspection uses `ExerciseCatalogStore.load()` from the external
`hevy-unofficial==0.0.2a0` distribution (Sahil624/hevy-unofficial,
https://github.com/Sahil624/hevy-unofficial). Its JS/HAR offline parsers are also
supported by `scripts/import_catalog.py`. This project does not vendor the
library. All rows and provenance are independently schema-validated here; the
upstream parser/cache is not trusted as a validation boundary.

See the catalog maintenance section of `docs/OPERATIONS.md` for the reproducible
import command, checksums, evidence limits and acceptance checks. No timed refresh or implicit
catalog download is performed at startup, search, build or test time.
