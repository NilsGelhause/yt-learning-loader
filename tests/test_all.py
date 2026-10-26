"""Comprehensive unittest suite for the learning webapp (scraper.py + server.py).

Pure stdlib: unittest, unittest.mock, tempfile, http.client. Every test
operates on temp directories only — the project's real data/ directory and
nils-profile.md are never touched because all module-level path constants are
patched with unittest.mock.patch around every filesystem access.

Run via tests/run.py (which also writes data/test_results.json).
"""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402
import server  # noqa: E402
from tests.run import Collector, module_group  # noqa: E402


def fake_http_response(payload: bytes) -> mock.MagicMock:
    """Build a MagicMock that works as a urlopen() context-manager response."""
    resp = mock.MagicMock(name="http_response")
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def make_index(entries: dict) -> dict:
    return {"videos": entries}


# ---------------------------------------------------------------- scraper

class ScraperPureTests(unittest.TestCase):
    """Pure-function tests for scraper.py — no network, temp dirs only."""

    def test_extract_video_id_watch_url_with_params(self):
        """A watch URL with extra params still yields the 11-char video id."""
        vid = scraper.extract_video_id("https://www.youtube.com/watch?v=abc123def45&t=42s&list=xyz")
        self.assertEqual(vid, "abc123def45")

    def test_extract_video_id_youtu_be(self):
        """A youtu.be short link with tracking params yields the video id."""
        vid = scraper.extract_video_id("https://youtu.be/abc123def45?si=tracking")
        self.assertEqual(vid, "abc123def45")

    def test_extract_video_id_shorts(self):
        """A /shorts/ URL yields the video id."""
        vid = scraper.extract_video_id("https://www.youtube.com/shorts/abc123def45")
        self.assertEqual(vid, "abc123def45")

    def test_extract_video_id_bare(self):
        """A bare 11-char id is accepted as-is."""
        vid = scraper.extract_video_id("abc123def45")
        self.assertEqual(vid, "abc123def45")

    def test_extract_video_id_garbage_is_none(self):
        """Garbage input returns None instead of raising."""
        self.assertIsNone(scraper.extract_video_id("https://vimeo.com/12345"))
        self.assertIsNone(scraper.extract_video_id("hello world"))
        self.assertIsNone(scraper.extract_video_id(""))
        self.assertIsNone(scraper.extract_video_id(None))

    def test_pick_english_track_manual_preferred_over_asr(self):
        """A manual English track beats an English ASR track."""
        tracks = [
            {"languageCode": "en", "kind": "asr", "name": "auto"},
            {"languageCode": "en", "name": "manual"},
            {"languageCode": "de"},
        ]
        self.assertEqual(scraper.pick_english_track(tracks)["name"], "manual")

    def test_pick_english_track_asr_over_other_languages(self):
        """English ASR is preferred over non-English tracks."""
        tracks = [{"languageCode": "de"}, {"languageCode": "en", "kind": "asr"}]
        self.assertEqual(scraper.pick_english_track(tracks)["languageCode"], "en")

    def test_pick_english_track_first_when_no_english(self):
        """With no English track at all the first track is used."""
        tracks = [{"languageCode": "de"}, {"languageCode": "fr"}]
        self.assertEqual(scraper.pick_english_track(tracks)["languageCode"], "de")

    def test_pick_english_track_empty_is_none(self):
        """An empty track list returns None."""
        self.assertIsNone(scraper.pick_english_track([]))

    def test_parse_transcript_srv3_xml(self):
        """A srv3 XML payload parses into start/text segments in seconds."""
        raw = (
            '<timedtext format="3"><body>'
            '<p t="1200">Hello</p>'
            '<p t="2500">World</p>'
            "</body></timedtext>"
        )
        segs = scraper.parse_transcript_payload(raw)
        self.assertEqual(segs, [{"start": 1.2, "text": "Hello"},
                                {"start": 2.5, "text": "World"}])

    def test_parse_transcript_json3(self):
        """A json3 payload parses and joins per-segment utf8 chunks."""
        raw = json.dumps({
            "events": [
                {"tStartMs": 1200, "segs": [{"utf8": "Hi"}, {"utf8": " there"}]},
                {"tStartMs": 2400, "segs": [{"utf8": "Next"}]},
                {"tStartMs": None, "segs": [{"utf8": "skipped"}]},
            ]
        })
        segs = scraper.parse_transcript_payload(raw)
        self.assertEqual(segs, [{"start": 1.2, "text": "Hi there"},
                                {"start": 2.4, "text": "Next"}])

    def test_parse_transcript_empty_is_none(self):
        """An empty or whitespace-only payload returns None."""
        self.assertIsNone(scraper.parse_transcript_payload(""))
        self.assertIsNone(scraper.parse_transcript_payload("   \n  "))

    def test_parse_transcript_dedupes_collapses_and_drops_empty(self):
        """Adjacent duplicate lines are deduped, whitespace collapsed, and
        text-less entries dropped."""
        raw = (
            '<timedtext><body>'
            '<p t="1000">Hello   world</p>'
            '<p t="2000">Hello world</p>'
            '<p t="3000"></p>'
            '<p start="4500">Bye</p>'
            "</body></timedtext>"
        )
        segs = scraper.parse_transcript_payload(raw)
        self.assertEqual(segs, [{"start": 1.0, "text": "Hello world"},
                                {"start": 4.5, "text": "Bye"}])


class ScraperListChannelTests(unittest.TestCase):
    """list_channel with a fake yt-dlp process — no subprocess, no network."""

    def _fake_proc(self, count: int, returncode: int = 0) -> subprocess.CompletedProcess:
        lines = "\n".join(json.dumps({
            "id": f"vid{i:02d}abcde",
            "title": f"Video {i}",
            "channel": "Chan",
            "upload_date": f"2024010{i % 10}",
        }) for i in range(1, count + 1))
        return subprocess.CompletedProcess(["yt-dlp"], returncode, stdout=lines, stderr="boom")

    def test_list_channel_caps_results_and_maps_fields(self):
        """A channel add with limit N never lists more than N videos."""
        with mock.patch.object(scraper, "ytdlp", return_value=self._fake_proc(5)):
            entries = scraper.list_channel("https://www.youtube.com/@chan/videos", 3)
        self.assertEqual(len(entries), 3)
        for e in entries:
            self.assertEqual(set(e), {"id", "title", "url", "channel", "published"})
            self.assertTrue(e["id"])
            self.assertTrue(e["url"].startswith("https://www.youtube.com/watch?v="))
            self.assertTrue(e["published"])

    def test_list_channel_failure_raises_system_exit(self):
        """A non-zero yt-dlp exit raises SystemExit."""
        with mock.patch.object(scraper, "ytdlp", return_value=self._fake_proc(1, returncode=1)):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    scraper.list_channel("https://www.youtube.com/@chan/videos", 3)


class ScraperIngestTests(unittest.TestCase):
    """ingest / backfill_index against a temp data layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.info_dir = root / "info"
        self.info_dir.mkdir()
        self.videos_json = root / "videos.json"
        self.patcher = mock.patch.multiple(
            scraper, VIDEOS_JSON=self.videos_json, INFO_DIR=self.info_dir)

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _entries(self, *ids):
        return [{"id": v, "title": f"T {v}", "channel": "C", "published": "20240101"}
                for v in ids]

    def test_ingest_adds_defaults_and_need_lists_only_missing_info(self):
        """New entries get default flags, and only ids without an info file
        are returned as needing enrichment."""
        (self.info_dir / "aaa.info.json").write_text("{}", encoding="utf-8")
        self.patcher.start()
        index = make_index({})
        need = scraper.ingest(self._entries("aaa", "bbb"), index)
        self.assertEqual(need, ["bbb"])
        saved = json.loads(self.videos_json.read_text(encoding="utf-8"))
        v = saved["videos"]["bbb"]
        self.assertFalse(v["learned"])
        self.assertEqual(v["added"], time.strftime("%Y-%m-%d"))
        self.assertEqual(v["url"], "https://www.youtube.com/watch?v=bbb")

    def test_ingest_preserves_learned_flag_on_reingest(self):
        """Re-ingesting an existing video keeps its learned flag."""
        self.patcher.start()
        index = make_index({})
        scraper.ingest(self._entries("aaa"), index)
        index = scraper.load_index()
        index["videos"]["aaa"]["learned"] = True
        scraper.save_index(index)
        need = scraper.ingest(self._entries("aaa"), scraper.load_index())
        self.assertEqual(need, ["aaa"])  # no info file written -> still needed
        self.assertTrue(scraper.load_index()["videos"]["aaa"]["learned"])

    def test_backfill_index_pulls_metadata_from_info_file(self):
        """Metadata from a video's info file lands in the index, with the
        dash-format publish date converted to YYYYMMDD."""
        self.patcher.start()
        info = {
            "videoDetails": {
                "lengthSeconds": "123",
                "shortDescription": "A description",
                "author": "Chan Name",
            },
            "microformat": {"playerMicroformatRenderer": {"publishDate": "2023-01-15"}},
        }
        (self.info_dir / "aaa.info.json").write_text(json.dumps(info), encoding="utf-8")
        index = make_index({"aaa": {"id": "aaa"}, "bbb": {"id": "bbb", "published": "keep"}})
        scraper.backfill_index(index)
        a = index["videos"]["aaa"]
        self.assertEqual(a["duration"], 123)
        self.assertEqual(a["published"], "20230115")
        self.assertEqual(a["description"], "A description")
        self.assertEqual(a["channel"], "Chan Name")
        self.assertEqual(index["videos"]["bbb"], {"id": "bbb", "published": "keep"})

    def test_backfill_index_without_info_file_is_untouched(self):
        """A video with no info file keeps its index entry unchanged."""
        self.patcher.start()
        index = make_index({"bbb": {"id": "bbb", "published": "keep"}})
        scraper.backfill_index(index)
        self.assertEqual(index["videos"]["bbb"], {"id": "bbb", "published": "keep"})


class ScraperFetchTests(unittest.TestCase):
    """fetch_transcript / fetch_thumbnail with a fake urlopen — no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.thumb_dir = Path(self.tmp.name) / "thumbs"

    def tearDown(self):
        self.tmp.cleanup()

    def _player(self, with_captions=True):
        if not with_captions:
            return {}
        return {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
            {"baseUrl": "https://x.test/track", "languageCode": "en", "kind": "asr"},
        ]}}}

    def test_fetch_transcript_requests_srv3_and_parses(self):
        """The english track URL is requested with &fmt=srv3 and the payload
        is parsed into segments."""
        payload = b'<timedtext><body><p t="1200">Hi</p></body></timedtext>'
        resp = fake_http_response(payload)
        with mock.patch.object(scraper.urllib.request, "urlopen", return_value=resp) as m:
            segs = scraper.fetch_transcript(None, self._player())
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.startswith("https://x.test/track&fmt=srv3"))
        self.assertEqual(segs, [{"start": 1.2, "text": "Hi"}])

    def test_fetch_transcript_no_captions_is_none(self):
        """A player response without captions returns None without fetching."""
        with mock.patch.object(scraper.urllib.request, "urlopen") as m:
            self.assertIsNone(scraper.fetch_transcript(None, self._player(False)))
        m.assert_not_called()

    def test_fetch_thumbnail_success_writes_file(self):
        """A large image payload is written to the thumbnail directory."""
        blob = b"\xff\xd8" + b"x" * 3000
        with mock.patch.object(scraper.urllib.request, "urlopen",
                               return_value=fake_http_response(blob)) as m:
            with mock.patch.object(scraper, "THUMB_DIR", self.thumb_dir):
                self.assertTrue(scraper.fetch_thumbnail("vid1"))
        dest = self.thumb_dir / "vid1.jpg"
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), blob)
        self.assertEqual(m.call_count, 1)

    def test_fetch_thumbnail_skips_placeholder_and_tries_next_quality(self):
        """A grey placeholder (tiny payload) falls through to the next
        thumbnail quality."""
        big = fake_http_response(b"x" * 3000)
        tiny = fake_http_response(b"grey")
        with mock.patch.object(scraper.urllib.request, "urlopen",
                               side_effect=[tiny, big]) as m:
            with mock.patch.object(scraper, "THUMB_DIR", self.thumb_dir):
                self.assertTrue(scraper.fetch_thumbnail("vid2"))
        self.assertEqual(m.call_count, 2)
        self.assertTrue((self.thumb_dir / "vid2.jpg").is_file())

    def test_fetch_thumbnail_all_qualities_fail_is_false(self):
        """When every quality returns a placeholder no file is written and
        the result is False."""
        tiny = fake_http_response(b"grey")
        with mock.patch.object(scraper.urllib.request, "urlopen",
                               side_effect=[tiny, tiny, tiny]) as m:
            with mock.patch.object(scraper, "THUMB_DIR", self.thumb_dir):
                self.assertFalse(scraper.fetch_thumbnail("vid3"))
        self.assertEqual(m.call_count, 3)
        self.assertFalse((self.thumb_dir / "vid3.jpg").exists())

    def test_fetch_thumbnail_network_error_is_false(self):
        """Connection errors on all qualities return False without raising."""
        with mock.patch.object(scraper.urllib.request, "urlopen",
                               side_effect=OSError("no network")):
            with mock.patch.object(scraper, "THUMB_DIR", self.thumb_dir):
                self.assertFalse(scraper.fetch_thumbnail("vid4"))


