#!/usr/bin/env python3
"""Minecraft-Server-Manager – einfaches Fenster für Felix' Mod-Server."""
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPixmap, QTextCursor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QAbstractItemView)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # vor QApplication importieren
    from PyQt6.QtCore import QUrl
except Exception:  # noqa: BLE001
    QWebEngineView = None

sys.path.insert(0, str(Path(__file__).parent))
import mcserver_core as core  # noqa: E402
import modseite  # noqa: E402
import einstellungen  # noqa: E402

PRESET_CREATE = ["create", "create-aeronautics", "create-walking-steel"]


# ------------------------------------------------------- Hintergrund-Jobs --
class Job(QObject):
    log = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args, log=self.log.emit))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(str(e))


class Quick(QObject):
    """Kleiner Job ohne log-Parameter (z. B. Suche)."""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ----------------------------------------------------------- Dialoge ------
class VersionDialog(QDialog):
    """Auswahl Minecraft-Version + Loader (+ Name bei neuem Server)."""

    def __init__(self, parent, title, with_name=True, cfg=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        lay = QFormLayout(self)
        self.name = QLineEdit("Create")
        if with_name:
            lay.addRow("Name:", self.name)
        self.mc = QComboBox()
        self.snap = QCheckBox("auch Snapshots zeigen")
        self.loader = QComboBox()
        for l in core.LOADERS:
            self.loader.addItem(core.LOADER_NAMES[l], l)
        self.lv = QComboBox()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addRow("Minecraft-Version:", self.mc)
        lay.addRow("", self.snap)
        lay.addRow("Mod-Loader:", self.loader)
        lay.addRow("Loader-Version:", self.lv)
        lay.addRow(self.info)
        hint = QLabel("Tipp: Create + Aeronautics gibt es nur für <b>1.21.1 + NeoForge</b>.")
        hint.setWordWrap(True)
        lay.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)
        self._cfg = cfg or {"mc": "1.21.1", "loader": "neoforge"}
        self.loader.setCurrentIndex(core.LOADERS.index(self._cfg["loader"]))
        self.snap.toggled.connect(self.load_mc)
        self.mc.currentTextChanged.connect(self.load_lv)
        self.loader.currentIndexChanged.connect(self.load_lv)
        self.threads = []
        self.load_mc()

    def _bg(self, fn, cb, *args):
        t = QThread(self)
        j = Quick(fn, *args)
        j.moveToThread(t)
        t.started.connect(j.run)
        j.done.connect(cb)
        j.failed.connect(lambda e: self.info.setText(f"<span style='color:#c33'>{e}</span>"))
        j.done.connect(t.quit)
        j.failed.connect(t.quit)
        self.threads.append((t, j))
        t.start()

    def load_mc(self):
        self.info.setText("Lade Versionen …")
        self._bg(lambda s: core.mc_versions(s)[0], self._got_mc, self.snap.isChecked())

    def _got_mc(self, vs):
        self.mc.blockSignals(True)
        self.mc.clear()
        self.mc.addItems(vs)
        i = self.mc.findText(self._cfg["mc"])
        self.mc.setCurrentIndex(max(i, 0))
        self.mc.blockSignals(False)
        self.load_lv()

    def load_lv(self):
        mc, loader = self.mc.currentText(), self.loader.currentData()
        if not mc:
            return
        self.lv.clear()
        self.info.setText("Lade Loader-Versionen …")
        self._bg(core.loader_versions, lambda vs: self._got_lv(vs, mc, loader), loader, mc)

    def _got_lv(self, vs, mc, loader):
        if mc != self.mc.currentText() or loader != self.loader.currentData():
            return
        self.lv.clear()
        self.lv.addItems(vs)
        self.info.setText("" if vs else "<span style='color:#c33'>Für diese Version gibt es diesen Loader nicht.</span>")

    def values(self):
        return self.name.text().strip(), self.mc.currentText(), self.loader.currentData(), self.lv.currentText()


