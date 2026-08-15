import logging
import math
import os
import sys
from datetime import datetime

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
from db.database import (
    authorize_chat,
    deauthorize_chat,
    get_connection,
    get_user_overview,
    is_chat_authorized,
    list_authorized_chats,
    touch_user_profile,
)
from expenses.ledger_store import (
    Entry,
    PERSONEN,
    TYP_AUSGABE,
    TYP_RUECKZAHLUNG,
    add_entry,
    delete_entry_at,
    format_saldo,
    get_all_entries,
    get_all_entries_indexed,
    get_balance,
    get_entry_at,
    get_summary,
    init_ledger,
    remove_last_entry,
    update_entry,
)

logger = logging.getLogger(__name__)

WER, BETRAG, BESCHREIBUNG = range(3)
EDIT_SELECT, DELETE_SELECT, DELETE_CONFIRM = range(3, 6)
CONVERSATION_TIMEOUT_SECONDS = 300

_COMMANDS = [
    BotCommand("add", "Neue Ausgabe eintragen"),
    BotCommand("repay", "Rückzahlung eintragen"),
    BotCommand("saldo", "Wer schuldet wem wie viel"),
    BotCommand("recent", "Alle Einträge anzeigen"),
    BotCommand("edit", "Alten Eintrag bearbeiten"),
    BotCommand("delete", "Eintrag löschen"),
    BotCommand("undo", "Letzten Eintrag rückgängig machen"),
    BotCommand("export", "Excel-Datei herunterladen"),
    BotCommand("cancel", "Eingabe abbrechen"),
    BotCommand("help", "Hilfe / Befehlsübersicht"),
]

_SUPERUSER_COMMANDS = [
    BotCommand("users", "Alle autorisierten Nutzer anzeigen (Admin)"),
    BotCommand("authorize", "Nutzer freischalten (Admin)"),
    BotCommand("deauthorize", "Nutzer sperren (Admin)"),
    BotCommand("backup", "Backup jetzt senden (Admin)"),
    BotCommand("restart", "Bot neu starten (Admin)"),
]

BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_FIRST_DELAY_SECONDS = 60


def _help_text(is_admin: bool) -> str:
    lines = [
        "💸 Schulden-Tracker",
        "",
        "/add - neue Ausgabe eintragen (wer hat gezahlt, wie viel, wofür -- wird automatisch 50/50 aufgeteilt)",
        "/repay - Rückzahlung eintragen (wer zahlt wie viel an die andere Person zurück)",
        "/saldo - aktueller Stand: wer schuldet wem wie viel",
        "/recent - alle Einträge anzeigen",
        "/edit - einen bestehenden Eintrag bearbeiten (Liste mit Nummern, Nummer senden)",
        "/delete - einen bestehenden Eintrag löschen (Liste mit Nummern, Nummer senden)",
        "/undo - letzten Eintrag rückgängig machen",
        "/export - Excel-Datei als Datei bekommen",
        "/cancel - laufende Eingabe abbrechen",
        "/help - diese Übersicht anzeigen",
    ]
    if is_admin:
        lines += [
            "",
            "Admin-Befehle:",
            "/users - alle autorisierten Nutzer anzeigen",
            "/authorize <chat_id> - Nutzer freischalten",
            "/deauthorize [chat_id] - Nutzer sperren (per Button auswählen oder ID angeben)",
            "/backup - Backup (Excel-Datei + Datenbank) sofort per Telegram senden",
            "/restart - Bot neu starten",
        ]
    return "\n".join(lines)


def _is_superuser(update: Update) -> bool:
    return (
        update.effective_chat is not None
        and bool(config.SUPERUSER_CHAT_ID)
        and update.effective_chat.id == config.SUPERUSER_CHAT_ID
    )


def _touch_profile(update: Update) -> None:
    """Aktualisiert die gespeicherten Profildaten des anfragenden Nutzers. Best-effort --
    ein Fehler hier darf nie einen Befehl blockieren."""
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    if user is None or chat_id is None:
        return
    conn = get_connection()
    try:
        touch_user_profile(
            conn,
            chat_id=chat_id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
            is_premium=user.is_premium,
        )
    except Exception:
        logger.exception(f"Konnte Nutzerprofil für {chat_id} nicht aktualisieren")
    finally:
        conn.close()


