#!/usr/bin/env python3
"""Telegram buttons for the EOD stop alert: the "Holding on purpose" flow, inside Telegram.

The daily job (eod_stops.py) is a one-shot script, so it cannot listen for button presses.  The web
app can: Telegram calls POST /api/telegram/webhook (app.py) and that hands the update to
`handle_update` below.  The flow, all by tapping:

  alert  ->  [📌 Holding on purpose: MU]          callback_data  h:<position id>
          ->  cost preview + reasons              callback_data  r:<position id>:<reason>:<alert message id>
          ->  (a 2nd hold of the same position asks for one more tap)    c:<...same>
          ->  state recorded (eod_holds / eod_stop_state), message edited to a confirmation
          ->  [Cancel]                            callback_data  x:<position id>

Security: the webhook only accepts requests carrying TELEGRAM_WEBHOOK_SECRET, and `handle_update`
ignores every chat except TELEGRAM_CHAT_ID.

Setup (once):  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_WEBHOOK_SECRET in the environment, then
  python telegram_hold.py set-webhook https://<your host>/api/telegram/webhook
  python telegram_hold.py webhook-info
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime
from typing import Any, Callable, Mapping

import eod_stops

log = logging.getLogger("telegram_hold")
Tg = Callable[[str, dict[str, Any]], dict[str, Any]]
DOUBLE_TAP_SECONDS = 120


def make_tg(token: str, opener: Callable[..., Any] = urllib.request.urlopen) -> Tg:
    """A tiny Bot API client.  Errors never contain the URL, which holds the token."""
    def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with opener(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception as exc:
            raise RuntimeError(f"telegram {method} failed ({type(exc).__name__})") from None
    return call


def _ils(n: float) -> str:
    return f"₪{abs(round(n)):,.0f}"


def _prior_holds(db_path: Any, position_id: int) -> int:
    conn = eod_stops.open_state(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM eod_holds WHERE position_id = ?", (position_id,)).fetchone()[0])
    finally:
        conn.close()


def _recent_hold(db_path: Any, position_id: int, now: datetime | None = None) -> bool:
    conn = eod_stops.open_state(db_path)
    try:
        row = conn.execute("SELECT decided_at FROM eod_holds WHERE position_id = ? ORDER BY id DESC LIMIT 1",
                           (position_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    when = datetime.fromisoformat(row["decided_at"])
    return ((now or datetime.now(eod_stops.NY)) - when).total_seconds() < DOUBLE_TAP_SECONDS


def _reason_keyboard(position_id: int, alert_id: int) -> dict[str, Any]:
    buttons = [{"text": label.capitalize(), "callback_data": f"r:{position_id}:{key}:{alert_id}"}
               for key, label in eod_stops.HOLD_REASONS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": "Cancel", "callback_data": f"x:{position_id}"}])
    return {"inline_keyboard": rows}


def handle_update(update: Mapping[str, Any], *, db: Any, settings: Mapping[str, float],
                  downloader: Callable[..., Any] | None, tg: Tg, allowed_chat_id: str | int) -> str | None:
    """Process one Telegram update.  Returns a short outcome word (used by tests and logs)."""
    cq = update.get("callback_query")
    if not cq:
        return None
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    qid = cq.get("id")
    if str(chat_id) != str(allowed_chat_id):
        tg("answerCallbackQuery", {"callback_query_id": qid, "text": "Not allowed."})
        return "forbidden"
    parts = str(cq.get("data", "")).split(":")
    kind = parts[0]
    try:
        position_id = int(parts[1])
    except (IndexError, ValueError):
        tg("answerCallbackQuery", {"callback_query_id": qid, "text": "Unknown button."})
        return "bad_data"

    def say(text: str, markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        tg("sendMessage", payload)

    def answer(text: str) -> None:        # stops the button spinner; a stale/expired query must not block the flow
        try:
            tg("answerCallbackQuery", {"callback_query_id": qid, "text": text})
        except Exception as exc:
            log.info("answerCallbackQuery skipped: %s", exc)

    def finish(text: str) -> None:        # replace the preview message and drop its buttons
        tg("editMessageText", {"chat_id": chat_id, "message_id": message.get("message_id"), "text": text,
                               "reply_markup": {"inline_keyboard": []}})

    if kind == "x":
        answer("Cancelled.")
        finish("Cancelled. Nothing was changed.")
        return "cancelled"
    if kind not in ("h", "r", "c"):
        tg("answerCallbackQuery", {"callback_query_id": qid, "text": "Unknown button."})
        return "bad_data"

    answer("Checking the chart…" if kind == "h" else "Saving…")
    try:
        position = db.get_active(position_id)
        if position is None:
            say("That position no longer exists.")
            return "missing"
        if eod_stops.is_excluded(position["ticker"]):
            say(f"{position['ticker']} is a Tel Aviv stock, which the stop engine does not cover.")
            return "excluded"
        ticker = position["ticker"]
        preview = eod_stops.hold_preview(position, settings, downloader)

        if kind == "h":
            n = eod_stops.hold_numbers(position, preview)
            if preview["new_stop"]:
                cost = (f"If you hold on: the chart gives a new stop at ${preview['new_stop']:,.2f}. From today's "
                        f"price that is about {_ils(n['extra_risk_ils'])} more you could lose.")
            else:
                cost = (f"The chart has no support below today's price, so there would be NO stop and the whole "
                        f"position ({_ils(n['position_value_ils'])}) stays exposed.")
            down = f"You are already down {_ils(max(n['loss_so_far_ils'], 0))} versus your entry."
            again = f"\nYou already did this once for {ticker}." if _prior_holds(db.path, position_id) else ""
            say(f"📌 {ticker}: holding on purpose?\n{cost}\n{down}{again}\n\nWhy are you holding?",
                _reason_keyboard(position_id, int(message.get("message_id") or 0)))
            return "preview"

        reason = parts[2] if len(parts) > 2 else ""
        if _recent_hold(db.path, position_id):
            finish(f"Already recorded for {ticker}.")
            return "duplicate"
        prior = _prior_holds(db.path, position_id)
        if kind == "r" and prior >= 1:
            alert_id = parts[3] if len(parts) > 3 else "0"
            finish(f"You already held {ticker} on purpose before. Confirm that you want to do it again.")
            say(f"Hold {ticker} again?",
                {"inline_keyboard": [[{"text": f"Yes, hold {ticker} again", "callback_data": f"c:{position_id}:{reason}:{alert_id}"}],
                                     [{"text": "Cancel", "callback_data": f"x:{position_id}"}]]})
            return "confirm"
        result = eod_stops.record_hold(db.path, position, reason, None, preview,
                                       confirm_ticker=ticker if prior else None)
        new_stop = result["new_stop"]
        if new_stop:
            finish(f"✅ {ticker}: holding on purpose, recorded.\nNew chart-based stop: ${new_stop:,.2f}. "
                   f"Place it at your broker if you want it protected.")
        else:
            finish(f"✅ {ticker}: holding on purpose, recorded.\nThere is no chart support below, so there is no "
                   f"stop. You won't get daily flags for it; you will be told if the chart gives one.")
        return "recorded"
    except Exception as exc:                       # the user must always get an answer, never silence
        log.warning("telegram hold failed for position %s: %s: %s", position_id, type(exc).__name__, exc)
        try:
            say(f"Couldn't do that just now ({type(exc).__name__}). Nothing was changed; try again in a minute.")
        except Exception:
            pass
        return "error"


def _cli(argv: list[str]) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or len(argv) < 1:
        print("usage: TELEGRAM_BOT_TOKEN=... python telegram_hold.py set-webhook <https-url> | webhook-info | delete-webhook",
              file=sys.stderr)
        return 2
    tg = make_tg(token)
    try:
        if argv[0] == "set-webhook" and len(argv) == 2:
            secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
            if not secret or not argv[1].startswith("https://"):
                print("need TELEGRAM_WEBHOOK_SECRET and an https:// URL", file=sys.stderr)
                return 2
            out = tg("setWebhook", {"url": argv[1], "secret_token": secret, "allowed_updates": ["callback_query"],
                                    "drop_pending_updates": True})
        elif argv[0] == "webhook-info":
            out = tg("getWebhookInfo", {})
        elif argv[0] == "delete-webhook":
            out = tg("deleteWebhook", {})
        else:
            print("unknown command", file=sys.stderr)
            return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
