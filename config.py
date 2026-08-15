import os

from dotenv import load_dotenv

load_dotenv()


def _int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Komma-getrennte Chat-IDs fuer die einmalige Erstbefuellung der Autorisierung (nur bei
# komplett leerer DB relevant). Danach laeuft die Autorisierung dynamisch ueber /authorize,
# /deauthorize und die Autorisieren/Ablehnen-Buttons bei neuen /start-Anfragen.
AUTHORIZED_CHAT_IDS = _int_list(os.environ.get("AUTHORIZED_CHAT_IDS", ""))

# Einzige Excel-Datei mit dem gesamten Schulden-Ledger (kein Trip-Konzept -- ein
# durchgehender gemeinsamer Topf fuer Johannes & Anna).
LEDGER_PATH = os.environ.get("LEDGER_PATH", "data/schulden.xlsx")
LOG_PATH = os.environ.get("LOG_PATH", "logs/bot.log")
DB_PATH = os.environ.get("DB_PATH", "data/schuldentracker.db")

# Chat-ID des Superusers -- bekommt /restart, /authorize, /deauthorize, /users und ist
# immer autorisiert, unabhaengig von der DB. 0 = deaktiviert.
SUPERUSER_CHAT_ID = int(os.environ.get("SUPERUSER_CHAT_ID", "0"))

# Chat-ID, an die taeglich automatisch (und per /backup manuell) die Excel-Datei + die
# Datenbank geschickt werden -- unabhaengig vom Superuser, fest auf diesen Chat gesetzt. Der
# Chat muss den Bot vorher einmal mit /start gestartet haben, sonst kann Telegram keine
# Nachricht zustellen. 0 = deaktiviert.
BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID", "0"))
