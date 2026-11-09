"""Einstellungs-Editoren für den Server-Manager:
- Alle Server-Einstellungen (server.properties)
- Spielregeln (Gamerules) – live per Befehl oder beim nächsten Start
- Mod-Konfiguration (config/*.toml) – generischer Editor mit Haken/Zahlenfeldern
"""
import gzip
import json
import re
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

import mcserver_core as core

INT = re.compile(r"^-?\d+$")
FLOAT = re.compile(r"^-?\d+\.\d*(?:[eE]-?\d+)?$")
INT_MAX = 2_147_483_647


def same(a, b):
    """Gleicher Wert? Kommazahlen mit Toleranz (Mods speichern 0.2 als 0.20000000298…)."""
    if a == b:
        return True
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= 1e-6 * max(1.0, abs(fa))
    except ValueError:
        return False


def human(key):
    """camelCase / snake_case → lesbare Wörter."""
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key.split(".")[-1]).replace("_", " ").replace("-", " ")
    return k[:1].upper() + k[1:]


class _Base(QWidget):
    """Formular mit Suchfeld, Scrollbereich und Speichern-Knopf."""

    def __init__(self, hint):
        super().__init__()
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Suchen …")
        self.search.textChanged.connect(self.filter)
        top.addWidget(self.search)
        self.extra_top(top)
        self.b_save = QPushButton("Speichern")
        self.b_save.clicked.connect(self.save)
        top.addWidget(self.b_save)
        v.addLayout(top)
        self.info = QLabel(hint)
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: gray")
        v.addWidget(self.info)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        v.addWidget(self.area)
        self.rows = []  # (Suchtext, Zeilen-Widgets)
        self.form = None

    def extra_top(self, layout):
        pass

    def new_form(self):
        w = QWidget()
        self.form = QFormLayout(w)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.area.setWidget(w)
        self.rows = []

    def add_section(self, title):
        lab = QLabel(f"<b>{title}</b>")
        lab.setStyleSheet("margin-top: 10px")
        self.form.addRow(lab)
        self.rows.append(("§section", self.form.rowCount() - 1))

    def add_row(self, label, widget, tip="", search=""):
        lab = QLabel(label + ":")
        if tip:
            lab.setToolTip(tip)
            widget.setToolTip(tip)
            first = tip.strip().splitlines()[0]
            if first and first != label:
                box = QWidget()
                bl = QVBoxLayout(box)
                bl.setContentsMargins(0, 0, 0, 0)
                bl.setSpacing(0)
                bl.addWidget(widget)
                sub = QLabel(first[:160])
                sub.setStyleSheet("color: gray; font-size: 11px")
                bl.addWidget(sub)
                widget = box
        self.form.addRow(lab, widget)
        self.rows.append(((label + " " + search + " " + tip).lower(), self.form.rowCount() - 1))

    def filter(self, text):
        t = text.lower().strip()
        for s, row in self.rows:
            if s == "§section":
                self.form.setRowVisible(row, not t)
            else:
                self.form.setRowVisible(row, not t or t in s)

    def save(self):
        pass


def make_widget(value, comment=""):
    """Passendes Eingabefeld für einen Wert. Gibt (Widget, Leser-Funktion) zurück."""
    rng = re.search(r"Range:\s*(-?[\d.]+(?:E\d+)?)\s*~\s*(-?[\d.]+(?:E\d+)?)", comment)
    allowed = re.search(r"Allowed Values:\s*(.+)", comment)
    if value in ("true", "false"):
        w = QCheckBox()
        w.setChecked(value == "true")
        return w, lambda: "true" if w.isChecked() else "false"
    if INT.match(value) and abs(int(value)) <= INT_MAX:
        w = QSpinBox()
        lo, hi = -INT_MAX - 1, INT_MAX
        if rng:
            try:
                lo, hi = max(lo, int(float(rng.group(1)))), min(hi, int(float(rng.group(2))))
            except ValueError:
                pass
        w.setRange(lo, hi)
        try:
            w.setValue(int(value))
        except (ValueError, OverflowError):
            pass
        w.setMinimumWidth(130)
        w.setMaximumWidth(200)
        return w, lambda: str(w.value())
    if FLOAT.match(value):
        w = QDoubleSpinBox()
        w.setDecimals(6)
        lo, hi = -1e12, 1e12
        if rng:
            try:
                lo, hi = float(rng.group(1)), float(rng.group(2))
            except ValueError:
                pass
        w.setRange(lo, hi)
        w.setValue(float(value))
        w.setMinimumWidth(130)
        w.setMaximumWidth(200)
        return w, lambda: repr(round(w.value(), 6))
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        inner = value[1:-1]
        if allowed:
            opts = [o.strip() for o in allowed.group(1).split(",") if o.strip()]
            w = QComboBox()
            w.addItems(opts)
            if inner not in opts:
                w.addItem(inner)
            w.setCurrentText(inner)
            return w, lambda: '"' + w.currentText() + '"'
        w = QLineEdit(inner)
        w.setMinimumWidth(300)
        return w, lambda: '"' + w.text().replace('"', '\\"') + '"'
    w = QLineEdit(value)
    w.setMinimumWidth(300)
    return w, lambda: w.text()


