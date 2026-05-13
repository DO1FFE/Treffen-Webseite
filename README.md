# Treffen-Verwaltungssystem

## Überblick
Diese Flask-Anwendung organisiert die Teilnahme am L11 Clubtreffen. Besucher können sich mit Name oder Rufzeichen anmelden und einen vorhandenen Eintrag über denselben Wert wieder entfernen. Die Adminseite zeigt Teilnehmerliste und Statistik.

## Funktionen
- Zusagen und Absagen mit Name oder Rufzeichen.
- Automatische Statusanzeige, ob das Treffen ab vier Teilnehmenden stattfindet.
- Automatisches Zurücksetzen der Teilnehmerliste freitags um 21:00 Uhr.
- Adminbereich mit HTTP-Basic-Auth.
- Optionales DAPNET-Logging, wenn Zugangsdaten hinterlegt sind.

## Voraussetzungen
- Python 3.11 oder neuer
- pip

## Installation
1. Repository klonen:
   ```powershell
   git clone https://github.com/DO1FFE/Treffen-Webseite.git
   cd Treffen-Webseite
   ```

2. Abhängigkeiten installieren:
   ```powershell
   python -m pip install -r requirements.txt
   ```

## Konfiguration
Die App kann ohne `.pwd` für die öffentliche Seite starten. Für den Adminbereich werden Zugangsdaten benötigt.

Lege dafür eine `.pwd`-Datei an:
```env
ADMIN_USERNAME=IhrBenutzername
ADMIN_PASSWORD=IhrPasswort
```

Optional kann DAPNET aktiviert werden:
```env
DAPNET_USERNAME=IhrRufzeichen
DAPNET_PASSWORD=IhrPasswort
```

Alternativ können dieselben Werte als Umgebungsvariablen gesetzt werden.

## Ausführung
Starte die Anwendung mit:
```powershell
python treff.py
```

Die Anwendung läuft dann unter `http://localhost:8083/`.

Bei Bedarf kann der Port geändert werden:
```powershell
$env:PORT=8090
python treff.py
```

## Lokale Dateien
Die App erzeugt lokale Laufzeitdateien wie `meeting.db`, `treff.log` und `teilnahmen.log`. Diese Dateien sind in `.gitignore` ausgeschlossen.

## Beitrag
Beiträge sind willkommen. Bitte erstelle einen Pull Request oder ein Issue, wenn du Änderungen oder Verbesserungen vorschlagen möchtest.