# ---------------------------------------------------------------- server: files & settings

class ServerFileTests(unittest.TestCase):
    """read_json / write_json_atomic / load_index / save_index on temp paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_json_missing_file_returns_default(self):
        """A missing file yields the supplied default."""
        self.assertEqual(server.read_json(self.root / "nope.json", {"d": 1}), {"d": 1})

    def test_read_json_corrupt_returns_default(self):
        """Corrupt JSON yields the default instead of raising."""
        p = self.root / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(server.read_json(p, ["fallback"]), ["fallback"])

    def test_write_json_atomic_roundtrip(self):
        """An atomic write produces a file that reads back identical."""
        p = self.root / "sub" / "data.json"
        server.write_json_atomic(p, {"a": [1, 2], "b": "x"})
        self.assertEqual(server.read_json(p, None), {"a": [1, 2], "b": "x"})
        self.assertFalse((self.root / "sub" / "data.json.tmp").exists())

    def test_load_index_save_index_roundtrip(self):
        """Saving and reloading the video index roundtrips the data."""
        videos_json = self.root / "videos.json"
        with mock.patch.object(server, "VIDEOS_JSON", videos_json):
            server.save_index(make_index({"v1": {"id": "v1", "title": "T"}}))
            self.assertEqual(server.load_index()["videos"]["v1"]["title"], "T")

    def test_load_index_corrupt_is_empty(self):
        """A corrupt index file loads as an empty video map."""
        videos_json = self.root / "videos.json"
        videos_json.write_text("###", encoding="utf-8")
        with mock.patch.object(server, "VIDEOS_JSON", videos_json):
            self.assertEqual(server.load_index(), {"videos": {}})


class ServerSettingsTests(unittest.TestCase):
    """load_settings merging behaviour."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_json = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_settings_defaults_include_all_thirteen_tabs(self):
        """With no saved file the defaults come back with all 13 tabs in order."""
        with mock.patch.object(server, "SETTINGS_JSON", self.settings_json):
            s = server.load_settings()
        self.assertEqual(s["accent"], "#7c5cff")
        self.assertEqual([t["id"] for t in s["tabs"]],
                         ["overview", "teach", "visualize", "relations", "videos",
                          "profile", "learned", "skills", "tests", "structure",
                          "settings", "console", "network"])
        self.assertEqual([t["order"] for t in s["tabs"]], list(range(13)))

    def test_load_settings_merges_saved_subset_and_renumbers(self):
        """A saved custom accent and a subset of tabs merge with missing
        default tabs appended and order renumbered 0..n-1."""
        saved = {
            "accent": "#ff0000",
            "tabs": [
                {"id": "videos", "label": "My Videos", "order": 7},
                {"id": "custom", "label": "Custom", "order": 3},
            ],
        }
        self.settings_json.write_text(json.dumps(saved), encoding="utf-8")
        with mock.patch.object(server, "SETTINGS_JSON", self.settings_json):
            s = server.load_settings()
        self.assertEqual(s["accent"], "#ff0000")
        ids = [t["id"] for t in s["tabs"]]
        self.assertEqual(ids[:2], ["videos", "custom"])
        self.assertEqual(len(ids), 14)
        for default_tab in server.DEFAULT_SETTINGS["tabs"]:
            self.assertIn(default_tab["id"], ids)
        self.assertEqual([t["order"] for t in s["tabs"]], list(range(14)))
        skills_tab = next(t for t in s["tabs"] if t["id"] == "skills")
        self.assertEqual(skills_tab["order"], 8)
        structure_tab = next(t for t in s["tabs"] if t["id"] == "structure")
        self.assertEqual(structure_tab["order"], 10)
        videos_tab = next(t for t in s["tabs"] if t["id"] == "videos")
        self.assertEqual(videos_tab["label"], "My Videos")


# ---------------------------------------------------------------- server: pure helpers

class ServerVideoHelpersTests(unittest.TestCase):
    """video_object / sorted_videos / sanitize_label / kebab."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.transcripts = root / "transcripts"
        self.learnings = root / "learnings"
        self.transcripts.mkdir()
        self.learnings.mkdir()
        self.patcher = mock.patch.multiple(
            server,
            VIDEOS_JSON=root / "videos.json",
            TRANSCRIPT_DIR=self.transcripts,
            LEARNINGS_DIR=self.learnings,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_video_object_sets_flags_and_thumbnail_url(self):
        """A video object gains has_transcript/has_learnings flags and a
        thumbnail URL based on the files that exist."""
        (self.transcripts / "v1.json").write_text("[]", encoding="utf-8")
        (self.learnings / "v1.json").write_text("{}", encoding="utf-8")
        obj = server.video_object("v1", {"id": "v1", "title": "T"})
        self.assertTrue(obj["has_transcript"])
        self.assertTrue(obj["has_learnings"])
        self.assertEqual(obj["thumbnail_url"], "/api/thumb/v1")
        obj2 = server.video_object("v2", {"id": "v2"})
        self.assertFalse(obj2["has_transcript"])
        self.assertFalse(obj2["has_learnings"])

    def test_video_object_marks_shorts_by_duration(self):
        """is_short follows YouTube's 3-minute Shorts cutoff; missing or
        zero duration is never a short."""
        self.assertTrue(server.video_object("s", {"id": "s", "duration": 45})["is_short"])
        self.assertTrue(server.video_object("s", {"id": "s", "duration": 180})["is_short"])
        self.assertFalse(server.video_object("l", {"id": "l", "duration": 181})["is_short"])
        self.assertFalse(server.video_object("l", {"id": "l", "duration": 0})["is_short"])
        self.assertFalse(server.video_object("n", {"id": "n"})["is_short"])
        self.assertFalse(server.video_object("b", {"id": "b", "duration": "junk"})["is_short"])

    def test_sorted_videos_descending_by_published(self):
        """Videos sort newest-first by published date."""
        index = make_index({
            "old": {"id": "old", "published": "20220101"},
            "new": {"id": "new", "published": "20240202"},
            "mid": {"id": "mid", "published": "20230101"},
        })
        server.save_index(index)
        self.assertEqual([v["id"] for v in server.sorted_videos()], ["new", "mid", "old"])

    def test_sanitize_label_strips_special_chars(self):
        """Quotes, brackets and braces are stripped from a label."""
        self.assertEqual(server.sanitize_label('a"b(c)d[e]f{g}'), "abcdefg")

    def test_kebab_lowercases_and_dashes(self):
        """Kebab-case slugs are lowercase with non-alphanumerics as dashes."""
        self.assertEqual(server.kebab("Hello World! Foo"), "hello-world-foo")
        self.assertEqual(server.kebab("  trailing---dashes "), "trailing-dashes")

    def test_kebab_truncates_to_40_chars(self):
        """A long title is truncated to 40 characters."""
        slug = server.kebab("A" + " very long title" * 10)
        self.assertLessEqual(len(slug), 40)
        self.assertTrue(slug.startswith("a-very-long-title"))

    def test_kebab_empty_is_video(self):
        """A title with no usable characters becomes 'video'."""
        self.assertEqual(server.kebab("!!!"), "video")
        self.assertEqual(server.kebab(""), "video")


class ServerSkillMdTests(unittest.TestCase):
    """skill_md and scan_skills."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skills_dir = Path(self.tmp.name) / "skills"

    def tearDown(self):
        self.tmp.cleanup()

    def test_skill_md_contains_frontmatter_and_all_sections(self):
        """The generated SKILL.md has frontmatter with name and video_id plus
        the summary, each learning title, key points, and the source URL."""
        learnings = {
            "summary": "A great summary",
            "what_you_learn": ["W1"],
            "learnings": [{"title": "First thing", "detail": "Detail one"},
                          {"title": "Second thing"}],
            "key_points": ["Point A", "Point B"],
        }
        video = {"title": "Cool Video", "url": "https://www.youtube.com/watch?v=v1"}
        md = server.skill_md(learnings, video, "v1")
        self.assertIn("name: Cool Video", md)
        self.assertIn("video_id: v1", md)
        self.assertIn('description: "Learnings from Cool Video — https://www.youtube.com/watch?v=v1"', md)
        self.assertIn("A great summary", md)
        self.assertIn("## What You Learn", md)
        self.assertIn("- W1", md)
        self.assertIn("## First thing", md)
        self.assertIn("Detail one", md)
        self.assertIn("## Second thing", md)
        self.assertIn("## Key Points", md)
        self.assertIn("- Point A", md)
        self.assertIn("- Point B", md)
        self.assertIn("## Source", md)
        self.assertIn("https://www.youtube.com/watch?v=v1", md)

    def test_scan_skills_reads_frontmatter_and_headings(self):
        """scan_skills lists each SKILL.md, preferring frontmatter name and
        video_id and falling back to the first markdown heading."""
        (self.skills_dir / "alpha").mkdir(parents=True)
        (self.skills_dir / "alpha" / "SKILL.md").write_text(
            "---\nname: Alpha Skill\nvideo_id: v1\n---\n\n# Ignored\n", encoding="utf-8")
        (self.skills_dir / "beta").mkdir(parents=True)
        (self.skills_dir / "beta" / "SKILL.md").write_text(
            "# Beta Heading\n\nSome body.\n", encoding="utf-8")
        with mock.patch.object(server, "SKILLS_DIR", self.skills_dir):
            skills = server.scan_skills()
        self.assertEqual(len(skills), 2)
        alpha, beta = skills
        self.assertEqual(alpha["slug"], "alpha")
        self.assertEqual(alpha["name"], "Alpha Skill")
        self.assertEqual(alpha["video_id"], "v1")
        self.assertEqual(alpha["path"], "skills/alpha/SKILL.md")
        self.assertEqual(beta["slug"], "beta")
        self.assertEqual(beta["name"], "Beta Heading")
        self.assertIsNone(beta["video_id"])

    def test_scan_skills_missing_dir_is_empty(self):
        """A missing skills directory yields an empty list, not an error."""
        with mock.patch.object(server, "SKILLS_DIR", self.skills_dir / "nope"):
            self.assertEqual(server.scan_skills(), [])

    def test_scan_skills_reads_description_and_source_workspace(self):
        """Frontmatter description and source_workspace are surfaced for the
        My Skills detail pane."""
        (self.skills_dir / "gamma").mkdir(parents=True)
        (self.skills_dir / "gamma" / "SKILL.md").write_text(
            '---\nname: Gamma\ndescription: "Does gamma things"\nsource_workspace: free-gamma\n---\n\n# X\n',
            encoding="utf-8")
        with mock.patch.object(server, "SKILLS_DIR", self.skills_dir):
            skills = server.scan_skills()
        gamma = next(s for s in skills if s["slug"] == "gamma")
        self.assertEqual(gamma["description"], "Does gamma things")
        self.assertEqual(gamma["source_workspace"], "free-gamma")
        self.assertIsNone(gamma["video_id"])

    def test_scan_skills_reports_installed_state(self):
        """installed is true only when the slug exists under KIMI_SKILLS_DIR,
        and installed_stale flags a drifted copy."""
        (self.skills_dir / "delta").mkdir(parents=True)
        src = self.skills_dir / "delta" / "SKILL.md"
        src.write_text("---\nname: Delta\n---\nbody v1\n", encoding="utf-8")
        kimi_dir = self.skills_dir / "kimi-home"

        with mock.patch.object(server, "SKILLS_DIR", self.skills_dir), \
             mock.patch.object(server, "KIMI_SKILLS_DIR", kimi_dir):
            not_inst = next(s for s in server.scan_skills() if s["slug"] == "delta")
            self.assertFalse(not_inst["installed"])
            self.assertFalse(not_inst["installed_stale"])

            (kimi_dir / "delta").mkdir(parents=True)
            (kimi_dir / "delta" / "SKILL.md").write_text("same", encoding="utf-8")
            inst = next(s for s in server.scan_skills() if s["slug"] == "delta")
            self.assertTrue(inst["installed"])
            self.assertTrue(inst["installed_stale"])  # differs from source

            (kimi_dir / "delta" / "SKILL.md").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8")
            fresh = next(s for s in server.scan_skills() if s["slug"] == "delta")
            self.assertTrue(fresh["installed"])
            self.assertFalse(fresh["installed_stale"])