# ------------------------------------------------------ server.properties --
PROP_DE = {
    "accepts-transfers": "Weiterleitungen von anderen Servern annehmen",
    "allow-flight": "Fliegen erlauben (für Create/Aeronautics nötig)",
    "allow-nether": "Nether erlauben",
    "broadcast-console-to-ops": "Konsolen-Ausgaben an Admins senden",
    "broadcast-rcon-to-ops": "RCON-Ausgaben an Admins senden",
    "bug-report-link": "Link für Fehlerberichte",
    "difficulty": "Schwierigkeit (peaceful/easy/normal/hard)",
    "enable-command-block": "Befehlsblöcke",
    "enable-jmx-monitoring": "JMX-Überwachung",
    "enable-query": "Query-Protokoll (Serverabfrage)",
    "enable-rcon": "RCON (Fernsteuerung)",
    "enable-status": "In der Serverliste als online anzeigen",
    "enforce-secure-profile": "Signierte Chat-Profile verlangen",
    "enforce-whitelist": "Whitelist sofort durchsetzen (wirft Nicht-Gelistete raus)",
    "entity-broadcast-range-percentage": "Sichtweite für Kreaturen (%)",
    "force-gamemode": "Spielmodus beim Betreten erzwingen",
    "function-permission-level": "Rechte-Stufe für Funktionen",
    "gamemode": "Spielmodus (survival/creative/adventure/spectator)",
    "generate-structures": "Strukturen erzeugen (Dörfer usw.)",
    "generator-settings": "Generator-Einstellungen (JSON)",
    "hardcore": "Hardcore",
    "hide-online-players": "Online-Spieler in der Liste verstecken",
    "initial-disabled-packs": "Anfangs deaktivierte Datenpakete",
    "initial-enabled-packs": "Anfangs aktivierte Datenpakete",
    "level-name": "Welt-Ordner",
    "level-seed": "Seed (nur für neue Welt)",
    "level-type": "Welttyp",
    "log-ips": "IP-Adressen ins Log schreiben",
    "max-chained-neighbor-updates": "Max. verkettete Block-Updates",
    "max-players": "Max. Spieler",
    "max-tick-time": "Max. Tick-Dauer (ms, -1 = aus)",
    "max-world-size": "Max. Weltgröße (Radius)",
    "motd": "Servername / Beschreibung (MOTD)",
    "network-compression-threshold": "Netzwerk-Komprimierung ab (Bytes)",
    "online-mode": "Nur echte Minecraft-Konten",
    "op-permission-level": "Rechte-Stufe für Admins",
    "player-idle-timeout": "AFK-Rauswurf nach Minuten (0 = aus)",
    "prevent-proxy-connections": "Proxy/VPN-Verbindungen blockieren",
    "pvp": "PvP (Spieler gegen Spieler)",
    "query.port": "Query-Port",
    "rate-limit": "Paket-Limit pro Sekunde (0 = aus)",
    "rcon.password": "RCON-Passwort",
    "rcon.port": "RCON-Port",
    "region-file-compression": "Komprimierung der Weltdateien",
    "require-resource-pack": "Ressourcenpaket erzwingen",
    "resource-pack": "Ressourcenpaket-URL",
    "resource-pack-id": "Ressourcenpaket-ID",
    "resource-pack-prompt": "Ressourcenpaket-Hinweistext",
    "resource-pack-sha1": "Ressourcenpaket-SHA1",
    "server-ip": "Server-IP (leer = alle)",
    "server-port": "Port",
    "simulation-distance": "Simulationsweite (Chunks)",
    "spawn-animals": "Tiere spawnen",
    "spawn-monsters": "Monster spawnen",
    "spawn-npcs": "Dorfbewohner spawnen",
    "spawn-protection": "Spawn-Schutz (Blöcke)",
    "sync-chunk-writes": "Chunks synchron speichern",
    "text-filtering-config": "Textfilter-Konfiguration",
    "use-native-transport": "Linux-Netzwerkoptimierung",
    "view-distance": "Sichtweite (Chunks)",
    "white-list": "Nur Spieler auf der Whitelist",
}


