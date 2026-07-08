"""Presence layer (Phase 3): heartbeat + Telegram so Jarvis can reach you when away.

Two pieces:
  * `telegram_bot.TelegramBridge` — bi-directional Telegram (send notes out, take replies in).
  * `manager.PresenceManager` — a 15-min heartbeat that routes Jarvis's queued notes to
    whoever can hear them (voice if you're here, Telegram if you're away) and guarantees he
    never sleeps forever.

Both are optional and import their (heavier) deps lazily, so a text/voice-only install that
never configures Telegram pays nothing for this package.
"""
