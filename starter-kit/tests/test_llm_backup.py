"""A model call that gets no reply is given up early and asked a second time, against a fake client (no network)."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402

ANSWER = '{"thought": "ok", "actions": []}'


def reply(content: str = ANSWER) -> SimpleNamespace:
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None),
                           choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


class Completions:
    """Answers each request as scripted: "ok", "hang" (never answers), "slow" (answers after 0.15 s) or "error"."""

    def __init__(self, *script: str):
        self.script, self.sent, self.cancelled = list(script), 0, 0

    async def create(self, **kwargs):
        self.sent += 1
        how = self.script.pop(0) if self.script else "ok"
        try:
            if how == "hang":
                await asyncio.Event().wait()
            if how == "slow":
                await asyncio.sleep(0.15)
            if how == "error":
                raise RuntimeError("connection reset")
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return reply()


class Backups(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = dict(llm.STATS)
        for key in llm.STATS:
            llm.STATS[key] = 0
        llm._backups["flying"] = 0
        self.warm = dict(llm._warm)
        llm._warm.clear()
        llm._warm["done"] = True

    def tearDown(self):
        llm.STATS.update(self.saved)
        llm._warm.clear()
        llm._warm.update(self.warm)

    async def ask(self, completions: Completions, cap: float = 0.4, backup_after: float = 0.05, **more):
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch.object(llm, "_session", return_value=(client, asyncio.Semaphore(4))), \
             patch.object(llm, "STEP_CAP", cap), patch.object(llm, "STEP_HEDGE", backup_after), \
             patch.object(llm, "LONG_CAP", cap), patch.object(llm, "LONG_HEDGE", backup_after), \
             patch.object(llm, "RETRY_PAUSE", (0.0, 0.0)):
            return await llm.chat_json([{"role": "user", "content": "next step"}], think=False, label="erp", **more)

    async def test_a_quick_answer_needs_one_request(self):
        completions = Completions("ok")
        self.assertEqual(await self.ask(completions), {"thought": "ok", "actions": []})
        self.assertEqual((completions.sent, llm.STATS["backups"], llm.STATS["failed"]), (1, 0, 0))

    async def test_a_lost_request_is_answered_by_its_backup_and_dropped(self):
        completions = Completions("hang", "ok")
        started = asyncio.get_running_loop().time()
        self.assertEqual(await self.ask(completions), {"thought": "ok", "actions": []})
        self.assertLess(asyncio.get_running_loop().time() - started, 0.3)  # not the cap, let alone the old 120 s
        self.assertEqual((completions.sent, completions.cancelled), (2, 1))
        self.assertEqual((llm.STATS["backups"], llm.STATS["rescued"], llm.STATS["failed"]), (1, 1, 0))
        self.assertEqual(llm._backups["flying"], 0)

    async def test_a_slow_first_request_can_still_win_and_the_backup_is_dropped(self):
        completions = Completions("slow", "hang")
        self.assertEqual(await self.ask(completions), {"thought": "ok", "actions": []})
        self.assertEqual((completions.sent, completions.cancelled, llm.STATS["rescued"]), (2, 1, 0))

    async def test_when_nothing_comes_back_the_call_is_given_up_at_the_cap_and_asked_again(self):
        completions = Completions("hang", "hang", "ok")
        self.assertEqual(await self.ask(completions, cap=0.2), {"thought": "ok", "actions": []})
        self.assertEqual((completions.sent, completions.cancelled, llm.STATS["failed"]), (3, 2, 1))

    async def test_a_request_that_fails_at_once_is_asked_again_without_a_backup(self):
        completions = Completions("error", "ok")
        with patch.object(llm.asyncio, "sleep", lambda _: _none()):  # no backoff wait in the test
            self.assertEqual(await self.ask(completions), {"thought": "ok", "actions": []})
        self.assertEqual((completions.sent, llm.STATS["backups"], llm.STATS["failed"]), (2, 0, 1))

    async def test_backups_are_rationed(self):
        llm._backups["flying"] = llm.MAX_BACKUPS_FLYING  # as many in flight as allowed: this call gets none
        completions = Completions("slow")
        self.assertEqual(await self.ask(completions), {"thought": "ok", "actions": []})
        self.assertEqual((completions.sent, llm.STATS["backups"]), (1, 0))
        llm._backups["flying"] = 0
        llm.STATS["calls"], llm.STATS["backups"] = 10, 5  # the share is used up
        self.assertFalse(llm._may_send_backup())

    async def test_the_limits_follow_the_kind_of_call(self):
        self.assertEqual(llm._limits(False, 6000, "erp"), (llm.STEP_CAP, llm.STEP_HEDGE))
        self.assertEqual(llm._limits(False, 12000, "extract:material"), (llm.LONG_CAP, llm.LONG_HEDGE))
        self.assertEqual(llm._limits(True, 1500, "context"), (llm.LONG_CAP, llm.LONG_HEDGE))
        llm._warm["done"] = False  # the first call of a run: the model may still be waking
        self.assertEqual(llm._limits(False, 6000, "erp"), (llm.CALL_TIMEOUT, llm.COLD_HEDGE))

    async def test_the_summary_names_the_backups(self):
        await self.ask(Completions("hang", "ok"))
        self.assertIn("1 backup requests, 1 of them answered first", llm.summary())


async def _none():
    return None


if __name__ == "__main__":
    unittest.main()