class PropsEditor(_Base):
    def __init__(self, log):
        super().__init__("Alle Einträge aus server.properties. Grau = Original-Schlüssel. "
                         "Änderungen wirken nach einem Neustart des Servers.")
        self.log = log
        self.name = None
        self.readers = {}

    def load(self, name):
        self.name = name
        self.new_form()
        self.readers = {}
        if not name:
            return
        props = core.DEFAULT_PROPS | core.read_properties(name)
        for key in sorted(props):
            w, rd = make_widget(props[key])
            self.readers[key] = rd
            self.add_row(PROP_DE.get(key, human(key)), w, tip=key, search=key)
        self.filter(self.search.text())

    def save(self):
        if not self.name:
            return
        core.write_properties(self.name, {k: rd() for k, rd in self.readers.items()})
        self.log("Server-Einstellungen gespeichert – wirken nach einem Neustart.")


# ------------------------------------------------------------ Gamerules ---
GAMERULES_DE = {
    "announceAdvancements": "Fortschritte im Chat ankündigen",
    "blockExplosionDropDecay": "Block-Explosionen: nicht alle Blöcke droppen",
    "commandBlockOutput": "Befehlsblock-Ausgaben im Chat",
    "commandModificationBlockLimit": "Max. Blöcke pro /fill bzw. /clone",
    "disableElytraMovementCheck": "Elytra-Bewegungsprüfung aus",
    "disableRaids": "Überfälle (Raids) aus",
    "doDaylightCycle": "Tag-Nacht-Wechsel",
    "doEntityDrops": "Objekte (Loren, Rahmen …) droppen Gegenstände",
    "doFireTick": "Feuer breitet sich aus",
    "doImmediateRespawn": "Sofort wiederbeleben (ohne Todesbildschirm)",
    "doInsomnia": "Phantome spawnen bei Schlafmangel",
    "doLimitedCrafting": "Nur freigeschaltete Rezepte craftbar",
    "doMobLoot": "Mobs droppen Beute",
    "doMobSpawning": "Mobs spawnen",
    "doPatrolSpawning": "Plünderer-Patrouillen",
    "doTileDrops": "Abgebaute Blöcke droppen",
    "doTraderSpawning": "Fahrende Händler",
    "doVinesSpread": "Ranken wachsen",
    "doWardenSpawning": "Wärter (Warden) spawnt",
    "doWeatherCycle": "Wetterwechsel",
    "drowningDamage": "Ertrinkungsschaden",
    "enderPearlsVanishOnDeath": "Enderperlen verschwinden beim Tod",
    "fallDamage": "Fallschaden",
    "fireDamage": "Feuerschaden",
    "forgiveDeadPlayers": "Neutrale Mobs vergeben tote Spieler",
    "freezeDamage": "Erfrierungsschaden",
    "globalSoundEvents": "Globale Geräusche (Wither, Enderdrache …)",
    "keepInventory": "Inventar beim Tod behalten",
    "lavaSourceConversion": "Lava bildet neue Quellen",
    "logAdminCommands": "Admin-Befehle protokollieren",
    "maxCommandChainLength": "Max. Befehlsketten-Länge",
    "maxCommandForkCount": "Max. Befehls-Verzweigungen",
    "maxEntityCramming": "Max. Kreaturen auf einem Block",
    "mobExplosionDropDecay": "Mob-Explosionen: nicht alle Blöcke droppen",
    "mobGriefing": "Mobs verändern Blöcke (Creeper, Endermen …)",
    "naturalRegeneration": "Natürliche Heilung",
    "playersNetherPortalCreativeDelay": "Netherportal-Wartezeit Kreativ (Ticks)",
    "playersNetherPortalDefaultDelay": "Netherportal-Wartezeit (Ticks)",
    "playersSleepingPercentage": "Anteil schlafender Spieler für Nacht-Überspringen (%)",
    "projectilesCanBreakBlocks": "Geschosse zerstören Blöcke",
    "randomTickSpeed": "Zufalls-Ticks (Wachstum, Standard 3)",
    "reducedDebugInfo": "Reduzierte F3-Infos",
    "sendCommandFeedback": "Befehls-Rückmeldungen im Chat",
    "showDeathMessages": "Todesnachrichten",
    "snowAccumulationHeight": "Max. Schneehöhe bei Schneefall",
    "spawnChunkRadius": "Geladene Spawn-Chunks (Radius)",
    "spawnRadius": "Spawn-Radius für neue Spieler",
    "spectatorsGenerateChunks": "Zuschauer erzeugen Chunks",
    "tntExplosionDropDecay": "TNT: nicht alle Blöcke droppen",
    "universalAnger": "Neutrale Mobs greifen alle Spieler an",
    "waterSourceConversion": "Wasser bildet neue Quellen",
}