class ServerRelationsTests(unittest.TestCase):
    """build_relations with temp learnings and a mocked index."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.learnings = Path(self.tmp.name) / "learnings"
        self.learnings.mkdir()
        self.patcher = mock.patch.multiple(
            server, LEARNINGS_DIR=self.learnings,
            load_index=mock.DEFAULT,
        )
        self.mocks = self.patcher.start()
        self.mocks["load_index"].return_value = make_index({
            "a": {"id": "a", "title": "Alpha (intro)"},
            "b": {"id": "b", "title": "Beta [advanced]"},
        })

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _write_learnings(self, vid, skills):
        (self.learnings / f"{vid}.json").write_text(
            json.dumps({"skills": [{"name": s} for s in skills]}), encoding="utf-8")

    def test_shared_skill_produces_quoted_edge_label(self):
        """Two videos sharing a skill get an edge whose quoted label contains
        the shared skill."""
        self._write_learnings("a", ["Design (Patterns)", "Python"])
        self._write_learnings("b", ["design (patterns)", "SQL"])
        result = server.build_relations(["a", "b"])
        self.assertEqual(result["shared"], [["a", "b"]])
        self.assertEqual(result["skills"]["a"], ["design (patterns)", "python"])
        self.assertIn('a ---|"design patterns"| b', result["mermaid"])
        self.assertIn('a["Alpha intro"]', result["mermaid"])

    def test_no_shared_skills_means_no_edge(self):
        """Two videos with disjoint skills produce nodes but no edge."""
        self._write_learnings("a", ["Python"])
        self._write_learnings("b", ["SQL"])
        result = server.build_relations(["a", "b"])
        self.assertEqual(result["shared"], [])
        self.assertIn("graph LR", result["mermaid"])
        self.assertNotIn("---|", result["mermaid"])
        self.assertNotIn("note", result)

    def test_fewer_than_two_videos_with_learnings_gets_note(self):
        """With under 2 learnings files the mermaid is empty and a note explains why."""
        self._write_learnings("a", ["Python"])
        result = server.build_relations(["a"])
        self.assertEqual(result["mermaid"], "")
        self.assertIn("note", result)

    def test_mermaid_labels_contain_no_brackets_or_braces(self):
        """No label in the mermaid output keeps ()[]{} characters."""
        weird = "Skill [weird] {name} (v2)"
        self._write_learnings("a", [weird])
        self._write_learnings("b", [weird])
        result = server.build_relations(["a", "b"])
        labels = result["mermaid"]
        self.assertIn('---|"skill weird name v2"|', labels)
        stripped = labels.replace('["', "").replace('"]', "")
        for ch in "(){}[]":
            self.assertNotIn(ch, stripped)


class ServerUrlAndProgressTests(unittest.TestCase):
    """classify_urls and parse_progress — pure functions."""

    def test_classify_video_urls(self):
        """Watch, youtu.be and shorts URLs all classify as video URLs."""
        urls = [
            "https://www.youtube.com/watch?v=abc123def45&list=xyz",
            "https://youtu.be/abc123def45",
            "https://www.youtube.com/shorts/abc123def45",
        ]
        video_urls, channel_urls = server.classify_urls(urls, None)
        self.assertEqual(video_urls, urls)
        self.assertEqual(channel_urls, [])

    def test_classify_channel_urls(self):
        """@handle, /channel/UC and /videos URLs classify as channel URLs."""
        urls = [
            "https://www.youtube.com/@mattpocock",
            "https://www.youtube.com/channel/UCxKabvvbu9es9uQpC9aB3hQ",
            "https://www.youtube.com/someuser/videos",
        ]
        video_urls, channel_urls = server.classify_urls(urls, None)
        self.assertEqual(video_urls, [])
        self.assertEqual(channel_urls, urls)

    def test_classify_mixed_channel_param(self):
        """A valid channel param plus video URLs fills both lists."""
        video_urls, channel_urls = server.classify_urls(
            ["https://youtu.be/abc123def45"], "https://www.youtube.com/@chan")
        self.assertEqual(video_urls, ["https://youtu.be/abc123def45"])
        self.assertEqual(channel_urls, ["https://www.youtube.com/@chan"])

    def test_classify_garbage_raises(self):
        """A non-YouTube URL raises ValueError."""
        with self.assertRaises(ValueError):
            server.classify_urls(["https://vimeo.com/12345"], None)

    def test_classify_bad_channel_param_raises(self):
        """A channel param that is not a channel URL raises ValueError."""
        with self.assertRaises(ValueError):
            server.classify_urls([], "https://www.youtube.com/watch?v=abc123def45")

    def test_parse_progress_counts_and_eta(self):
        """'Found N videos' sets the total, enriched/transcript lines increment
        done_count, and eta is remaining times (3 + request_delay)."""
        job = {"total": 0, "done_count": 0}
        settings = {"request_delay": 0.8}
        server.parse_progress("Found 5 videos", job, settings)
        self.assertEqual(job["total"], 5)
        self.assertEqual(job["eta_seconds"], int(5 * 3.8))
        server.parse_progress("enriched abc123 (transcript: yes)", job, settings)
        self.assertEqual(job["done_count"], 1)
        self.assertEqual(job["eta_seconds"], int(4 * 3.8))
        server.parse_progress("transcript def456: yes", job, settings)
        self.assertEqual(job["done_count"], 2)
        self.assertEqual(job["eta_seconds"], int(3 * 3.8))


PROFILE_MD = """# Nils

## 1. About

Some intro text.

## 2. Technical Skills

### Strong / Professional Experience

- **Python**

### Basic / Touched

- Git

## 3. Projects

