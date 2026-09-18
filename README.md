# Navidrome History Analyzer

Navidrome History Analyzer is a standalone Python CLI that reads a Navidrome
SQLite database and generates a human-readable listening report plus CSV
datasets. It uses only the Python standard library and opens the database in
read-only mode.

## Usage

Run the analyzer with the path to an existing `navidrome.db`:

```bash
python3 navidrome_history_report.py /path/to/navidrome.db
```

By default, generated files are written to `navidrome_report/`. Use `-o` to
choose a different output directory:

```bash
python3 navidrome_history_report.py /path/to/navidrome.db -o /path/to/output
```

The selected directory contains `report.txt` and the CSV datasets supported by
the history source detected in the database.

`album_library_coverage.csv` contains one row per current-library album and
user, including albums with no known plays. Track coverage uses any known
listening evidence. For `play_count`, an accumulated `annotation.play_count`
is preferred for a track when present; otherwise temporal scrobbles are used.
The two sources are not added together because Navidrome does not guarantee
that their counters are perfectly equivalent. `last_played` is the latest
known timestamp from either source. `added_at` is the album's library-added
date: it uses `album.created_at` when available and otherwise the oldest known
`media_file.created_at` among the album's current tracks. It is empty when the
database does not provide either timestamp.

`album_metadata_issues.csv` is a library-level diagnostic for album entities
that may have been fragmented or duplicated by inconsistent metadata. It only
flags distinct entities whose normalized album artist and title match. A
one- or two-track album is not suspicious by itself, but is identified as a
possible fragment when a matching entity has more tracks. The export preserves
the original album IDs, track IDs, titles, and paths for manual review; it never
merges albums or modifies Navidrome data.

Use `--help` to see the available filters and analysis thresholds:

```bash
python3 navidrome_history_report.py --help
```

## Development

The analyzer has no third-party runtime dependencies. Development checks use
Ruff and Pyright:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
./check.sh
```

`check.sh` runs the unit and CLI integration tests, Ruff checks and formatting,
Pyright, and a CLI startup smoke test.

The implementation follows a one-way data flow:

```text
SQLite loading (db.py) -> analysis (analysis.py, albums.py)
                       -> CSV exports (exports.py)
                       -> text presentation (report.py)
                       -> orchestration (cli.py)
```

`navidrome_history_report.py` remains the compatible command-line entry point.