def read_gamerules(name):
    lvl = core.read_properties(name).get("level-name", "world") or "world"
    f = core.server_dir(name) / lvl / "level.dat"
    if not f.exists():
        return None
    import mobs
    d = mobs.read_nbt(gzip.decompress(f.read_bytes()))
    return dict((d.get("Data") or {}).get("GameRules") or {})


class GameRulesEditor(_Base):
    def __init__(self, log, running, send):
        super().__init__("")
        self.log, self.running, self.send = log, running, send
        self.name = None
        self.readers = {}
        self.current = {}

    def load(self, name):
        self.name = name
        self.new_form()
        self.readers = {}
        if not name:
            return
        try:
            rules = read_gamerules(name)
        except Exception as e:  # noqa: BLE001
            rules = None
            self.log(f"Spielregeln konnten nicht gelesen werden: {e}")
        cfg = core.load_cfg(name)
        pending = cfg.get("gamerules_pending", {})
        if rules is None:
            self.info.setText("Die Welt gibt es noch nicht – Server einmal starten, dann erscheinen hier alle "
                              "Spielregeln.")
            return
        rules.update(pending)
        self.current = dict(rules)
        txt = ("Server läuft: „Speichern“ setzt die Regeln sofort per /gamerule. "
               if self.running() else
               "Server ist aus: „Speichern“ merkt die Regeln vor, sie werden beim nächsten Start gesetzt. ")
        if pending:
            txt += f"Vorgemerkt: {len(pending)} Änderung(en). "
        self.info.setText(txt + "Mod-Spielregeln stehen unten, sobald sie in der Welt gespeichert sind.")
        known = [k for k in GAMERULES_DE if k in rules]
        other = sorted(k for k in rules if k not in GAMERULES_DE)
        self.add_section("Minecraft")
        for k in sorted(known, key=lambda k: GAMERULES_DE[k].lower()):
            w, rd = make_widget(str(rules[k]))
            self.readers[k] = rd
            self.add_row(GAMERULES_DE[k], w, tip=k, search=k)
        if other:
            self.add_section("Mods")
            for k in other:
                w, rd = make_widget(str(rules[k]))
                self.readers[k] = rd
                self.add_row(human(k), w, tip=k, search=k)
        self.filter(self.search.text())

    def save(self):
        if not self.name or not self.readers:
            return
        changed = {k: rd() for k, rd in self.readers.items() if rd() != str(self.current.get(k))}
        if not changed:
            self.log("Spielregeln: keine Änderung.")
            return
        cfg = core.load_cfg(self.name)
        if self.running():
            for k, v in changed.items():
                self.send(f"gamerule {k} {v}")
            cfg.pop("gamerules_pending", None)
            self.log(f"{len(changed)} Spielregel(n) gesetzt: " + ", ".join(f"{k}={v}" for k, v in changed.items()))
        else:
            p = cfg.get("gamerules_pending", {})
            p.update(changed)
            cfg["gamerules_pending"] = p
            self.log(f"{len(changed)} Spielregel(n) vorgemerkt – werden beim nächsten Start gesetzt.")
        core.save_cfg(self.name, cfg)
        self.current.update(changed)
        self.load(self.name)


