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

Use `--help` to see the available filters and analysis thresholds:

```bash
python3 navidrome_history_report.py --help
```
