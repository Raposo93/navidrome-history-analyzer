from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from navidrome_history_report import (
    Play,
    Track,
    aggregate_album_completion,
    aggregate_album_threads,
    album_catalog,
    album_key,
    build_album_runs,
    build_album_threads,
)


BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_track(
    track_id: str,
    position: int,
    *,
    album_id: str = "album-a",
    album: str = "Album A",
    artist: str = "Artist A",
    album_artist: str = "Artist A",
    disc_number: int = 1,
    year: int | None = 2020,
) -> Track:
    return Track(
        id=track_id,
        title=f"Track {position}",
        artist=artist,
        album=album,
        album_id=album_id,
        album_artist=album_artist,
        genre="Rock",
        year=year,
        duration=180.0,
        track_number=position,
        disc_number=disc_number,
        path=f"{position:02d} - Track {position}.flac",
        mbid="",
    )


def make_album(
    count: int = 5,
    *,
    prefix: str = "a",
    album_id: str = "album-a",
    album: str = "Album A",
    artist: str = "Artist A",
) -> dict[str, Track]:
    return {
        f"{prefix}{position}": make_track(
            f"{prefix}{position}",
            position,
            album_id=album_id,
            album=album,
            artist=artist,
            album_artist=artist,
        )
        for position in range(1, count + 1)
    }


def play(
    track: Track,
    minutes: float,
    *,
    user_id: str = "user-1",
    user_name: str = "Alice",
) -> Play:
    return Play(
        at=BASE_TIME + timedelta(minutes=minutes),
        track_id=track.id,
        user_id=user_id,
        user_name=user_name,
        track=track,
    )


class AlbumCatalogTests(unittest.TestCase):
    def test_groups_by_stable_identity_and_orders_by_disc_and_track(self) -> None:
        first_edition = {
            "a3": make_track("a3", 1, disc_number=2),
            "a2": make_track("a2", 2),
            "a1": make_track("a1", 1),
        }
        second_edition = {
            "b1": make_track("b1", 1, album_id="album-b"),
        }

        catalog = album_catalog({**first_edition, **second_edition})

        self.assertEqual(set(catalog), {"album-a", "album-b"})
        self.assertEqual(
            [track.id for track in catalog["album-a"]["tracks"]],
            ["a1", "a2", "a3"],
        )
        self.assertEqual(
            catalog["album-a"]["positions"],
            {"a1": 1, "a2": 2, "a3": 3},
        )
        self.assertEqual(catalog["album-a"]["track_count"], 3)
        self.assertEqual(catalog["album-a"]["duration"], 540.0)

    def test_falls_back_to_album_artist_and_album_name(self) -> None:
        track_1 = make_track(
            "fallback-1",
            1,
            album_id="",
            album="Shared Album",
            artist="Guest One",
            album_artist="Various Artists",
        )
        track_2 = make_track(
            "fallback-2",
            2,
            album_id="",
            album="shared album",
            artist="Guest Two",
            album_artist="VARIOUS ARTISTS",
        )

        catalog = album_catalog({track_1.id: track_1, track_2.id: track_2})

        self.assertEqual(album_key(track_1), "various artists\x1fshared album")
        self.assertEqual(album_key(track_1), album_key(track_2))
        self.assertEqual(len(catalog), 1)
        self.assertEqual(next(iter(catalog.values()))["track_count"], 2)


class AlbumRunTests(unittest.TestCase):
    def test_repeated_tracks_do_not_inflate_coverage_and_long_pause_splits_run(self) -> None:
        tracks = make_album()
        plays = [
            play(tracks["a1"], 0),
            play(tracks["a2"], 5),
            play(tracks["a2"], 6),
            play(tracks["a3"], 10),
            play(tracks["a4"], 60),
        ]

        runs = build_album_runs(plays, tracks, gap_minutes=30, complete_threshold=0.8)

        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["scrobbles"], 4)
        self.assertEqual(runs[0]["unique_tracks"], 3)
        self.assertEqual(runs[0]["completion_pct"], 60.0)
        self.assertEqual(runs[0]["sequential_pct"], 100.0)
        self.assertEqual(runs[0]["status"], "parcial_desde_inicio")
        self.assertEqual(runs[1]["status"], "pista_suelta")

    def test_restart_near_beginning_starts_a_new_run(self) -> None:
        tracks = make_album()
        plays = [
            play(tracks["a1"], 0),
            play(tracks["a2"], 5),
            play(tracks["a4"], 10),
            play(tracks["a1"], 15),
            play(tracks["a2"], 20),
        ]

        runs = build_album_runs(plays, tracks, gap_minutes=30, complete_threshold=0.8)

        self.assertEqual(len(runs), 2)
        self.assertEqual([run["scrobbles"] for run in runs], [3, 2])
        self.assertEqual([run["first_position"] for run in runs], [1, 1])
        self.assertEqual([run["last_position"] for run in runs], [4, 2])


