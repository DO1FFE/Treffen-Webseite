from datetime import datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
import logging
import os
import re
import sqlite3
import threading
import time

import requests
from flask import Flask, Response, redirect, render_template_string, request, send_from_directory, url_for


ERSTELLUNGSJAHR = 2024
ZEITZONE = ZoneInfo("Europe/Berlin")
MINDESTTEILNEHMER = 4
MAX_EINGABE_LAENGE = 60

RESET_WOCHENTAG = 4  # Freitag (Montag=0, Dienstag=1, ..., Sonntag=6)
RESET_STUNDE = 21
RESET_MINUTE = 0

EINGABE_MUSTER = re.compile(r"^[A-Z0-9ÄÖÜẞ\s-]+$", re.IGNORECASE)


def logger_einrichten():
    logger = logging.getLogger("TreffenLogger")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = RotatingFileHandler("treff.log", maxBytes=10000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)

    return logger


logger = logger_einrichten()


def lade_konfiguration(pfad=".pwd"):
    konfiguration = {}

    if os.path.exists(pfad):
        with open(pfad, "r", encoding="utf-8") as datei:
            for zeile in datei:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue

                schluessel, wert = zeile.split("=", 1)
                konfiguration[schluessel.strip()] = wert.strip()

    for schluessel in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "DAPNET_USERNAME",
        "DAPNET_PASSWORD",
        "dapnet_username",
        "dapnet_password",
    ):
        if os.environ.get(schluessel):
            konfiguration[schluessel] = os.environ[schluessel]

    return konfiguration


def konfigurationswert(konfiguration, *schluessel):
    for eintrag in schluessel:
        wert = konfiguration.get(eintrag)
        if wert:
            return wert
    return None


konfiguration = lade_konfiguration()
ADMIN_BENUTZERNAME = konfigurationswert(konfiguration, "ADMIN_USERNAME")
ADMIN_PASSWORT = konfigurationswert(konfiguration, "ADMIN_PASSWORD")


class DAPNET:
    """
    Client für die DAPNET-API.
    Nachrichten werden nur gesendet, wenn Zugangsdaten konfiguriert sind.
    """

    def __init__(self, rufzeichen=None, passwort=None, url="http://www.hampager.de:8080/calls"):
        self.rufzeichen = rufzeichen
        self.passwort = passwort
        self.url = url
        self.headers = {"Content-type": "application/json"}

    @property
    def ist_aktiv(self):
        return bool(self.rufzeichen and self.passwort)

    def sende_nachricht(self, nachricht, ziel_rufzeichen, sendergruppe, notfall=False):
        if not self.ist_aktiv:
            logger.info("DAPNET ist nicht konfiguriert; Nachricht wurde nicht gesendet.")
            return None

        daten = {
            "text": nachricht,
            "callSignNames": [ziel_rufzeichen] if isinstance(ziel_rufzeichen, str) else ziel_rufzeichen,
            "transmitterGroupNames": [sendergruppe] if isinstance(sendergruppe, str) else sendergruppe,
            "emergency": notfall,
        }

        try:
            antwort = requests.post(
                self.url,
                headers=self.headers,
                auth=(self.rufzeichen, self.passwort),
                json=daten,
                timeout=5,
            )
            antwort.raise_for_status()
            return antwort
        except requests.RequestException as fehler:
            logger.warning("DAPNET-Nachricht konnte nicht gesendet werden: %s", fehler)
            return None

    def logge_nachricht(self, nachricht, ziel_rufzeichen, sendergruppe, notfall=False):
        return self.sende_nachricht(nachricht, ziel_rufzeichen, sendergruppe, notfall)


dapnet_client = DAPNET(
    konfigurationswert(konfiguration, "DAPNET_USERNAME", "dapnet_username"),
    konfigurationswert(konfiguration, "DAPNET_PASSWORD", "dapnet_password"),
)