def apply_pending_gamerules(name, send, log):
    """Beim Serverstart (nach „Done“) vorgemerkte Spielregeln setzen."""
    cfg = core.load_cfg(name)
    p = cfg.pop("gamerules_pending", None)
    if not p:
        return
    for k, v in p.items():
        send(f"gamerule {k} {v}")
    core.save_cfg(name, cfg)
    log(f"Vorgemerkte Spielregeln gesetzt: {', '.join(f'{k}={v}' for k, v in p.items())}")


# --------------------------------------------------------- Mod-Configs ---
SEC = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$")
KV = re.compile(r"^(\s*)([A-Za-z0-9_\-]+|\"[^\"]+\")\s*=\s*(.*?)\s*$")


def parse_toml_lines(text):
    """Einträge (Zeilennr., Abschnitt, Schlüssel, Wert, Kommentar) – nur einzeilige Werte."""
    out, section, comment = [], "", []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if s.startswith("#"):
            c = s.lstrip("#").strip()
            if c and c != ".":
                comment.append(c)
            continue
        if m := SEC.match(line):
            section, comment = m.group(1), []
            continue
        if m := KV.match(line):
            val = m.group(3)
            if val.startswith("[") and not val.endswith("]"):
                comment = []
                continue  # mehrzeilige Liste – nicht anfassen
            out.append((i, section, m.group(2).strip('"'), val, "\n".join(comment)))
        comment = []
    return out


class ModConfigEditor(_Base):
    def __init__(self, log, open_path):
        super().__init__("Einstellungen der Mods (config-Ordner des Servers). Fahre mit der Maus über einen "
                         "Eintrag für die Beschreibung. Änderungen wirken nach einem Neustart des Servers.")
        self.log, self.open_path = log, open_path
        self.name = None
        self.file = None
        self.entries = []

    def extra_top(self, layout):
        self.files = QComboBox()
        self.files.setMinimumWidth(280)
        self.files.currentIndexChanged.connect(self.load_file)
        layout.insertWidget(0, self.files)
        b = QPushButton("Im Editor öffnen")
        b.clicked.connect(lambda: self.file and self.open_path(self.file))
        layout.addWidget(b)

    def load(self, name):
        self.name = name
        self.files.blockSignals(True)
        self.files.clear()
        if name:
            base = core.server_dir(name) / "config"
            fs = sorted(base.rglob("*.toml"), key=lambda p: str(p.relative_to(base)).lower()) if base.exists() else []
            for f in fs:
                rel = str(f.relative_to(base))
                if rel.endswith("-client.toml"):
                    continue  # wirkt nur beim Spieler, nicht auf dem Server
                self.files.addItem(rel, str(f))
        self.files.blockSignals(False)
        self.load_file()

    def load_file(self, *_):
        self.new_form()
        self.entries = []
        path = self.files.currentData()
        self.file = Path(path) if path else None
        if not self.file or not self.file.exists():
            return
        last = None
        for i, sec, key, val, com in parse_toml_lines(self.file.read_text(errors="replace")):
            if sec != last:
                self.add_section(sec or "Allgemein")
                last = sec
            w, rd = make_widget(val, com)
            self.entries.append((i, key, val, rd))
            self.add_row(human(key), w, tip=com, search=key + " " + sec)
        self.filter(self.search.text())

    def save(self):
        if not self.file:
            return
        lines = self.file.read_text(errors="replace").splitlines()
        n = 0
        for i, key, old, rd in self.entries:
            new = rd()
            if not same(new, old):
                m = KV.match(lines[i])
                lines[i] = f"{m.group(1)}{m.group(2)} = {new}"
                n += 1
        if not n:
            self.log("Mod-Konfiguration: keine Änderung.")
            return
        bak = self.file.with_name(self.file.name + ".bak")
        if not bak.exists():
            bak.write_text(self.file.read_text(errors="replace"))
        self.file.write_text("\n".join(lines) + "\n")
        self.log(f"{self.files.currentText()}: {n} Einstellung(en) gespeichert – wirken nach einem Neustart.")
        self.load_file()