Other section.
"""


class ServerProfileMarkdownTests(unittest.TestCase):
    """update_profile_markdown skill insertion."""

    def test_strong_skill_is_bold_bullet_under_strong_heading(self):
        """A strong skill is inserted as a bold bullet under the strong heading."""
        md = server.update_profile_markdown(PROFILE_MD, "Rust", "strong")
        self.assertIn("## 2. Technical Skills", md)
        strong_idx = md.index("### Strong / Professional Experience")
        basic_idx = md.index("### Basic / Touched")
        rust_idx = md.index("- **Rust**")
        self.assertTrue(strong_idx < rust_idx < basic_idx)
        self.assertIn("## 3. Projects", md)
        self.assertIn("Some intro text.", md)

    def test_basic_skill_is_plain_bullet(self):
        """A basic skill is inserted as a plain (non-bold) bullet."""
        md = server.update_profile_markdown(PROFILE_MD, "Docker", "basic")
        basic_idx = md.index("### Basic / Touched")
        docker_idx = md.index("- Docker")
        self.assertTrue(basic_idx < docker_idx)
        self.assertNotIn("- **Docker**", md)

    def test_unknown_level_heading_created_when_level_given(self):
        """A weak skill creates the missing weak subsection and lands there."""
        md = server.update_profile_markdown(PROFILE_MD, "Kubernetes", "weak")
        self.assertIn("### Weak / Gaps (be patient here, explain more)", md)
        weak_idx = md.index("### Weak / Gaps")
        k8s_idx = md.index("- Kubernetes")
        self.assertTrue(weak_idx < k8s_idx)

    def test_reinsert_same_skill_does_not_duplicate(self):
        """Inserting the same skill twice is idempotent."""
        once = server.update_profile_markdown(PROFILE_MD, "Rust", "strong")
        twice = server.update_profile_markdown(once, "Rust", "strong")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("- **Rust**"), 1)
        # and existing unrelated content is untouched
        self.assertEqual(twice.count("- **Python**"), 1)

    def test_skill_updated_in_place_when_already_listed(self):
        """Re-classifying an existing skill replaces its bullet in place,
        case-insensitively, under its own subsection."""
        md = server.update_profile_markdown(PROFILE_MD, "git", "basic")
        self.assertEqual(md.count("- git"), 1)
        self.assertNotIn("\n- Git\n", md)


class ServerWorkspaceTests(unittest.TestCase):
    """create_workspace on a temp TEACH_DIR."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.teach = root / "teach"
        settings = root / "settings.json"
        settings.write_text(json.dumps(
            {"profile_path": str(root / "no-such-profile.md")}), encoding="utf-8")
        self.patcher = mock.patch.multiple(
            server, TEACH_DIR=self.teach, TRANSCRIPT_DIR=root / "transcripts",
            SETTINGS_JSON=settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_create_workspace_creates_dirs_and_files(self):
        """A new workspace gets its subdirs plus MISSION.md (listing the
        skills from learnings), RESOURCES.md and NOTES.md."""
        video = {"title": "Cool Video", "url": "https://www.youtube.com/watch?v=v1"}
        learnings = {"summary": "The summary", "skills": [
            {"name": "SkillA", "description": "does A"}]}
        ws, created = server.create_workspace("v1", video, learnings)
        self.assertEqual(ws, "teach/v1")
        self.assertTrue(created)
        base = self.teach / "v1"
        for d in ("lessons", "reference", "learning-records", "assets"):
            self.assertTrue((base / d).is_dir(), d)
        mission = (base / "MISSION.md").read_text(encoding="utf-8")
        self.assertIn("Cool Video", mission)
        self.assertIn("The summary", mission)
        self.assertIn("**SkillA**", mission)
        # video workspaces must demand the learner interview inside MISSION.md
        # itself — the teach skill only interviews when the mission isn't
        # populated, so without this the agent skips the interview
        self.assertIn("STATUS: NOT INTERVIEWED", mission)
        self.assertIn("interview the learner", mission.lower())
        resources = (base / "RESOURCES.md").read_text(encoding="utf-8")
        self.assertIn("https://www.youtube.com/watch?v=v1", resources)
        # the agent's cwd is the workspace — the transcript path must be
        # workspace-relative (../../), not app-root-relative, or the agent
        # thinks the transcript is missing and tries to re-download it
        self.assertIn("`../../data/transcripts/v1.json`", resources)
        self.assertIn("NOTES.md", str(list(base.iterdir())))
        self.assertIn("Notes", (base / "NOTES.md").read_text(encoding="utf-8"))

    def test_second_call_does_not_overwrite_modified_mission(self):
        """Re-creating an existing workspace reports created=False and keeps
        a modified MISSION.md."""
        video = {"title": "T", "url": "u"}
        server.create_workspace("v1", video, None)
        mission = self.teach / "v1" / "MISSION.md"
        mission.write_text("# MODIFIED\n", encoding="utf-8")
        ws, created = server.create_workspace("v1", video, None)
        self.assertFalse(created)
        self.assertEqual(mission.read_text(encoding="utf-8"), "# MODIFIED\n")


# ---------------------------------------------------------------- server: HTTP API

class ServerApiTests(unittest.TestCase):
    """In-process API tests: real ThreadingHTTPServer, temp data only."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        (root / "skills").mkdir()
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (static / "styles.css").write_text("body{}\n", encoding="utf-8")
        cls.root = root
        cls.data = data

        videos = {
            "vidA": {"id": "vidA", "title": "Alpha Video", "channel": "Chan",
                     "url": "https://www.youtube.com/watch?v=vidA",
                     "published": "20240202", "learned": False, "added": "2024-01-01"},
            "vidB": {"id": "vidB", "title": "Beta Video", "channel": "Chan",
                     "url": "https://www.youtube.com/watch?v=vidB",
                     "published": "20240115", "learned": False, "added": "2024-01-01"},
        }
        (data / "videos.json").write_text(json.dumps({"videos": videos}), encoding="utf-8")
        (data / "transcripts" / "vidA.json").write_text(
            json.dumps([{"start": 0.0, "text": "hello"}]), encoding="utf-8")
        (data / "learnings" / "vidA.json").write_text(json.dumps({
            "video_id": "vidA", "title": "Alpha Video", "summary": "Sum",
            "skills": [{"name": "Python"}],
            "learnings": [{"title": "L1", "detail": "D1"}],
            "key_points": ["K1"],
        }), encoding="utf-8")
        (data / "thumbs" / "vidA.jpg").write_bytes(b"\xff\xd8fakejpeg")
        (data / "test_results.json").write_text(json.dumps({
            "ran_at": "2026-01-01T00:00:00", "duration": 0.1, "total": 1,
            "passed": 1, "failed": 0, "tests": [],
        }), encoding="utf-8")
        cls.profile = root / "profile.md"
        cls.profile.write_text(PROFILE_MD, encoding="utf-8")
        (data / "settings.json").write_text(
            json.dumps({"profile_path": str(cls.profile)}), encoding="utf-8")

        cls.patchers = [
            mock.patch.object(server, "VIDEOS_JSON", data / "videos.json"),
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "JOBS_JSON", data / "jobs.json"),
            mock.patch.object(server, "PROFILE_SKILLS_JSON", data / "profile-skills.json"),
            mock.patch.object(server, "TEST_RESULTS_JSON", data / "test_results.json"),
            mock.patch.object(server, "LEARNINGS_DIR", data / "learnings"),
            mock.patch.object(server, "TRANSCRIPT_DIR", data / "transcripts"),
            mock.patch.object(server, "THUMB_DIR", data / "thumbs"),
            mock.patch.object(server, "INFO_DIR", data / "info"),
            mock.patch.object(server, "PROGRESS_DIR", data / "progress"),
            mock.patch.object(server, "TEACH_DIR", root / "teach"),
            mock.patch.object(server, "SKILLS_DIR", root / "skills"),
            mock.patch.object(server, "STATIC_DIR", static),
            mock.patch.object(server, "JOBS", {}),
        ]
        for p in cls.patchers:
            p.start()

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for p in cls.patchers:
            p.stop()
        cls.tmp.cleanup()

    def api(self, method, path, body=None, want_json=True):
        """Send one request on a fresh connection and return (status, data)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        if want_json and "application/json" in ctype:
            return status, json.loads(raw.decode("utf-8"))
        return status, raw

    # -- static

    def test_index_page_served(self):
        """GET / returns the index HTML page."""
        status, body = self.api("GET", "/", want_json=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<html>", body)

    def test_static_css_served(self):
        """GET /static/styles.css returns the stylesheet."""
        status, body = self.api("GET", "/static/styles.css", want_json=False)
        self.assertEqual(status, 200)
        self.assertIn(b"body", body)

    def test_static_traversal_is_forbidden(self):
        """A path traversal request outside the static dir is rejected."""
        status, _ = self.api("GET", "/static/../server.py")
        self.assertEqual(status, 403)

    # -- read-only api

    def test_api_state_reports_stats(self):
        """GET /api/state returns settings, tabs and video statistics."""
        status, body = self.api("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["stats"]["total"], 2)
        self.assertEqual(body["stats"]["with_transcript"], 1)
        self.assertEqual(body["stats"]["with_learnings"], 1)
        self.assertEqual(len(body["tabs"]), 13)

    def test_api_videos_sorted_desc_with_flags(self):
        """GET /api/videos lists both videos newest-first with file flags."""
        status, body = self.api("GET", "/api/videos")
        self.assertEqual(status, 200)
        ids = [v["id"] for v in body["videos"]]
        self.assertEqual(ids, ["vidA", "vidB"])
        a = body["videos"][0]
        self.assertTrue(a["has_transcript"])
        self.assertTrue(a["has_learnings"])
        self.assertEqual(a["thumbnail_url"], "/api/thumb/vidA")
        self.assertFalse(body["videos"][1]["has_transcript"])

    def test_api_video_found_and_unknown(self):
        """GET /api/video/<id> returns the video; an unknown id is a 404."""
        status, body = self.api("GET", "/api/video/vidA")
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "vidA")
        self.assertEqual(body["learnings"]["summary"], "Sum")
        status, _ = self.api("GET", "/api/video/nope")
        self.assertEqual(status, 404)

    def test_api_learnings_found_and_unknown(self):
        """GET /api/learnings/<id> returns learnings; an unknown id is 404."""
        status, body = self.api("GET", "/api/learnings/vidA")
        self.assertEqual(status, 200)
        self.assertEqual(body["video_id"], "vidA")
        status, _ = self.api("GET", "/api/learnings/nope")
        self.assertEqual(status, 404)

    def test_api_transcript(self):
        """GET /api/transcript/<id> wraps segments in a JSON object."""
        status, body = self.api("GET", "/api/transcript/vidA")
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [{"start": 0.0, "text": "hello"}])
        status, _ = self.api("GET", "/api/transcript/vidB")
        self.assertEqual(status, 404)

    def test_api_thumb(self):
        """GET /api/thumb/<id> serves the jpeg with an image content type."""
        status, body = self.api("GET", "/api/thumb/vidA", want_json=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\xff\xd8fakejpeg")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/thumb/vidA")
        resp = conn.getresponse()
        resp.read()
        self.assertIn("image/jpeg", resp.getheader("Content-Type"))
        conn.close()
        status, _ = self.api("GET", "/api/thumb/vidB")
        self.assertEqual(status, 404)

    def test_api_relations(self):
        """GET /api/relations with one learnings file returns an explanatory note."""
        status, body = self.api("GET", "/api/relations?ids=vidA,vidB")
        self.assertEqual(status, 200)
        self.assertIn("note", body)
        status, _ = self.api("GET", "/api/relations?ids=")
        self.assertEqual(status, 400)

    def test_api_settings_get(self):
        """GET /api/settings returns the merged defaults."""
        status, body = self.api("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["tabs"]), 13)
        self.assertEqual(body["profile_path"], str(self.profile))

    def test_api_profile_get(self):
        """GET /api/profile returns the markdown from the configured path."""
        status, body = self.api("GET", "/api/profile")
        self.assertEqual(status, 200)
        self.assertIn("## 2. Technical Skills", body["markdown"])
        self.assertEqual(body["path"], str(self.profile))

    def test_api_profile_skills_get(self):
        """GET /api/profile/skills returns an empty list when nothing stored."""
        status, body = self.api("GET", "/api/profile/skills")
        self.assertEqual(status, 200)
        self.assertEqual(body["skills"], [])

    def test_api_test_results(self):
        """GET /api/test-results returns the stored test report."""
        status, body = self.api("GET", "/api/test-results")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)

    # -- learned flag

    def test_set_learned_true_then_false_persists(self):
        """Toggling the learned flag on and off persists both ways."""
        status, body = self.api("POST", "/api/video/vidB/learned", {"learned": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["learned"])
        _, body = self.api("GET", "/api/video/vidB")
        self.assertTrue(body["learned"])
        _, body = self.api("POST", "/api/video/vidB/learned", {"learned": False})
        self.assertFalse(body["learned"])
        _, body = self.api("GET", "/api/video/vidB")
        self.assertFalse(body["learned"])

    def test_set_learned_invalid_body_is_400(self):
        """A learned request without a boolean body is rejected with 400."""
        status, _ = self.api("POST", "/api/video/vidA/learned", {"learned": "yes"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/video/vidA/learned", None)
        self.assertEqual(status, 400)

    def test_set_learned_unknown_video_is_404(self):
        """Setting the learned flag on an unknown video returns 404."""
        status, _ = self.api("POST", "/api/video/nope/learned", {"learned": True})
        self.assertEqual(status, 404)

    # -- progress

    def test_progress_post_get_roundtrip(self):
        """Progress saved with POST comes back identically from GET."""
        status, body = self.api("POST", "/api/progress/vidA", {"completed": ["s1", "s2"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], ["s1", "s2"])
        status, body = self.api("GET", "/api/progress/vidA")
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], ["s1", "s2"])

    def test_progress_invalid_body_is_400(self):
        """A progress request without a completed list is rejected with 400."""
        status, _ = self.api("POST", "/api/progress/vidA", {"completed": "nope"})
        self.assertEqual(status, 400)

    # -- settings write

    def test_update_settings_merges_and_keeps_other_keys(self):
        """POSTing a new accent merges it in and leaves other keys intact."""
        status, body = self.api("POST", "/api/settings", {"accent": "#123456"})
        self.assertEqual(status, 200)
        self.assertEqual(body["accent"], "#123456")
        self.assertEqual(body["port"], server.DEFAULT_SETTINGS["port"])
        self.assertEqual(len(body["tabs"]), 13)
        status, body = self.api("GET", "/api/settings")
        self.assertEqual(body["accent"], "#123456")

    # -- teach workspace

    def test_teach_start_twice_second_is_created_false(self):
        """Starting a workspace twice creates it once and reports the second
        call as not created."""
        status, body = self.api("POST", "/api/teach/start", {"video_id": "vidB"})
        self.assertEqual(status, 200)
        self.assertTrue(body["created"])
        self.assertEqual(body["workspace"], "teach/vidB")
        status, body = self.api("POST", "/api/teach/start", {"video_id": "vidB"})
        self.assertFalse(body["created"])

    def test_lessons_list_and_lesson_content(self):
        """Lessons list html files and a lesson can be fetched by name; a
        path traversal attempt is rejected."""
        self.api("POST", "/api/teach/start", {"video_id": "vidA"})
        lessons = self.root / "teach" / "vidA" / "lessons"
        lessons.mkdir(exist_ok=True)
        (lessons / "intro.html").write_text("<h1>Lesson</h1>", encoding="utf-8")
        status, body = self.api("GET", "/api/teach/vidA/lessons")
        self.assertEqual(status, 200)
        self.assertEqual(body["lessons"], [{"file": "intro.html"}])
        status, body = self.api(
            "GET", "/api/teach/lesson?video=vidA&file=intro.html", want_json=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<h1>Lesson</h1>", body)
        status, _ = self.api("GET", "/api/teach/lesson?video=vidA&file=../secret")
        self.assertEqual(status, 400)

    def test_teach_start_missing_body_is_400(self):
        """Starting a workspace without a video_id is rejected with 400."""
        status, _ = self.api("POST", "/api/teach/start", {})
        self.assertEqual(status, 400)

    # -- skills

    def test_skill_create_then_listed(self):
        """Creating a skill from a video's learnings writes SKILL.md and the
        skill appears in /api/skills."""
        status, body = self.api("POST", "/api/skill/create", {"video_id": "vidA"})
        self.assertEqual(status, 200)
        slug = body["slug"]
        skill_file = self.root / "skills" / slug / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        self.assertIn("video_id: vidA", skill_file.read_text(encoding="utf-8"))
        status, body = self.api("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertIn(slug, [s["slug"] for s in body["skills"]])

    def test_skill_create_without_learnings_is_400(self):
        """Creating a skill for a video without learnings is rejected."""
        status, _ = self.api("POST", "/api/skill/create", {"video_id": "vidZZZ"})
        self.assertEqual(status, 400)

    # -- profile write

    def test_put_profile_then_get_returns_new_markdown(self):
        """Saving profile markdown with PUT makes it readable via GET."""
        status, _ = self.api("PUT", "/api/profile", {"markdown": "# Changed\n"})
        self.assertEqual(status, 200)
        status, body = self.api("GET", "/api/profile")
        self.assertEqual(body["markdown"], "# Changed\n")

    def test_put_profile_invalid_body_is_400(self):
        """A PUT profile without a markdown string is rejected."""
        status, _ = self.api("PUT", "/api/profile", {"nope": 1})
        self.assertEqual(status, 400)

    # -- learnings write

    def test_put_learnings_ok_and_mismatch_400(self):
        """PUT learnings with a matching video_id saves, a mismatched one 400s."""
        body = {"video_id": "vidB", "title": "B", "skills": []}
        status, _ = self.api("PUT", "/api/learnings/vidB", body)
        self.assertEqual(status, 200)
        status, saved = self.api("GET", "/api/learnings/vidB")
        self.assertEqual(saved["title"], "B")
        status, _ = self.api("PUT", "/api/learnings/vidB",
                             {"video_id": "someone-else"})
        self.assertEqual(status, 400)

    # -- add videos (validation only)

    def test_add_videos_garbage_url_is_400_and_starts_no_job(self):
        """A garbage URL fails validation with 400 before any scrape job
        can start."""
        status, body = self.api("POST", "/api/videos/add",
                                {"urls": ["https://vimeo.com/123"]})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(server.JOBS, {})

    def test_add_videos_empty_is_400(self):
        """An add request with no URLs at all is rejected with 400."""
        status, _ = self.api("POST", "/api/videos/add", {})
        self.assertEqual(status, 400)

    # -- delete

    def test_delete_video_removes_it_and_unknown_404(self):
        """Deleting a video removes it from the index; a second delete of an
        unknown id returns 404."""
        index = server.load_index()
        index["videos"]["vidC"] = {"id": "vidC", "title": "C", "published": ""}
        server.save_index(index)
        status, _ = self.api("DELETE", "/api/video/vidC")
        self.assertEqual(status, 200)
        _, body = self.api("GET", "/api/videos")
        self.assertNotIn("vidC", [v["id"] for v in body["videos"]])
        status, _ = self.api("DELETE", "/api/video/vidC")
        self.assertEqual(status, 404)

    # -- learned toggle (Overview / Learned tabs)

    def test_set_learned_toggles_and_persists(self):
        """Toggling learned returns the updated video object and persists to
        the index; bad bodies and unknown ids are rejected."""
        status, body = self.api("POST", "/api/video/vidA/learned", {"learned": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["learned"])
        self.assertTrue(server.load_index()["videos"]["vidA"]["learned"])
        status, _ = self.api("POST", "/api/video/vidA/learned", {"learned": "yes"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/video/vidZZ/learned", {"learned": True})
        self.assertEqual(status, 404)

    # -- profile write (Profile tab)

    def test_put_profile_writes_markdown_and_validates(self):
        """PUT /api/profile writes the markdown to the profile path; a bad
        body is rejected."""
        status, body = self.api("PUT", "/api/profile", {"markdown": "# New Profile\n"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.profile.read_text(encoding="utf-8"), "# New Profile\n")
        status, _ = self.api("PUT", "/api/profile", {"md": "# oops"})
        self.assertEqual(status, 400)


class ShutdownApiTests(unittest.TestCase):
    """POST /api/shutdown with a dedicated server instance."""

    def test_shutdown_stops_the_server(self):
        """After /api/shutdown the port stops answering requests."""
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/shutdown")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)

            stopped = False
            for _ in range(100):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                    c.request("GET", "/api/state")
                    r = c.getresponse()
                    r.read()
                    c.close()
                except OSError:
                    stopped = True
                    break
                time.sleep(0.1)
            self.assertTrue(stopped, "server still answering after shutdown")
            with self.assertRaises(OSError):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/state")
                c.getresponse().read()
        finally:
            thread.join(timeout=5)
            httpd.server_close()


# ---------------------------------------------------------------- server v2: skills library, workspaces, agent

class ServerSkillsLibraryTests(unittest.TestCase):
    """build_skills_library with temp index + learnings fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.learnings = root / "learnings"
        self.learnings.mkdir()
        self.videos_json = root / "videos.json"
        self.patcher = mock.patch.multiple(
            server, VIDEOS_JSON=self.videos_json, LEARNINGS_DIR=self.learnings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _seed(self):
        index = make_index({
            "vidL": {"id": "vidL", "title": "Learned Vid",
                     "url": "https://www.youtube.com/watch?v=vidL", "learned": True},
            "vidU": {"id": "vidU", "title": "Unlearned Vid",
                     "url": "https://www.youtube.com/watch?v=vidU", "learned": False},
        })
        server.save_index(index)
        (self.learnings / "vidL.json").write_text(json.dumps(
            {"skills": [{"name": "Testing"}, {"name": "Python"}]}), encoding="utf-8")
        (self.learnings / "vidU.json").write_text(json.dumps(
            {"skills": [{"name": "Rust"}, {"name": "Python"}]}), encoding="utf-8")

    def test_skills_library_buckets_counts_videos_and_sorts(self):
        """Learned videos feed 'known', unlearned feed 'learnable', counts and
        video entries are embedded, and each bucket sorts by count desc."""
        self._seed()
        lib = server.build_skills_library()
        known, learnable = lib["known"], lib["learnable"]
        self.assertEqual([s["name"] for s in known], ["Python", "Testing"])
        self.assertEqual([s["name"] for s in learnable], ["Python", "Rust"])
        for entry in known + learnable:
            self.assertEqual(entry["count"], len(entry["videos"]))
            for v in entry["videos"]:
                self.assertIn("id", v)
                self.assertIn("title", v)
                self.assertIn("learned", v)
        py_known = next(s for s in known if s["name"] == "Python")
        self.assertEqual(py_known["videos"][0]["id"], "vidL")
        self.assertTrue(py_known["videos"][0]["learned"])
        py_learn = next(s for s in learnable if s["name"] == "Python")
        self.assertEqual(py_learn["videos"][0]["id"], "vidU")
        self.assertFalse(py_learn["videos"][0]["learned"])

    def test_skills_library_ignores_videos_without_learnings(self):
        """Videos with no learnings file contribute no skills."""
        server.save_index(make_index(
            {"vidX": {"id": "vidX", "title": "X", "learned": True}}))
        lib = server.build_skills_library()
        self.assertEqual(lib["known"], [])
        self.assertEqual(lib["learnable"], [])


class ServerMySkillsAndTaxonomyTests(unittest.TestCase):
    """Skill taxonomy guard (My Skills tab stays distinct as videos are
    added) plus the run_analysis branches that feed it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.learnings = Path(self.tmp.name) / "learnings"
        self.learnings.mkdir()
        self.patcher = mock.patch.object(server, "LEARNINGS_DIR", self.learnings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _write(self, vid, skills):
        (self.learnings / f"{vid}.json").write_text(
            json.dumps({"video_id": vid, "skills": skills}), encoding="utf-8")

    # -- normalize_skills_to_taxonomy (pure)

    def test_normalize_exact_match_adopts_canonical_spelling(self):
        """A case-insensitive exact match adopts the established spelling."""
        out = server.normalize_skills_to_taxonomy(
            [{"name": "prompt engineering", "description": "d"}],
            ["Prompt Engineering", "Testing"])
        self.assertEqual(out, [{"name": "Prompt Engineering", "description": "d"}])

    def test_normalize_fuzzy_match_reuses_existing_skill(self):
        """A close variant maps onto the existing skill instead of forking."""
        out = server.normalize_skills_to_taxonomy(
            [{"name": "Prompt Engineering Best Practices"}],
            ["Prompt Engineering"])
        self.assertEqual(out[0]["name"], "Prompt Engineering")

    def test_normalize_keeps_genuinely_new_skills(self):
        """Distinct concepts pass through untouched."""
        out = server.normalize_skills_to_taxonomy(
            [{"name": "Underwater Basket Weaving", "description": "x"}],
            ["Prompt Engineering"])
        self.assertEqual(out[0]["name"], "Underwater Basket Weaving")

    def test_normalize_merges_in_file_duplicates_longest_description(self):
        """Two variants that map to one canonical skill merge; the longest
        description wins."""
        out = server.normalize_skills_to_taxonomy(
            [{"name": "prompt engineering", "description": "short"},
             {"name": "Prompt Engineering Techniques", "description": "a much longer description"}],
            ["Prompt Engineering"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Prompt Engineering")
        self.assertEqual(out[0]["description"], "a much longer description")

    def test_normalize_empty_and_no_mutation(self):
        """Empty input yields []; the caller's list is never mutated."""
        self.assertEqual(server.normalize_skills_to_taxonomy([], ["X"]), [])
        src = [{"name": "prompt engineering", "description": "d"}]
        snapshot = json.dumps(src)
        server.normalize_skills_to_taxonomy(src, ["Prompt Engineering"])
        self.assertEqual(json.dumps(src), snapshot)

    # -- consolidate_learnings_skills (integration)

    def test_consolidate_maps_new_video_onto_corpus(self):
        """A freshly generated video with near-duplicate skill names is
        rewritten onto the corpus taxonomy."""
        self._write("old1", [{"name": "Prompt Engineering", "description": "core"},
                             {"name": "Testing", "description": "tests"}])
        self._write("new1", [{"name": "prompt engineering techniques", "description": "t"},
                             {"name": "Yoga", "description": "y"}])
        changed = server.consolidate_learnings_skills("new1")
        self.assertTrue(changed)
        data = json.loads((self.learnings / "new1.json").read_text(encoding="utf-8"))
        names = [s["name"] for s in data["skills"]]
        self.assertEqual(names, ["Prompt Engineering", "Yoga"])

    def test_consolidate_idempotent_when_already_canonical(self):
        """A second pass over a normalized file changes nothing."""
        self._write("old1", [{"name": "Testing", "description": "t"}])
        self._write("new1", [{"name": "Testing", "description": "t"}])
        self.assertFalse(server.consolidate_learnings_skills("new1"))

    # -- run_analysis early branches

    def test_run_analysis_no_ids_marks_done(self):
        """Empty id list completes immediately with zero totals."""
        with mock.patch.object(server, "JOBS_JSON", Path(self.tmp.name) / "jobs.json"):
            job: dict = {}
            server.run_analysis(job, [])
        self.assertEqual(job["phase"], "done")
        self.assertEqual(job["analysis_total"], 0)

    def test_run_analysis_without_cli_pends_learnings(self):
        """Without the kimi CLI the job finishes with a pending message."""
        with mock.patch.object(server, "find_kimi", return_value=None), \
             mock.patch.object(server.agentd_client, "backend", return_value="cli"), \
             mock.patch.object(server, "JOBS_JSON", Path(self.tmp.name) / "jobs.json"):
            job: dict = {}
            server.run_analysis(job, ["vidA"])
        self.assertEqual(job["phase"], "done")
        self.assertEqual(job["analysis_total"], 1)
        self.assertIn("pending", job["message"])


class ServerAgentHelpersTests(unittest.TestCase):
    """Pure helpers: find_kimi, safe_ws_name, analysis_prompt, workspaces."""

    def test_find_kimi_uses_shutil_which(self):
        """find_kimi returns the which() path, or None when not installed."""
        with mock.patch("shutil.which", return_value="/usr/local/bin/kimi"):
            self.assertEqual(server.find_kimi(), "/usr/local/bin/kimi")
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(server.find_kimi())

    def test_safe_ws_name_accepts_normal_ids(self):
        """Normal workspace ids, free- topics and video ids are accepted."""
        for name in ("free-foo-bar", "abc123_XYZ-1.2_3", "dQw4w9WgXcQ", "a"):
            self.assertTrue(server.safe_ws_name(name), name)

    def test_safe_ws_name_rejects_unsafe(self):
        """Empty, dot-dot, slashes, spaces and leading dashes are rejected."""
        for name in ("", "..", "a/b", "a\\b", "a b", "-abc", "../x", ".hidden"):
            self.assertFalse(server.safe_ws_name(name), name)

    def test_analysis_prompt_contains_ids_schema_and_forbidden_chars(self):
        """The prompt names every id, the example file, the schema keywords
        and the forbidden-characters instruction."""
        prompt = server.analysis_prompt(["abc123", "def456"], "example9")
        self.assertIn("abc123", prompt)
        self.assertIn("def456", prompt)
        self.assertIn("example9", prompt)
        for kw in ("summary", "mermaid", "time_hint", "key_points", "what_you_learn"):
            self.assertIn(kw, prompt)
        self.assertIn("data/learnings/<id>.json", prompt)
        self.assertIn("( ) [ ] { }", prompt)

    def test_skill_distill_prompt_pins_topic_and_forbids_meta(self):
        """The distillation prompt targets the workspace's TOPIC and explicitly
        forbids teaching-process/meta content."""
        prompt = server.skill_distill_prompt("free-x", "x", "X Skill")
        self.assertIn("TOPIC the workspace taught", prompt)
        self.assertIn("NOT the teaching process", prompt)
        self.assertIn("name: X Skill", prompt)
        self.assertIn("DISTILLED skills/x/SKILL.md", prompt)

    def test_agent_noise_filter_drops_runtime_not_found_block(self):
        """The benign 'workspace is not materialized' RuntimeError block is
        swallowed; real lines before and after pass through."""
        filt = server.agent_noise_filter()
        block = [
            "real output line",
            "[unexpected] RuntimeError: workspace wd_x_123 is not materialized",
            "    at RuntimeResolver.acquire (file:///main.mjs:1:1)",
            "    at AgentFileHistoryService.endCheckpoint (file:///main.mjs:2:2)",
            "  code: 'runtime.not_found'",
            "}",
            "next real line",
        ]
        kept = [filt(l) for l in block]
        self.assertEqual([l for l in kept if l is not None],
                         ["real output line", "next real line"])
        # unrelated RuntimeErrors still pass
        filt2 = server.agent_noise_filter()
        self.assertEqual(filt2("[unexpected] RuntimeError: something else broke"),
                         "[unexpected] RuntimeError: something else broke")


class ServerWorkspaceListingTests(unittest.TestCase):
    """list_workspaces / workspace_lesson_titles / workspace_state_path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.teach = root / "teach"
        self.teach.mkdir()
        self.patcher = mock.patch.multiple(
            server, TEACH_DIR=self.teach, VIDEOS_JSON=root / "videos.json")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _make_ws(self, name, mission, lessons=(), completed=()):
        ws = self.teach / name
        (ws / "lessons").mkdir(parents=True)
        if mission is not None:
            (ws / "MISSION.md").write_text(mission, encoding="utf-8")
        for f in lessons:
            (ws / "lessons" / f).write_text("<html></html>", encoding="utf-8")
        if completed:
            (ws / ".app_state.json").write_text(
                json.dumps({"completed": list(completed)}), encoding="utf-8")
        return ws

    def test_list_workspaces_titles_counts_progress_and_order(self):
        """Workspaces report mission titles, lesson counts, progress and come
        back newest-mtime-first."""
        older = self._make_ws("free-bar", "# Mission: Bar\n")
        newer = self._make_ws("free-foo", "# Mission: Foo\n",
                              lessons=("one.html", "two.html"),
                              completed=("one.html", "ghost.html"))
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        result = server.list_workspaces()
        self.assertEqual([w["id"] for w in result], ["free-foo", "free-bar"])
        foo = result[0]
        self.assertEqual(foo["title"], "Mission: Foo")
        self.assertEqual(foo["lessons"], [{"file": "one.html"}, {"file": "two.html"}])
        self.assertEqual(foo["completed"], ["one.html"])
        self.assertEqual(foo["progress"], 0.5)
        self.assertFalse(foo["is_video"])
        self.assertIsNone(foo["video_id"])
        bar = result[1]
        self.assertEqual(bar["title"], "Mission: Bar")
        self.assertEqual(bar["lessons"], [])
        self.assertEqual(bar["progress"], 0)

    def test_list_workspaces_empty_dir(self):
        """An empty teach directory yields no workspaces."""
        self.assertEqual(server.list_workspaces(), [])

    def test_workspace_lesson_titles_preference(self):
        """Lesson titles come from <title>, then <h1>, then the file stem."""
        ws = self._make_ws("free-t", "# Mission: T\n")
        (ws / "lessons" / "a.html").write_text(
            "<html><head><title>Bar</title></head></html>", encoding="utf-8")
        (ws / "lessons" / "b.html").write_text(
            "<html><body><h1>Baz</h1></body></html>", encoding="utf-8")
        (ws / "lessons" / "c.html").write_text(
            "<html><body>nothing</body></html>", encoding="utf-8")
        self.assertEqual(server.workspace_lesson_titles(ws),
                         [("a.html", "Bar"), ("b.html", "Baz"), ("c.html", "c")])

    def test_workspace_state_path_lives_in_workspace(self):
        """The state path is the hidden .app_state.json inside the workspace."""
        self.assertEqual(server.workspace_state_path("free-x"),
                         self.teach / "free-x" / ".app_state.json")


class ServerCreateWorkspaceV2Tests(unittest.TestCase):
    """create_workspace with mission_title (topic) and focus_skill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.teach = root / "teach"
        settings = root / "settings.json"
        settings.write_text(json.dumps(
            {"profile_path": str(root / "no-such-profile.md")}), encoding="utf-8")
        self.patcher = mock.patch.multiple(
            server, TEACH_DIR=self.teach, TRANSCRIPT_DIR=root / "transcripts",
            SETTINGS_JSON=settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_mission_title_writes_topic_workflow_mission(self):
        """A freestyle workspace MISSION.md names the topic and marks the
        mission as DRAFT so the teach skill's interview step fires."""
        ws, created = server.create_workspace(
            "free-quantum", None, None, mission_title="Quantum Computing")
        self.assertTrue(created)
        self.assertEqual(ws, "teach/free-quantum")
        mission = (self.teach / "free-quantum" / "MISSION.md").read_text(encoding="utf-8")
        self.assertIn("Quantum Computing", mission)
        self.assertIn("STATUS: DRAFT", mission)
        self.assertIn("interview", mission.lower())

    def test_focus_skill_adds_focus_section_with_description(self):
        """A focused mission names the skill and uses its description."""
        learnings = {"skills": [{"name": "Python", "description": "A language"}],
                     "summary": "S"}
        server.create_workspace("vidA", {"title": "T"}, learnings, focus="Python")
        mission = (self.teach / "vidA" / "MISSION.md").read_text(encoding="utf-8")
        self.assertIn("## Focus skill: Python", mission)
        self.assertIn("A language", mission)

    def test_focus_second_call_never_overwrites_mission(self):
        """Re-creating a workspace with a different focus keeps the original
        MISSION.md untouched."""
        learnings = {"skills": [{"name": "Python"}, {"name": "Rust"}]}
        server.create_workspace("vidA", {"title": "T"}, learnings, focus="Python")
        first = (self.teach / "vidA" / "MISSION.md").read_text(encoding="utf-8")
        server.create_workspace("vidA", {"title": "T"}, learnings, focus="Rust")
        second = (self.teach / "vidA" / "MISSION.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertIn("## Focus skill: Python", second)
        self.assertNotIn("## Focus skill: Rust", second)


class FakeAgentProc:
    """Stand-in for subprocess.Popen result in run_agent_job tests."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 424242
        self.stdout = iter(["line one\n", "line two\n"])
        self.stderr = iter([])
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


class ServerTeachAgentApiTests(unittest.TestCase):
    """HTTP tests for the new teach/agent/skill-library endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        (root / "skills").mkdir()
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        cls.root = root
        cls.data = data

        videos = {
            "vidA": {"id": "vidA", "title": "Alpha Video", "channel": "Chan",
                     "url": "https://www.youtube.com/watch?v=vidA",
                     "published": "20240202", "learned": False, "added": "2024-01-01"},
            "vidB": {"id": "vidB", "title": "Beta Video", "channel": "Chan",
                     "url": "https://www.youtube.com/watch?v=vidB",
                     "published": "20240115", "learned": True, "added": "2024-01-01"},
        }
        (data / "videos.json").write_text(json.dumps({"videos": videos}), encoding="utf-8")
        (data / "learnings" / "vidA.json").write_text(json.dumps({
            "video_id": "vidA", "title": "Alpha Video", "summary": "Sum",
            "skills": [{"name": "Python", "description": "Lang"},
                       {"name": "Rust", "description": "Systems"}],
        }), encoding="utf-8")
        (data / "learnings" / "vidB.json").write_text(json.dumps({
            "video_id": "vidB", "title": "Beta Video", "summary": "Sum",
            "skills": [{"name": "Python"}, {"name": "Testing"}],
        }), encoding="utf-8")
        ws1 = root / "teach" / "ws1"
        ws1.mkdir()
        (ws1 / "lessons").mkdir()
        (ws1 / "MISSION.md").write_text("# Mission: Ws One\n", encoding="utf-8")
        (data / "settings.json").write_text(json.dumps(
            {"profile_path": str(root / "no-such-profile.md")}), encoding="utf-8")

        cls.patchers = [
            mock.patch.object(server, "VIDEOS_JSON", data / "videos.json"),
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "JOBS_JSON", data / "jobs.json"),
            mock.patch.object(server, "PROFILE_SKILLS_JSON", data / "profile-skills.json"),
            mock.patch.object(server, "TEST_RESULTS_JSON", data / "test_results.json"),
            mock.patch.object(server, "LEARNINGS_DIR", data / "learnings"),
            mock.patch.object(server, "TRANSCRIPT_DIR", data / "transcripts"),
            mock.patch.object(server, "THUMB_DIR", data / "thumbs"),
            mock.patch.object(server, "INFO_DIR", data / "info"),
            mock.patch.object(server, "PROGRESS_DIR", data / "progress"),
            mock.patch.object(server, "TEACH_DIR", root / "teach"),
            mock.patch.object(server, "SKILLS_DIR", root / "skills"),
            mock.patch.object(server, "STATIC_DIR", static),
            mock.patch.object(server, "JOBS", {}),
            mock.patch.object(server, "AGENT_JOBS", {}),
            mock.patch.object(server.agentd_client, "backend", return_value="cli"),
        ]
        for p in cls.patchers:
            p.start()

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for p in cls.patchers:
            p.stop()
        cls.tmp.cleanup()

    def api(self, method, path, body=None, want_json=True):
        """Send one request on a fresh connection and return (status, data)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        if want_json and "application/json" in ctype:
            return status, json.loads(raw.decode("utf-8"))
        return status, raw

    # -- teach start

    def test_teach_start_topic_creates_workspace(self):
        """Starting a topic workspace creates the directory and MISSION.md,
        and repeating the same topic reports created=false."""
        status, body = self.api("POST", "/api/teach/start", {"topic": "Quantum Computing"})
        self.assertEqual(status, 200)
        self.assertTrue(body["id"].startswith("free-"))
        ws_dir = self.root / "teach" / body["id"]
        self.assertTrue(ws_dir.is_dir())
        self.assertTrue((ws_dir / "MISSION.md").is_file())
        status, body = self.api("POST", "/api/teach/start", {"topic": "Quantum Computing"})
        self.assertEqual(status, 200)
        self.assertFalse(body["created"])

    def test_teach_start_topic_mission_marks_draft(self):
        """A topic workspace created over HTTP gets a MISSION.md marked
        STATUS: DRAFT so the interview step fires."""
        status, body = self.api("POST", "/api/teach/start", {"topic": "Puppetry"})
        self.assertEqual(status, 200)
        mission = (self.root / "teach" / body["id"] / "MISSION.md").read_text(
            encoding="utf-8")
        self.assertIn("STATUS: DRAFT", mission)
        self.assertIn("Puppetry", mission)

    def test_teach_start_garbage_topic_is_contained(self):
        """A traversal-style topic cannot escape teach/ — it is sanitized
        into a safe 'free-*' workspace name."""
        status, body = self.api("POST", "/api/teach/start", {"topic": "../../evil"})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "free-evil")
        self.assertTrue((self.root / "teach" / "free-evil" / "MISSION.md").is_file())
        self.assertFalse((self.root / "evil").exists())

    def test_teach_start_neither_topic_nor_video_is_400(self):
        """A start request without topic or video_id is rejected with 400."""
        status, _ = self.api("POST", "/api/teach/start", {})
        self.assertEqual(status, 400)

    def test_teach_start_video_with_valid_focus(self):
        """A video workspace with a valid focus_skill gets a focus section in
        its MISSION.md."""
        status, body = self.api("POST", "/api/teach/start",
                                {"video_id": "vidA", "focus_skill": "Python"})
        self.assertEqual(status, 200)
        self.assertTrue(body["created"])
        mission = (self.root / "teach" / "vidA" / "MISSION.md").read_text(encoding="utf-8")
        self.assertIn("## Focus skill: Python", mission)

    def test_teach_start_invalid_focus_is_400(self):
        """A focus_skill that is not in the video's learnings is rejected."""
        status, _ = self.api("POST", "/api/teach/start",
                             {"video_id": "vidA", "focus_skill": "Cooking"})
        self.assertEqual(status, 400)

    def test_teach_start_unknown_video_is_404(self):
        """Starting a workspace for an unknown video returns 404."""
        status, _ = self.api("POST", "/api/teach/start", {"video_id": "vidZZZ"})
        self.assertEqual(status, 404)

    # -- workspace listing + state

    def test_workspaces_lists_lessons_and_progress(self):
        """The workspace list reports lesson files and completion progress."""
        _, body = self.api("POST", "/api/teach/start", {"topic": "Graph Theory"})
        ws_id = body["id"]
        lessons = self.root / "teach" / ws_id / "lessons"
        (lessons / "l1.html").write_text("<title>L1</title>", encoding="utf-8")
        status, _ = self.api("POST", f"/api/teach/{ws_id}/state",
                             {"completed": ["l1.html"]})
        self.assertEqual(status, 200)
        status, body = self.api("GET", "/api/teach/workspaces")
        self.assertEqual(status, 200)
        entry = next(w for w in body["workspaces"] if w["id"] == ws_id)
        self.assertEqual(entry["lessons"], [{"file": "l1.html"}])
        self.assertEqual(entry["completed"], ["l1.html"])
        self.assertEqual(entry["progress"], 1.0)
        self.assertFalse(entry["is_video"])

    def test_ws_state_roundtrip(self):
        """Workspace state saved with POST comes back from GET, and a missing
        state file reads back as empty."""
        status, body = self.api("POST", "/api/teach/ws1/state",
                                {"completed": ["a.html", "b.html"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], ["a.html", "b.html"])
        status, body = self.api("GET", "/api/teach/ws1/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], ["a.html", "b.html"])
        status, body = self.api("GET", "/api/teach/free-ghost/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], [])

    def test_ws_state_bad_name_is_400(self):
        """Encoded or literal dot-dot workspace ids are rejected with 400."""
        status, _ = self.api("GET", "/api/teach/..%2Fevil/state")
        self.assertEqual(status, 400)
        status, _ = self.api("GET", "/api/teach/../state")
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/teach/..%2Fevil/state",
                             {"completed": []})
        self.assertEqual(status, 400)

    def test_ws_state_invalid_body_is_400(self):
        """A state POST without a completed list is rejected with 400."""
        status, _ = self.api("POST", "/api/teach/ws1/state", {"completed": "nope"})
        self.assertEqual(status, 400)

    # -- delete workspace

    def test_delete_workspace_removes_dir_state_and_listing(self):
        """Deleting a workspace returns ok, removes its directory, clears its
        state, and drops it from the workspace list."""
        _, body = self.api("POST", "/api/teach/start", {"topic": "Deletion Demo"})
        ws_id = body["id"]
        self.assertTrue((self.root / "teach" / ws_id).is_dir())
        status, body = self.api("DELETE", f"/api/teach/{ws_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertFalse((self.root / "teach" / ws_id).exists())
        status, body = self.api("GET", f"/api/teach/{ws_id}/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["completed"], [])
        _, body = self.api("GET", "/api/teach/workspaces")
        self.assertNotIn(ws_id, [w["id"] for w in body["workspaces"]])

    def test_delete_workspace_unknown_404_and_bad_id_400(self):
        """Deleting a missing workspace 404s, and unsafe ids are rejected
        with 400."""
        status, _ = self.api("DELETE", "/api/teach/no-such-ws")
        self.assertEqual(status, 404)
        status, _ = self.api("DELETE", "/api/teach/..%2Fevil")
        self.assertEqual(status, 400)
        status, _ = self.api("DELETE", "/api/teach/..")
        self.assertEqual(status, 400)

    def test_delete_workspace_cancels_running_agent_job(self):
        """A running agent job attached to the deleted workspace is marked
        cancelled."""
        _, body = self.api("POST", "/api/teach/start", {"topic": "Cancel Land"})
        ws_id = body["id"]
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["fakejob1"] = {
                "id": "fakejob1", "workspace": ws_id, "status": "running",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, _ = self.api("DELETE", f"/api/teach/{ws_id}")
        self.assertEqual(status, 200)
        self.assertEqual(server.AGENT_JOBS["fakejob1"]["status"], "cancelled")
        self.assertFalse((self.root / "teach" / ws_id).exists())

    def test_delete_workspace_rmtree_failure_is_409(self):
        """If the directory removal fails the endpoint reports a 409 conflict
        and keeps the workspace."""
        _, body = self.api("POST", "/api/teach/start", {"topic": "Sticky Dir"})
        ws_id = body["id"]
        with mock.patch("shutil.rmtree", side_effect=OSError("in use")):
            status, body = self.api("DELETE", f"/api/teach/{ws_id}")
        self.assertEqual(status, 409)
        self.assertIn("error", body)
        self.assertTrue((self.root / "teach" / ws_id).is_dir())

    # -- skill from workspace

    def test_skill_from_workspace_writes_frontmatter_and_content(self):
        """A skill built from a workspace has frontmatter with name and
        source_workspace plus the mission and numbered lesson list."""
        status, body = self.api("POST", "/api/skill/from_workspace",
                                {"workspace": "ws1"})
        self.assertEqual(status, 200)
        skill_file = self.root / "skills" / body["slug"] / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        text = skill_file.read_text(encoding="utf-8")
        self.assertIn("name: Mission: Ws One", text)
        self.assertIn("source_workspace: ws1", text)
        self.assertIn("## Mission", text)
        self.assertIn("Built from teaching workspace `teach/ws1`", text)

    def test_skill_from_workspace_custom_name(self):
        """A custom name overrides the mission title for the skill."""
        status, body = self.api("POST", "/api/skill/from_workspace",
                                {"workspace": "ws1", "name": "My Custom Skill"})
        self.assertEqual(status, 200)
        self.assertEqual(body["slug"], "my-custom-skill")
        text = (self.root / "skills" / body["slug"] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: My Custom Skill", text)

    def test_skill_from_workspace_includes_lesson_list(self):
        """Created skills embed a numbered list of lesson titles."""
        lessons = self.root / "teach" / "ws1" / "lessons"
        (lessons / "intro.html").write_text("<title>Intro</title>", encoding="utf-8")
        (lessons / "deep.html").write_text("<h1>Deep Dive</h1>", encoding="utf-8")
        status, body = self.api("POST", "/api/skill/from_workspace",
                                {"workspace": "ws1", "name": "Lesson Skill"})
        self.assertEqual(status, 200)
        text = (self.root / "skills" / body["slug"] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("1. [Deep Dive](../teach/ws1/lessons/deep.html)", text)
        self.assertIn("2. [Intro](../teach/ws1/lessons/intro.html)", text)

    def test_skill_from_unknown_workspace_is_404(self):
        """Building a skill from a missing workspace returns 404."""
        status, _ = self.api("POST", "/api/skill/from_workspace",
                             {"workspace": "no-such-ws"})
        self.assertEqual(status, 404)

    def test_skill_from_video_workspace_marks_video_learned(self):
        """Turning a VIDEO workspace into a skill marks the video learned and
        tags the skill with its video id."""
        (self.root / "teach" / "vidA").mkdir()  # the video's workspace
        before = {k: v.get("learned") for k, v in server.load_index()["videos"].items()}
        try:
            index = server.load_index()
            index["videos"]["vidA"]["learned"] = False
            server.save_index(index)
            status, body = self.api("POST", "/api/skill/from_workspace",
                                    {"workspace": "vidA", "name": "Video Skill"})
            self.assertEqual(status, 200)
            self.assertTrue(server.load_index()["videos"]["vidA"]["learned"])
            text = (self.root / "skills" / body["slug"] / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("video_id: vidA", text)
            self.assertIn("source_workspace: vidA", text)
        finally:
            index = server.load_index()
            for k, learned in before.items():
                index["videos"][k]["learned"] = learned
            server.save_index(index)
            import shutil
            shutil.rmtree(self.root / "teach" / "vidA", ignore_errors=True)

    def test_skill_from_freestyle_workspace_leaves_videos_untouched(self):
        """A freestyle workspace is not a video: no learned flag, no video_id."""
        before = {k: v.get("learned") for k, v in server.load_index()["videos"].items()}
        try:
            index = server.load_index()  # reset in case a sibling test flipped it
            for v in index["videos"].values():
                v["learned"] = False
            server.save_index(index)
            status, body = self.api("POST", "/api/skill/from_workspace",
                                    {"workspace": "ws1", "name": "Fs Skill"})
            self.assertEqual(status, 200)
            text = (self.root / "skills" / body["slug"] / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("video_id:", text)
            for v in server.load_index()["videos"].values():
                self.assertFalse(v["learned"])
        finally:
            index = server.load_index()
            for k, learned in before.items():
                index["videos"][k]["learned"] = learned
            server.save_index(index)

    def test_skill_from_workspace_bad_name_is_400(self):
        """A workspace id that fails the safety check is rejected with 400."""
        status, _ = self.api("POST", "/api/skill/from_workspace",
                             {"workspace": "../escape"})
        self.assertEqual(status, 400)

    def test_skill_from_workspace_distill_flag_no_cli(self):
        """With distill:true but no CLI, the skill is still created and no
        job_id is returned."""
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, body = self.api("POST", "/api/skill/from_workspace",
                                    {"workspace": "ws1", "name": "Distill Me",
                                     "distill": True})
        self.assertEqual(status, 200)
        self.assertNotIn("job_id", body)
        self.assertTrue((self.root / "skills" / body["slug"] / "SKILL.md").is_file())

    def test_skill_from_workspace_distill_spawns_job(self):
        """With distill:true and a CLI present, a distillation job is queued
        for the workspace."""
        with server.AGENT_JOBS_LOCK:  # drop leftover ws1 jobs from other tests
            for jid in [k for k, v in server.AGENT_JOBS.items()
                        if v.get("workspace") == "ws1"]:
                server.AGENT_JOBS.pop(jid, None)
        fake_popen = mock.MagicMock(side_effect=FakeAgentProc)
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"), \
             mock.patch.object(server.subprocess, "Popen", fake_popen):
            status, body = self.api("POST", "/api/skill/from_workspace",
                                    {"workspace": "ws1", "name": "Distill Job",
                                     "distill": True})
        self.assertEqual(status, 200)
        self.assertIn("job_id", body)
        with server.AGENT_JOBS_LOCK:
            job = server.AGENT_JOBS.get(body["job_id"])
            self.assertEqual(job["workspace"], "ws1")
            server.AGENT_JOBS.pop(body["job_id"], None)

    # -- file preview (structure tab)

    def test_file_preview_returns_content(self):
        """A text file under the root previews with its content intact."""
        (self.root / "notes.md").write_text("# Hello\nsome content", encoding="utf-8")
        with mock.patch.object(server, "ROOT", self.root):
            status, body = self.api("GET", "/api/file?path=notes.md")
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "# Hello\nsome content")
        self.assertFalse(body["truncated"])
        self.assertFalse(body["full"])

    def test_file_preview_truncates_long_file(self):
        """Content beyond the preview cap is cut and flagged truncated."""
        (self.root / "big.json").write_text("x" * 30_000, encoding="utf-8")
        with mock.patch.object(server, "ROOT", self.root):
            status, body = self.api("GET", "/api/file?path=big.json")
            self.assertEqual(status, 200)
            self.assertTrue(body["truncated"])
            self.assertEqual(len(body["content"]), server.PREVIEW_CHARS)
            status, full = self.api("GET", "/api/file?path=big.json&full=1")
        self.assertEqual(status, 200)
        self.assertFalse(full["truncated"])
        self.assertEqual(len(full["content"]), 30_000)

    def test_file_preview_rejects_escape_and_binary(self):
        """Dot-dot paths are a 400 and non-text extensions a 415."""
        with mock.patch.object(server, "ROOT", self.root):
            status, _ = self.api("GET", "/api/file?path=../outside.txt")
            self.assertEqual(status, 400)
            (self.root / "thumb.png").write_bytes(b"\x89PNG")
            status, _ = self.api("GET", "/api/file?path=thumb.png")
        self.assertEqual(status, 415)

    # -- skill install to ~/.kimi-code/skills

    def test_skill_install_copies_to_kimi_dir(self):
        """Installing a skill copies SKILL.md into the Kimi user skills dir,
        returns the /skill invocation name, and /api/skills then reports the
        skill as installed."""
        skill_dir = self.root / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: My Skill\n---\n", encoding="utf-8")
        kimi_dir = self.root / "kimi-home" / "skills"
        with mock.patch.object(server, "KIMI_SKILLS_DIR", kimi_dir):
            status, body = self.api("POST", "/api/skill/install", {"slug": "my-skill"})
            self.assertEqual(status, 200)
            self.assertTrue((kimi_dir / "my-skill" / "SKILL.md").is_file())
            self.assertEqual(body["invoke"], "/skill:my-skill")
            status, listing = self.api("GET", "/api/skills")
        self.assertEqual(status, 200)
        mine = next(s for s in listing["skills"] if s["slug"] == "my-skill")
        self.assertTrue(mine["installed"])
        self.assertFalse(mine["installed_stale"])

    def test_skill_install_bad_slug_and_unknown(self):
        """Bad slugs are 400 and missing skills 404."""
        status, _ = self.api("POST", "/api/skill/install", {"slug": "../evil"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/skill/install", {"slug": "no-such"})
        self.assertEqual(status, 404)

    # -- agent status / run / job / cancel

    def test_agent_status_available_and_unavailable(self):
        """/api/agent/status reflects whether the kimi CLI is on PATH."""
        with mock.patch.object(server, "find_kimi", return_value="/usr/local/bin/kimi"):
            status, body = self.api("GET", "/api/agent/status")
            self.assertEqual(status, 200)
            self.assertTrue(body["available"])
            self.assertEqual(body["exe"], "/usr/local/bin/kimi")
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, body = self.api("GET", "/api/agent/status")
            self.assertEqual(status, 200)
            self.assertFalse(body["available"])
            self.assertIsNone(body["exe"])

    def test_agent_run_completes_with_fake_process(self):
        """An agent run streams the fake process output into the job record
        and finishes with status done."""
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"), \
             mock.patch.object(server.subprocess, "Popen", FakeAgentProc):
            status, body = self.api("POST", "/api/agent/run",
                                    {"workspace": "ws1", "message": "summarize",
                                     "new_session": True})
            self.assertEqual(status, 200)
            job_id = body["job_id"]
            deadline = time.monotonic() + 5
            job = {}
            while time.monotonic() < deadline:
                status, job = self.api("GET", f"/api/agent/job/{job_id}")
                self.assertEqual(status, 200)
                if job["status"] in ("done", "error"):
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["exit_code"], 0)
            self.assertEqual(job["workspace"], "ws1")
            self.assertIn("cmd_preview", job)
            self.assertIn("kimi -p", job["cmd_preview"])
            texts = [l["x"] for l in job["lines"]]
            self.assertIn("line one", texts)
            self.assertIn("line two", texts)

    def test_agent_run_missing_message_is_400(self):
        """An agent run without a message is rejected with 400."""
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"):
            status, _ = self.api("POST", "/api/agent/run", {"workspace": "ws1"})
            self.assertEqual(status, 400)

    def test_agent_run_unknown_workspace_is_404(self):
        """An agent run in a missing workspace returns 404."""
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"):
            status, _ = self.api("POST", "/api/agent/run",
                                 {"workspace": "no-such", "message": "hi"})
            self.assertEqual(status, 404)

    def test_agent_run_without_kimi_is_503(self):
        """An agent run when the kimi CLI is missing returns 503."""
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, _ = self.api("POST", "/api/agent/run",
                                 {"workspace": "ws1", "message": "hi"})
            self.assertEqual(status, 503)

    def test_agent_run_conflict_returns_409(self):
        """Starting a second run in a workspace with a live job is rejected
        with 409 and leaves the existing job untouched."""
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["busyjob"] = {
                "id": "busyjob", "workspace": "ws1", "status": "running",
                "lines": [{"t": "out", "x": "prior output"}], "exit_code": None,
                "pid": None, "started": int(time.time()), "ended": None,
            }
            jobs_before = len(server.AGENT_JOBS)
        fake_popen = mock.MagicMock(side_effect=FakeAgentProc)
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"), \
             mock.patch.object(server.subprocess, "Popen", fake_popen):
            status, body = self.api("POST", "/api/agent/run",
                                    {"workspace": "ws1", "message": "again"})
        self.assertEqual(status, 409)
        self.assertIn("still working", body["error"])
        fake_popen.assert_not_called()
        self.assertEqual(len(server.AGENT_JOBS), jobs_before)
        self.assertEqual(server.AGENT_JOBS["busyjob"]["status"], "running")
        self.assertEqual(server.AGENT_JOBS["busyjob"]["lines"],
                         [{"t": "out", "x": "prior output"}])

    def test_agent_job_unknown_is_404(self):
        """Polling an unknown agent job returns 404."""
        status, _ = self.api("GET", "/api/agent/job/doesnotexist")
        self.assertEqual(status, 404)

    def test_agent_cancel_known_and_unknown(self):
        """Cancelling an existing job is ok; an unknown job id returns 404."""
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["jobtocancel"] = {
                "id": "jobtocancel", "workspace": "ws1", "status": "running",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, _ = self.api("POST", "/api/agent/job/jobtocancel/cancel")
        self.assertEqual(status, 200)
        status, _ = self.api("POST", "/api/agent/job/neverexisted/cancel")
        self.assertEqual(status, 404)

    # -- skills library endpoint

    def test_skills_library_endpoint_buckets(self):
        """/api/skills/library returns known skills from the learned video and
        learnable ones from the unlearned video."""
        status, body = self.api("GET", "/api/skills/library")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in body["known"]], ["Python", "Testing"])
        self.assertEqual([s["name"] for s in body["learnable"]], ["Python", "Rust"])
        known_py = next(s for s in body["known"] if s["name"] == "Python")
        self.assertEqual(known_py["videos"][0]["id"], "vidB")
        self.assertTrue(known_py["videos"][0]["learned"])
        learn_py = next(s for s in body["learnable"] if s["name"] == "Python")
        self.assertEqual(learn_py["videos"][0]["id"], "vidA")
        self.assertFalse(learn_py["videos"][0]["learned"])


# ---------------------------------------------------------------- server v3: structure tab

MOD_PY = '''"""Module docstring."""
def alpha():
    pass


def beta():
    return alpha()


class Gamma:
    def method(self):
        pass
'''

APP_JS = '''function one() { return 1; }
async function two() { return 2; }
'''


class ServerStructureTests(unittest.TestCase):
    """build_structure / _structure_symbols on a synthetic temp tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        pkg = self.root / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text(MOD_PY, encoding="utf-8")
        (pkg / "app.js").write_text(APP_JS, encoding="utf-8")
        many = self.root / "many"
        many.mkdir()
        for i in range(30):
            (many / f"f{i:02d}.txt").write_text("x\n", encoding="utf-8")
        few = self.root / "few"
        few.mkdir()
        (few / "a.txt").write_text("a\n", encoding="utf-8")
        (few / "b.txt").write_text("b\n", encoding="utf-8")
        venv = self.root / ".venv"
        venv.mkdir()
        (venv / "lib.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "junk.py").write_text("def junk():\n    pass\n", encoding="utf-8")
        (self.root / "stray.pyc").write_bytes(b"\x00" * 16)
        (self.root / "README.md").write_text("# Read me\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _children_by_name(self, node):
        return {c["name"]: c for c in node["children"]}

    def test_build_structure_tree_symbols_lines_and_paths(self):
        """A nested tree builds with forward-slash paths, and a .py file gets
        exactly its three top-level symbols in order plus lines and size."""
        tree = server.build_structure(self.root)
        self.assertEqual(tree["name"], self.root.name)
        self.assertEqual(tree["type"], "dir")
        pkg = self._children_by_name(tree)["pkg"]
        self.assertEqual(pkg["type"], "dir")
        self.assertEqual(pkg["path"], "pkg")
        files = self._children_by_name(pkg)
        mod = files["mod.py"]
        self.assertEqual(mod["path"], "pkg/mod.py")
        self.assertEqual(mod["symbols"], ["alpha", "beta", "Gamma"])
        self.assertGreater(mod["lines"], 0)
        self.assertGreater(mod["size"], 0)
        self.assertEqual(files["app.js"]["symbols"], ["one", "two"])

    def test_build_structure_child_count_over_threshold(self):
        """A folder with 30 files reports child_count and no children, while a
        small folder enumerates its children."""
        tree = server.build_structure(self.root)
        children = self._children_by_name(tree)
        many = children["many"]
        self.assertEqual(many["child_count"], 30)
        self.assertNotIn("children", many)
        few = children["few"]
        self.assertIn("children", few)
        self.assertEqual(len(few["children"]), 2)

    def test_build_structure_excludes_venv_pycache_and_pyc(self):
        """Excluded directories and .pyc files never appear in the tree."""
        tree = server.build_structure(self.root)
        names = set(self._children_by_name(tree))
        self.assertNotIn(".venv", names)
        self.assertNotIn("__pycache__", names)
        self.assertNotIn("stray.pyc", names)
        self.assertIn("pkg", names)
        self.assertIn("README.md", names)

    def test_build_structure_descriptions_from_patched_dict(self):
        """Descriptions come from STRUCTURE_DESCRIPTIONS by rel path and
        default to an empty string."""
        with mock.patch.object(server, "STRUCTURE_DESCRIPTIONS",
                               {"pkg/mod.py": "Does things", "pkg": "The package"}):
            tree = server.build_structure(self.root)
            children = self._children_by_name(tree)
            self.assertEqual(children["pkg"]["description"], "The package")
            mod = self._children_by_name(children["pkg"])["mod.py"]
            self.assertEqual(mod["description"], "Does things")
            self.assertEqual(children["README.md"]["description"], "")

    def test_structure_symbols_skip_nested_and_unsupported(self):
        """Indented defs and methods are not top-level symbols, and
        unsupported file types yield no symbols."""
        nested = self.root / "nested.py"
        nested.write_text(
            "def outer():\n    def inner():\n        pass\n\n\n"
            "class Klass:\n    def m(self):\n        pass\n",
            encoding="utf-8")
        self.assertEqual(server._structure_symbols(nested), ["outer", "Klass"])
        self.assertEqual(server._structure_symbols(self.root / "README.md"), [])
        css = self.root / "style.css"
        css.write_text("body {}\n", encoding="utf-8")
        self.assertEqual(server._structure_symbols(css), [])

    def test_structure_symbols_capped_at_twelve(self):
        """At most 12 symbols are returned even when a file defines more."""
        big = self.root / "big.py"
        big.write_text("".join(f"def f{i}():\n    pass\n\n" for i in range(15)),
                       encoding="utf-8")
        symbols = server._structure_symbols(big)
        self.assertEqual(len(symbols), 12)
        self.assertEqual(symbols[0], "f0")
        self.assertEqual(symbols[-1], "f11")


class ServerAppStructureApiTests(unittest.TestCase):
    """GET /api/app-structure against the real (read-only) project root."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def api(self, method, path):
        """Send one request on a fresh connection and return (status, json)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        status = resp.status
        conn.close()
        return status, body

    def test_app_structure_endpoint_describes_the_project(self):
        """The endpoint returns the project tree: correct root name, the
        known top-level entries, a documented server.py with symbols, and a
        summarized data/learnings folder."""
        status, body = self.api("GET", "/api/app-structure")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "yt learning loader")
        children = {c["name"]: c for c in body["children"]}
        for expected in ("server.py", "scraper.py", "static", "tests", "data"):
            self.assertIn(expected, children)
        server_node = children["server.py"]
        self.assertTrue(server_node["description"])
        self.assertIn("find_kimi", server_node["symbols"])
        data_children = {c["name"]: c for c in children["data"]["children"]}
        learnings = data_children["learnings"]
        self.assertIn("child_count", learnings)
        self.assertGreater(learnings["child_count"], 100)


# ---------------------------------------------------------------- runner reporting + profile-seeded missions

class RunnerModuleGroupTests(unittest.TestCase):
    """module_group naming and Collector entries from tests/run.py."""

    def _instance(self, cls_name):
        """A throwaway TestCase instance with the given class name."""
        cls = type(cls_name, (unittest.TestCase,), {})
        return cls("runTest")

    def test_module_group_documented_mappings(self):
        """The documented class-name mappings produce the human-readable
        module names."""
        self.assertEqual(module_group(self._instance("ServerTeachAgentApiTests")),
                         "Teach Agent Api")
        self.assertEqual(module_group(self._instance("TestScraperParsers")),
                         "Scraper Parsers")
        self.assertEqual(module_group(self._instance("ShutdownApiTests")),
                         "Shutdown Api")

    def test_module_group_general_guard(self):
        """A class name that strips down to nothing falls back to 'General'."""
        self.assertEqual(module_group(self._instance("Test")), "General")
        self.assertEqual(module_group(self._instance("Server")), "General")

    def test_module_group_uses_only_the_class_name(self):
        """A class defined inside the test still maps from its own name."""
        class ServerInnerDemoTests(unittest.TestCase):
            def test_inner(self):
                pass

        self.assertEqual(module_group(ServerInnerDemoTests("test_inner")),
                         "Inner Demo")

    def test_collector_records_module_group_and_description(self):
        """A suite run through the Collector emits an entry whose module is
        the derived group name and whose description is the docstring."""
        class ServerDemoFeatureTests(unittest.TestCase):
            def test_demo_thing(self):
                """A demo passing test."""

        suite = unittest.TestSuite([ServerDemoFeatureTests("test_demo_thing")])
        collector = Collector()
        suite.run(collector)
        self.assertEqual(len(collector.entries), 1)
        entry = collector.entries[0]
        self.assertEqual(entry["module"], "Demo Feature")
        self.assertEqual(entry["description"], "A demo passing test.")
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["class"], "ServerDemoFeatureTests")


PROFILE_SNIPPET = "Nils builds learning software with Python."


class ServerProfileExcerptTests(unittest.TestCase):
    """profile_excerpt trimming against a temp profile and settings file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profile = root / "profile.md"
        lines = ["# Learner Profile", "", "> a secret aside", "---", "Name: Nils"]
        lines += [f"Fact line {i}." for i in range(40)]
        self.profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.settings = root / "settings.json"
        self.settings.write_text(
            json.dumps({"profile_path": str(self.profile)}), encoding="utf-8")
        self.patcher = mock.patch.object(server, "SETTINGS_JSON", self.settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_excerpt_drops_blockquotes_and_separators(self):
        """Blockquoted prose and --- separator lines never appear in the
        excerpt, while real content lines do."""
        excerpt = server.profile_excerpt(max_chars=100_000)
        self.assertNotIn("a secret aside", excerpt)
        for line in excerpt.splitlines():
            self.assertFalse(line.startswith("---"), line)
        self.assertIn("Name: Nils", excerpt)
        self.assertIn("Fact line 39.", excerpt)

    def test_excerpt_is_capped_near_max_chars(self):
        """The excerpt stops accumulating once the character cap is crossed."""
        full = server.profile_excerpt(max_chars=100_000)
        excerpt = server.profile_excerpt(max_chars=200)
        self.assertLess(len(excerpt), len(full))
        self.assertGreaterEqual(len(excerpt), 150)
        self.assertLessEqual(len(excerpt), 260)

    def test_excerpt_missing_profile_file_is_empty(self):
        """A profile path that does not exist yields an empty excerpt."""
        self.settings.write_text(json.dumps(
            {"profile_path": str(Path(self.tmp.name) / "gone.md")}), encoding="utf-8")
        self.assertEqual(server.profile_excerpt(), "")


class ServerMissionProfileSectionTests(unittest.TestCase):
    """The learner-profile section appended to new MISSION.md files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.teach = root / "teach"
        self.settings = root / "settings.json"
        profile = root / "profile.md"
        profile.write_text(f"# Nils\n\n{PROFILE_SNIPPET}\n", encoding="utf-8")
        self.settings.write_text(
            json.dumps({"profile_path": str(profile)}), encoding="utf-8")
        self.patcher = mock.patch.multiple(
            server, TEACH_DIR=self.teach, TRANSCRIPT_DIR=root / "transcripts",
            SETTINGS_JSON=self.settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _mission(self, ws_id):
        return (self.teach / ws_id / "MISSION.md").read_text(encoding="utf-8")

    def test_all_mission_variants_embed_profile_section(self):
        """Topic, learnings and generic missions all gain a fenced
        '## Learner profile' section with the profile text."""
        server.create_workspace("free-topic", None, None, mission_title="Crochet")
        topic = self._mission("free-topic")
        self.assertIn("## Learner profile", topic)
        self.assertIn(PROFILE_SNIPPET, topic)

        server.create_workspace("vidP", {"title": "T"},
                                {"summary": "S", "skills": [{"name": "Python"}]})
        learned = self._mission("vidP")
        self.assertIn("## Skills you will build", learned)
        self.assertIn("## Learner profile", learned)
        self.assertIn(PROFILE_SNIPPET, learned)

        server.create_workspace("vidG", {"title": "T"}, None)
        generic = self._mission("vidG")
        self.assertIn("Learn this video thoroughly", generic)
        self.assertIn("## Learner profile", generic)
        self.assertIn(PROFILE_SNIPPET, generic)

    def test_existing_mission_is_not_given_a_profile_section(self):
        """A workspace whose MISSION.md already exists is left untouched."""
        ws = self.teach / "vidE"
        ws.mkdir(parents=True)
        (ws / "MISSION.md").write_text("# Custom mission\n", encoding="utf-8")
        server.create_workspace("vidE", {"title": "T"}, {"skills": []})
        self.assertEqual(self._mission("vidE"), "# Custom mission\n")
        self.assertNotIn("## Learner profile", self._mission("vidE"))

    def test_missing_profile_still_writes_mission_without_section(self):
        """When the profile cannot be read the mission writes normally with
        no profile section and no exception."""
        self.settings.write_text(json.dumps(
            {"profile_path": str(Path(self.tmp.name) / "gone.md")}), encoding="utf-8")
        ws, created = server.create_workspace(
            "free-nop", None, None, mission_title="Birding")
        self.assertTrue(created)
        mission = self._mission("free-nop")
        self.assertIn("Birding", mission)
        self.assertNotIn("## Learner profile", mission)


if __name__ == "__main__":
    unittest.main()