class DatenbankVerwaltung:
    def __init__(self, datenbank_name="meeting.db"):
        self.datenbank_name = datenbank_name
        self.sperre = threading.Lock()
        self.datenbank_initialisieren()

    def datenbank_initialisieren(self):
        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            cursor = verbindung.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    name TEXT NOT NULL DEFAULT '',
                    call_sign TEXT NOT NULL DEFAULT ''
                )
                """
            )
            verbindung.commit()

    def datenbank_zuruecksetzen(self):
        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            verbindung.execute("DELETE FROM meetings")
            verbindung.commit()

    def eintrag_hinzufuegen(self, name, rufzeichen):
        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            verbindung.execute(
                "INSERT INTO meetings (name, call_sign) VALUES (?, ?)",
                (name, rufzeichen),
            )
            verbindung.commit()

        logger.info("Eintrag hinzugefügt: Rufzeichen: %s, Name: %s", rufzeichen, name)
        dapnet_client.logge_nachricht(
            f"Treff: Eintrag hinzugefügt: Rufzeichen: {rufzeichen}, Name: {name}",
            "DO1FFE",
            "all",
            False,
        )

    def eintrag_loeschen(self, name, rufzeichen):
        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            if name and rufzeichen:
                verbindung.execute(
                    "DELETE FROM meetings WHERE name = ? AND call_sign = ?",
                    (name, rufzeichen),
                )
            elif name:
                verbindung.execute("DELETE FROM meetings WHERE name = ?", (name,))
            elif rufzeichen:
                verbindung.execute("DELETE FROM meetings WHERE call_sign = ?", (rufzeichen,))
            verbindung.commit()

        logger.info("Eintrag gelöscht: Rufzeichen: %s, Name: %s", rufzeichen, name)
        dapnet_client.logge_nachricht(
            f"Treff: Eintrag gelöscht: Rufzeichen: {rufzeichen}, Name: {name}",
            "DO1FFE",
            "all",
            False,
        )

    def eintrag_existiert(self, name, rufzeichen):
        if not name and not rufzeichen:
            return False

        abfrage = "SELECT 1 FROM meetings WHERE "
        werte = ()

        if name and rufzeichen:
            abfrage += "name = ? AND call_sign = ?"
            werte = (name, rufzeichen)
        elif name:
            abfrage += "name = ?"
            werte = (name,)
        else:
            abfrage += "call_sign = ?"
            werte = (rufzeichen,)

        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            cursor = verbindung.execute(f"{abfrage} LIMIT 1", werte)
            return cursor.fetchone() is not None

    def alle_eintraege(self):
        with self.sperre, sqlite3.connect(self.datenbank_name) as verbindung:
            cursor = verbindung.execute(
                """
                SELECT name, call_sign
                FROM meetings
                ORDER BY rowid ASC
                """
            )
            return cursor.fetchall()

    def treff_informationen(self):
        teilnehmer = self.alle_eintraege()
        return len(teilnehmer), teilnehmer


datenbank = DatenbankVerwaltung()


def aktuelle_zeit():
    return datetime.now(ZEITZONE)


def naechstes_treffen_datum():
    jetzt = aktuelle_zeit()
    naechster_freitag = jetzt + timedelta((RESET_WOCHENTAG - jetzt.weekday()) % 7)

    if jetzt.weekday() == RESET_WOCHENTAG and jetzt.hour >= RESET_STUNDE:
        naechster_freitag += timedelta(days=7)

    return naechster_freitag.strftime("%d.%m.%Y")


def aktuelles_jahr():
    return aktuelle_zeit().year


def copyright_text():
    jahr = aktuelles_jahr()
    jahresangabe = str(ERSTELLUNGSJAHR)

    if jahr > ERSTELLUNGSJAHR:
        jahresangabe = f"{ERSTELLUNGSJAHR} - {jahr}"

    return f"© {jahresangabe} Erik Schauer, do1ffe@darc.de"


def server_port():
    try:
        return int(os.environ.get("PORT", "8083"))
    except ValueError:
        logger.warning("Ungültiger PORT-Wert, verwende 8083.")
        return 8083


def eingabe_ist_gueltig(text):
    if text is None or text.strip() == "":
        return True

    if len(text) > MAX_EINGABE_LAENGE:
        return False

    return EINGABE_MUSTER.fullmatch(text) is not None


def eingabe_normalisieren(text):
    return text.strip().upper()


def anmeldung_erforderlich(funktion):
    @wraps(funktion)
    def dekorierte_funktion(*args, **kwargs):
        if not ADMIN_BENUTZERNAME or not ADMIN_PASSWORT:
            return Response("Der Admin-Zugang ist noch nicht konfiguriert.", 503)

        auth = request.authorization
        if not auth or not (auth.username == ADMIN_BENUTZERNAME and auth.password == ADMIN_PASSWORT):
            return Response(
                "Bitte Anmeldedaten eingeben",
                401,
                {"WWW-Authenticate": 'Basic realm="Treffen Admin"'},
            )

        return funktion(*args, **kwargs)

    return dekorierte_funktion


def teilnehmer_in_datei_loggen(teilnehmer, log_datei_pfad="teilnahmen.log"):
    if not teilnehmer:
        return

    aktuelles_datum = aktuelle_zeit().strftime("%d.%m.%Y")

    with open(log_datei_pfad, "a", encoding="utf-8") as datei:
        for name, rufzeichen in teilnehmer:
            datei.write(f"{aktuelles_datum}, {rufzeichen}, {name}\n")


def woechentlicher_datenbank_reset():
    logger.info("Reset-Thread gestartet.")

    while True:
        jetzt = aktuelle_zeit()
        tage_bis_reset = (RESET_WOCHENTAG - jetzt.weekday()) % 7
        naechster_reset = jetzt + timedelta(days=tage_bis_reset)
        naechster_reset = naechster_reset.replace(
            hour=RESET_STUNDE,
            minute=RESET_MINUTE,
            second=0,
            microsecond=0,
        )

        if naechster_reset <= jetzt:
            naechster_reset += timedelta(days=7)

        wartezeit = (naechster_reset - jetzt).total_seconds()
        logger.info("Nächstes Datenbank-Reset geplant für: %s", naechster_reset)
        time.sleep(max(wartezeit, 60))

        teilnehmer = datenbank.alle_eintraege()
        teilnehmer_in_datei_loggen(teilnehmer)
        datenbank.datenbank_zuruecksetzen()
        logger.info("Datenbank wurde zurückgesetzt.")


def anmeldungen_erlaubt():
    lokale_zeit = aktuelle_zeit()
    return not (lokale_zeit.weekday() == RESET_WOCHENTAG and 12 <= lokale_zeit.hour < RESET_STUNDE)


def treff_status_text(teilnehmer_anzahl, ist_anmeldung_erlaubt):
    datum = naechstes_treffen_datum()

    if teilnehmer_anzahl >= MINDESTTEILNEHMER:
        if ist_anmeldung_erlaubt:
            return (
                f"Das Treffen am {datum} findet statt. Es haben sich {teilnehmer_anzahl} Personen angemeldet. "
                "Bitte trotzdem weiter anmelden, falls wieder jemand absagt."
            )
        return f"Das Treffen am {datum} findet statt. Es haben sich {teilnehmer_anzahl} Personen angemeldet."

    if ist_anmeldung_erlaubt:
        return (
            f"Das Treffen am {datum} findet wegen zu geringer Beteiligung ({teilnehmer_anzahl} Personen) noch nicht statt. "
            f"Sobald mindestens {MINDESTTEILNEHMER} Personen angemeldet sind, findet es statt."
        )

    return (
        f"Das Treffen am {datum} findet wegen zu geringer Beteiligung ({teilnehmer_anzahl} Personen) nicht statt. "
        "Vielleicht klappt es nächsten Freitag."
    )


treff = Flask(__name__)


@treff.route("/", methods=["GET", "POST"])
def index():
    fehler_meldung = ""

    if request.method == "POST":
        name = eingabe_normalisieren(request.form.get("name", ""))
        rufzeichen = eingabe_normalisieren(request.form.get("call_sign", ""))

        if not eingabe_ist_gueltig(name) or not eingabe_ist_gueltig(rufzeichen):
            fehler_meldung = (
                "Ungültige Eingabe. Bitte nur Buchstaben, Zahlen, Leerzeichen und Bindestriche verwenden "
                f"(maximal {MAX_EINGABE_LAENGE} Zeichen)."
            )
        elif not name and not rufzeichen:
            fehler_meldung = "Bitte mindestens Rufzeichen oder Name ausfüllen."
        elif datenbank.eintrag_existiert(name, rufzeichen):
            return redirect(url_for("loeschen_bestaetigen", name=name, call_sign=rufzeichen))
        else:
            datenbank.eintrag_hinzufuegen(name, rufzeichen)

    teilnehmer_anzahl, teilnehmer = datenbank.treff_informationen()
    teilnehmer_mit_index = list(enumerate(teilnehmer, start=1))
    ist_anmeldung_erlaubt = anmeldungen_erlaubt()
    treffen_findet_statt = teilnehmer_anzahl >= MINDESTTEILNEHMER

    return render_template_string(
        INDEX_TEMPLATE,
        copyright=copyright_text(),
        fehler_meldung=fehler_meldung,
        ist_anmeldung_erlaubt=ist_anmeldung_erlaubt,
        mindestteilnehmer=MINDESTTEILNEHMER,
        naechstes_treffen=naechstes_treffen_datum(),
        status_klasse="status-ok" if treffen_findet_statt else "status-offen",
        statuskarte_klasse="statuskarte-ok" if treffen_findet_statt else "statuskarte-offen",
        status_text="Treffen findet statt" if treffen_findet_statt else "Noch zu wenige Zusagen",
        teilnehmer_anzahl=teilnehmer_anzahl,
        teilnehmer_mit_index=teilnehmer_mit_index,
        treffen_findet_statt=treffen_findet_statt,
        treffen_status=treff_status_text(teilnehmer_anzahl, ist_anmeldung_erlaubt),
    )


@treff.route("/confirm_delete")
def loeschen_bestaetigen():
    name = request.args.get("name", "")
    rufzeichen = request.args.get("call_sign", "")

    return render_template_string(
        LOESCHEN_TEMPLATE,
        copyright=copyright_text(),
        name=name,
        rufzeichen=rufzeichen,
    )


@treff.route("/delete", methods=["POST"])
def loeschen():
    name = request.form.get("name", "")
    rufzeichen = request.form.get("call_sign", "")
    datenbank.eintrag_loeschen(name, rufzeichen)
    return redirect(url_for("index"))


@treff.route("/admin")
@anmeldung_erforderlich
def admin():
    _, teilnehmer = datenbank.treff_informationen()
    teilnehmer_mit_index = list(enumerate(teilnehmer, start=1))
    statistik_pfad = os.path.join("statistik", "teilnahmen_statistik.png")

    return render_template_string(
        ADMIN_TEMPLATE,
        copyright=copyright_text(),
        statistik_vorhanden=os.path.exists(statistik_pfad),
        teilnehmer_mit_index=teilnehmer_mit_index,
    )


@treff.route("/statistik/<filename>")
def statistik(filename):
    return send_from_directory("statistik", filename)


SEITEN_CSS = """
    :root {
        --hintergrund: #e8e8e8;
        --flaeche: #ffffff;
        --flaeche-zart: #fdf4e7;
        --text: #273d5e;
        --text-weich: #485a63;
        --linie: #cfd6db;
        --akzent: #2aa6da;
        --akzent-mittel: #0076b5;
        --akzent-dunkel: #273d5e;
        --darc-grau: #90989e;
        --warnung: #c15413;
        --warnung-dunkel: #8a2d0b;
        --warnung-flaeche: #fff0e8;
        --ok: #278a45;
        --ok-dunkel: #176234;
        --ok-flaeche: #edf9f0;
        --sonne: #f7a900;
        --schatten: 0 22px 60px rgba(39, 61, 94, 0.14);
    }

    * {
        box-sizing: border-box;
    }

    body {
        margin: 0;
        min-height: 100vh;
        color: var(--text);
        background:
            linear-gradient(180deg, #ffffff 0%, var(--hintergrund) 46%, #f4f7f9 100%);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        line-height: 1.55;
    }

    a {
        color: var(--akzent-dunkel);
        text-decoration: none;
        font-weight: 700;
    }

    a:hover {
        text-decoration: underline;
    }

    .seitenkopf {
        padding: 24px clamp(18px, 4vw, 56px) 44px;
    }

    .kopfleiste,
    .hero,
    .bereich,
    .fusszeile {
        width: min(1120px, 100%);
        margin: 0 auto;
    }

    .kopfleiste {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        margin-bottom: 46px;
    }

    .marke {
        display: flex;
        align-items: center;
        gap: 14px;
        min-width: 0;
    }

    .markenzeichen {
        display: grid;
        place-items: center;
        width: 48px;
        height: 48px;
        flex: 0 0 auto;
        border: 1px solid rgba(42, 166, 218, 0.34);
        border-radius: 16px;
        background: linear-gradient(135deg, var(--akzent-dunkel), var(--akzent), var(--akzent-mittel));
        color: white;
        font-weight: 900;
        letter-spacing: 0;
        box-shadow: 0 14px 34px rgba(39, 61, 94, 0.18);
    }

    .marke strong {
        display: block;
        font-size: 1.02rem;
    }

    .marke span:last-child {
        display: block;
        color: var(--text-weich);
        font-size: 0.92rem;
    }

    .nav-link {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 42px;
        padding: 0 16px;
        border: 1px solid rgba(42, 166, 218, 0.35);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.82);
        box-shadow: 0 10px 28px rgba(39, 61, 94, 0.08);
    }

    .hero {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(300px, 390px);
        gap: clamp(24px, 5vw, 56px);
        align-items: end;
    }

    .hero h1 {
        max-width: 760px;
        margin: 0 0 18px;
        color: var(--akzent);
        font-size: clamp(2.35rem, 7vw, 5.2rem);
        line-height: 0.96;
        letter-spacing: 0;
    }

    .vorspann {
        margin: 0 0 18px;
        color: var(--akzent-dunkel);
        font-size: 0.84rem;
        font-weight: 900;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    .hero-text {
        max-width: 700px;
        color: var(--text-weich);
        font-size: clamp(1rem, 2vw, 1.16rem);
    }

    .statuskarte,
    .panel,
    .hinweisbox {
        border: 1px solid rgba(39, 61, 94, 0.16);
        background: rgba(255, 255, 255, 0.92);
        box-shadow: var(--schatten);
        backdrop-filter: blur(16px);
    }

    .statuskarte {
        border-radius: 24px;
        padding: 24px;
    }

    .statuskarte-offen {
        border-color: rgba(193, 84, 19, 0.38);
        background: linear-gradient(135deg, var(--warnung-flaeche), #ffe0d3);
        color: var(--warnung-dunkel);
    }

    .statuskarte-ok {
        border-color: rgba(39, 138, 69, 0.38);
        background: linear-gradient(135deg, var(--ok-flaeche), #dff4e6);
        color: var(--ok-dunkel);
    }

    .statuskopf {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 14px;
        margin-bottom: 22px;
    }

    .status-badge {
        display: inline-flex;
        align-items: center;
        min-height: 34px;
        padding: 0 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 900;
    }

    .status-ok {
        color: white;
        background: var(--ok);
    }

    .status-offen {
        color: white;
        background: var(--warnung);
    }

    .anzahl {
        display: grid;
        gap: 2px;
    }

    .anzahl strong {
        font-size: 4rem;
        line-height: 0.9;
    }

    .anzahl span {
        color: var(--text-weich);
        font-weight: 700;
    }

    .statuskarte-offen .anzahl span,
    .statuskarte-offen .statuskopf > span:last-child,
    .statuskarte-offen p {
        color: var(--warnung-dunkel);
    }

    .statuskarte-ok .anzahl span,
    .statuskarte-ok .statuskopf > span:last-child,
    .statuskarte-ok p {
        color: var(--ok-dunkel);
    }

    .statuskarte p {
        margin: 18px 0 0;
    }

    main {
        padding: 0 clamp(18px, 4vw, 56px) 56px;
    }

    .app-raster {
        display: grid;
        grid-template-columns: minmax(300px, 0.88fr) minmax(0, 1.12fr);
        gap: 24px;
        align-items: start;
    }

    .panel {
        border-radius: 24px;
        padding: clamp(22px, 4vw, 32px);
    }

    .panel h2 {
        margin: 0 0 18px;
        color: var(--akzent);
        font-size: clamp(1.35rem, 3vw, 2rem);
        line-height: 1.12;
        letter-spacing: 0;
    }

    .formular {
        display: grid;
        gap: 18px;
    }

    .formular label {
        display: grid;
        gap: 8px;
        color: var(--text-weich);
        font-weight: 800;
    }

    .formular input[type="text"] {
        width: 100%;
        min-height: 52px;
        border: 1px solid var(--linie);
        border-radius: 14px;
        padding: 0 16px;
        background: white;
        color: var(--text);
        font: inherit;
        font-weight: 700;
        outline: none;
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }

    .formular input[type="text"]:focus {
        border-color: var(--akzent);
        box-shadow: 0 0 0 4px rgba(42, 166, 218, 0.2);
    }

    .formular input:disabled {
        cursor: not-allowed;
        opacity: 0.58;
    }

    .hauptaktion,
    .nebenaktion,
    .warnaktion {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 52px;
        border: 0;
        border-radius: 16px;
        padding: 0 18px;
        font: inherit;
        font-weight: 900;
        cursor: pointer;
        transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
    }

    .hauptaktion {
        width: 100%;
        color: white;
        background: var(--akzent);
        box-shadow: 0 16px 32px rgba(39, 61, 94, 0.22);
    }

    .hauptaktion:hover {
        background: var(--akzent-mittel);
        transform: translateY(-1px);
    }

    .hauptaktion:disabled {
        cursor: not-allowed;
        transform: none;
        opacity: 0.58;
        box-shadow: none;
    }

    .nebenaktion,
    .warnaktion {
        border: 1px solid var(--linie);
        background: white;
        color: var(--text);
    }

    .warnaktion {
        border-color: rgba(180, 35, 24, 0.28);
        color: var(--warnung);
    }

    .fehler {
        margin: 0;
        padding: 12px 14px;
        border: 1px solid rgba(180, 35, 24, 0.22);
        border-radius: 14px;
        background: var(--warnung-flaeche);
        color: var(--warnung);
        font-weight: 800;
    }

    .hinweisbox {
        margin-top: 18px;
        border-radius: 18px;
        padding: 16px;
        color: var(--text-weich);
    }

    .hinweisbox strong {
        color: var(--text);
    }

    .tabelle-huelle {
        overflow-x: auto;
        border: 1px solid var(--linie);
        border-radius: 18px;
        background: white;
    }

    table {
        width: 100%;
        border-collapse: collapse;
    }

    th,
    td {
        padding: 14px 16px;
        text-align: left;
        border-bottom: 1px solid #e3e7ea;
        vertical-align: top;
    }

    th {
        color: var(--text-weich);
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        background: #f4f7f9;
    }

    tr:last-child td {
        border-bottom: 0;
    }

    .nummer {
        width: 64px;
        color: var(--text-weich);
        font-weight: 900;
    }

    .leer {
        color: var(--text-weich);
        text-align: center;
    }

    .adressblock {
        display: grid;
        gap: 4px;
        margin-top: 12px;
    }

    .bestaetigung {
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
    }

    .dialog {
        width: min(560px, 100%);
    }

    .aktionszeile {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 24px;
    }

    .statistikbild {
        width: 100%;
        max-width: 760px;
        border: 1px solid var(--linie);
        border-radius: 18px;
        background: white;
    }

    .fusszeile {
        padding: 20px 0 0;
        color: var(--text-weich);
        font-size: 0.92rem;
    }

    .fusszeile p {
        margin: 6px 0;
    }

    @media (max-width: 860px) {
        .hero,
        .app-raster {
            grid-template-columns: 1fr;
        }

        .kopfleiste {
            align-items: flex-start;
        }

        .hero h1 {
            font-size: clamp(2.4rem, 13vw, 4rem);
        }
    }

    @media (max-width: 560px) {
        .seitenkopf {
            padding-top: 18px;
        }

        .kopfleiste {
            flex-direction: column;
            margin-bottom: 34px;
        }

        .nav-link {
            width: 100%;
        }

        .statuskopf,
        .aktionszeile {
            align-items: stretch;
            flex-direction: column;
        }

        .panel,
        .statuskarte {
            border-radius: 18px;
        }

        th,
        td {
            padding: 12px;
        }
    }
"""


INDEX_TEMPLATE = """
<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>L11 Clubtreffen</title>
    <style>{{ css|safe }}</style>
</head>
<body>
    <!-- scrape: treffen_findet_statt={{ 'ja' if treffen_findet_statt else 'nein' }}; angemeldete_personen={{ teilnehmer_anzahl }} -->
    <header class="seitenkopf">
        <nav class="kopfleiste" aria-label="Hauptnavigation">
            <a class="marke" href="{{ url_for('index') }}">
                <span class="markenzeichen">L11</span>
                <span>
                    <strong>DARC OV L11</strong>
                    <span>Clubtreffen Essen</span>
                </span>
            </a>
        </nav>

        <section class="hero">
            <div class="hero-text">
                <p class="vorspann">Freitag, {{ naechstes_treffen }} · 17:00 bis 21:00 Uhr</p>
                <h1>Teilnahme am Clubtreffen</h1>
                <p>Haus der Begegnung, I. Weberstraße 28, 45127 Essen-Mitte.</p>
            </div>
            <aside class="statuskarte {{ statuskarte_klasse }}" aria-label="Aktueller Treffenstatus">
                <div class="statuskopf">
                    <span class="status-badge {{ status_klasse }}">{{ status_text }}</span>
                    <span>mind. {{ mindestteilnehmer }}</span>
                </div>
                <div class="anzahl">
                    <strong>{{ teilnehmer_anzahl }}</strong>
                    <span>angemeldete Personen</span>
                </div>
                <p>{{ treffen_status }}</p>
            </aside>
        </section>
    </header>

    <main>
        <section class="bereich app-raster">
            <div class="panel">
                <h2>Zusagen oder absagen</h2>
                {% if fehler_meldung %}
                    <p class="fehler" role="alert">{{ fehler_meldung }}</p>
                {% endif %}
                <form class="formular" method="post">
                    <label>
                        Rufzeichen
                        <input type="text" name="call_sign" autocomplete="nickname" {% if not ist_anmeldung_erlaubt %}disabled{% endif %}>
                    </label>
                    <label>
                        Name
                        <input type="text" name="name" autocomplete="name" {% if not ist_anmeldung_erlaubt %}disabled{% endif %}>
                    </label>
                    <button class="hauptaktion" type="submit" {% if not ist_anmeldung_erlaubt %}disabled{% endif %}>
                        Eintrag speichern
                    </button>
                </form>

                <div class="hinweisbox">
                    <strong>Anmeldeschluss ist Freitag um 12:00 Uhr.</strong>
                    <p>Rufzeichen oder Name genügt. Wer bereits eingetragen ist, kann den Eintrag über denselben Wert wieder entfernen.</p>
                    <div class="adressblock">
                        <span>I. Weberstraße 28</span>
                        <span>45127 Essen-Mitte</span>
                    </div>
                </div>
            </div>

            <div class="panel">
                <h2>Teilnehmerliste</h2>
                <div class="tabelle-huelle">
                    <table>
                        <thead>
                            <tr>
                                <th class="nummer">#</th>
                                <th>Rufzeichen</th>
                                <th>Name</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for index, (name, rufzeichen) in teilnehmer_mit_index %}
                                <tr>
                                    <td class="nummer">{{ index }}</td>
                                    <td>{{ rufzeichen or '—' }}</td>
                                    <td>{{ name or '—' }}</td>
                                </tr>
                            {% else %}
                                <tr>
                                    <td class="leer" colspan="3">Noch keine Anmeldungen vorhanden.</td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                <div class="hinweisbox">
                    <p>Fehler bitte per Mail an <a href="mailto:do1ffe@darc.de">do1ffe@darc.de</a> senden. Vy 73 Erik, DO1FFE - OVV L11</p>
                </div>
            </div>
        </section>
    </main>

    <footer class="fusszeile">
        <p>{{ copyright }}</p>
        <p><strong>Hinweis:</strong> Alle Daten auf dieser Seite sind streng vertraulich und dürfen nicht auf anderen Plattformen weiterverwendet werden.</p>
    </footer>
</body>
</html>
""".replace("{{ css|safe }}", SEITEN_CSS)


LOESCHEN_TEMPLATE = """
<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Eintrag löschen</title>
    <style>{{ css|safe }}</style>
</head>
<body class="bestaetigung">
    <main class="panel dialog">
        <p class="vorspann">Eintrag gefunden</p>
        <h1>Eintrag löschen?</h1>
        <p>Möchtest du den Eintrag für <strong>{{ name or rufzeichen }}</strong> wirklich löschen?</p>
        <form action="{{ url_for('loeschen') }}" method="post" class="aktionszeile">
            <input type="hidden" name="name" value="{{ name }}">
            <input type="hidden" name="call_sign" value="{{ rufzeichen }}">
            <button class="warnaktion" type="submit">Ja, löschen</button>
            <a class="nebenaktion" href="{{ url_for('index') }}">Abbrechen</a>
        </form>
        <footer class="fusszeile">
            <p>{{ copyright }}</p>
        </footer>
    </main>
</body>
</html>
""".replace("{{ css|safe }}", SEITEN_CSS)


ADMIN_TEMPLATE = """
<!doctype html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Treffen Admin</title>
    <style>{{ css|safe }}</style>
</head>
<body>
    <header class="seitenkopf">
        <nav class="kopfleiste" aria-label="Hauptnavigation">
            <a class="marke" href="{{ url_for('index') }}">
                <span class="markenzeichen">L11</span>
                <span>
                    <strong>DARC OV L11</strong>
                    <span>Administration</span>
                </span>
            </a>
            <a class="nav-link" href="{{ url_for('index') }}">Hauptseite</a>
        </nav>
        <section class="hero">
            <div class="hero-text">
                <p class="vorspann">Verwaltung</p>
                <h1>Treffen Admin</h1>
                <p>Teilnehmer und Statistik im Überblick.</p>
            </div>
        </section>
    </header>

    <main>
        <section class="bereich app-raster">
            <div class="panel">
                <h2>Statistik</h2>
                {% if statistik_vorhanden %}
                    <img class="statistikbild" src="{{ url_for('statistik', filename='teilnahmen_statistik.png') }}" alt="Teilnahmen-Statistik">
                {% else %}
                    <p class="leer">Noch keine Statistikgrafik vorhanden.</p>
                {% endif %}
            </div>

            <div class="panel">
                <h2>Teilnehmerliste</h2>
                <div class="tabelle-huelle">
                    <table>
                        <thead>
                            <tr>
                                <th class="nummer">#</th>
                                <th>Rufzeichen</th>
                                <th>Name</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for index, (name, rufzeichen) in teilnehmer_mit_index %}
                                <tr>
                                    <td class="nummer">{{ index }}</td>
                                    <td>{{ rufzeichen or '—' }}</td>
                                    <td>{{ name or '—' }}</td>
                                </tr>
                            {% else %}
                                <tr>
                                    <td class="leer" colspan="3">Noch keine Anmeldungen vorhanden.</td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </section>
    </main>

    <footer class="fusszeile">
        <p>{{ copyright }}</p>
        <p><strong>Hinweis:</strong> Alle Daten auf dieser Seite sind streng vertraulich und dürfen nicht auf anderen Plattformen weiterverwendet werden.</p>
    </footer>
</body>
</html>
""".replace("{{ css|safe }}", SEITEN_CSS)


if __name__ == "__main__":
    if not treff.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        logger.info("Hauptprogramm gestartet, starte den Reset-Thread.")
        db_reset_thread = threading.Thread(
            target=woechentlicher_datenbank_reset,
            name="Datenbank-Reset",
            daemon=True,
        )
        db_reset_thread.start()

    treff.run(host="0.0.0.0", port=server_port(), use_reloader=False)
