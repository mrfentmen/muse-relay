#!/usr/bin/env python3
"""Tests for brief.py — the context an instance wakes up into.

The brief is the answer to "what do I need to know right now?", so the
tests are about what it must not miss: a mention waiting on you, a status
that says someone is heads-down, the topic, and the job queue. Two
properties matter as much as the contents: asking for a brief must not
eat messages poll.py has not shown yet, and a broken piece (no job
queue, no presence) must degrade one section instead of the whole brief.
"""
import argparse
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase  # noqa: E402
import brief  # noqa: E402
import jobs  # noqa: E402
import relay_common as rc  # noqa: E402


def cli_args(*argv):
    """Namespace for brief.build carrying the CLI's own defaults.

    build() takes a parsed namespace, so the tests that are about the
    brief's *contents* parse real flags into one here; the tests that are
    about the command line go through brief.main instead.
    """
    ns = argparse.Namespace(room=None, limit=brief.DEFAULT_LIMIT, watch=[],
                            json=False, consume=False, no_jobs=False,
                            jobs_limit=brief.DEFAULT_JOBS_LIMIT)
    argv, i = list(argv), 0
    while i < len(argv):
        flag = argv[i].lstrip("-").replace("-", "_")
        if flag == "no_jobs":
            setattr(ns, flag, True)
            i += 1
            continue
        value = argv[i + 1]
        setattr(ns, flag, int(value) if flag in ("limit", "jobs_limit")
                else value)
        i += 2
    return ns


class BriefBuildTest(RelayTestCase):
    def run_brief(self, *args):
        return self.run_cli(brief.main, list(args))

    @staticmethod
    def args(*argv):
        return cli_args(*argv)

    def post_job(self, title, **kw):
        argv = ["post", "--title", title, "--spec", "s"]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", str(v)]
        rc, out, err = self.run_cli(jobs.main, argv)
        self.assertEqual(rc, 0, err)
        return out.split()[-1]

    # -- what the brief has to contain ---------------------------------
    def test_recent_messages_mark_which_are_mine(self):
        self.fake.bus_push("alice: hello")
        self.fake.bus_push("tester: mine")
        b = brief.build(self.args())
        texts = [r["text"] for r in b["recent"]]
        self.assertEqual(texts, ["alice: hello", "tester: mine"])
        self.assertFalse(b["recent"][0]["mine"])
        self.assertTrue(b["recent"][1]["mine"])

    def test_mentions_and_unanswered_mentions(self):
        self.fake.bus_push("alice: @tester ship it")
        self.fake.bus_push("bob: unrelated")
        b = brief.build(self.args())
        self.assertEqual([r["text"] for r in b["mentions"]],
                         ["alice: @tester ship it"])
        self.assertEqual([r["text"] for r in b["unanswered"]],
                         ["alice: @tester ship it"])

    def test_a_mention_stops_waiting_once_you_post_after_it(self):
        self.fake.bus_push("alice: @tester ship it")
        self.fake.bus_push("tester: on it")
        b = brief.build(self.args())
        # it is still a mention (it happened), but nothing is waiting:
        # the brief stopped pointing at it the moment we replied.
        self.assertEqual([r["text"] for r in b["mentions"]],
                         ["alice: @tester ship it"])
        self.assertEqual(b["unanswered"], [])

    def test_your_own_mentions_of_others_are_not_for_you(self):
        self.fake.bus_push("tester: @alice over to you")
        b = brief.build(self.args())
        self.assertEqual(b["mentions"], [])

    def test_broadcast_mention_counts(self):
        self.fake.bus_push("alice: @all standup")
        b = brief.build(self.args())
        self.assertEqual(len(b["mentions"]), 1)

    def test_presence_and_status_are_included(self):
        rc.presence_beat("muse-bus")
        rc.status_set("heads down")
        b = brief.build(self.args())
        self.assertIn("tester", [p["nick"] for p in b["online"]])
        self.assertEqual(b["online"][0]["status"], "heads down")

    def test_topic_comes_off_the_bus(self):
        self.fake.bus_push("alice: TOPIC: parser rewrite")
        b = brief.build(self.args())
        self.assertEqual(b["topic"], "parser rewrite")

    def test_topic_is_the_newest_line_not_the_first(self):
        self.fake.bus_push("alice: TOPIC: old plan")
        self.fake.bus_push("bob: TOPIC: new plan")
        b = brief.build(self.args())
        self.assertEqual(b["topic"], "new plan")

    def test_room_description(self):
        self.fake.api_get("hset/muse-bus:roominfo:muse-bus:room:eng/desc/"
                          "the%20eng%20room")
        b = brief.build(self.args("--room", "eng"))
        self.assertEqual(b["desc"], "the eng room")

    def test_limit_bounds_the_tail(self):
        for i in range(10):
            self.fake.bus_push(f"alice: m{i}")
        b = brief.build(self.args("--limit", "3"))
        self.assertEqual([r["text"] for r in b["recent"]],
                         ["alice: m7", "alice: m8", "alice: m9"])

    def test_room_selection(self):
        self.fake.bus_push("alice: main line")
        self.fake.bus_push("bob: eng line", key="muse-bus:room:eng")
        b = brief.build(self.args("--room", "eng"))
        self.assertEqual([r["text"] for r in b["recent"]], ["bob: eng line"])

    # -- job queue ------------------------------------------------------
    def test_jobs_section_reports_counts_and_rows(self):
        self.post_job("wire the parser")
        b = brief.build(self.args())
        self.assertEqual(b["jobs"]["counts"].get("open"), 1)
        self.assertEqual(b["jobs"]["rows"][0]["title"], "wire the parser")

    def test_jobs_section_can_be_skipped(self):
        b = brief.build(self.args("--no-jobs"))
        self.assertEqual(b["jobs"], {})

    def test_a_broken_job_queue_degrades_one_section(self):
        self.fake.bus_push("alice: hello")
        with mock.patch.object(jobs, "job_rows",
                              side_effect=RuntimeError("down")):
            b = brief.build(self.args())
        self.assertIn("error", b["jobs"])
        self.assertIn("JOBS (unavailable", brief.render(b))
        self.assertEqual([r["text"] for r in b["recent"]], ["alice: hello"])

    # -- reading side effects -------------------------------------------
    def test_peeking_does_not_advance_the_cursor(self):
        self.fake.bus_push("alice: hello")
        self.run_brief("--json")
        # poll.py must still see it: the brief only looked
        import poll
        _, new, _ = poll.poll_one("muse-bus", poll.seen_path(None))
        self.assertEqual([m for m in new], ["alice: hello"])

    def test_consume_advances_the_cursor(self):
        self.fake.bus_push("alice: hello")
        self.run_brief("--consume")
        import poll
        _, new, _ = poll.poll_one("muse-bus", poll.seen_path(None))
        self.assertEqual(new, [])

    def test_unread_counts_what_the_cursor_has_not_seen(self):
        self.fake.bus_push("alice: one")
        self.fake.bus_push("bob: two")
        b = brief.build(self.args())
        self.assertEqual(b["unread"], 2)

    def test_trim_loss_is_surfaced(self):
        for i in range(10):
            self.fake.bus_push(f"alice: m{i}")
        rc.bus_trim(key="muse-bus", keep=2)
        b = brief.build(self.args())
        self.assertEqual(b["lost"], 8)
        self.assertIn("trimmed off the bus", brief.render(b))


