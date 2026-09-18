from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from albums import (
    aggregate_album_completion,
    aggregate_album_threads,
    album_catalog,
    album_key,
    album_library_coverage,
    build_album_runs,
    build_album_threads,
    suspicious_album_entities,
)
from models import AnnotationPlay, Play, Track

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
    added_at: datetime | None = None,
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
        added_at=added_at,
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


class AlbumLibraryCoverageTests(unittest.TestCase):
    def test_added_at_distinguishes_old_new_and_unknown_unplayed_albums(self) -> None:
        old_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracks = {
            "old": make_track("old", 1, album_id="old", album="Old", added_at=old_date),
            "old-later": make_track(
                "old-later",
                2,
                album_id="old",
                album="Old",
                added_at=old_date + timedelta(days=30),
            ),
            "new": make_track("new", 1, album_id="new", album="New", added_at=new_date),
            "unknown": make_track("unknown", 1, album_id="unknown", album="Unknown"),
        }

        rows = album_library_coverage(
            tracks=tracks,
            plays=[],
            annotation_history={},
            users={"user-1": "Alice"},
            selected_users=None,
        )

        by_album = {row["album_id"]: row for row in rows}
        self.assertEqual(by_album["old"]["added_at"], old_date.isoformat(sep=" "))
        self.assertEqual(by_album["new"]["added_at"], new_date.isoformat(sep=" "))
        self.assertEqual(by_album["unknown"]["added_at"], "")
        self.assertTrue(all(row["tracks_heard"] == 0 for row in rows))

    def test_combines_known_evidence_without_merging_users_or_editions(self) -> None:
        first_edition = make_album(count=3)
        second_edition = make_album(count=2, prefix="b", album_id="album-b")
        tracks = {**first_edition, **second_edition}
        plays = [
            play(first_edition["a1"], 0),
            play(first_edition["a2"], 10),
            play(first_edition["a2"], 20),
        ]
        annotations = {
            ("user-1", "a1"): AnnotationPlay(
                play_count=5,
                last_played=BASE_TIME + timedelta(minutes=5),
            ),
            ("user-2", "b1"): AnnotationPlay(
                play_count=4,
                last_played=BASE_TIME + timedelta(minutes=15),
            ),
        }

        rows = album_library_coverage(
            tracks=tracks,
            plays=plays,
            annotation_history=annotations,
            users={"user-1": "Alice", "user-2": "Bob"},
            selected_users=None,
        )

        self.assertEqual(len(rows), 4)
        by_user_and_edition = {(row["user"], row["album_id"]): row for row in rows}
        alice_first = by_user_and_edition[("Alice", "album-a")]
        self.assertEqual(alice_first["total_tracks"], 3)
        self.assertEqual(alice_first["tracks_heard"], 2)
        self.assertEqual(alice_first["tracks_unheard"], 1)
        self.assertEqual(alice_first["heard_pct"], 66.7)
        self.assertEqual(alice_first["play_count"], 7)
        self.assertEqual(
            alice_first["last_played"],
            (BASE_TIME + timedelta(minutes=20)).isoformat(sep=" "),
        )

        self.assertEqual(
            by_user_and_edition[("Alice", "album-b")]["tracks_heard"],
            0,
        )
        self.assertEqual(
            by_user_and_edition[("Bob", "album-a")]["tracks_heard"],
            0,
        )
        bob_second = by_user_and_edition[("Bob", "album-b")]
        self.assertEqual(bob_second["tracks_heard"], 1)
        self.assertEqual(bob_second["play_count"], 4)

    def test_selected_user_limits_rows_but_keeps_unheard_albums(self) -> None:
        tracks = {
            **make_album(count=1),
            **make_album(count=1, prefix="b", album_id="album-b", album="Album B"),
        }

        rows = album_library_coverage(
            tracks=tracks,
            plays=[],
            annotation_history={},
            users={"user-1": "Alice", "user-2": "Bob"},
            selected_users={"user-2"},
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["user"] for row in rows}, {"Bob"})
        self.assertTrue(all(row["tracks_heard"] == 0 for row in rows))
        self.assertTrue(all(row["heard_pct"] == 0.0 for row in rows))


class SuspiciousAlbumEntityTests(unittest.TestCase):
    def test_flags_duplicate_entities_and_identifies_small_fragment(self) -> None:
        complete = make_album(count=5)
        fragment = make_album(count=1, prefix="b", album_id="album-b")

        rows = suspicious_album_entities({**complete, **fragment})

        self.assertEqual(len(rows), 2)
        by_id = {row["album_id"]: row for row in rows}
        self.assertEqual(by_id["album-a"]["related_album_ids"], "album-b")
        self.assertEqual(by_id["album-b"]["track_ids"], "b1")
        self.assertIn("small_fragment_of_larger_entity", by_id["album-b"]["signals"])
        self.assertNotIn("small_fragment_of_larger_entity", by_id["album-a"]["signals"])

    def test_flags_format_only_album_artist_variation(self) -> None:
        first = make_album(count=3)
        second = {
            "b1": make_track(
                "b1",
                1,
                album_id="album-b",
                album_artist="Artist-A",
            )
        }

        rows = suspicious_album_entities({**first, **second})

        self.assertEqual(len(rows), 2)
        self.assertTrue(
            all("album_artist_format_variation" in row["signals"] for row in rows)
        )

    def test_does_not_flag_legitimate_short_or_same_title_other_artist(self) -> None:
        short_album = make_album(count=2)
        other_artist = make_album(
            count=4,
            prefix="b",
            album_id="album-b",
            artist="Artist B",
        )

        rows = suspicious_album_entities({**short_album, **other_artist})

        self.assertEqual(rows, [])


class AlbumRunTests(unittest.TestCase):
    def test_repeated_tracks_do_not_inflate_coverage_and_long_pause_splits_run(
        self,
    ) -> None:
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
            *(
                play(tracks[f"a{position}"], 60 + position * 5)
                for position in range(1, 4)
            ),
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