def _is_authorized(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    if _is_superuser(update):
        _touch_profile(update)
        return True
    conn = get_connection()
    try:
        authorized = is_chat_authorized(conn, update.effective_chat.id)
    finally:
        conn.close()
    if authorized:
        _touch_profile(update)
    return authorized


def _format_amount(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


_MAX_BETRAG = 1_000_000


def _parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace("€", "").replace(",", ".").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    # float() akzeptiert auch "inf"/"nan"/"infinity" als gueltige Zahlen -- ungefiltert wuerden
    # solche Werte die Saldo-Berechnung dauerhaft zerstören.
    if not math.isfinite(value):
        return None
    value = round(value, 2)
    if value <= 0 or value > _MAX_BETRAG:
        return None
    return value


async def _reply_long(update: Update, text: str) -> None:
    """Telegram-Nachrichten sind auf 4096 Zeichen begrenzt -- ggf. aufteilen."""
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i : i + 3800])


def _describe_action(typ: str, von: str, betrag: float, beschreibung: str, an: str | None) -> str:
    """Wie _entry_line, aber ohne Datum -- fuer Bestaetigungs-Nachrichten direkt nach dem
    Speichern/Bearbeiten, wo kein Entry mit bereits zugewiesenem Datum vorliegt."""
    betrag_text = _format_amount(betrag)
    if typ == TYP_AUSGABE:
        return f"🧾 {von} zahlte {betrag_text} für „{beschreibung}“"
    return f"💶 {von} zahlte {betrag_text} an {an} zurück"


def _entry_line(e: Entry) -> str:
    tag = datetime.fromisoformat(e.datum).strftime("%d.%m.%Y")
    betrag_text = _format_amount(e.betrag)
    if e.typ == TYP_AUSGABE:
        return f"{tag} 🧾 {e.von} zahlte {betrag_text} für „{e.beschreibung}“"
    return f"{tag} 💶 {e.von} zahlte {betrag_text} an {e.an} zurück"


def _numbered_entry_lines(indexed: list[tuple[int, Entry]]) -> tuple[str, list[int]]:
    """Baut eine durchnummerierte Liste (1..N, chronologisch nach Eintragsreihenfolge) und
    gibt zusaetzlich die Zuordnung Nr. -> Excel-Zeilennummer zurueck, damit die anschliessend
    per Zahl getroffene Auswahl (bei /edit bzw. /delete) eindeutig aufgeloest werden kann."""
    ordered = sorted(indexed, key=lambda pair: pair[0])
    row_numbers = []
    lines = []
    for i, (row_number, e) in enumerate(ordered, start=1):
        lines.append(f"{i}. {_entry_line(e)}")
        row_numbers.append(row_number)
    return "\n".join(lines), row_numbers


def _wer_buttons(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(p, callback_data=f"{prefix}:{p}") for p in PERSONEN]])


def _new_user_info_text(update: Update) -> str:
    chat_id = update.effective_chat.id if update.effective_chat else None
    user = update.effective_user
    lines = [f"Chat-ID: {chat_id}"]
    if user is not None:
        name = " ".join(filter(None, [user.first_name, user.last_name])) or "-"
        lines.append(f"Name: {name}")
        lines.append(f"Username: @{user.username}" if user.username else "Username: -")
        lines.append(f"Sprache: {user.language_code or '-'}")
    return "\n".join(lines)


async def _notify_superuser_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.SUPERUSER_CHAT_ID:
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None or chat_id == config.SUPERUSER_CHAT_ID:
        return

    # Profildaten zwischenspeichern: zum Zeitpunkt des Autorisierens (Button-Klick) ist
    # update.effective_user der Superuser, nicht mehr der neue Nutzer.
    user = update.effective_user
    if user is not None:
        context.bot_data.setdefault("pending_profiles", {})[chat_id] = {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username,
            "language_code": user.language_code,
            "is_premium": user.is_premium,
        }

    text = f"🆕 Neue Start-Anfrage\n\n{_new_user_info_text(update)}"
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Autorisieren", callback_data=f"newuser_auth:{chat_id}"),
        InlineKeyboardButton("❌ Ablehnen", callback_data=f"newuser_reject:{chat_id}"),
    ]])
    try:
        await context.bot.send_message(chat_id=config.SUPERUSER_CHAT_ID, text=text, reply_markup=buttons)
    except Exception:
        logger.exception("Konnte neue Start-Anfrage nicht an Superuser senden")