class AlbumThreadTests(unittest.TestCase):
    def test_reconstructs_resume_across_interleaved_album(self) -> None:
        tracks = make_album()
        other = make_album(
            count=1,
            prefix="b",
            album_id="album-b",
            album="Album B",
            artist="Artist B",
        )
        all_tracks = {**tracks, **other}
        plays = [
            play(tracks["a1"], 0),
            play(other["b1"], 30),
            play(tracks["a2"], 120),
            play(tracks["a3"], 125),
            play(tracks["a4"], 60 * 60),
        ]

        threads, resumes = build_album_threads(
            plays,
            all_tracks,
            session_gap_minutes=30,
            thread_hours=48,
            resume_slack=2,
            complete_threshold=0.8,
        )

        album_a_threads = [row for row in threads if row["album_id"] == "album-a"]
        self.assertEqual(len(album_a_threads), 2)
        self.assertEqual(album_a_threads[0]["unique_tracks"], 3)
        self.assertEqual(album_a_threads[0]["coverage_pct"], 60.0)
        self.assertEqual(album_a_threads[0]["resume_count"], 1)
        self.assertEqual(album_a_threads[0]["max_pause_hours"], 2.0)
        self.assertEqual(album_a_threads[1]["status"], "pista_suelta")

        album_a_resumes = [row for row in resumes if row["album"] == "Album A"]
        self.assertEqual(len(album_a_resumes), 1)
        self.assertEqual(album_a_resumes[0]["from_position"], 1)
        self.assertEqual(album_a_resumes[0]["to_position"], 2)
        self.assertEqual(album_a_resumes[0]["interleaved_scrobbles"], 1)

    def test_restart_after_reaching_later_track_starts_new_thread(self) -> None:
        tracks = make_album()
        plays = [
            play(tracks["a1"], 0),
            play(tracks["a2"], 5),
            play(tracks["a3"], 10),
            play(tracks["a4"], 15),
            play(tracks["a1"], 20),
        ]

        threads, _ = build_album_threads(
            plays,
            tracks,
            session_gap_minutes=30,
            thread_hours=48,
            resume_slack=2,
            complete_threshold=0.8,
        )

        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]["coverage_pct"], 80.0)
        self.assertEqual(threads[0]["full_thread"], 1)
        self.assertEqual(threads[1]["first_position"], 1)


class AlbumCompletionTests(unittest.TestCase):
    def test_aggregates_only_meaningful_runs(self) -> None:
        tracks = make_album()
        plays = [
            *(play(tracks[f"a{position}"], position * 5) for position in range(1, 6)),
            *(play(tracks[f"a{position}"], 60 + position * 5) for position in range(1, 4)),
            play(tracks["a4"], 120),
        ]
        runs = build_album_runs(plays, tracks, gap_minutes=30, complete_threshold=0.8)

        completion = aggregate_album_completion(runs)

        self.assertEqual(len(runs), 3)
        self.assertEqual(len(completion), 1)
        row = completion[0]
        self.assertEqual(row["album_runs"], 2)
        self.assertEqual(row["probable_full_plays"], 1)
        self.assertEqual(row["near_complete_runs"], 1)
        self.assertEqual(row["started_from_beginning"], 2)
        self.assertEqual(row["probable_abandons"], 1)
        self.assertEqual(row["avg_completion_pct"], 80.0)
        self.assertEqual(row["median_completion_pct"], 80.0)
        self.assertEqual(row["favorite_stop_track"], "Track 3")
        self.assertEqual(row["full_play_rate_pct"], 50.0)

    def test_thread_aggregation_keeps_users_separate(self) -> None:
        base = {
            "album_key": "album-a",
            "album_id": "album-a",
            "artist": "Artist A",
            "album": "Album A",
            "release_year": 2020,
            "library_tracks": 5,
            "meaningful": 1,
            "full_thread": 0,
            "resume_count": 0,
            "starts_at_beginning": 1,
            "complete_like": 0,
            "coverage_pct": 60.0,
            "ordered_coverage_pct": 60.0,
            "coverage_24h_pct": 60.0,
            "coverage_48h_pct": 60.0,
        }
        threads = [
            {**base, "user": "Alice"},
            {
                **base,
                "user": "Bob",
                "full_thread": 1,
                "complete_like": 1,
                "coverage_pct": 100.0,
                "ordered_coverage_pct": 100.0,
                "coverage_24h_pct": 100.0,
                "coverage_48h_pct": 100.0,
            },
        ]

        rows = aggregate_album_threads(threads)

        self.assertEqual({row["user"] for row in rows}, {"Alice", "Bob"})
        by_user = {row["user"]: row for row in rows}
        self.assertEqual(by_user["Alice"]["probable_abandons_after_window"], 1)
        self.assertEqual(by_user["Bob"]["probable_full_plays"], 1)


if __name__ == "__main__":
    unittest.main()
