import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import eod_stops as e
import telegram_hold as th
from database import DEFAULT_SETTINGS, Database
from qa.test_eod_stops import Opener, FakeResponse, http_error, make_downloader, two_floors

CHAT = 777


def decision(action, ticker="MU", pid=4, **kw):
    base = dict(ticker=ticker, close=884.74, prev_stop=873.45, new_stop=873.45, tier=e.NORMAL, action=action,
                reasons=["On Oct 30 it traded down to $861.59, at or below your stop of $873.45."], position_id=pid)
    base.update(kw)
    return e.Decision(**base)


class AlertButtonTests(unittest.TestCase):
    def test_a_reached_stop_carries_a_holding_on_purpose_button(self):
        msgs = e.format_alert([decision("BREACH")], "2026-10-30")
        self.assertEqual(msgs[0].buttons, [{"text": "📌 Holding on purpose: MU", "callback_data": "h:4"}])
        self.assertIn("tap \"Holding on purpose\" below", msgs[0])

    def test_no_support_positions_each_get_a_button_and_raises_get_none(self):
        ds = [decision("NO_SUPPORT", "AAA", 1, reasons=["cause", e.NO_SUPPORT_SENTENCE]),
              decision("NO_SUPPORT", "BBB", 2, reasons=["cause", e.NO_SUPPORT_SENTENCE]),
              decision("RAISE", "CCC", 3, prev_stop=10.0, new_stop=11.0)]
        buttons = e.format_alert(ds, "d")[0].buttons
        self.assertEqual([b["callback_data"] for b in buttons], ["h:1", "h:2"])

    def test_buttons_stay_with_the_message_that_holds_their_block(self):
        ds = [decision("BREACH", f"T{i}", i, reasons=["x" * 300]) for i in range(1, 25)]
        msgs = e.format_alert(ds, "d")
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            for b in m.buttons:
                ticker = b["text"].split(": ")[1]
                self.assertIn(f"<b>{ticker}</b>", m)
        self.assertEqual(sum(len(m.buttons) for m in msgs), 24)

    def test_messages_are_still_plain_strings(self):
        msg = e.format_alert([decision("BREACH")], "d")[0]
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("<b>EOD stops"))

    def test_keyboard_has_one_button_per_row_and_an_https_link_to_the_positions_page(self):
        buttons = [{"text": "a", "callback_data": "h:1"}, {"text": "b", "callback_data": "h:2"}]
        with patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "https://example.test/"}):
            rows = e.keyboard(buttons)["inline_keyboard"]
        self.assertEqual([len(r) for r in rows], [1, 1, 1])
        self.assertEqual(rows[-1][0]["url"], "https://example.test/#positions")
        with patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "http://insecure.test"}):
            self.assertEqual(len(e.keyboard(buttons)["inline_keyboard"]), 2)       # Telegram needs https links
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(e.keyboard([]))

    def test_send_attaches_the_keyboard_and_keeps_it_on_the_plain_text_retry(self):
        msg = e.format_alert([decision("BREACH")], "d")[0]
        op = Opener(FakeResponse())
        e.send_telegram([msg], "T", "1", opener=op, sleep=lambda s: None)
        self.assertIn("reply_markup=", op.bodies[0])
        self.assertIn("h%3A4", op.bodies[0])
        op = Opener(http_error(400), FakeResponse())
        e.send_telegram([msg], "T", "1", opener=op, sleep=lambda s: None)
        self.assertNotIn("parse_mode", op.bodies[1])
        self.assertIn("reply_markup=", op.bodies[1])

    def test_a_message_without_buttons_sends_no_keyboard(self):
        op = Opener(FakeResponse())
        e.send_telegram(["plain"], "T", "1", opener=op, sleep=lambda s: None)
        self.assertNotIn("reply_markup", op.bodies[0])


class FakeTg:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == self.fail_on:
            raise RuntimeError("down")
        return {"ok": True}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


class HoldFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.df = two_floors()
        self.pos = self.db.create_active(dict(
            ticker="ZZZ", entry_price=110.0, atr=1.0, quantity=100, value_ils=36500, currency="USD", fx_to_ils=3.65,
            fx_ticker=None, display_name=None, legacy=0, risk_status="RISK_ON", rung=0, opened_at=str(self.df.index[-30].date())))
        with sqlite3.connect(self.path) as c:
            e.ensure_schema(c)
            c.execute("INSERT INTO eod_stop_state VALUES (?, 'ZZZ', 99.0, 'CHART', ?, 'x')",
                      (self.pos["id"], str(self.df.index[-6].date())))
        self.tg = FakeTg()
        self.downloader = make_downloader({"*": self.df})

    def tearDown(self):
        self.tmp.cleanup()

    def press(self, data, chat=CHAT, message_id=50, tg=None):
        update = {"callback_query": {"id": "q1", "data": data,
                                     "message": {"message_id": message_id, "chat": {"id": chat}}}}
        return th.handle_update(update, db=self.db, settings=dict(DEFAULT_SETTINGS), downloader=self.downloader,
                                tg=tg or self.tg, allowed_chat_id=CHAT)

    def holds(self):
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT COUNT(*) FROM eod_holds").fetchone()[0]

    def backdate_holds(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE eod_holds SET decided_at = ?",
                      ((datetime.now(e.NY) - timedelta(days=3)).isoformat(timespec="seconds"),))

    def pid(self):
        return self.pos["id"]

    def test_pressing_the_alert_button_shows_the_cost_and_the_reasons_and_changes_nothing(self):
        self.assertEqual(self.press(f"h:{self.pid()}"), "preview")
        self.assertEqual(self.tg.sent("answerCallbackQuery")[0]["callback_query_id"], "q1")
        msg = self.tg.sent()[0]
        self.assertIn("holding on purpose?", msg["text"])
        self.assertIn("more you could lose", msg["text"])
        self.assertIn("already down", msg["text"])
        data = [b["callback_data"] for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"r:{self.pid()}:thesis:50", data)
        self.assertIn(f"x:{self.pid()}", data)
        self.assertEqual(self.holds(), 0)

    def test_choosing_a_reason_records_the_hold_and_confirms_in_place(self):
        self.assertEqual(self.press(f"r:{self.pid()}:thesis:50", message_id=51), "recorded")
        self.assertEqual(self.holds(), 1)
        info = e.stop_info(self.path)[self.pid()]
        self.assertEqual((info["hold_count"], info["held_reason"], info["stop_last_action"]), (1, "thesis", "OVERRIDE"))
        edit = self.tg.sent("editMessageText")[0]
        self.assertEqual(edit["message_id"], 51)
        self.assertIn("holding on purpose, recorded", edit["text"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})        # the buttons disappear

    def test_pressing_the_reason_twice_records_it_only_once(self):
        self.press(f"r:{self.pid()}:thesis:50")
        self.assertEqual(self.press(f"r:{self.pid()}:thesis:50"), "duplicate")
        self.assertEqual(self.holds(), 1)

    def test_a_second_hold_needs_one_more_tap(self):
        self.press(f"r:{self.pid()}:thesis:50")
        self.backdate_holds()
        self.tg.calls.clear()
        self.assertEqual(self.press(f"r:{self.pid()}:earnings:50"), "confirm")
        self.assertEqual(self.holds(), 1)
        confirm = self.tg.sent()[0]
        self.assertIn("Hold ZZZ again?", confirm["text"])
        self.assertEqual(self.press(f"c:{self.pid()}:earnings:50"), "recorded")
        self.assertEqual(self.holds(), 2)

    def test_an_expired_button_press_still_gets_its_answer_message(self):
        self.assertEqual(self.press(f"h:{self.pid()}", tg=FakeTg(fail_on="answerCallbackQuery")), "preview")

    def test_cancel_changes_nothing(self):
        self.assertEqual(self.press(f"x:{self.pid()}"), "cancelled")
        self.assertEqual(self.holds(), 0)
        self.assertIn("Nothing was changed", self.tg.sent("editMessageText")[0]["text"])

    def test_other_chats_are_ignored(self):
        self.assertEqual(self.press(f"r:{self.pid()}:thesis:50", chat=999), "forbidden")
        self.assertEqual(self.holds(), 0)
        self.assertEqual(self.tg.sent("answerCallbackQuery")[0]["text"], "Not allowed.")
        self.assertEqual(self.tg.sent(), [])

    def test_bad_buttons_and_unknown_positions_get_a_clear_answer(self):
        self.assertEqual(self.press("garbage"), "bad_data")
        self.assertEqual(self.press("h:abc"), "bad_data")
        self.assertEqual(self.press("z:1"), "bad_data")
        self.assertEqual(self.press("h:999"), "missing")
        self.assertIn("no longer exists", self.tg.sent()[-1]["text"])

    def test_tel_aviv_positions_are_refused(self):
        ta = self.db.create_active(dict(ticker="IMCO.TA", entry_price=10.0, atr=1.0, quantity=1, value_ils=10, currency="ILS",
                                        fx_to_ils=1, fx_ticker=None, display_name=None, legacy=0, risk_status="RISK_ON",
                                        rung=0, opened_at="2026-01-01"))
        self.assertEqual(self.press(f"h:{ta['id']}"), "excluded")

    def test_a_failure_tells_the_user_and_changes_nothing(self):
        def broken(*a, **k):
            raise ConnectionError("yahoo down")
        self.downloader = broken
        self.assertEqual(self.press(f"r:{self.pid()}:thesis:50"), "error")
        self.assertEqual(self.holds(), 0)
        self.assertIn("Nothing was changed", self.tg.sent()[-1]["text"])

    def test_non_button_updates_are_ignored(self):
        self.assertIsNone(th.handle_update({"message": {"text": "hi"}}, db=self.db, settings={}, downloader=None,
                                           tg=self.tg, allowed_chat_id=CHAT))
        self.assertEqual(self.tg.calls, [])

    def test_the_bot_api_client_never_leaks_the_token(self):
        def boom(req, timeout=None):
            raise OSError("https://api.telegram.org/botSECRET/sendMessage")
        with self.assertRaises(RuntimeError) as cm:
            th.make_tg("SECRET", opener=boom)("sendMessage", {})
        self.assertNotIn("SECRET", str(cm.exception))


class WebhookEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "w.db")
        self.database.initialize()
        patches = [patch.object(app_module, "db", self.database),
                   patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "s3cret", "TELEGRAM_BOT_TOKEN": "T",
                                           "TELEGRAM_CHAT_ID": "777", "RISK_SIZER_API_KEY": "k"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app_module.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.tmp.cleanup)
        self.update = {"callback_query": {"id": "1", "data": "h:1", "message": {"message_id": 5, "chat": {"id": 777}}}}

    def test_requests_without_the_secret_are_rejected(self):
        self.assertEqual(self.client.post("/api/telegram/webhook", json=self.update).status_code, 401)
        self.assertEqual(self.client.post("/api/telegram/webhook", json=self.update,
                                          headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code, 401)

    def test_no_configured_secret_means_everything_is_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.client.post("/api/telegram/webhook", json=self.update,
                                              headers={"X-Telegram-Bot-Api-Secret-Token": ""}).status_code, 401)

    def test_a_valid_update_is_handed_to_the_hold_flow(self):
        with patch.object(th, "handle_update") as handler:
            r = self.client.post("/api/telegram/webhook", json=self.update,
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        handler.assert_called_once()
        self.assertEqual(handler.call_args.kwargs["allowed_chat_id"], "777")
        self.assertEqual(handler.call_args.args[0], self.update)

    def test_the_webhook_needs_no_api_key_but_the_rest_of_the_api_still_does(self):
        self.assertEqual(self.client.get("/api/positions").status_code, 401)

    def test_garbage_bodies_are_a_400_not_a_crash(self):
        r = self.client.post("/api/telegram/webhook", content=b"not json",
                             headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret", "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