def _authorize(chat_id: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> bool:
    conn = get_connection()
    try:
        added = authorize_chat(conn, chat_id)
        profile = context.bot_data.get("pending_profiles", {}).pop(chat_id, None) if context is not None else None
        if added and profile:
            touch_user_profile(conn, chat_id=chat_id, **profile)
    finally:
        conn.close()
    if added:
        logger.info(f"Chat {chat_id} wurde autorisiert.")
    return added


def _deauthorize(chat_id: int) -> bool:
    conn = get_connection()
    try:
        removed = deauthorize_chat(conn, chat_id)
    finally:
        conn.close()
    if removed:
        logger.info(f"Chat {chat_id} wurde deautorisiert.")
    return removed


async def _cb_new_user_authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_superuser(update):
        return

    chat_id = int(query.data.split(":", 1)[1])
    added = _authorize(chat_id, context)
    suffix = "\n\n✅ Autorisiert." if added else "\n\n✅ War bereits autorisiert."
    await query.edit_message_text(f"{query.message.text}{suffix}")

    try:
        await context.bot.send_message(chat_id=chat_id, text="✅ Du wurdest freigeschaltet! /start für eine Übersicht.")
    except Exception:
        logger.exception(f"Konnte Freischaltung nicht an Chat {chat_id} melden")


async def _cb_new_user_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_superuser(update):
        return

    chat_id = int(query.data.split(":", 1)[1])
    context.bot_data.get("pending_profiles", {}).pop(chat_id, None)
    await query.edit_message_text(f"{query.message.text}\n\n❌ Abgelehnt.")


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text(
            "👋 Willkommen! Dieser Bot ist nur für freigeschaltete Chats.\n"
            "Deine Anfrage wurde an den Admin weitergeleitet -- "
            "sobald du freigeschaltet bist, bekommst du eine Nachricht."
        )
        await _notify_superuser_new_user(update, context)
        return
    await update.message.reply_text(_help_text(_is_superuser(update)))


async def _cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_superuser(update):
        return
    conn = get_connection()
    try:
        users = get_user_overview(conn)
    finally:
        conn.close()

    if not users:
        await update.message.reply_text("📭 Keine autorisierten Nutzer vorhanden.")
        return

    blocks = []
    for row in users:
        chat_id = row["chat_id"]
        role = " (Superuser)" if chat_id == config.SUPERUSER_CHAT_ID else ""
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or "-"
        username = f"@{row['username']}" if row["username"] else "-"
        authorized_date = row["authorized_at"].split("T")[0]
        last_seen = row["last_seen_at"].split("T")[0] if row["last_seen_at"] else "noch nicht gesehen"
        blocks.append(
            f"👤 Chat {chat_id}{role}\n"
            f"   Name: {name}\n"
            f"   Username: {username}\n"
            f"   Autorisiert seit: {authorized_date}\n"
            f"   Zuletzt aktiv: {last_seen}"
        )
    await update.message.reply_text("\n\n".join(blocks))


async def _cmd_authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_superuser(update):
        return
    if not context.args:
        await update.message.reply_text("ℹ️ Nutzung: /authorize <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Bitte eine gültige Chat-ID angeben.")
        return

    if _authorize(chat_id, context):
        await update.message.reply_text(f"✅ Chat {chat_id} ist jetzt autorisiert.")
    else:
        await update.message.reply_text(f"ℹ️ Chat {chat_id} war bereits autorisiert.")


async def _cmd_deauthorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_superuser(update):
        return

    if context.args:
        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine gültige Chat-ID angeben.")
            return
        if chat_id == config.SUPERUSER_CHAT_ID:
            await update.message.reply_text("⚠️ Der Superuser kann sich nicht selbst deautorisieren.")
            return

        if _deauthorize(chat_id):
            await update.message.reply_text(f"🚫 Chat {chat_id} wurde deautorisiert.")
        else:
            await update.message.reply_text(f"ℹ️ Chat {chat_id} war nicht autorisiert.")
        return

    conn = get_connection()
    try:
        chat_ids = [c for c in list_authorized_chats(conn) if c != config.SUPERUSER_CHAT_ID]
    finally:
        conn.close()

    if not chat_ids:
        await update.message.reply_text("📭 Keine Nutzer zum Deautorisieren vorhanden.")
        return

    buttons = [[InlineKeyboardButton(str(chat_id), callback_data=f"deauth:{chat_id}")] for chat_id in chat_ids]
    await update.message.reply_text(
        "🚫 Welchen Nutzer deautorisieren?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _cb_deauthorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_superuser(update):
        return

    chat_id = int(query.data.split(":", 1)[1])
    if chat_id == config.SUPERUSER_CHAT_ID:
        await query.edit_message_text("⚠️ Der Superuser kann sich nicht selbst deautorisieren.")
        return

    if _deauthorize(chat_id):
        await query.edit_message_text(f"🚫 Chat {chat_id} wurde deautorisiert.")
    else:
        await query.edit_message_text(f"ℹ️ Chat {chat_id} war bereits nicht autorisiert.")


async def _cmd_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not _is_authorized(update):
        return ConversationHandler.END
    context.user_data["typ"] = TYP_AUSGABE
    await update.message.reply_text("Wer hat gezahlt?", reply_markup=_wer_buttons("wer"))
    return WER


async def _cmd_repay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not _is_authorized(update):
        return ConversationHandler.END
    context.user_data["typ"] = TYP_RUECKZAHLUNG
    await update.message.reply_text("Wer zahlt zurück?", reply_markup=_wer_buttons("wer"))
    return WER


async def _cb_wer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    von = query.data.split(":", 1)[1]
    context.user_data["von"] = von
    typ = context.user_data.get("typ")

    if typ == TYP_RUECKZAHLUNG:
        other = [p for p in PERSONEN if p != von][0]
        await query.edit_message_text(f"Wer zahlt zurück? {von}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Wie viel zahlt {von} an {other} zurück? (z.B. 25,50)",
        )
    else:
        await query.edit_message_text(f"Wer hat gezahlt? {von}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Wie viel hat es gekostet? (z.B. 25,50)",
        )
    return BETRAG


async def _handle_betrag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = _parse_amount(update.message.text or "")
    if amount is None:
        await update.message.reply_text("⚠️ Bitte einen gültigen Betrag senden (z.B. 25,50).")
        return BETRAG
    context.user_data["betrag"] = amount

    if context.user_data.get("typ") == TYP_RUECKZAHLUNG:
        return await _finalize_entry(update, context, beschreibung="")

    await update.message.reply_text(f"Betrag: {_format_amount(amount)}\n\nWofür? (kurze Beschreibung)")
    return BESCHREIBUNG


async def _handle_beschreibung(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    beschreibung = (update.message.text or "").strip()
    if not beschreibung:
        await update.message.reply_text("⚠️ Bitte eine kurze Beschreibung senden.")
        return BESCHREIBUNG
    return await _finalize_entry(update, context, beschreibung=beschreibung)


async def _finalize_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, *, beschreibung: str) -> int:
    typ = context.user_data.pop("typ")
    von = context.user_data.pop("von")
    betrag = context.user_data.pop("betrag")
    edit_row = context.user_data.pop("edit_row", None)
    an = None
    if typ == TYP_RUECKZAHLUNG:
        an = [p for p in PERSONEN if p != von][0]

    if edit_row is not None:
        try:
            updated = update_entry(
                config.LEDGER_PATH, edit_row, typ=typ, von=von, betrag=betrag, beschreibung=beschreibung, an=an,
            )
        except Exception:
            logger.exception("Konnte Eintrag nicht aktualisieren")
            await update.message.reply_text("⚠️ Fehler beim Speichern -- bitte nochmal mit /edit versuchen.")
            return ConversationHandler.END

        if not updated:
            await update.message.reply_text(
                "⚠️ Dieser Eintrag wurde zwischenzeitlich entfernt (z.B. per /undo) -- nichts geändert."
            )
            return ConversationHandler.END

        logger.info(f"Eintrag bearbeitet (Zeile {edit_row}): {typ} | {von} | {betrag} | {beschreibung}")
        await update.message.reply_text(f"✏️ Aktualisiert: {_describe_action(typ, von, betrag, beschreibung, an)}")
        await _backup_ledger(context.bot)
        return ConversationHandler.END

    try:
        add_entry(config.LEDGER_PATH, typ=typ, von=von, betrag=betrag, beschreibung=beschreibung, an=an)
    except Exception:
        logger.exception("Konnte Eintrag nicht speichern")
        await update.message.reply_text("⚠️ Fehler beim Speichern -- bitte nochmal versuchen.")
        return ConversationHandler.END

    logger.info(f"Eintrag gespeichert: {typ} | {von} | {betrag} | {beschreibung}")
    await update.message.reply_text(f"✅ Eingetragen: {_describe_action(typ, von, betrag, beschreibung, an)}")
    await _backup_ledger(context.bot)
    return ConversationHandler.END


async def _cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Abgebrochen.")
    return ConversationHandler.END


async def _cmd_conversation_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Faengt alles auf, was waehrend einer offenen Eingabe (z.B. /add, /repay, /edit) nicht
    zum aktuellen Schritt passt: ein anderer Befehl, unerwarteter Text bei einem reinen
    Button-Schritt (z.B. Text statt Button-Klick bei "Wer hat gezahlt?"), oder Nicht-Text-Inhalte
    (Foto, Sticker, Sprachnachricht, ...). Ohne das bliebe die Konversation intern haengen und
    der Bot wuerde bis zum Timeout einfach schweigen, statt eine Rueckmeldung zu geben."""
    context.user_data.clear()
    text = update.message.text if update.message else None
    if text and text.startswith("/"):
        reply = f"⚠️ Offene Eingabe abgebrochen (du hast {text.split()[0]} geschickt). Bitte nochmal senden."
    else:
        reply = "⚠️ Offene Eingabe abgebrochen (unerwartete Eingabe). Bitte nochmal senden."
    if update.message is not None:
        await update.message.reply_text(reply)
    return ConversationHandler.END


async def _conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Zusaetzliches Sicherheitsnetz: falls eine Eingabe aus anderen Gruenden haengen bleibt,
    loest sie sich nach CONVERSATION_TIMEOUT_SECONDS von selbst auf."""
    context.user_data.clear()
    if update.effective_chat is not None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏱️ Eingabe abgebrochen (zu lange keine Antwort).",
        )
    return ConversationHandler.END


async def _cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    try:
        summary = get_summary(config.LEDGER_PATH)
    except Exception:
        logger.exception("Konnte Saldo nicht berechnen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return

    if summary["count"] == 0:
        await update.message.reply_text("📭 Noch keine Einträge vorhanden.")
        return

    lines = [
        f"💸 {summary['count']} Eintrag/Einträge",
        f"Ausgaben gesamt: {_format_amount(summary['total_ausgaben'])}",
        f"Rückzahlungen gesamt: {_format_amount(summary['total_rueckzahlungen'])}",
        "",
        f"⚖️ {format_saldo(summary['balance'])}",
    ]
    await update.message.reply_text("\n".join(lines))


async def _cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    try:
        entries = get_all_entries(config.LEDGER_PATH)
    except Exception:
        logger.exception("Konnte Einträge nicht lesen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return

    if not entries:
        await update.message.reply_text("📭 Noch keine Einträge vorhanden.")
        return

    by_date: dict[str, list] = {}
    for e in entries:
        by_date.setdefault(e.datum.split("T")[0], []).append(e)

    lines = [f"🧾 Alle Einträge ({len(entries)} gesamt)"]
    for datum in sorted(by_date):
        tag = datetime.fromisoformat(datum).strftime("%d.%m.%Y")
        lines.append(f"\n📅 {tag}")
        for e in by_date[datum]:
            lines.append(f"  {_entry_line(e)}")

    balance = get_balance(entries)
    lines.append(f"\n⚖️ {format_saldo(balance)}")

    await _reply_long(update, "\n".join(lines))


async def _cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    try:
        removed = remove_last_entry(config.LEDGER_PATH)
    except Exception:
        logger.exception("Konnte letzten Eintrag nicht entfernen")
        await update.message.reply_text("⚠️ Fehler beim Bearbeiten der Excel-Datei.")
        return

    if removed is None:
        await update.message.reply_text("📭 Kein Eintrag zum Rückgängigmachen vorhanden.")
        return
    logger.info(f"Eintrag rückgängig gemacht: {removed.typ} | {removed.von} | {removed.betrag}")
    await update.message.reply_text(f"↩️ Entfernt: {_entry_line(removed)}")
    await _backup_ledger(context.bot)


async def _cmd_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not _is_authorized(update):
        return ConversationHandler.END
    try:
        indexed = get_all_entries_indexed(config.LEDGER_PATH)
    except Exception:
        logger.exception("Konnte Einträge nicht lesen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return ConversationHandler.END

    if not indexed:
        await update.message.reply_text("📭 Noch keine Einträge vorhanden.")
        return ConversationHandler.END

    text, row_numbers = _numbered_entry_lines(indexed)
    context.user_data["edit_candidates"] = row_numbers
    await _reply_long(
        update, f"✏️ Einträge:\n\n{text}\n\nSende die Nummer des Eintrags, der bearbeitet werden soll (oder /cancel)."
    )
    return EDIT_SELECT


async def _handle_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        number = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ Bitte eine gültige Nummer aus der Liste senden (oder /cancel).")
        return EDIT_SELECT

    row_numbers = context.user_data.get("edit_candidates", [])
    if number < 1 or number > len(row_numbers):
        await update.message.reply_text(f"⚠️ Bitte eine Nummer zwischen 1 und {len(row_numbers)} senden (oder /cancel).")
        return EDIT_SELECT

    row_number = row_numbers[number - 1]
    try:
        entry = get_entry_at(config.LEDGER_PATH, row_number)
    except Exception:
        logger.exception("Konnte Eintrag nicht lesen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return ConversationHandler.END

    if entry is None:
        await update.message.reply_text("⚠️ Dieser Eintrag wurde zwischenzeitlich entfernt.")
        return ConversationHandler.END

    context.user_data.pop("edit_candidates", None)
    context.user_data["edit_row"] = row_number
    context.user_data["typ"] = entry.typ

    frage = "Wer zahlt zurück?" if entry.typ == TYP_RUECKZAHLUNG else "Wer hat gezahlt?"
    await update.message.reply_text(
        f"✏️ Bisheriger Wert: {_entry_line(entry)}\n\n{frage}", reply_markup=_wer_buttons("wer")
    )
    return WER


async def _cmd_delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not _is_authorized(update):
        return ConversationHandler.END
    try:
        indexed = get_all_entries_indexed(config.LEDGER_PATH)
    except Exception:
        logger.exception("Konnte Einträge nicht lesen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return ConversationHandler.END

    if not indexed:
        await update.message.reply_text("📭 Noch keine Einträge vorhanden.")
        return ConversationHandler.END

    text, row_numbers = _numbered_entry_lines(indexed)
    context.user_data["delete_candidates"] = row_numbers
    await _reply_long(
        update, f"🗑️ Einträge:\n\n{text}\n\nSende die Nummer des Eintrags, der gelöscht werden soll (oder /cancel)."
    )
    return DELETE_SELECT


def _delete_confirm_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ja, löschen", callback_data="delconfirm:yes"),
        InlineKeyboardButton("❌ Abbrechen", callback_data="delconfirm:no"),
    ]])


async def _handle_delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        number = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ Bitte eine gültige Nummer aus der Liste senden (oder /cancel).")
        return DELETE_SELECT

    row_numbers = context.user_data.get("delete_candidates", [])
    if number < 1 or number > len(row_numbers):
        await update.message.reply_text(f"⚠️ Bitte eine Nummer zwischen 1 und {len(row_numbers)} senden (oder /cancel).")
        return DELETE_SELECT

    row_number = row_numbers[number - 1]
    try:
        entry = get_entry_at(config.LEDGER_PATH, row_number)
    except Exception:
        logger.exception("Konnte Eintrag nicht lesen")
        await update.message.reply_text("⚠️ Fehler beim Lesen der Excel-Datei.")
        return ConversationHandler.END

    if entry is None:
        await update.message.reply_text("⚠️ Dieser Eintrag wurde zwischenzeitlich entfernt.")
        return ConversationHandler.END

    context.user_data.pop("delete_candidates", None)
    context.user_data["delete_row"] = row_number

    await update.message.reply_text(
        f"🗑️ Wirklich löschen?\n\n{_entry_line(entry)}", reply_markup=_delete_confirm_buttons(),
    )
    return DELETE_CONFIRM


async def _cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    decision = query.data.split(":", 1)[1]
    row_number = context.user_data.pop("delete_row", None)

    if decision == "no" or row_number is None:
        context.user_data.clear()
        await query.edit_message_text("Abgebrochen -- nichts gelöscht.")
        return ConversationHandler.END

    try:
        removed = delete_entry_at(config.LEDGER_PATH, row_number)
    except Exception:
        logger.exception("Konnte Eintrag nicht löschen")
        await query.edit_message_text("⚠️ Fehler beim Bearbeiten der Excel-Datei.")
        return ConversationHandler.END

    if removed is None:
        await query.edit_message_text("⚠️ Dieser Eintrag wurde zwischenzeitlich bereits entfernt.")
        return ConversationHandler.END

    logger.info(f"Eintrag gelöscht: {removed.typ} | {removed.von} | {removed.betrag}")
    await query.edit_message_text(f"🗑️ Gelöscht: {_entry_line(removed)}")
    await _backup_ledger(context.bot)
    return ConversationHandler.END


async def _cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    init_ledger(config.LEDGER_PATH)
    try:
        with open(config.LEDGER_PATH, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(config.LEDGER_PATH))
    except Exception:
        logger.exception("Konnte Excel-Datei nicht senden")
        await update.message.reply_text("⚠️ Fehler beim Senden der Datei.")


async def _cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_superuser(update):
        return
    logger.info("Neustart durch Superuser angefordert.")
    await update.message.reply_text("🔄 Starte neu...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def _backup_ledger(bot) -> None:
    """Schickt die Ledger-Excel-Datei an config.BACKUP_CHAT_ID -- wird nach jeder Änderung
    (add/repay/edit/delete/undo) aufgerufen, damit eine aktuelle Kopie ausserhalb der Pi-SD-Karte
    liegt, ohne bei jeder einzelnen Änderung auch die Datenbank neu zu verschicken (das
    uebernimmt weiterhin der taegliche/manuelle Voll-Backup via _run_backup). Best-effort -- ein
    Fehler hier darf den ausloesenden Befehl nicht scheitern lassen."""
    if not config.BACKUP_CHAT_ID or not os.path.exists(config.LEDGER_PATH):
        return
    try:
        with open(config.LEDGER_PATH, "rb") as f:
            await bot.send_document(
                chat_id=config.BACKUP_CHAT_ID, document=f, filename=os.path.basename(config.LEDGER_PATH),
                caption="🗄️ Auto-Backup (Änderung)",
            )
    except Exception:
        logger.exception("Änderungs-Backup fehlgeschlagen")


async def _run_backup(bot) -> None:
    """Schickt die Ledger-Excel-Datei + die Datenbank als Telegram-Dokumente an
    config.BACKUP_CHAT_ID -- fest, unabhaengig vom aktuellen Superuser. Laeuft sowohl als
    taeglicher Hintergrund-Job als auch manuell per /backup -- so liegt eine Kopie ausserhalb
    der Pi-SD-Karte, ohne dass dafuer ein zusaetzlicher Cloud-Account/Credential auf dem Pi
    eingerichtet werden muss."""
    if not config.BACKUP_CHAT_ID:
        return

    if os.path.exists(config.LEDGER_PATH):
        try:
            with open(config.LEDGER_PATH, "rb") as f:
                await bot.send_document(
                    chat_id=config.BACKUP_CHAT_ID, document=f, filename=os.path.basename(config.LEDGER_PATH),
                    caption="🗄️ Backup: Schulden-Ledger",
                )
        except Exception:
            logger.exception("Backup des Ledgers fehlgeschlagen")

    try:
        with open(config.DB_PATH, "rb") as f:
            await bot.send_document(
                chat_id=config.BACKUP_CHAT_ID, document=f, filename=os.path.basename(config.DB_PATH),
                caption="🗄️ Backup: Datenbank (Autorisierungen)",
            )
    except Exception:
        logger.exception("Backup der Datenbank fehlgeschlagen")

    logger.info(f"Backup gesendet an {config.BACKUP_CHAT_ID}")


async def _job_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _run_backup(context.bot)
    except Exception:
        logger.exception("Automatisches Backup fehlgeschlagen")


async def _cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_superuser(update):
        return
    await update.message.reply_text("🗄️ Backup wird gesendet...")
    try:
        await _run_backup(context.bot)
    except Exception:
        logger.exception("Manuelles Backup fehlgeschlagen")
        await update.message.reply_text("⚠️ Backup fehlgeschlagen -- siehe Log.")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unbehandelter Bot-Fehler", exc_info=context.error)


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(_COMMANDS)
    if config.SUPERUSER_CHAT_ID:
        await application.bot.set_my_commands(
            _COMMANDS + _SUPERUSER_COMMANDS,
            scope=BotCommandScopeChat(chat_id=config.SUPERUSER_CHAT_ID),
        )


def build_bot_application() -> Application:
    timeout_kwargs = dict(connect_timeout=15.0, read_timeout=15.0, write_timeout=15.0, pool_timeout=10.0)
    application = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(HTTPXRequest(**timeout_kwargs))
        .get_updates_request(HTTPXRequest(**timeout_kwargs))
        .post_init(_post_init)
        .build()
    )

    # Ein einziger ConversationHandler fuer /add, /repay, /edit UND /delete (statt getrennter
    # Instanzen): PTB prueft Entry-Points nur, wenn fuer den Chat/User gerade KEINE Konversation
    # laeuft. Mit getrennten Handlern wuerde der Entry-Point des einen (z.B. /add) die offene
    # Eingabe eines anderen (z.B. /edit) nicht abbrechen, weil der zuerst registrierte Handler
    # die Update sofort als neuen Entry-Point konsumiert -- die andere Konversation bliebe bis
    # zum Timeout haengen. In einem gemeinsamen Handler greift stattdessen sauber der gemeinsame
    # Fallback.
    conversation_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", _cmd_add_start),
            CommandHandler("repay", _cmd_repay_start),
            CommandHandler("edit", _cmd_edit_start),
            CommandHandler("delete", _cmd_delete_start),
        ],
        states={
            WER: [CallbackQueryHandler(_cb_wer, pattern=r"^wer:")],
            BETRAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_betrag)],
            BESCHREIBUNG: [MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_beschreibung)],
            EDIT_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_edit_select)],
            DELETE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_delete_select)],
            DELETE_CONFIRM: [CallbackQueryHandler(_cb_delete_confirm, pattern=r"^delconfirm:(yes|no)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _conversation_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", _cmd_cancel),
            MessageHandler(filters.ALL, _cmd_conversation_fallback),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(CommandHandler("help", _cmd_start))
    application.add_handler(conversation_handler)
    application.add_handler(CommandHandler("saldo", _cmd_saldo))
    application.add_handler(CommandHandler("recent", _cmd_recent))
    application.add_handler(CommandHandler("undo", _cmd_undo))
    application.add_handler(CommandHandler("export", _cmd_export))
    application.add_handler(CommandHandler("users", _cmd_users))
    application.add_handler(CommandHandler("authorize", _cmd_authorize))
    application.add_handler(CommandHandler("deauthorize", _cmd_deauthorize))
    application.add_handler(CommandHandler("backup", _cmd_backup))
    application.add_handler(CommandHandler("restart", _cmd_restart))
    application.add_handler(CallbackQueryHandler(_cb_new_user_authorize, pattern=r"^newuser_auth:\d+$"))
    application.add_handler(CallbackQueryHandler(_cb_new_user_reject, pattern=r"^newuser_reject:\d+$"))
    application.add_handler(CallbackQueryHandler(_cb_deauthorize, pattern=r"^deauth:\d+$"))
    application.add_error_handler(_on_error)

    if config.BACKUP_CHAT_ID and application.job_queue is not None:
        application.job_queue.run_repeating(
            _job_backup, interval=BACKUP_INTERVAL_SECONDS, first=BACKUP_FIRST_DELAY_SECONDS
        )

    return application
