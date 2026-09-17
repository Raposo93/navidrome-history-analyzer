from __future__ import annotations

import csv
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliIntegrationTests(unittest.TestCase):
    def test_temporal_history_keeps_expected_outputs_and_database_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "navidrome.db"
            output_path = root / "report"
            self._create_temporal_database(database_path)
            database_hash = self._sha256(database_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "navidrome_history_report.py"),
                    str(database_path),
                    "--output",
                    str(output_path),
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._sha256(database_path), database_hash)
            self.assertIn("Historial temporal detectado: scrobbles", result.stdout)

            expected_files = {
                "album_completion.csv",
                "album_eras.csv",
                "album_resume_events.csv",
                "album_resurrections.csv",
                "album_runs.csv",
                "album_thread_completion.csv",
                "album_thread_resurrections.csv",
                "album_threads.csv",
                "album_transitions.csv",
                "artist_depth.csv",
                "artist_eras.csv",
                "context_jumps.csv",
                "discoveries.csv",
                "historical_vs_recent_albums.csv",
                "historical_vs_recent_artists.csv",
                "metadata_issues.csv",
                "monthly.csv",
                "plays.csv",
                "report.txt",
                "resurrections.csv",
                "sessions.csv",
                "top_tracks.csv",
                "track_pairs.csv",
                "weekly_album_dominance.csv",
                "weekly_obsessions.csv",
            }
            self.assertEqual(
                {path.name for path in output_path.iterdir()},
                expected_files,
            )

            self.assertEqual(
                self._csv_header(output_path / "plays.csv"),
                [
                    "datetime",
                    "date",
                    "year",
                    "month",
                    "weekday",
                    "hour",
                    "user",
                    "track_id",
                    "artist",
                    "album_artist",
                    "album",
                    "album_id",
                    "disc_number",
                    "track_number",
                    "title",
                    "genre",
                    "release_year",
                    "duration",
                    "path",
                    "mbid",
                ],
            )
            self.assertEqual(
                self._csv_header(output_path / "album_threads.csv"),
                [
                    "thread_id",
                    "user",
                    "start",
                    "end",
                    "span_hours",
                    "album_key",
                    "album_id",
                    "artist",
                    "album",
                    "release_year",
                    "library_tracks",
                    "scrobbles",
                    "unique_tracks",
                    "coverage_pct",
                    "ordered_coverage_pct",
                    "coverage_24h_pct",
                    "ordered_coverage_24h_pct",
                    "coverage_48h_pct",
                    "ordered_coverage_48h_pct",
                    "first_position",
                    "last_position",
                    "starts_at_beginning",
                    "reaches_end",
                    "resume_count",
                    "max_pause_hours",
                    "complete_like",
                    "full_thread",
                    "meaningful",
                    "status",
                    "metadata_confidence",
                ],
            )

            report = (output_path / "report.txt").read_text(encoding="utf-8")
            self.assertIn("NAVIDROME — RADIOGRAFÍA DE ESCUCHA", report)
            self.assertIn("V4 — HILOS LÓGICOS DE ÁLBUM", report)
            self.assertIn("BIBLIOTECA", report)

    def test_legacy_annotations_keep_expected_output_and_database_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "navidrome.db"
            output_path = root / "report"
            self._create_legacy_database(database_path)
            database_hash = self._sha256(database_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "navidrome_history_report.py"),
                    str(database_path),
                    "--output",
                    str(output_path),
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._sha256(database_path), database_hash)
            self.assertIn("usando play_count de annotation", result.stdout)
            self.assertEqual(
                {path.name for path in output_path.iterdir()},
                {"legacy_playcounts.csv", "report.txt"},
            )
            self.assertEqual(
                self._csv_header(output_path / "legacy_playcounts.csv"),
                [
                    "user",
                    "play_count",
                    "last_played",
                    "track_id",
                    "artist",
                    "album",
                    "title",
                    "genre",
                    "release_year",
                ],
            )
            report = (output_path / "report.txt").read_text(encoding="utf-8")
            self.assertIn("RADIOGRAFÍA DE ESCUCHA (MODO LEGACY)", report)
            self.assertIn("Reproducciones acumuladas según annotation: 7", report)
            self.assertIn("BIBLIOTECA", report)

    @staticmethod
    def _create_temporal_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE media_file (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    album_id TEXT,
                    album_artist TEXT,
                    genre TEXT,
                    year INTEGER,
                    duration REAL,
                    track_number INTEGER,
                    disc_number INTEGER,
                    path TEXT,
                    mbz_recording_id TEXT
                );
                CREATE TABLE user (
                    id TEXT PRIMARY KEY,
                    user_name TEXT
                );
                CREATE TABLE scrobbles (
                    media_file_id TEXT,
                    user_id TEXT,
                    submission_time TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO user (id, user_name) VALUES (?, ?)",
                ("user-1", "Alice"),
            )
            for position in range(1, 4):
                track_id = f"track-{position}"
                connection.execute(
                    """
                    INSERT INTO media_file (
                        id, title, artist, album, album_id, album_artist,
                        genre, year, duration, track_number, disc_number,
                        path, mbz_recording_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id,
                        f"Track {position}",
                        "Artist",
                        "Album",
                        "album-1",
                        "Artist",
                        "Rock",
                        2020,
                        180.0,
                        position,
                        1,
                        f"{position:02d} - Track {position}.flac",
                        "",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO scrobbles (
                        media_file_id, user_id, submission_time
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        track_id,
                        "user-1",
                        f"2026-01-01T12:{position * 5:02d}:00+00:00",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _create_legacy_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE media_file (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    album_id TEXT,
                    album_artist TEXT,
                    genre TEXT,
                    year INTEGER,
                    duration REAL,
                    track_number INTEGER,
                    disc_number INTEGER,
                    path TEXT,
                    mbz_recording_id TEXT
                );
                CREATE TABLE user (
                    id TEXT PRIMARY KEY,
                    user_name TEXT
                );
                CREATE TABLE annotation (
                    user_id TEXT,
                    item_id TEXT,
                    item_type TEXT,
                    play_count INTEGER,
                    play_date TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO user (id, user_name) VALUES (?, ?)",
                ("user-1", "Alice"),
            )
            connection.execute(
                """
                INSERT INTO media_file (
                    id, title, artist, album, album_id, album_artist,
                    genre, year, duration, track_number, disc_number,
                    path, mbz_recording_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "track-1",
                    "Track 1",
                    "Artist",
                    "Album",
                    "album-1",
                    "Artist",
                    "Rock",
                    2020,
                    180.0,
                    1,
                    1,
                    "01 - Track 1.flac",
                    "",
                ),
            )
            connection.execute(
                """
                INSERT INTO annotation (
                    user_id, item_id, item_type, play_count, play_date
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "user-1",
                    "track-1",
                    "media_file",
                    7,
                    "2026-01-01T12:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _csv_header(path: Path) -> list[str]:
        with path.open(encoding="utf-8", newline="") as csv_file:
            return next(csv.reader(csv_file))


if __name__ == "__main__":
    unittest.main()