# ----------------------------------------------------------- Hauptfenster --
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minecraft-Server")
        self.resize(1100, 760)
        self.proc = None
        self.players = set()
        self.threads = []
        self.busy = False
        self.icon_cache = {}

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        # Kopfzeile
        top = QHBoxLayout()
        top.addWidget(QLabel("Server:"))
        self.sel = QComboBox()
        self.sel.setMinimumWidth(180)
        self.sel.currentTextChanged.connect(self.on_select)
        top.addWidget(self.sel)
        b = QPushButton("＋ Neuer Server")
        b.clicked.connect(self.new_server)
        top.addWidget(b)
        b = QPushButton("📦 Modpack importieren")
        b.setToolTip("Modpack-Datei (.mrpack oder CurseForge-Zip) als neuen Server + Prism-Instanz anlegen")
        b.clicked.connect(self.import_pack)
        top.addWidget(b)
        top.addSpacing(20)
        self.status = QLabel()
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.status.setFont(f)
        top.addWidget(self.status)
        top.addStretch()
        self.b_start = QPushButton("▶  Starten")
        self.b_stop = QPushButton("■  Stoppen")
        self.b_restart = QPushButton("⟳  Neustart")
        for b in (self.b_start, self.b_stop, self.b_restart):
            b.setMinimumHeight(36)
            b.setMinimumWidth(110)
            top.addWidget(b)
        self.b_start.clicked.connect(self.start)
        self.b_stop.clicked.connect(self.stop)
        self.b_restart.clicked.connect(self.restart)
        v.addLayout(top)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs)
        self.tabs.addTab(self.build_overview(), "Übersicht")
        self.tabs.addTab(self.build_console(), "Konsole")
        self.tabs.addTab(self.build_settings_tabs(), "Einstellungen")
        self.tabs.addTab(self.build_mods(), "Mods")
        self.tabs.addTab(self.build_browser(), "Mod-Browser")
        self.tabs.addTab(self.build_players(), "Spieler")
        self.tabs.addTab(self.build_map(), "Karte")

        self.statusBar().showMessage("Bereit")
        self.mob_timer = QTimer(self)
        self.mob_timer.timeout.connect(self.export_mobs)
        self.mob_timer.start(60000)
        try:
            last = json.loads((core.BASE / "gui.json").read_text()).get("last")
        except (OSError, ValueError):
            last = None
        self.ts_ip = None
        try:
            modseite.start()
        except OSError:
            pass  # läuft schon als Dienst mc-modseite.service
        self.refresh_servers(last if last in core.list_servers() else None)
        self.update_state()
        QTimer.singleShot(300, self.load_ips)

    # ---------------------------------------------------------- Helfer ---
    @property
    def name(self):
        return self.sel.currentText()

    def logmsg(self, text):
        self.console.appendPlainText(f"[Manager] {text}")
        self.statusBar().showMessage(text, 8000)

    def run_job(self, fn, *args, on_done=None, title=None, show_console=True):
        if self.busy:
            QMessageBox.information(self, "Moment", "Es läuft gerade schon eine Aufgabe.")
            return
        self.busy = True
        self.update_state()
        if show_console:
            self.tabs.setCurrentIndex(1)
        if title:
            self.logmsg(title)
        t = QThread(self)
        j = Job(fn, *args)
        j.moveToThread(t)
        t.started.connect(j.run)
        j.log.connect(self.logmsg)

        def fin(res=None, err=None):
            self.busy = False
            t.quit()
            self.update_state()
            if err:
                self.logmsg("FEHLER: " + err)
                QMessageBox.warning(self, "Fehler", err)
            elif on_done:
                on_done(res)
        j.done.connect(lambda r: fin(res=r))
        j.failed.connect(lambda e: fin(err=e))
        self.threads.append((t, j))
        t.start()

    def bg(self, fn, cb, *args, err=None):
        t = QThread(self)
        j = Quick(fn, *args)
        j.moveToThread(t)
        t.started.connect(j.run)
        j.done.connect(cb)
        j.failed.connect(err or (lambda e: self.statusBar().showMessage("Fehler: " + e, 8000)))
        j.done.connect(t.quit)
        j.failed.connect(t.quit)
        self.threads.append((t, j))
        self.threads = [(a, b) for a, b in self.threads if a.isRunning() or a is t]
        t.start()

    def running(self):
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    # -------------------------------------------------------- Übersicht --
    def build_overview(self):
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Server")
        f = QFormLayout(g)
        self.l_version = QLabel()
        self.l_mods = QLabel()
        self.l_players = QLabel("–")
        f.addRow("Version:", self.l_version)
        f.addRow("Mods:", self.l_mods)
        f.addRow("Spieler online:", self.l_players)
        ram = QHBoxLayout()
        self.ram = QSpinBox()
        self.ram.setRange(1, 24)
        self.ram.setSuffix(" GB")
        self.ram.valueChanged.connect(self.save_ram)
        ram.addWidget(self.ram)
        ram.addWidget(QLabel("(Create-Pack: 6–8 GB empfohlen, dein PC hat 32 GB)"))
        ram.addStretch()
        f.addRow("Arbeitsspeicher:", ram)
        v.addWidget(g)

        g = QGroupBox("So verbinden sich die Leute")
        f = QFormLayout(g)
        self.l_you = QLabel("localhost")
        self.l_lan = QLabel("…")
        self.l_ts = QLabel("…")
        self.l_pub = QLabel("…")
        for l in (self.l_you, self.l_lan, self.l_ts, self.l_pub):
            l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f.addRow("Du selbst (dieser PC):", self.l_you)
        f.addRow("Im gleichen WLAN/Heimnetz:", self.l_lan)
        f.addRow("Über Tailscale:", self.l_ts)
        f.addRow("Übers Internet:", self.l_pub)
        self.l_modseite = QLabel("…")
        self.l_modseite.setOpenExternalLinks(True)
        self.l_modseite.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        f.addRow("Mod-Seite für Mitspieler:", self.l_modseite)
        hint = QLabel("Übers Internet geht nur mit Portfreigabe im Router (TCP-Port des Servers auf "
                      "diesen PC) oder einem Tunnel wie playit.gg. Freunde brauchen dieselben Mods: "
                      "„Modpack für Freunde exportieren“ und die Datei in Prism Launcher oder der "
                      "Modrinth App importieren.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        f.addRow(hint)
        v.addWidget(g)

        g = QGroupBox("Aktionen")
        h = QHBoxLayout(g)
        for text, fn in [("Selbst spielen (Prism)", self.play),
                         ("Modpack für Freunde exportieren", self.export_pack),
                         ("Welt sichern (Backup)", self.do_backup),
                         ("Version ändern …", self.change_version),
                         ("Server-Ordner öffnen", lambda: self.open_path(core.server_dir(self.name)))]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addWidget(g)
        v.addStretch()
        return w

    def load_ips(self):
        self.l_lan.setText("…")
        self.bg(lambda: (core.lan_ip(), core.tailscale_ip(), core.public_ip()), self._got_ips)

    def _got_ips(self, ips):
        lan, ts, pub = ips
        port = core.read_properties(self.name).get("server-port", "25565") if self.name else "25565"
        sfx = "" if port == "25565" else f":{port}"
        self.l_you.setText(f"<b>localhost{sfx}</b>")
        self.l_lan.setText(f"<b>{lan}{sfx}</b>")
        self.l_ts.setText(f"<b>{ts}{sfx}</b> (Freunde brauchen dafür Tailscale + Einladung)" if ts else "nicht aktiv")
        self.ts_ip = ts
        host = f"{ts or lan}:{modseite.PORT}"
        self.l_modseite.setText(f"<a href='http://{host}/'>http://{host}</a> – fehlende Mods, Downloads, "
                                "Auto-Update für Prism (steht auch in der Serverliste)")
        self.l_pub.setText(f"<b>{pub}{sfx}</b> (nur mit Portfreigabe {port}/TCP im Router)")

    def save_ram(self, gb):
        if self.name:
            cfg = core.load_cfg(self.name)
            cfg["ram_mb"] = gb * 1024
            core.save_cfg(self.name, cfg)

    def open_path(self, p):
        subprocess.Popen(["xdg-open", str(p)])

    def play(self):
        if not self.name:
            return
        core.ensure_prism_instance(self.name, lambda *_: None)
        inst = core.prism_instance_dir(self.name).name
        subprocess.Popen(["prismlauncher", "--launch", inst, "--server",
                          "localhost:" + core.read_properties(self.name).get("server-port", "25565")],
                         start_new_session=True)
        self.logmsg(f"Prism startet Instanz „{inst}“ und verbindet sich mit dem Server.")

    def export_pack(self):
        if not self.name:
            return

        def both(name, log):
            return core.export_mrpack(name, log), core.export_curseforge(name, log)

        def done(res):
            mr, cf = res
            QMessageBox.information(
                self, "Modpack exportiert",
                "Zwei Dateien erstellt – schick deinen Freunden die passende:\n\n"
                f"• CurseForge-App:  {cf.name}\n"
                "   → CurseForge öffnen → Minecraft → „Erstellen“ → „Importieren“ → Zip-Datei wählen\n\n"
                f"• Prism Launcher / Modrinth App:  {mr.name}\n"
                "   → Prism: „Instanz hinzufügen“ → „Importieren“, Modrinth App: Datei hineinziehen\n\n"
                f"Ordner: {cf.parent}")
            self.open_path(cf.parent)
        self.run_job(both, self.name, show_console=False, on_done=done, title="Exportiere Modpack …")

    def do_backup(self):
        if not self.name:
            return
        if self.running():
            self.send("save-all flush")
        QTimer.singleShot(2000 if self.running() else 0,
                          lambda: self.run_job(core.backup, self.name, title="Sichere Welt …"))

    def change_version(self):
        if not self.name:
            return
        if self.running():
            QMessageBox.information(self, "Server läuft", "Bitte den Server zuerst stoppen.")
            return
        cfg = core.load_cfg(self.name)
        d = VersionDialog(self, f"Version ändern – {self.name}", with_name=False, cfg=cfg)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        _, mc, loader, lv = d.values()
        if not lv:
            return
        if QMessageBox.question(
                self, "Version ändern",
                f"Von {cfg['mc']} / {core.LOADER_NAMES[cfg['loader']]} auf {mc} / {core.LOADER_NAMES[loader]} {lv} wechseln?\n\n"
                "Die Welt wird vorher gesichert. Mods werden – soweit vorhanden – in der passenden Version "
                "neu geladen. Ältere Version als vorher kann die Welt beschädigen (dann Backup nutzen).") \
                != QMessageBox.StandardButton.Yes:
            return

        def done(missing):
            self.refresh_all()
            if missing:
                QMessageBox.warning(self, "Nicht alle Mods verfügbar",
                                    "Für die neue Version gibt es diese Mods nicht:\n\n• " + "\n• ".join(missing))
            else:
                QMessageBox.information(self, "Fertig", "Version gewechselt.")
        self.run_job(core.change_version, self.name, mc, loader, lv, on_done=done, title="Wechsle Version …")

    # ----------------------------------------------------------- Konsole --
    def build_console(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setFont(QFont("monospace", 9))
        v.addWidget(self.console)
        h = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Befehl ohne / eingeben, z. B.  time set day   oder   say Hallo")
        self.cmd.returnPressed.connect(self.send_cmd)
        h.addWidget(self.cmd)
        b = QPushButton("Senden")
        b.clicked.connect(self.send_cmd)
        h.addWidget(b)
        v.addLayout(h)
        return w

    def send_cmd(self):
        t = self.cmd.text().strip().lstrip("/")
        if t:
            self.send(t)
            self.cmd.clear()

    def send(self, text):
        if self.running():
            self.console.appendPlainText("> " + text)
            self.proc.write((text + "\n").encode())
        else:
            self.logmsg("Server läuft nicht.")

    # ---------------------------------------------------- Einstellungen --
    PROP_FIELDS = [
        ("motd", "Servername (in der Liste)", "text"),
        ("max-players", "Max. Spieler", ("int", 1, 100)),
        ("gamemode", "Spielmodus", ["survival", "creative", "adventure", "spectator"]),
        ("difficulty", "Schwierigkeit", ["peaceful", "easy", "normal", "hard"]),
        ("hardcore", "Hardcore", "bool"),
        ("pvp", "PvP (Spieler gegen Spieler)", "bool"),
        ("white-list", "Nur Spieler auf der Whitelist", "bool"),
        ("online-mode", "Nur echte Minecraft-Konten (empfohlen)", "bool"),
        ("allow-flight", "Fliegen erlauben (für Create/Aeronautics nötig!)", "bool"),
        ("view-distance", "Sichtweite (Chunks)", ("int", 3, 32)),
        ("simulation-distance", "Simulationsweite (Chunks)", ("int", 3, 32)),
        ("spawn-protection", "Spawn-Schutz (Blöcke)", ("int", 0, 100)),
        ("enable-command-block", "Befehlsblöcke", "bool"),
        ("server-port", "Port", ("int", 1024, 65535)),
        ("level-name", "Welt-Ordner", "text"),
        ("level-seed", "Seed (nur für neue Welt)", "text"),
    ]

    def build_settings_tabs(self):
        t = QTabWidget()
        t.addTab(self.build_settings(), "Wichtigste")
        self.ed_props = einstellungen.PropsEditor(self.logmsg)
        t.addTab(self.ed_props, "Alle Server-Einstellungen")
        self.ed_rules = einstellungen.GameRulesEditor(self.logmsg, self.running, self.send)
        t.addTab(self.ed_rules, "Spielregeln")
        self.ed_mods = einstellungen.ModConfigEditor(self.logmsg, self.open_path)
        t.addTab(self.ed_mods, "Mod-Konfiguration")
        t.currentChanged.connect(lambda i: self.reload_editor(t.widget(i)))
        self.settings_tabs = t
        return t

    def reload_editor(self, w):
        if w in (self.ed_props, self.ed_rules, self.ed_mods):
            try:
                w.load(self.name)
            except Exception as e:  # noqa: BLE001
                self.logmsg(f"Einstellungen konnten nicht geladen werden: {e}")

    def build_settings(self):
        w = QWidget()
        v = QVBoxLayout(w)
        f = QFormLayout()
        self.prop_widgets = {}
        for key, label, kind in self.PROP_FIELDS:
            if kind == "text":
                wd = QLineEdit()
            elif kind == "bool":
                wd = QCheckBox()
            elif isinstance(kind, list):
                wd = QComboBox()
                wd.addItems(kind)
            else:
                wd = QSpinBox()
                wd.setRange(kind[1], kind[2])
            self.prop_widgets[key] = wd
            f.addRow(label + ":", wd)
        v.addLayout(f)
        h = QHBoxLayout()
        b = QPushButton("Speichern")
        b.clicked.connect(self.save_settings)
        h.addWidget(b)
        b = QPushButton("server.properties im Editor öffnen")
        b.clicked.connect(lambda: self.open_path(core.server_dir(self.name) / "server.properties"))
        h.addWidget(b)
        h.addStretch()
        v.addLayout(h)
        n = QLabel("Änderungen wirken nach einem Neustart des Servers.")
        n.setStyleSheet("color: gray")
        v.addWidget(n)

        g = QGroupBox("Spawnpunkt der Welt (Server muss laufen)")
        gv = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(QLabel("Auf Position von Spieler:"))
        self.spawn_player = QComboBox()
        self.spawn_player.setMinimumWidth(160)
        h.addWidget(self.spawn_player)
        b = QPushButton("Hier Spawn setzen")
        b.setToolTip("Setzt den Welt-Spawn genau dahin, wo der Spieler gerade steht")
        b.clicked.connect(self.spawn_at_player)
        h.addWidget(b)
        h.addStretch()
        gv.addLayout(h)
        h = QHBoxLayout()
        h.addWidget(QLabel("Auf Koordinaten:"))
        self.spawn_xyz = []
        for axis, default in (("X", 0), ("Y", 100), ("Z", 0)):
            h.addWidget(QLabel(axis))
            sb = QSpinBox()
            sb.setRange(-30_000_000, 30_000_000) if axis != "Y" else sb.setRange(-64, 320)
            sb.setValue(default)
            self.spawn_xyz.append(sb)
            h.addWidget(sb)
        b = QPushButton("Spawn setzen")
        b.clicked.connect(self.spawn_at_xyz)
        h.addWidget(b)
        h.addStretch()
        gv.addLayout(h)
        h = QHBoxLayout()
        b = QPushButton("Alle Spieler zum Spawn teleportieren")
        b.clicked.connect(self.tp_all_spawn)
        h.addWidget(b)
        h.addStretch()
        gv.addLayout(h)
        n = QLabel("Tipp: Koordinaten siehst du im Spiel mit F3. Neue Spieler und Spieler ohne Bett erscheinen am "
                   "Spawnpunkt. Spawn-Schutz (oben) schützt die Blöcke um den Spawn vor Nicht-Admins.")
        n.setWordWrap(True)
        n.setStyleSheet("color: gray")
        gv.addWidget(n)
        v.addWidget(g)
        v.addStretch()
        return w

    def send_checked(self, cmd):
        if not self.running():
            QMessageBox.information(self, "Server aus", "Dafür muss der Server laufen.")
            return False
        self.send(cmd)
        return True

    def spawn_at_player(self):
        p = self.spawn_player.currentText()
        if not p:
            QMessageBox.information(self, "Kein Spieler", "Es ist gerade niemand online.")
            return
        if self.send_checked(f"execute as {p} at @s run setworldspawn ~ ~ ~"):
            self.send(f"tell {p} Welt-Spawn wurde auf deine Position gesetzt.")
            self.statusBar().showMessage(f"Spawn auf Position von {p} gesetzt.", 8000)

    def spawn_at_xyz(self):
        x, y, z = (sb.value() for sb in self.spawn_xyz)
        if self.send_checked(f"setworldspawn {x} {y} {z}"):
            self.statusBar().showMessage(f"Spawn auf {x} {y} {z} gesetzt.", 8000)

    def tp_all_spawn(self):
        # Befehle aus der Server-Konsole laufen am Welt-Spawn → ~ ~ ~ = Spawnpunkt
        if self.send_checked("execute in minecraft:overworld run tp @a ~ ~ ~"):
            self.statusBar().showMessage("Alle Spieler zum Spawn teleportiert.", 8000)

    def load_settings(self):
        if not self.name:
            return
        self.reload_editor(self.settings_tabs.currentWidget())
        props = core.DEFAULT_PROPS | core.read_properties(self.name)
        for key, _, kind in self.PROP_FIELDS:
            wd, val = self.prop_widgets[key], props.get(key, "")
            if isinstance(wd, QLineEdit):
                wd.setText(val)
            elif isinstance(wd, QCheckBox):
                wd.setChecked(val == "true")
            elif isinstance(wd, QComboBox):
                wd.setCurrentText(val)
            else:
                try:
                    wd.setValue(int(val))
                except ValueError:
                    pass

    def save_settings(self):
        new = {}
        for key, _, _ in self.PROP_FIELDS:
            wd = self.prop_widgets[key]
            if isinstance(wd, QLineEdit):
                new[key] = wd.text()
            elif isinstance(wd, QCheckBox):
                new[key] = "true" if wd.isChecked() else "false"
            elif isinstance(wd, QComboBox):
                new[key] = wd.currentText()
            else:
                new[key] = str(wd.value())
        core.write_properties(self.name, new)
        self.load_ips()
        self.logmsg("Einstellungen gespeichert" + (" – Neustart nötig." if self.running() else "."))

    # -------------------------------------------------------------- Mods --
    def build_mods(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.mod_table = QTableWidget(0, 4)
        self.mod_table.setHorizontalHeaderLabels(["Mod", "Version", "Wo", "Grund"])
        self.mod_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mod_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mod_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.mod_table)
        h = QHBoxLayout()
        for text, fn in [("＋ Mods hinzufügen (Mod-Browser)", lambda: self.tabs.setCurrentIndex(4)),
                         ("Create-Paket installieren", self.install_preset),
                         ("Ausgewählte entfernen", self.remove_selected),
                         ("⟳ Updates prüfen + installieren", self.updates),
                         ("Mods-Ordner öffnen", lambda: self.open_path(core.server_dir(self.name) / "mods"))]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addLayout(h)
        n = QLabel("„Wo“: Server + Spieler = auf beiden nötig, nur Spieler = z. B. Grafik-Mods (nur in deiner "
                   "Prism-Instanz/Modpack), nur Server = nur auf dem Server. Abhängigkeiten kommen automatisch mit.")
        n.setWordWrap(True)
        n.setStyleSheet("color: gray")
        v.addWidget(n)
        return w

    def load_mods_table(self):
        mods = core.load_mods(self.name) if self.name else []
        mods.sort(key=lambda m: (m.get("dependency_of") is not None, m["title"].lower()))
        self.mod_table.setRowCount(len(mods))
        for r, m in enumerate(mods):
            it = QTableWidgetItem(m["title"])
            it.setData(Qt.ItemDataRole.UserRole, m["project_id"])
            self.mod_table.setItem(r, 0, it)
            self.mod_table.setItem(r, 1, QTableWidgetItem(m["version"]))
            self.mod_table.setItem(r, 2, QTableWidgetItem(core.SIDE_NAMES[m["side"]]))
            self.mod_table.setItem(r, 3, QTableWidgetItem(
                f"benötigt von {m['dependency_of']}" if m.get("dependency_of") else "von dir gewählt"))
        self.mod_table.resizeColumnsToContents()
        self.mod_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.l_mods.setText(f"{len(mods)} installiert")
        self.installed_ids = {m["project_id"] for m in mods}

    def mods_changed_note(self):
        if self.running():
            self.logmsg("Mods geändert – Server neu starten, damit sie aktiv werden.")

    def install_preset(self):
        def go(name, log):
            for slug in PRESET_CREATE:
                core.install_mod(name, slug, log)
        self.run_job(go, self.name, title="Installiere Create + Aeronautics + Walking Steel …",
                     on_done=lambda _: (self.load_mods_table(), self.mods_changed_note()))

    def remove_selected(self):
        rows = sorted({i.row() for i in self.mod_table.selectedItems()})
        if not rows:
            return
        ids = [self.mod_table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]
        titles = [self.mod_table.item(r, 0).text() for r in rows]
        if QMessageBox.question(self, "Entfernen", "Entfernen?\n\n• " + "\n• ".join(titles)) \
                != QMessageBox.StandardButton.Yes:
            return
        for pid in ids:
            core.remove_mod(self.name, pid, self.logmsg)
        self.load_mods_table()
        self.mods_changed_note()

    def updates(self):
        """Updates suchen und direkt installieren (Modrinth + CurseForge)."""
        def done(res):
            self.load_mods_table()
            if not res:
                QMessageBox.information(self, "Updates", "Alle Mods sind aktuell.")
                return
            ok = [r for r in res if not r[3]]
            bad = [r for r in res if r[3]]
            txt = "\n".join(f"• {t}: {a} → {n}" for t, a, n, _ in ok)
            if bad:
                txt += "\n\nFehlgeschlagen:\n" + "\n".join(f"• {t}: {e}" for t, _, _, e in bad)
            txt += ("\n\nServer neu starten, damit die Updates aktiv werden. Mitspieler holen sich den neuen "
                    "Stand über die Mod-Seite.")
            QMessageBox.information(self, f"{len(ok)} Mods aktualisiert", txt)
            self.mods_changed_note()
        self.run_job(core.update_all, self.name, on_done=done, title="Suche und installiere Updates …")

    # -------------------------------------------------------- Mod-Browser --
    def build_browser(self):
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Mod suchen, z. B. create, jei, journeymap …  (leer = beliebteste)")
        self.q.returnPressed.connect(self.search)
        h.addWidget(self.q)
        self.sort = QComboBox()
        for label, key in [("Relevanz", "relevance"), ("Downloads", "downloads"), ("Neueste", "newest"),
                           ("Zuletzt aktualisiert", "updated")]:
            self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(self.search)
        h.addWidget(self.sort)
        b = QPushButton("Suchen")
        b.clicked.connect(self.search)
        h.addWidget(b)
        v.addLayout(h)
        h2 = QHBoxLayout()
        self.b_info = QLabel("Zeigt nur Mods, die zu Minecraft-Version und Loader des Servers passen "
                             "(Quelle: CurseForge).")
        self.b_info.setStyleSheet("color: gray")
        self.b_info.setWordWrap(True)
        h2.addWidget(self.b_info, 1)
        b = QPushButton("CurseForge-API-Schlüssel …")
        b.clicked.connect(self.ask_cf_key)
        h2.addWidget(b)
        v.addLayout(h2)
        self.results = QListWidget()
        self.results.setIconSize(QPixmap(48, 48).size())
        self.results.setWordWrap(True)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results.itemDoubleClicked.connect(lambda _: self.install_selected())
        v.addWidget(self.results)
        h = QHBoxLayout()
        self.b_more = QPushButton("Mehr laden")
        self.b_more.clicked.connect(lambda: self.search(more=True))
        h.addWidget(self.b_more)
        h.addStretch()
        b = QPushButton("Auf CurseForge ansehen")
        b.clicked.connect(self.open_mod_page)
        h.addWidget(b)
        b = QPushButton("⬇  Installieren")
        b.setMinimumHeight(34)
        b.clicked.connect(self.install_selected)
        h.addWidget(b)
        v.addLayout(h)
        self.offset = 0
        self.installed_ids = set()
        return w

    def search(self, more=False):
        if not self.name:
            return
        cfg = core.load_cfg(self.name)
        if cfg["loader"] == "vanilla":
            self.b_info.setText("Vanilla-Server können keine Mods laden – über „Version ändern“ einen Loader wählen.")
            return
        if not more:
            self.offset = 0
            self.results.clear()
        self.b_info.setText(f"Suche für {core.LOADER_NAMES[cfg['loader']]} {cfg['mc']} …")
        if not core.cf_key():
            self.b_info.setText("Für die CurseForge-Suche fehlt noch der API-Schlüssel – Knopf rechts.")
            return
        self.bg(core.cf_search, self._got_results, self.q.text().strip(), cfg["mc"], cfg["loader"],
                self.offset, 30, self.sort.currentData() if self.q.text().strip() or self.sort.currentIndex() else "downloads",
                err=lambda e: self.b_info.setText("Fehler bei der Suche: " + e))

    def _got_results(self, data):
        hits = data["hits"]
        self.offset += len(hits)
        cfg = core.load_cfg(self.name)
        self.b_info.setText(f"{data['total_hits']} Mods für {core.LOADER_NAMES[cfg['loader']]} {cfg['mc']} – "
                            "Doppelklick oder „Installieren“ (Mehrfachauswahl mit Strg).")
        for hit in hits:
            dl = hit["downloads"]
            dls = f"{dl / 1e6:.1f} Mio." if dl >= 1e6 else f"{dl / 1e3:.0f} Tsd." if dl >= 1e3 else str(dl)
            mark = "  ✔ installiert" if hit["project_id"] in self.installed_ids else ""
            if not hit.get("has_file", True):
                mark += "  (keine Datei für diese Version)"
            it = QListWidgetItem(f"{hit['title']}{mark}   ·   {dls} Downloads   ·   von {hit['author']}\n"
                                 f"{hit['description']}")
            it.setData(Qt.ItemDataRole.UserRole, hit)
            self.results.addItem(it)
            if hit.get("icon_url"):
                self.load_icon(it, hit["icon_url"])
        self.b_more.setEnabled(self.offset < data["total_hits"])

    def load_icon(self, item, url):
        if url in self.icon_cache:
            item.setIcon(self.icon_cache[url])
            return

        def fetch(u):
            req = urllib.request.Request(u, headers={"User-Agent": core.UA})
            return urllib.request.urlopen(req, timeout=10).read()

        def got(data):
            pm = QPixmap()
            if pm.loadFromData(data):
                ic = QIcon(pm.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
                self.icon_cache[url] = ic
                try:
                    item.setIcon(ic)
                except RuntimeError:
                    pass
        self.bg(fetch, got, url, err=lambda e: None)

    def open_mod_page(self):
        for it in self.results.selectedItems():
            h = it.data(Qt.ItemDataRole.UserRole)
            subprocess.Popen(["xdg-open", h.get("url") or f"https://www.curseforge.com/minecraft/mc-mods/{h['slug']}"])

    def ask_cf_key(self):
        t = ("CurseForge erlaubt die Mod-Suche nur mit einem eigenen API-Schlüssel (kostenlos).\n\n"
             "1. https://console.curseforge.com öffnen und anmelden\n"
             "2. Links „API Keys“ → den Schlüssel kopieren\n"
             "3. Hier einfügen.\n\nDer Schlüssel wird nur lokal gespeichert (~/MinecraftServer/curseforge-api-key.txt).")
        key, ok = QInputDialog.getText(self, "CurseForge-API-Schlüssel", t, QLineEdit.EchoMode.Normal,
                                       core.cf_key() or "")
        if ok and key.strip():
            core.save_cf_key(key)
            self.b_info.setText("Schlüssel gespeichert – suche …")
            self.search()

    def install_selected(self):
        hits = [it.data(Qt.ItemDataRole.UserRole) for it in self.results.selectedItems()]
        if not hits:
            return

        def go(name, log):
            added = []
            for h in hits:
                if "cf_id" in h:
                    added += core.cf_install_mod(name, h["cf_id"], log)
                else:
                    added += core.install_mod(name, h["project_id"], log)
            return added

        def done(added):
            self.load_mods_table()
            for i in range(self.results.count()):
                it = self.results.item(i)
                h = it.data(Qt.ItemDataRole.UserRole)
                if h["project_id"] in self.installed_ids and "✔" not in it.text():
                    first, rest = it.text().split("\n", 1)
                    t, r = first.split("   ·   ", 1)
                    it.setText(f"{t}  ✔ installiert   ·   {r}\n{rest}")
            self.statusBar().showMessage(f"Installiert: {', '.join(added) or 'nichts Neues'}", 10000)
            self.mods_changed_note()
        self.run_job(go, self.name, on_done=done, show_console=False,
                     title="Installiere " + ", ".join(h["title"] for h in hits) + " …")

    # ----------------------------------------------------------- Spieler --
    def build_players(self):
        w = QWidget()
        h = QHBoxLayout(w)
        self.lists = {}
        for key, title, file in [("whitelist", "Whitelist (wer darf rein)", "whitelist.json"),
                                 ("op", "Admins (OP)", "ops.json")]:
            g = QGroupBox(title)
            v = QVBoxLayout(g)
            lw = QListWidget()
            v.addWidget(lw)
            hb = QHBoxLayout()
            b = QPushButton("＋ Hinzufügen")
            b.clicked.connect(lambda _, k=key: self.add_player(k))
            hb.addWidget(b)
            b = QPushButton("Entfernen")
            b.clicked.connect(lambda _, k=key: self.del_player(k))
            hb.addWidget(b)
            v.addLayout(hb)
            self.lists[key] = (lw, file)
            h.addWidget(g)
        g = QGroupBox("Gerade online")
        v = QVBoxLayout(g)
        self.online = QListWidget()
        v.addWidget(self.online)
        b = QPushButton("Rauswerfen (kick)")
        b.clicked.connect(lambda: [self.send(f"kick {i.text()}") for i in self.online.selectedItems()])
        v.addWidget(b)
        h.addWidget(g)
        return w

    def load_players(self):
        for key, (lw, file) in self.lists.items():
            lw.clear()
            p = core.server_dir(self.name) / file if self.name else None
            if p and p.exists():
                try:
                    for e in json.loads(p.read_text()):
                        lw.addItem(e["name"])
                except Exception:
                    pass

    def add_player(self, key):
        name, ok = QInputDialog.getText(self, "Spieler hinzufügen", "Minecraft-Name:")
        name = name.strip()
        if not ok or not name:
            return
        if self.running():
            self.send(f"whitelist add {name}" if key == "whitelist" else f"op {name}")
            QTimer.singleShot(1500, self.load_players)
            return
        lw, file = self.lists[key]

        def got(res):
            uuid, real = res
            p = core.server_dir(self.name) / file
            data = json.loads(p.read_text()) if p.exists() else []
            if any(e["uuid"] == uuid for e in data):
                return
            e = {"uuid": uuid, "name": real}
            if key == "op":
                e |= {"level": 4, "bypassesPlayerLimit": False}
            data.append(e)
            p.write_text(json.dumps(data, indent=2))
            self.load_players()
        self.bg(core.mojang_uuid, got, name,
                err=lambda e: QMessageBox.warning(self, "Nicht gefunden", f"Spieler „{name}“ nicht gefunden."))

    def del_player(self, key):
        lw, file = self.lists[key]
        for it in lw.selectedItems():
            n = it.text()
            if self.running():
                self.send(f"whitelist remove {n}" if key == "whitelist" else f"deop {n}")
            else:
                p = core.server_dir(self.name) / file
                data = [e for e in json.loads(p.read_text()) if e["name"] != n]
                p.write_text(json.dumps(data, indent=2))
        QTimer.singleShot(1500 if self.running() else 0, self.load_players)

    # ------------------------------------------------------------- Karte --
    def build_map(self):
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.map_info = QLabel()
        self.map_info.setWordWrap(True)
        h.addWidget(self.map_info, 1)
        self.b_map_setup = QPushButton("3D-Karte einrichten (BlueMap)")
        self.b_map_setup.clicked.connect(self.setup_map)
        h.addWidget(self.b_map_setup)
        b = QPushButton("⟳ Neu laden")
        b.clicked.connect(self.reload_map)
        h.addWidget(b)
        b = QPushButton("Ganze Welt zeichnen")
        b.setToolTip("Zeichnet alle bisher entdeckten Gebiete komplett neu (dauert je nach Weltgröße einige Minuten)")
        b.clicked.connect(self.render_full_map)
        h.addWidget(b)
        b = QPushButton("Im Browser öffnen")
        b.clicked.connect(lambda: subprocess.Popen(["xdg-open", self.map_url()]))
        h.addWidget(b)
        v.addLayout(h)
        if QWebEngineView:
            self.map_view = QWebEngineView()
            v.addWidget(self.map_view, 1)
        else:
            self.map_view = None
            v.addWidget(QLabel("Eingebettete Ansicht nicht verfügbar – „Im Browser öffnen“ nutzen."))
        self.tabs.currentChanged.connect(lambda i: self.tabs.widget(i) is w and self.reload_map(auto=True))
        return w

    def map_port(self):
        p = core.server_dir(self.name) / "config/bluemap/webserver.conf"
        if p.exists():
            m = re.search(r"^\s*port:\s*(\d+)", p.read_text(), re.M)
            if m:
                return int(m.group(1))
        return 8100

    def map_url(self):
        return f"http://localhost:{self.map_port()}"

    def has_bluemap(self):
        return any(m["slug"] == "bluemap" for m in core.load_mods(self.name)) if self.name else False

    def update_map_info(self):
        if not self.name:
            return
        if not self.has_bluemap():
            self.map_info.setText("Die 3D-Karte zeigt deine Welt von oben/schräg (wie Google Earth) und live, wo "
                                  "die Spieler sind. Dafür wird die Server-Mod <b>BlueMap</b> installiert – "
                                  "Freunde brauchen sie nicht.")
            self.b_map_setup.show()
        else:
            self.b_map_setup.hide()
            lan = core.lan_ip()
            self.map_info.setText(f"BlueMap aktiv. Karte: <b>{self.map_url()}</b> – im Heimnetz auch "
                                  f"<b>http://{lan}:{self.map_port()}</b>. Gebiete werden nach und nach "
                                  "gezeichnet (erster Durchlauf dauert einige Minuten). Spieler = Köpfe auf der Karte.")

    def reload_map(self, auto=False):
        self.update_map_info()
        if not self.map_view:
            return
        if self.has_bluemap() and self.running():
            cur = self.map_view.url().toString()
            if cur.startswith(self.map_url()):
                if not auto:  # „Neu laden“: ohne Cache, damit neue Skripte/Skins ankommen
                    from PyQt6.QtWebEngineCore import QWebEnginePage
                    self.map_view.page().triggerAction(QWebEnginePage.WebAction.ReloadAndBypassCache)
            else:
                self.map_view.setUrl(QUrl(self.map_url()))
        else:
            why = ("Server läuft nicht – starte ihn, dann erscheint hier die Karte." if self.has_bluemap()
                   else "Noch keine Karte eingerichtet – oben auf „3D-Karte einrichten“ klicken.")
            self.map_view.setHtml(f"<body style='background:#202326;color:#bbb;font-family:sans-serif;"
                                  f"display:flex;align-items:center;justify-content:center;height:90vh'>"
                                  f"<h2>{why}</h2></body>")

    def setup_map(self):
        r = QMessageBox.question(
            self, "3D-Karte (BlueMap)",
            "BlueMap wird als Server-Mod installiert.\n\nFür die Block-Texturen lädt BlueMap einmalig die "
            "Minecraft-Spieldateien von Mojang herunter. Das ist nur erlaubt, wenn du der Minecraft-EULA "
            "zustimmst (https://aka.ms/MinecraftEULA) – wie beim Server selbst.\n\nEinverstanden?")
        if r != QMessageBox.StandardButton.Yes:
            return
        cfg = core.load_cfg(self.name)
        cfg["bluemap_accept"] = True
        core.save_cfg(self.name, cfg)

        def done(_):
            self.load_mods_table()
            self.reload_map()
            QMessageBox.information(self, "BlueMap installiert",
                                    "Fertig. Server " + ("neu starten" if self.running() else "starten") +
                                    " – danach erscheint die Karte im Reiter „Karte“.")
        self.run_job(core.install_mod, self.name, "bluemap", on_done=done, show_console=False,
                     title="Installiere BlueMap …")

    def export_mobs(self):
        """Mobs aus den Speicherdateien für die Karte aufbereiten (läuft im Hintergrund)."""
        if not (self.running() and self.has_bluemap()):
            return
        import mobs
        d = core.server_dir(self.name)
        level = core.read_properties(self.name).get("level-name", "world")
        self.bg(mobs.export, lambda _: None, d, core.bluemap_webroot(self.name), level, err=lambda e: None)

    def setup_map_addon(self):
        """3D-Figuren-Skript einhängen und Skins bekannter Spieler laden."""
        if not self.has_bluemap():
            return
        try:
            if core.bluemap_addon(self.name):
                self.logmsg("Karte: 3D-Spielerfiguren eingerichtet.")
        except Exception as e:  # noqa: BLE001
            self.logmsg(f"Karte: Zusatz-Skript nicht eingerichtet ({e})")

        def skins(name):
            for _, uuid in core.known_players(name):
                try:
                    core.fetch_skin(name, uuid)
                except Exception:  # noqa: BLE001
                    pass
        self.bg(skins, lambda _: None, self.name, err=lambda e: None)
        QTimer.singleShot(5000, self.export_mobs)

    def render_full_map(self):
        if not self.has_bluemap():
            return
        if self.send_checked("save-all"):
            maps = sorted(p.stem for p in (core.server_dir(self.name) / "config/bluemap/maps").glob("*.conf")) or ["world"]
            for i, m in enumerate(maps):
                QTimer.singleShot(3000 + i * 500, lambda m=m: self.send(f"bluemap force-update {m}"))
            self.statusBar().showMessage("Karte wird komplett gezeichnet – Fortschritt mit „bluemap“ in der Konsole.", 10000)

    def bluemap_accept(self):
        """Setzt accept-download in BlueMaps core.conf, wenn Felix zugestimmt hat."""
        if not self.name or not core.load_cfg(self.name).get("bluemap_accept"):
            return False
        p = core.server_dir(self.name) / "config/bluemap/core.conf"
        if p.exists():
            t = p.read_text()
            if re.search(r"^\s*accept-download:\s*false", t, re.M):
                p.write_text(re.sub(r"^(\s*accept-download:\s*)false", r"\1true", t, flags=re.M))
                return True
        return False

    # ----------------------------------------------------- Server wählen --
    def refresh_servers(self, select=None):
        cur = select or self.name
        self.sel.blockSignals(True)
        self.sel.clear()
        self.sel.addItems(core.list_servers())
        if cur:
            self.sel.setCurrentText(cur)
        self.sel.blockSignals(False)
        self.on_select()

    def on_select(self, *_):
        if self.running():
            return
        if self.name:
            modseite.active["name"] = self.name
            try:
                gj = core.BASE / "gui.json"
                try:
                    d = json.loads(gj.read_text())
                except (OSError, ValueError):
                    d = {}
                d["last"] = self.name
                gj.write_text(json.dumps(d))
            except OSError:
                pass
        self.refresh_all()

    def refresh_all(self):
        if not self.name:
            self.l_version.setText("Noch kein Server – „＋ Neuer Server“ klicken.")
            return
        cfg = core.load_cfg(self.name)
        self.l_version.setText(f"<b>Minecraft {cfg['mc']}</b> · {core.LOADER_NAMES[cfg['loader']]} "
                               f"{cfg['loader_version'] if cfg['loader'] != 'vanilla' else ''} · Java {cfg['java']}")
        self.ram.blockSignals(True)
        self.ram.setValue(max(1, cfg["ram_mb"] // 1024))
        self.ram.blockSignals(False)
        self.load_settings()
        self.load_mods_table()
        self.load_players()
        self.results.clear()
        self.update_state()
        self.reload_map(auto=True)

    def new_server(self):
        d = VersionDialog(self, "Neuen Server anlegen")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        name, mc, loader, lv = d.values()
        name = re.sub(r"[^\w\- ]", "", name).strip()
        if not name or not lv:
            QMessageBox.warning(self, "Fehlt", "Name und Loader-Version angeben.")
            return
        self.run_job(core.create_server, name, mc, loader, lv,
                     on_done=lambda _: self.refresh_servers(name), title=f"Lege Server „{name}“ an …")

    def import_pack(self):
        if self.running():
            QMessageBox.information(self, "Server läuft", "Bitte den Server zuerst stoppen.")
            return
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "Modpack wählen", start,
                                              "Modpacks (*.mrpack *.zip);;Alle Dateien (*)")
        if not path:
            return
        try:
            info = core.read_modpack(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kein Modpack", f"Die Datei lässt sich nicht lesen:\n{e}")
            return
        default = re.sub(r"[^\w\- ]", "", info["name"]).strip()[:40] or "Modpack"
        name, ok = QInputDialog.getText(
            self, "Modpack importieren",
            f"„{info['name']}“\nMinecraft {info['mc']} · {core.LOADER_NAMES[info['loader']]} "
            f"{info['loader_version']} · {info['count']} Mods\n\n"
            "Wird als neuer Server angelegt, dazu eine Prism-Instanz mit denselben Mods.\n\nName:",
            text=default)
        name = re.sub(r"[^\w\- ]", "", name).strip()
        if not ok or not name:
            return
        if name in core.list_servers():
            QMessageBox.warning(self, "Name vergeben", f"Es gibt schon einen Server „{name}“.")
            return

        def done(res):
            self.refresh_servers(name)
            msg = (f"Fertig: {res['mods']} Mods installiert.\n\nServer „{name}“ ist ausgewählt – "
                   "„Starten“ klicken. Zum Spielen „Selbst spielen (Prism)“ "
                   f"(Instanz „Server {name}“).")
            if res["unsure"]:
                msg += ("\n\nBei diesen Mods weiß ich nicht sicher, ob sie auf den Server gehören. Falls der "
                        "Server deswegen abstürzt, im Reiter „Mods“ entfernen:\n• "
                        + "\n• ".join(res["unsure"][:15]) + ("\n• …" if len(res["unsure"]) > 15 else ""))
            QMessageBox.information(self, "Modpack importiert", msg)
        self.run_job(core.import_modpack, path, name, on_done=done, title=f"Importiere Modpack als „{name}“ …")

    # -------------------------------------------------- Start / Stopp -----
    def update_state(self):
        run = self.running()
        has = bool(self.name)
        if run:
            self.status.setText("<span style='color:#2a2'>● läuft</span>")
        else:
            self.status.setText("<span style='color:#999'>● gestoppt</span>")
        self.b_start.setEnabled(has and not run and not self.busy)
        self.b_stop.setEnabled(run)
        self.b_restart.setEnabled(run)
        self.sel.setEnabled(not run and not self.busy)

    # KDE-Connect-Gerät für Status-Meldungen aufs Handy (optional, in ~/MinecraftServer/gui.json „kdeconnect_device“)
    try:
        HANDY = json.loads((core.BASE / "gui.json").read_text()).get("kdeconnect_device")
    except (OSError, ValueError):
        HANDY = None

    def phone(self, text):
        """Kurze Meldung per KDE Connect ans Handy (nur wenn per Handy-Befehl ausgelöst)."""
        if getattr(self, "notify_phone", False) and self.HANDY:
            subprocess.Popen(["kdeconnect-cli", "-d", self.HANDY, "--ping-msg", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self):
        if not self.name or self.running():
            return
        if not core.eula_ok(self.name):
            r = QMessageBox.question(
                self, "Minecraft-EULA",
                "Damit der Server startet, musst du die Minecraft-EULA (Lizenzbedingungen von Mojang) "
                "akzeptieren:\nhttps://aka.ms/MinecraftEULA\n\nAkzeptierst du sie?")
            if r != QMessageBox.StandardButton.Yes:
                return
            core.accept_eula(self.name)
        try:
            cmd = core.java_cmd(self.name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Fehler", str(e))
            return
        self.bluemap_accept()
        self.modseite_motd()
        self.players.clear()
        self.update_players()
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(str(core.server_dir(self.name)))
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.read_out)
        self.proc.finished.connect(self.on_finished)
        self.restart_after = False
        self.console.appendPlainText(f"[Manager] Starte Server „{self.name}“ …")
        self.proc.start(cmd[0], cmd[1:])
        self.tabs.setCurrentIndex(1)
        self.update_state()

    def modseite_motd(self):
        """Modpack-Version + Adresse der Mod-Seite als 2. Zeile in die Serverliste (MOTD)."""
        modseite.active["name"] = self.name
        try:
            ts = self.ts_ip or core.tailscale_ip() or core.lan_ip()
            host = f"{ts}:{modseite.PORT}"
            core.write_properties(self.name, {"motd": modseite.motd(self.name, host)})
            st = modseite.pack_state(self.name)
            self.logmsg(f"Modpack v{st['version']} – Mod-Seite: http://{host}/")
            self.bg(modseite.build, lambda _: None, self.name)
        except Exception as e:  # noqa: BLE001
            self.logmsg(f"Mod-Seite: {e}")

    def stop(self):
        if self.running():
            self.send("stop")
            # nur DIESEN Prozess notfalls abwürgen – nicht einen später neu gestarteten
            p = self.proc
            QTimer.singleShot(60000, lambda: p.state() != QProcess.ProcessState.NotRunning and p.kill())

    def restart(self):
        self.restart_after = True
        self.stop()

    def on_finished(self, code, _status):
        self.console.appendPlainText(f"[Manager] Server beendet (Code {code}).")
        if not getattr(self, "restart_after", False):
            self.phone(f"MC-Server „{self.name}“ ist aus.")
            self.notify_phone = False
        self.players.clear()
        self.update_players()
        self.update_state()
        if getattr(self, "restart_after", False):
            self.restart_after = False
            QTimer.singleShot(1000, self.start)

    UUID = re.compile(r"UUID of player (\w{2,16}) is ([0-9a-f-]{36})")
    JOIN = re.compile(r"\]: (\w{2,16}) joined the game")
    LEFT = re.compile(r"\]: (\w{2,16}) left the game")
    DONE = re.compile(r"\]: Done \(([\d.]+)s\)!")
    ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[m")

    def read_out(self):
        data = self.ANSI.sub("", bytes(self.proc.readAllStandardOutput()).decode(errors="replace"))
        for line in data.splitlines():
            self.console.appendPlainText(line)
            if m := self.UUID.search(line):
                if self.has_bluemap():
                    self.bg(core.fetch_skin, lambda _: None, self.name, m.group(2), err=lambda e: None)
            if m := self.JOIN.search(line):
                self.players.add(m.group(1))
                self.update_players()
            elif m := self.LEFT.search(line):
                self.players.discard(m.group(1))
                self.update_players()
            elif m := self.DONE.search(line):
                self.phone(f"MC-Server „{self.name}“ läuft (Start {m.group(1)} s).")
                self.status.setText(f"<span style='color:#2a2'>● läuft – bereit (Start {m.group(1)} s)</span>")
                self.load_players()
                einstellungen.apply_pending_gamerules(self.name, self.send, self.logmsg)
                if self.bluemap_accept():
                    self.send("bluemap reload")
                self.setup_map_addon()
                QTimer.singleShot(3000, lambda: self.reload_map(auto=True))
            elif "You must accept the required file download" in line:
                # BlueMap schreibt core.conf erst nach dem Serverstart → jetzt zustimmen + neu laden
                if self.bluemap_accept():
                    self.logmsg("BlueMap-Download freigegeben, lade BlueMap neu …")
                    QTimer.singleShot(1500, lambda: self.send("bluemap reload"))
                    QTimer.singleShot(15000, lambda: self.reload_map())
            elif "agree to the EULA" in line:
                self.logmsg("EULA nicht akzeptiert.")
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def update_players(self):
        self.online.clear()
        self.online.addItems(sorted(self.players))
        cur = self.spawn_player.currentText()
        self.spawn_player.clear()
        self.spawn_player.addItems(sorted(self.players))
        if cur in self.players:
            self.spawn_player.setCurrentText(cur)
        self.l_players.setText(", ".join(sorted(self.players)) or ("niemand" if self.running() else "–"))

    def closeEvent(self, ev):
        if self.running():
            r = QMessageBox.question(self, "Server läuft",
                                     "Der Server läuft noch. Server stoppen und Fenster schließen?")
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.send("stop")
            if not self.proc.waitForFinished(60000):
                self.proc.kill()
        ev.accept()


def main():
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    app = QApplication(sys.argv)
    app.setApplicationName("Minecraft-Server")
    app.setDesktopFileName("mc-server")
    want_start = "--start" in sys.argv
    want_stop = "--stop" in sys.argv
    key = "felix-mc-server-gui"

    # Läuft das Programm schon? Dann nur dem offenen Fenster Bescheid geben.
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(500):
        sock.write(b"stop" if want_stop else b"start" if want_start else b"show")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return
    if want_stop:  # Fenster läuft nicht → dann läuft auch kein Server
        print("Server-Fenster läuft nicht – nichts zu stoppen.")
        if Main.HANDY:
            subprocess.Popen(["kdeconnect-cli", "-d", Main.HANDY, "--ping-msg", "MC-Server war schon aus."])
        return

    QLocalServer.removeServer(key)
    srv = QLocalServer()
    srv.listen(key)
    w = Main()

    def on_conn():
        c = srv.nextPendingConnection()
        c.waitForReadyRead(1000)
        msg = bytes(c.readAll())
        if msg == b"stop":
            if w.running():
                w.notify_phone = True
                w.stop()
            else:
                w.notify_phone = True
                w.phone("MC-Server war schon aus.")
                w.notify_phone = False
            return
        if msg == b"start":
            if w.running():
                w.notify_phone = True
                w.phone(f"MC-Server „{w.name}“ läuft schon.")
                w.notify_phone = False
            else:
                w.notify_phone = True
                w.start()
            return
        w.showNormal()
        w.raise_()
        w.activateWindow()
    srv.newConnection.connect(on_conn)
    w.show()
    if want_start:
        w.notify_phone = True
        QTimer.singleShot(800, w.start)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
