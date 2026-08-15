import asyncio
import logging
import time

import config
from bot.commands import build_bot_application
from db.database import get_connection, init_db, seed_authorized_chats
from logging_config import setup_logging

logger = logging.getLogger(__name__)

CRASH_RESTART_DELAY_SECONDS = 10


async def main() -> None:
    init_db()
    conn = get_connection()
    try:
        seed_authorized_chats(conn, config.AUTHORIZED_CHAT_IDS)
    finally:
        conn.close()

    application = build_bot_application()
    await application.initialize()
    # Application.initialize() ruft post_init() NICHT auf (das macht laut PTB-Doku nur
    # run_polling()/run_webhook()) -- ohne diesen expliziten Aufruf wuerde set_my_commands()
    # bei jedem Neustart uebersprungen und das Telegram-Befehlsmenü liefe auf Dauer auseinander.
    if application.post_init is not None:
        await application.post_init(application)
    await application.start()
    await application.updater.start_polling()
    try:
        logger.info("Schulden-Tracker-Bot läuft. Warte auf Nachrichten... (Strg+C zum Beenden)")
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def _run_forever() -> None:
    """Haelt den Bot am Laufen: startet nach einem unerwarteten Absturz automatisch neu."""
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception(f"Bot abgestürzt, Neustart in {CRASH_RESTART_DELAY_SECONDS}s")
        time.sleep(CRASH_RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    setup_logging()
    _run_forever()
