# Quickstart: Discord Mathematics Research

Dieser Quickstart führt vom Crawl der vier Mathematik-Kanäle bis zum
fortsetzbaren Research-Batch. Die Pipeline ist einsatzbereit; für einen echten
Lauf brauchst du lediglich den Discord-Zugang, die drei angemeldeten
Kuratoren-CLIs und entweder ein lokales Open-Weight-Modell oder das
bereitgestellte Colab-Notebook.

## 1. Voraussetzungen

- Python 3.11 oder neuer und Git
- Discord-Token für den Crawl
- angemeldete CLIs für Claude Code, OpenAI Codex und Google Antigravity
- OpenCode für den günstigen High-Volume-Batch
- für einen lokalen GPU-Lauf: Linux/WSL, CUDA-kompatible GPU und vLLM
- optional: `age`, wenn der verschlüsselte GitHub-Actions-Export benutzt wird

Nutze ausschließlich einen autorisierten Discord-Zugang und Kanäle, auf die du
regulär zugreifen darfst. Die eingebauten Ratelimits nicht herabsetzen oder
umgehen.

Beide Repositories nebeneinander auschecken:

```bash
git clone https://github.com/Erikiss/The-Agentic-Researcher.git
git clone https://github.com/Erikiss/Discord-Mathematics-Early-University.git

cd Discord-Mathematics-Early-University
python -m pip install -r requirements.txt
```

Unter Windows/PowerShell funktionieren die Pipeline-Kommandos über
`python -m agentic_researcher`; Bash oder WSL ist dafür nicht erforderlich.
Die mehrzeiligen Beispiele unten verwenden Bash-Zeilenfortsetzungen.

## 2. Discord-Daten lokal aufbereiten

Token nur für die aktuelle Shell setzen:

```bash
# Bash/WSL
export DISCORD_TOKEN_Backupper123="..."

# PowerShell:
# $env:DISCORD_TOKEN_Backupper123 = "..."
```

Dann crawlen und das Übergabebundle erzeugen:

```bash
python discord_math_crawl.py --days 3 --max 5000
python extract_resources.py
python extract_discussions.py
python materialize_media.py
```

Die relevanten privaten Ausgaben sind:

- `discord_exports/ingest_bundle.json`
- `discord_exports/curation_media/media_manifest.json`
- `discord_exports/curation_media/*` für sicher materialisierte Bilder

`ingest_bundle.json` enthält eine private Rückverfolgungsprojektion und darf
nicht veröffentlicht werden. Der Agentic Researcher sendet an die Kuratoren
nur die bereinigte Projektion.

### Alternative: verschlüsselten Actions-Export verwenden

Im Discord-Repository müssen dafür gesetzt sein:

- Secret `DISCORD_TOKEN_Backupper123`
- empfohlenes Secret `DISCORD_GUILD_ID`; es pinnt den Crawl auf den
  eindeutigen Server und erzeugt klickbare Discord-Quelllinks. Ohne dieses
  Secret bleibt die Namenssuche nach `Mathematics` als Fallback aktiv.
- Variable `DISCORD_EXPORT_AGE_RECIPIENT` mit dem öffentlichen `age1...`-Key

Das Schlüsselpaar einmalig lokal erzeugen; nur den ausgegebenen öffentlichen
Empfänger als Repository-Variable speichern:

```bash
age-keygen --output /sicherer/pfad/discord-export-key.txt
```

Das heruntergeladene `.age`-Artefakt anschließend in den Discord-Checkout
legen und dort entschlüsseln:

```bash
age --decrypt --identity /sicherer/pfad/discord-export-key.txt \
  --output discord-math-export.tar.gz \
  discord-math-export.tar.gz.age
tar -xzf discord-math-export.tar.gz
```

### Windows: täglicher Import ohne manuelles Entschlüsseln

Für den normalen täglichen Betrieb ist die manuelle Variante oben nicht
erforderlich. Die lokale Automatik:

1. wählt den ältesten noch nicht bis zur angeforderten Stufe abgeschlossenen
   `Daily Math Crawl` mit einem verfügbaren verschlüsselten Artefakt,
2. lädt genau dieses Artefakt in einen geschützten lokalen Arbeitsordner,
3. entschlüsselt es als Stream mit der lokalen Identity-Datei,
4. lehnt unsichere Archivpfade, Links und übergroße Archive ab,
5. prüft Bundle, Medien und den Übergabevertrag und
6. markiert den GitHub-Run erst danach als lokal übernommen.

Ein teilweise verarbeiteter älterer Export wird damit fortgesetzt, bevor ein
neueres tägliches Artefakt beginnt.

Voraussetzungen sind eine angemeldete GitHub CLI (`gh`), `age`, Python und die
bereits erzeugte Identity-Datei:

```text
C:\Users\<DEIN-NAME>\AppData\Local\DiscordMathResearch\discord-export-key.txt
```

Die Datei wird automatisch benutzt und niemals nach GitHub hochgeladen. Der
Installer beschränkt die Windows-Dateirechte des gesamten lokalen Datenordners
auf dein Konto, `SYSTEM` und die lokalen Administratoren.

Vom Agentic-Researcher-Checkout aus einmalig installieren:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\automation\Install-DiscordMathScheduledTask.ps1
```

Der Task `Discord Math Research - Daily Import` prüft standardmäßig stündlich,
beginnend um 06:30 Uhr lokaler Zeit, auf einen neuen erfolgreichen
Export. Dadurch wird auch ein von GitHub verspätet gestarteter Workflow noch am
selben Tag übernommen. Der Task läuft mit eingeschränkten Rechten unter deinem
angemeldeten Windows-Konto, speichert kein Passwort und holt einen verpassten
Start nach der nächsten Anmeldung nach. Die Daten liegen außerhalb des
Git-Repositories unter:

```text
%LOCALAPPDATA%\DiscordMathResearch\automation\runs
```

Sobald ein erfolgreicher GitHub-Workflow ein Artefakt erzeugt hat, kann ein
Import auch sofort manuell angestoßen werden:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\automation\Invoke-DiscordMathImport.ps1
```

Der Standard endet bewusst nach sicherer Übergabe und Validierung. Commercial
Curation ist wegen möglicher Kosten und erforderlicher CLI-Logins nur explizit
aktivierbar:

```powershell
.\automation\Invoke-DiscordMathImport.ps1 `
  -Stage Curate `
  -AllowCommercialCuration
```

Im automatisierten Kurationsmodus werden Claude Code, Codex und Antigravity
unabhängig aufgerufen. Zwei gültige Antworten je Chunk bilden das notwendige
Quorum; der Ausfall genau eines Providers stoppt die Pipeline nicht. Bereits
validierte Chunk-Antworten werden sofort atomar gespeichert und bei einem
späteren Versuch wiederverwendet, damit erfolgreiche Commercial-Aufrufe nicht
erneut bezahlt werden.

Vor der ersten Aktivierung einmal interaktiv anmelden und danach den rein
lesenden Preflight ausführen:

```powershell
claude auth login
codex login status
python -m agentic_researcher provider-preflight `
  --provider claude `
  --provider codex `
  --minimum 2 `
  --require-auth
```

Der geplante Task startet Commercial-Aufrufe nur, wenn Claude Code und Codex
beide angemeldet sind. Antigravity wird weiterhin als unabhängiger dritter
Kurator versucht; sein Ausfall ist durch das 2-aus-3-Quorum abgedeckt.

Das überlappende 3-Tage-Crawlfenster verursacht keine wiederholte Curation:
Bereits erfolgreich verarbeitete Block-Fingerprints werden im geschützten
lokalen State gespeichert. Ein unveränderter Folgelauf erhält den Status
`no_work` und ruft keinen Commercial Provider auf. Inhaltlich geänderte Blöcke
werden dagegen erneut ausgewählt.