class BriefCliTest(RelayTestCase):
    def run_brief(self, *args):
        return self.run_cli(brief.main, list(args))

    def test_text_output_has_the_sections(self):
        self.fake.bus_push("alice: @tester ping")
        rc.presence_beat("muse-bus")
        code, out, _ = self.run_brief()
        self.assertEqual(code, 0)
        for section in ("BUS BRIEF", "room: main", "WHO'S ONLINE",
                        "WAITING ON YOU", "RECENT"):
            self.assertIn(section, out)

    def test_json_output_round_trips(self):
        self.fake.bus_push("alice: hello")
        code, out, _ = self.run_brief("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["nick"], "tester")
        self.assertEqual(data["room"], "main")
        self.assertEqual(data["recent"][0]["from"], "alice")

    def test_empty_bus_still_produces_a_brief(self):
        code, out, _ = self.run_brief()
        self.assertEqual(code, 0)
        self.assertIn("RECENT (nothing)", out)
        self.assertIn("unread: 0", out)

    def test_presence_failure_is_a_note_not_a_crash(self):
        with mock.patch.object(brief, "presence_list",
                               side_effect=RuntimeError("no presence")):
            b = brief.build(cli_args())
        self.assertEqual(b["online"], [])
        self.assertIn("presence unavailable", b["note"])
        self.assertIn("none", brief.render(b))

    def test_bad_arguments_are_rejected(self):
        self.assertEqual(self.run_brief("--limit", "0")[0], 2)
        self.assertEqual(self.run_brief("--jobs-limit", "0")[0], 2)
        code, _, err = self.run_brief("--room", "!!!")
        self.assertEqual(code, 2)
        self.assertIn("invalid room", err)

    def test_transport_failure_is_reported_not_traced(self):
        with mock.patch.object(brief, "bus_read",
                               side_effect=RuntimeError("redis down")):
            code, _, err = self.run_brief()
        self.assertEqual(code, 2)
        self.assertIn("RELAY_ERROR", err)


if __name__ == "__main__":
    unittest.main()
