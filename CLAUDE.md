# Schuldentracker

Telegram-Bot zum gemeinsamen Tracken gegenseitiger Ausgaben (zwei Nutzer: Johannes & Anna).
"Entschlackte" Variante des Schwesterprojekts `urlaubstracker` (gleiche Architektur, gleicher
Autor) -- kein Trip-Konzept, keine Kategorien, keine Fremdwährung, dafür mit einer eigenen
Rückzahlungs-Funktion. Läuft als eigener systemd-Dienst parallel zu `urlaubstracker` und
`telegram-scraper` auf demselben Raspberry Pi.

## Architektur

- **Speicherung**: EINE Excel-Datei (`data/schulden.xlsx`, openpyxl) für den gesamten,
  durchgehenden Ledger -- kein Trip/Konten-Konzept wie beim Urlaubstracker. Ledger-Blatt
  ("Ledger") + ein bei jeder Änderung komplett neu gebautes Übersichts-Blatt ("Übersicht") mit
  Balkendiagramm und aktuellem Saldo.
- **Zwei Eintragsarten in einer Tabelle** (Spalte "Typ"): `Ausgabe` (wird automatisch 50/50
  zwischen den beiden Personen aufgeteilt) und `Rückzahlung` (voller Betrag reduziert direkt die
  Schuld). Saldo-Berechnung (`get_balance` in `expenses/ledger_store.py`) verrechnet beide
  Eintragsarten zu einem einzigen Netto-Saldo pro Person; die Summe beider Salden ist dabei
  immer 0 (siehe Kommentar dort für die genaue Herleitung).
- **DB**: SQLite (`data/schuldentracker.db`) NUR für die Autorisierung (`authorized_users`) --
  kein Trip-/aktive-Auswahl-Konzept wie beim Urlaubstracker, da es nur einen einzigen Ledger
  gibt.
- **Bot**: python-telegram-bot. Ein einziger `ConversationHandler` bündelt /add, /repay, /edit,
  /delete (gleicher Grund wie beim Urlaubstracker, siehe Kommentar in `build_bot_application()`).
- **Backup**: täglicher automatischer Telegram-Versand (Ledger-Excel + DB) an eine feste
  `config.BACKUP_CHAT_ID`, zusätzlich ein schlankeres Backup (nur die Excel-Datei) nach jeder
  einzelnen Änderung -- unabhängig vom Superuser. Zusätzlich manuell per `/backup`.

## Wichtige Konventionen

- Excel-Writes sind **atomar** (`_atomic_save` in `expenses/ledger_store.py`) -- nie direkt
  überschreiben, wegen Absturz-/Stromausfallsicherheit auf der Pi-SD-Karte.
- Betrag-Eingabe (`_parse_amount`) lehnt nan/inf/negative/unrealistisch große Werte ab und
  rundet VOR der Nullprüfung.
- `PERSONEN` in `expenses/ledger_store.py` geht FEST von genau zwei Personen aus -- die
  Saldo-Berechnung (`get_balance`, `_other_person`) funktioniert nur für exakt zwei Einträge in
  dieser Liste.
- Der `ConversationHandler`-Fallback fängt **alles** ab (`filters.ALL`, nicht nur Befehle) --
  Text während eines reinen Button-Schritts oder Sticker/Fotos brechen die offene Eingabe
  sauber ab statt sie still hängen zu lassen.
- `/edit` und `/delete` listen ALLE Einträge durchnummeriert auf (Text, kein Button-Limit) --
  Auswahl per eingetippter Nummer. `/delete` verlangt eine explizite Bestätigung
  (Ja/Nein-Buttons) bevor wirklich gelöscht wird. `/edit` erkennt anhand des gespeicherten "Typ"
  automatisch, ob nach "Wer hat gezahlt?" (Ausgabe) oder "Wer zahlt zurück?" (Rückzahlung)
  gefragt wird.

## Deployment

- **Git-basiert**: lokal committen → push zu GitHub (privates Repo) → auf dem Pi `git pull` →
  `sudo systemctl restart schuldentracker.service`.
- Pi-Zugriff: `ssh pi` (Host-Alias, Key-basiert, kein Passwort).
- `.env`, `data/`, `logs/`, `.venv/` sind gitignored -- Secrets (Bot-Token) und persönliche
  Finanzdaten liegen NIE im Repo.
- Auf demselben Pi laufen parallel zwei weitere systemd-Dienste: `urlaubstracker.service`
  (Schwesterprojekt, komplett getrenntes Repo/getrennte Daten), `telegram-scraper.service`
  (unabhängiges drittes Bot-Projekt) und `claude-remote-control.service` (hält eine dauerhafte
  Claude-Code-Session offen, erreichbar über die Claude-App/claude.ai/code von überall, übersteht
  Absturz/Neustart/Stromausfall).
- **`claude-remote-control.service` hat Pi-weit höchste Priorität, höher als jede Aufgabe in
  diesem Repo.** Der Nutzer ist oft im Urlaub ohne jeden anderen Zugriff auf den Pi/dessen
  Netzwerk -- dieser Dienst ist dann der einzige Weg, überhaupt an den Pi heranzukommen. Niemals
  stoppen, neustarten, deaktivieren oder dessen Unit-Datei anfassen; das gilt für jede Person
  UND jeden automatisierten/autonomen Prozess auf diesem Pi, nicht nur für Arbeit an diesem
  Repo. Details (samt Vorfallshistorie) im `telegram-scraper`-Repo unter „Pi-Zuverlässigkeit" in
  dessen `CLAUDE.md`.

## Testing

Kein Unit-Test-Framework im Repo. Verifikation läuft über echte End-to-End-PTB-Dispatch-Tests
(gleiches Vorgehen wie beim Urlaubstracker): echte `telegram.Update`-Objekte via
`Update.de_json(data, bot)` bauen, `ExtBot`-Methoden per `unittest.mock.patch.object` mocken,
`await application.process_update(update)` aufrufen und `ConversationHandler._conversations`
direkt inspizieren. Solche Test-Skripte werden bei Bedarf neu als Scratch-Dateien geschrieben,
nicht im Repo versioniert.