Den wiederkehrenden Windows-Task auf diese 2-aus-3-Kuration umstellen:

```powershell
.\automation\Install-DiscordMathScheduledTask.ps1 `
  -Stage Curate `
  -AllowCommercialCuration `
  -PollEveryHours 1 `
  -Force
```

Der komplette Open-Weight-Batch verlangt zusätzlich
`-AllowOpenWeightBatch`. Für eine tägliche Installation in diesen Modi gelten
dieselben Bestätigungsschalter am Installer. Ein unbeaufsichtigter Cloud-Batch
benötigt zusätzlich einen ausdrücklich eingerichteten Open-Weight-Runner; ein
klassisches interaktives Colab-Notebook lässt sich nicht zuverlässig headless
vom Windows-Task starten.

Nur den geplanten Task entfernen; lokale Runs und Schlüssel bleiben erhalten:

```powershell
.\automation\Uninstall-DiscordMathScheduledTask.ps1
```

## 3. Übergabevertrag einmal prüfen

Vom Agentic-Researcher-Verzeichnis aus:

```bash
cd ../The-Agentic-Researcher
python ../Discord-Mathematics-Early-University/scripts/contract_smoke.py \
  --agentic-researcher . \
  --bundle ../Discord-Mathematics-Early-University/discord_exports/ingest_bundle.json \
  --media-root ../Discord-Mathematics-Early-University/discord_exports/curation_media
```

Erwartet wird JSON mit `"status": "ok"`.

## 4. Mit drei Systemen kuratieren

Vorher einmal prüfen, dass `claude`, `codex` und `agy` angemeldet und auf
`PATH` verfügbar sind. Dann:

```bash
python -m agentic_researcher provider-contract

python -m agentic_researcher curate \
  ../Discord-Mathematics-Early-University/discord_exports/ingest_bundle.json \
  --provider claude \
  --provider codex \
  --provider antigravity \
  --media-root ../Discord-Mathematics-Early-University/discord_exports/curation_media \
  --items-per-prompt 12 \
  --max-input-chars 16000 \
  --threshold 2 \
  --allow-degraded-consensus \
  --resume \
  --runs-dir private-runs/curation \
  --output private-runs/curated_topics.json
```

Alle drei Systeme werden versucht; jeder Chunk benötigt mindestens zwei gültige
Antworten. Zwei übereinstimmende Antworten entscheiden den Inhalt; Konflikte bei
Thema, Quellenblock oder Formel werden als `needs_review` markiert. Ohne
`--allow-degraded-consensus` bleibt weiterhin der strengere 3-aus-3-Modus
verfügbar.

Vor dem nächsten Schritt in `private-runs/curated_topics.json` kontrollieren:

- `quality_gate.all_required_curators_attempted` ist `true`
- `quality_gate.chunk_quorum_met` ist `true`
- `quality_gate.threshold` ist `2`
- alle `needs_review`-Themen wurden geklärt

## 5. Research-Queue erzeugen

```bash
python -m agentic_researcher expand \
  private-runs/curated_topics.json \
  --output private-runs/research_queue.json
```

Für jedes akzeptierte Thema entstehen mindestens vier Aufgaben:
Grundlagen/Prerequisites, verifiziertes Beispiel, Literatur- und Code-Survey
sowie Annahmen-/Gegenbeispielprüfung. Übereinstimmende Vorschläge der Kuratoren
kommen zusätzlich hinzu.

## 6. Erst Dry-Run, dann Open-Weight-Batch

Zuerst genau ein Projekt ohne Modellaufruf initialisieren:

```bash
python -m agentic_researcher run-batch \
  private-runs/research_queue.json \
  --work-root private-runs/projects \
  --state private-runs/dry-run-state.json \
  --provider opencode \
  --max-tasks 1 \
  --dry-run
```

