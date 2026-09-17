from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db import Schema, load_tracks
from utils import parse_dt


class TrackMetadataTests(unittest.TestCase):
    def test_album_created_at_precedes_media_fallback_and_missing_stays_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "navidrome.db"
            connection = sqlite3.connect(database_path)
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript(
                    """
                    CREATE TABLE album (
                        id TEXT PRIMARY KEY,
                        created_at TEXT
                    );
                    CREATE TABLE media_file (
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        album TEXT,
                        album_id TEXT,
                        created_at TEXT
                    );
                    INSERT INTO album VALUES
                        ('album-primary', '2020-01-01T00:00:00+00:00'),
                        ('album-fallback', NULL),
                        ('album-missing', NULL);
                    INSERT INTO media_file VALUES
                        ('primary', 'Primary', 'Artist', 'Primary',
                         'album-primary', '2024-01-01T00:00:00+00:00'),
                        ('fallback', 'Fallback', 'Artist', 'Fallback',
                         'album-fallback', '2025-01-01T00:00:00+00:00'),
                        ('missing', 'Missing', 'Artist', 'Missing',
                         'album-missing', NULL);
                    """
                )

                tracks = load_tracks(connection, Schema(connection))
            finally:
                connection.close()

        self.assertEqual(
            tracks["primary"].added_at, parse_dt("2020-01-01T00:00:00+00:00")
        )
        self.assertEqual(
            tracks["fallback"].added_at, parse_dt("2025-01-01T00:00:00+00:00")
        )
        self.assertIsNone(tracks["missing"].added_at)


if __name__ == "__main__":
    unittest.main()