Für einen lokalen 20B-Lauf zuerst vLLM starten:

Dieser Pfad setzt eine installierte vLLM-/OpenCode-Umgebung und eine geeignete
GPU voraus. Unter nativem Windows ist der Colab-Pfad in der Regel einfacher;
das Notebook installiert und konfiguriert beide Werkzeuge selbst.

```bash
python -m pip install vllm
npm install -g opencode-ai

vllm serve openai/gpt-oss-20b \
  --served-model-name default \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 32768 \
  --enable-auto-tool-choice \
  --tool-call-parser openai
```

OpenCode in `~/.config/opencode/opencode.json` auf diesen Server zeigen:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local Agentic Researcher",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "local"
      },
      "models": {
        "default": {
          "name": "openai/gpt-oss-20b"
        }
      }
    }
  },
  "model": "local/default",
  "permission": {
    "external_directory": "deny",
    "question": "deny",
    "doom_loop": "deny",
    "bash": {
      "*": "allow",
      "git push *": "deny",
      "git commit *": "deny"
    }
  }
}
```

Danach den echten Batch starten:

```bash
python -m agentic_researcher run-batch \
  private-runs/research_queue.json \
  --work-root private-runs/projects \
  --state private-runs/batch_state.json \
  --provider opencode \
  --command 'opencode run --auto {prompt}' \
  --max-attempts 2 \
  --timeout 7200
```

Bei Unterbrechung denselben Befehl mit derselben State-Datei erneut starten.
Erfolgreiche Tasks werden nicht wiederholt.

### Colab/A100 statt lokaler GPU

[`notebooks/open_weight_bulk_research.ipynb`](notebooks/open_weight_bulk_research.ipynb)
in Colab öffnen. Zuvor `private-runs/research_queue.json` exakt nach
`MyDrive/agentic-researcher/research_queue.json` hochladen und danach das
Notebook von oben nach unten ausführen. Es hält Projekte und Batch-State auf
Google Drive und wählt bei ausreichendem GPU-Speicher `gpt-oss-120b`, sonst
`gpt-oss-20b`.

## 7. Ergebnisse prüfen

Jeder Task liegt unter `private-runs/projects/<task-id>/`. Wichtig sind:

- `research-spec.json`: unveränderlicher Auftrag und Discord-Provenienz
- `report.tex`: Recherchebericht
- `references.bib`: verifizierte Literatur
- `evidence-ranking.json`: nachvollziehbare Evidenz- und Eignungsbewertung
- `run-result.json`: Status und Verifikationszusammenfassung
- `TODO.md`: offene Punkte

Die arXiv-2D-Karte dient nur zur Kandidatenentdeckung. t-SNE-Distanz und
scheinbare Punktdichte sind keine Rankingmetriken. Offene Erdős-Probleme dürfen
nur als Ausblick erscheinen; bewiesene, widerlegte oder gelöste Probleme
können als Lernmaterial vorgeschlagen werden.

## Häufige Probleme

- **Ein Kurator fehlt:** CLI separat anmelden oder den Befehl mit
  `AR_PROVIDER_<NAME>_COMMAND` an die lokale Installation anpassen.
- **Prompt zu groß:** `--items-per-prompt` oder `--max-input-chars` verkleinern.
- **Bild fehlt:** `media_manifest.json` auf `failed` und
  `missing_attachment_ids` prüfen; die Formel nicht erraten.
- **Batch wurde unterbrochen:** denselben Queue-, Work- und State-Pfad erneut
  verwenden.
- **Daten veröffentlichen:** niemals `discord_exports/`, `private-runs/`,
  Curator-Logs oder unredigierte Bundles committen.

Der ausführliche Datenvertrag, Provider-Overrides und die Evidenzadapter stehen
im [vollständigen Pipeline-Guide](docs/discord_math_pipeline.md).
