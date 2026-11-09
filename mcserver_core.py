"""Kern-Logik für den Minecraft-Server-Manager (ohne GUI).

Server liegen unter ~/MinecraftServer/server/<Name>/, jeder mit server.json
(Version, Loader, RAM) und mods.json (installierte Mods von Modrinth).
Zu jedem Server wird eine passende Prism-Launcher-Instanz angelegt,
damit Felix mit genau denselben Mods spielen kann.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = Path.home() / "MinecraftServer"
SERVERS = BASE / "server"
JAVA_DIR = BASE / "java"
CACHE = BASE / "cache"
BACKUPS = BASE / "backups"
EXPORTS = BASE / "modpacks"
PRISM = Path.home() / ".local/share/PrismLauncher"
UA = "felix-mcserver-gui/1.0 (local)"
MODRINTH = "https://api.modrinth.com/v2"

LOADERS = ["neoforge", "forge", "fabric", "vanilla"]
LOADER_NAMES = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric", "vanilla": "Vanilla (ohne Mods)"}


# ---------------------------------------------------------------- HTTP ----
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=30):
    return json.loads(http_get(url, timeout))


def download(url, dest, log=None, sha1=None, sha512=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h1, h512 = hashlib.sha1(), hashlib.sha512()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h1.update(chunk)
            h512.update(chunk)
            done += len(chunk)
            if log and total and time.time() - last > 1:
                last = time.time()
                log(f"  … {dest.name}: {done * 100 // total} %")
    if sha1 and h1.hexdigest() != sha1:
        tmp.unlink()
        raise RuntimeError(f"Prüfsumme falsch: {dest.name}")
    if sha512 and h512.hexdigest() != sha512:
        tmp.unlink()
        raise RuntimeError(f"Prüfsumme falsch: {dest.name}")
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------ Versionen ---
def mc_versions(include_snapshots=False):
    data = http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    return [v["id"] for v in data["versions"] if include_snapshots or v["type"] == "release"], data


def mc_version_meta(mc):
    _, data = mc_versions(True)
    for v in data["versions"]:
        if v["id"] == mc:
            return http_json(v["url"])
    raise RuntimeError(f"Minecraft-Version {mc} unbekannt")


def java_major_for(mc):
    try:
        return int(mc_version_meta(mc).get("javaVersion", {}).get("majorVersion", 21))
    except Exception:
        return 21


def _maven_versions(url):
    xml = http_get(url).decode()
    return re.findall(r"<version>([^<]+)</version>", xml)


def loader_versions(loader, mc):
    """Liste der Loader-Versionen für eine MC-Version, neueste zuerst."""
    if loader == "vanilla":
        return ["-"]
    if loader == "neoforge":
        allv = _maven_versions("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml")
        if mc.startswith("1."):
            parts = mc.split(".")
            prefix = f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}."
        else:
            prefix = mc + "."
        vs = [v for v in allv if v.startswith(prefix)]
        stable = [v for v in vs if "beta" not in v and "alpha" not in v]
        return list(reversed(stable or vs))
    if loader == "forge":
        allv = _maven_versions("https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml")
        vs = [v.split("-", 1)[1] for v in allv if v.startswith(mc + "-")]
        return vs  # Forge-Metadaten sind schon absteigend sortiert
    if loader == "fabric":
        data = http_json(f"https://meta.fabricmc.net/v2/versions/loader/{mc}")
        return [d["loader"]["version"] for d in data]
    raise ValueError(loader)


# ----------------------------------------------------------------- Java ---
def java_path(major, log=print):
    """Liefert java aus ~/MinecraftServer/java/<major>, lädt es bei Bedarf (Adoptium)."""
    d = JAVA_DIR / str(major)
    exe = d / "bin" / "java"
    if exe.exists():
        return exe
    log(f"Lade Java {major} (Eclipse Temurin) …")
    url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/linux/x64/jre/hotspot/normal/eclipse"
    tgz = download(url, CACHE / f"java{major}.tar.gz", log)
    tmp = JAVA_DIR / f".tmp{major}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(tgz) as t:
        t.extractall(tmp, filter="data")
    inner = next(tmp.iterdir())
    shutil.rmtree(d, ignore_errors=True)
    inner.rename(d)
    shutil.rmtree(tmp, ignore_errors=True)
    tgz.unlink()
    log(f"Java {major} bereit.")
    return exe


# --------------------------------------------------------------- Server ---
def server_dir(name):
    return SERVERS / name


def list_servers():
    if not SERVERS.exists():
        return []
    return sorted(p.name for p in SERVERS.iterdir() if (p / "server.json").exists())


def load_cfg(name):
    return json.loads((server_dir(name) / "server.json").read_text())


def save_cfg(name, cfg):
    (server_dir(name) / "server.json").write_text(json.dumps(cfg, indent=2))


def load_mods(name):
    p = server_dir(name) / "mods.json"
    return json.loads(p.read_text()) if p.exists() else []


def save_mods(name, mods):
    (server_dir(name) / "mods.json").write_text(json.dumps(mods, indent=2))


def install_loader(name, log=print):
    """Installiert/aktualisiert die Server-Software laut server.json."""
    cfg = load_cfg(name)
    d = server_dir(name)
    mc, loader, lv = cfg["mc"], cfg["loader"], cfg["loader_version"]
    java = java_path(cfg["java"], log)
    log(f"Installiere {LOADER_NAMES[loader]} {lv} für Minecraft {mc} …")
    if loader == "vanilla":
        meta = mc_version_meta(mc)
        s = meta["downloads"]["server"]
        download(s["url"], d / "server.jar", log, sha1=s["sha1"])
        cfg["launch"] = ["-jar", "server.jar"]
    elif loader == "fabric":
        inst = http_json("https://meta.fabricmc.net/v2/versions/installer")[0]["version"]
        download(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{lv}/{inst}/server/jar",
                 d / "fabric-server-launch.jar", log)
        cfg["launch"] = ["-jar", "fabric-server-launch.jar"]
    else:
        if loader == "neoforge":
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{lv}/neoforge-{lv}-installer.jar"
            args_file = f"libraries/net/neoforged/neoforge/{lv}/unix_args.txt"
        else:
            url = (f"https://maven.minecraftforge.net/net/minecraftforge/forge/{mc}-{lv}/"
                   f"forge-{mc}-{lv}-installer.jar")
            args_file = f"libraries/net/minecraftforge/forge/{mc}-{lv}/unix_args.txt"
        inst = download(url, CACHE / Path(url).name, log)
        log("Starte Installer (dauert 1–2 Minuten) …")
        p = subprocess.Popen([str(java), "-jar", str(inst), "--installServer", str(d)], cwd=d,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            line = line.rstrip()
            if line and ".class" not in line and any(k in line for k in ("ERROR", "Successfully", "failed")):
                log("  " + line[:160])
        if p.wait() != 0 or not (d / args_file).exists():
            raise RuntimeError("Loader-Installation fehlgeschlagen (siehe Ausgabe oben)")
        for junk in ["run.bat", "installer.log"] + [p.name for p in d.glob("*installer.jar.log")]:
            (d / junk).unlink(missing_ok=True)
        cfg["launch"] = ["@" + args_file]
    save_cfg(name, cfg)
    log("Server-Software installiert.")


def create_server(name, mc, loader, loader_version, ram_mb=6144, log=print):
    d = server_dir(name)
    if (d / "server.json").exists():
        raise RuntimeError(f"Server „{name}“ gibt es schon")
    d.mkdir(parents=True, exist_ok=True)
    (d / "mods").mkdir(exist_ok=True)
    cfg = {"name": name, "mc": mc, "loader": loader, "loader_version": loader_version,
           "ram_mb": ram_mb, "java": java_major_for(mc), "created": time.strftime("%Y-%m-%d %H:%M")}
    save_cfg(name, cfg)
    save_mods(name, [])
    if not (d / "server.properties").exists():
        write_properties(name, DEFAULT_PROPS | {"motd": f"{name} – Felix' Server"})
    install_loader(name, log)
    ensure_prism_instance(name, log)
    return cfg


def java_cmd(name):
    cfg = load_cfg(name)
    java = java_path(cfg["java"], lambda *_: None)
    ram = int(cfg["ram_mb"])
    flags = [f"-Xms{min(ram, 2048)}M", f"-Xmx{ram}M", "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled",
             "-XX:MaxGCPauseMillis=200", "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC",
             "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M"]
    return [str(java)] + flags + cfg["launch"] + ["nogui"]


def eula_ok(name):
    p = server_dir(name) / "eula.txt"
    return p.exists() and "eula=true" in p.read_text()


def accept_eula(name):
    (server_dir(name) / "eula.txt").write_text(
        "# Von Felix im Server-Manager akzeptiert: https://aka.ms/MinecraftEULA\neula=true\n")


# ---------------------------------------------------- server.properties ---
DEFAULT_PROPS = {
    "motd": "Felix' Minecraft-Server", "max-players": "10", "difficulty": "normal",
    "gamemode": "survival", "pvp": "true", "online-mode": "true", "white-list": "false",
    "view-distance": "10", "simulation-distance": "8", "server-port": "25565",
    "spawn-protection": "0", "allow-flight": "true", "level-name": "world", "level-seed": "",
    "enable-command-block": "true", "max-tick-time": "-1", "hardcore": "false",
}


def read_properties(name):
    p = server_dir(name) / "server.properties"
    props = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v
    return props


def write_properties(name, new):
    p = server_dir(name) / "server.properties"
    lines = p.read_text().splitlines() if p.exists() else ["#Minecraft server properties"]
    seen = set()
    out = []
    for line in lines:
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in new:
                out.append(f"{k}={new[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in new.items():
        if k not in seen:
            out.append(f"{k}={v}")
    p.write_text("\n".join(out) + "\n")


# --------------------------------------------------------------- Modrinth --
def mr_loader_facets(loader):
    # NeoForge kann viele alte Forge-Mods nicht laden – nur echte NeoForge-Mods zeigen
    return [[f"categories:{loader}"]]


def mr_search(query, mc, loader, offset=0, limit=30, sort="relevance"):
    facets = [["project_type:mod"], [f"versions:{mc}"]] + mr_loader_facets(loader)
    q = urllib.parse.urlencode({"query": query, "facets": json.dumps(facets), "limit": limit,
                                "offset": offset, "index": sort})
    return http_json(f"{MODRINTH}/search?{q}")


def mr_project(pid):
    return http_json(f"{MODRINTH}/project/{pid}")


def mr_best_version(pid, mc, loader):
    vs = http_json(f"{MODRINTH}/project/{pid}/version?" + urllib.parse.urlencode(
        {"loaders": json.dumps([loader]), "game_versions": json.dumps([mc])}))
    if not vs:
        return None
    rel = [v for v in vs if v["version_type"] == "release"]
    return (rel or vs)[0]  # Modrinth liefert neueste zuerst


def side_of(project):
    """'both', 'server' oder 'client' – wo die Mod hingehört."""
    c, s = project.get("client_side"), project.get("server_side")
    if s == "unsupported":
        return "client"
    if c == "unsupported":
        return "server"
    return "both"


def _primary(version):
    files = version["files"]
    return next((f for f in files if f.get("primary")), files[0])


def install_mod(name, project_id, log=print, version=None, _seen=None, dependency_of=None):
    """Installiert eine Mod samt benötigter Abhängigkeiten. Gibt Liste neu installierter Titel zurück."""
    cfg = load_cfg(name)
    mc, loader = cfg["mc"], cfg["loader"]
    mods = load_mods(name)
    installed = {m["project_id"]: m for m in mods}
    _seen = _seen if _seen is not None else set()
    proj = mr_project(project_id)
    pid = proj["id"]
    if pid in _seen:
        return []
    _seen.add(pid)
    if pid in installed:
        if dependency_of is None:
            log(f"„{proj['title']}“ ist schon installiert.")
        return []
    if version is None:
        version = mr_best_version(pid, mc, loader)
    if version is None:
        raise RuntimeError(f"„{proj['title']}“ hat keine Version für {LOADER_NAMES[loader]} {mc}")
    new = []
    # erst Abhängigkeiten
    for dep in version.get("dependencies", []):
        if dep["dependency_type"] != "required":
            continue
        dver = None
        dpid = dep.get("project_id")
        if dep.get("version_id"):
            try:
                dver = http_json(f"{MODRINTH}/version/{dep['version_id']}")
                if mc not in dver["game_versions"] or loader not in dver["loaders"]:
                    dver = None
                dpid = dpid or dver and dver["project_id"]
            except Exception:
                dver = None
        if not dpid:
            continue
        new += install_mod(name, dpid, log, dver, _seen, dependency_of=proj["title"])
    f = _primary(version)
    same = next((m for m in load_mods(name) if m["filename"] == f["filename"]), None)
    if same:  # gleiche Datei schon da (z. B. aus CurseForge-Import) → Modrinth-Eintrag übernimmt
        if not same["project_id"].startswith("cf:"):
            return new
        save_mods(name, [m for m in load_mods(name) if m is not same and m["filename"] != f["filename"]])
    side = side_of(proj)
    log(f"Installiere {proj['title']} {version['version_number']} ({SIDE_NAMES[side]})"
        + (f" – benötigt von {dependency_of}" if dependency_of else ""))
    cache_file = CACHE / "mods" / f["filename"]
    if not cache_file.exists():
        download(f["url"], cache_file, log, sha512=f["hashes"].get("sha512"))
    entry = {"project_id": pid, "slug": proj["slug"], "title": proj["title"],
             "version_id": version["id"], "version": version["version_number"],
             "filename": f["filename"], "url": f["url"], "size": f["size"],
             "sha1": f["hashes"].get("sha1"), "sha512": f["hashes"].get("sha512"),
             "side": side, "dependency_of": dependency_of, "icon": proj.get("icon_url")}
    mods = load_mods(name)
    mods.append(entry)
    save_mods(name, mods)
    sync_files(name)
    return new + [proj["title"]]


SIDE_NAMES = {"both": "Server + Spieler", "server": "nur Server", "client": "nur Spieler"}


def remove_mod(name, project_id, log=print):
    mods = load_mods(name)
    entry = next((m for m in mods if m["project_id"] == project_id), None)
    if not entry:
        return
    mods = [m for m in mods if m["project_id"] != project_id]
    save_mods(name, mods)
    log(f"Entfernt: {entry['title']}")
    # verwaiste Abhängigkeiten melden (nicht automatisch löschen)
    sync_files(name)


def sync_files(name):
    """Legt die Mod-Dateien laut mods.json in Server- und Prism-mods-Ordner (alles andere bleibt)."""
    mods = load_mods(name)
    targets = {"server": server_dir(name) / "mods", "client": prism_instance_dir(name) / "minecraft" / "mods"}
    for side, folder in targets.items():
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / ".verwaltet.json"
        managed_before = set(json.loads(marker.read_text())) if marker.exists() else set()
        wanted = {m["filename"]: m for m in mods if m["side"] in ("both", side)}
        for fn in managed_before - set(wanted):
            (folder / fn).unlink(missing_ok=True)
        for fn, m in wanted.items():
            dst = folder / fn
            if not dst.exists():
                src = CACHE / "mods" / fn
                if not src.exists():
                    download(m["url"], src, sha512=m.get("sha512"))
                shutil.copy2(src, dst)
        marker.write_text(json.dumps(sorted(wanted)))


CF_LOADER = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric"}


def cf_latest(pid, fid, mc, loader):
    """Neueste passende CurseForge-Datei (gleiche MC-Version + Loader, nicht instabiler als die jetzige)."""
    cur = _cf_get(f"{CF_WEB}/{pid}/files/{fid}")["data"] if fid else {}
    rtype = cur.get("releaseType", 1)
    data = _cf_get(f"{CF_WEB}/{pid}/files?pageIndex=0&pageSize=50&sort=dateCreated&sortDescending=true")["data"]
    ok = [f for f in data if mc in f.get("gameVersions", []) and CF_LOADER.get(loader) in f.get("gameVersions", [])
          and f.get("releaseType", 1) <= rtype and f.get("fileName", "").endswith(".jar")]
    return max(ok, key=lambda f: f["dateCreated"]) if ok else None


def check_updates(name, log=print):
    """Liste (Mod, neue Version) für Modrinth- und CurseForge-Mods."""
    cfg = load_cfg(name)
    res = []
    for m in load_mods(name):
        try:
            if m["project_id"].startswith("cf:"):
                pid = m["project_id"][3:]
                fm = re.search(r"/files/(\d+)", m.get("url", ""))
                fid = fm.group(1) if fm else None
                f = cf_latest(pid, fid, cfg["mc"], cfg["loader"])
                if f and str(f["id"]) != fid and f["fileName"] != m["filename"]:
                    res.append((m, {"cf": f, "version_number": f["fileName"]}))
                continue
            if not m.get("version_id"):
                continue  # lokale Datei
            v = mr_best_version(m["project_id"], cfg["mc"], cfg["loader"])
            if v and v["id"] != m["version_id"]:
                res.append((m, v))
        except Exception as e:  # noqa: BLE001
            log(f"Update-Prüfung für {m['title']} fehlgeschlagen: {e}")
    return res


def apply_update(name, m, v, log=print):
    if "cf" in v:
        f = v["cf"]
        url = f"{CF_WEB}/{m['project_id'][3:]}/files/{f['id']}/download"
        cache_file = CACHE / "mods" / f["fileName"]
        if not cache_file.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            tmp = cache_file.with_suffix(".part")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            if f.get("fileLength") and tmp.stat().st_size != f["fileLength"]:
                tmp.unlink()
                raise RuntimeError(f"Download unvollständig: {f['fileName']}")
            tmp.replace(cache_file)
        sha1, sha512 = _sha(cache_file)
        new = dict(version=f["fileName"], filename=f["fileName"], url=url, size=cache_file.stat().st_size,
                   sha1=sha1, sha512=sha512)
    else:
        f = _primary(v)
        cache_file = CACHE / "mods" / f["filename"]
        if not cache_file.exists():
            download(f["url"], cache_file, log, sha512=f["hashes"].get("sha512"))
        new = dict(version_id=v["id"], version=v["version_number"], filename=f["filename"],
                   url=f["url"], size=f["size"], sha1=f["hashes"].get("sha1"), sha512=f["hashes"].get("sha512"))
    mods = load_mods(name)
    for e in mods:
        if e["project_id"] == m["project_id"]:
            e.update(new)
    save_mods(name, mods)
    sync_files(name)
    log(f"Aktualisiert: {m['title']} → {v['version_number']}")


def update_all(name, log=print):
    """Sucht Updates und installiert alle. Sichert vorher mods.json. Gibt Liste (Titel, alt, neu, Fehler) zurück."""
    ups = check_updates(name, log)
    if not ups:
        log("Alle Mods sind aktuell.")
        return []
    bak = server_dir(name) / f"mods.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(server_dir(name) / "mods.json", bak)
    log(f"{len(ups)} Updates gefunden – Sicherung der Mod-Liste: {bak.name}")
    done = []
    for m, v in ups:
        try:
            apply_update(name, m, v, log)
            done.append((m["title"], m.get("version") or m["filename"], v["version_number"], None))
        except Exception as e:  # noqa: BLE001
            log(f"Update von {m['title']} fehlgeschlagen: {e}")
            done.append((m["title"], m.get("version") or m["filename"], v["version_number"], str(e)))
    return done


def change_version(name, mc, loader, loader_version, log=print):
    """Wechselt MC-/Loader-Version, sichert vorher die Welt und passt Mods an."""
    cfg = load_cfg(name)
    log("Sichere zuerst die Welt …")
    backup(name, log)
    old_mods = load_mods(name)
    cfg.update(mc=mc, loader=loader, loader_version=loader_version, java=java_major_for(mc))
    save_cfg(name, cfg)
    d = server_dir(name)
    shutil.rmtree(d / "libraries", ignore_errors=True)
    for f in ("server.jar", "fabric-server-launch.jar", "user_jvm_args.txt", "run.sh"):
        (d / f).unlink(missing_ok=True)
    install_loader(name, log)
    save_mods(name, [])
    missing = []
    seen = set()
    if loader != "vanilla":
        for m in [m for m in old_mods if not m.get("dependency_of")] + [m for m in old_mods if m.get("dependency_of")]:
            try:
                install_mod(name, m["project_id"], log, _seen=seen)
            except Exception as e:
                missing.append(m["title"])
                log(f"  nicht verfügbar: {m['title']} ({e})")
    else:
        missing = [m["title"] for m in old_mods]
    sync_files(name)
    ensure_prism_instance(name, log)
    return missing


# ------------------------------------------------------------ Prism ------
def prism_instance_dir(name):
    return PRISM / "instances" / f"Server {name}"


def ensure_prism_instance(name, log=print):
    """Legt eine Prism-Instanz mit gleicher Version/Loader an (oder passt sie an)."""
    cfg = load_cfg(name)
    d = prism_instance_dir(name)
    (d / "minecraft" / "mods").mkdir(parents=True, exist_ok=True)
    comps = [{"important": True, "uid": "net.minecraft", "version": cfg["mc"]}]
    if cfg["loader"] == "neoforge":
        comps.append({"uid": "net.neoforged", "version": cfg["loader_version"]})
    elif cfg["loader"] == "forge":
        comps.append({"uid": "net.minecraftforge", "version": cfg["loader_version"]})
    elif cfg["loader"] == "fabric":
        comps.append({"uid": "net.fabricmc.intermediary", "version": cfg["mc"], "dependencyOnly": True})
        comps.append({"uid": "net.fabricmc.fabric-loader", "version": cfg["loader_version"]})
    pack = d / "mmc-pack.json"
    pack.write_text(json.dumps({"components": comps, "formatVersion": 1}, indent=4))
    icfg = d / "instance.cfg"
    if not icfg.exists():
        icfg.write_text("[General]\nAutomaticJava=true\nConfigVersion=1.3\nInstanceType=OneSix\n"
                        f"name=Server {name}\niconKey=default\nOverrideMemory=true\n"
                        "MinMemAlloc=2048\nMaxMemAlloc=8192\n"
                        "JoinServerOnLaunch=true\n"
                        f"JoinServerOnLaunchAddress=localhost:{read_properties(name).get('server-port', '25565')}\n")
    log(f"Prism-Instanz „Server {name}“ bereit (Start direkt in Prism, verbindet automatisch).")


# --------------------------------------------------------- Modpack-Export --
def export_mrpack(name, log=print):
    cfg = load_cfg(name)
    mods = load_mods(name)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = EXPORTS / f"{name}-{cfg['mc']}.mrpack"
    files, embed = [], []
    for m in mods:
        if m["side"] == "server":
            continue
        if not m.get("url", "").startswith("https://cdn.modrinth.com/"):
            embed.append(m)  # nicht von Modrinth → Datei direkt ins Paket
            continue
        files.append({"path": f"mods/{m['filename']}", "hashes": {"sha1": m["sha1"], "sha512": m["sha512"]},
                      "env": {"client": "required", "server": "required" if m["side"] == "both" else "unsupported"},
                      "downloads": [m["url"]], "fileSize": m["size"]})
    deps = {"minecraft": cfg["mc"]}
    if cfg["loader"] == "neoforge":
        deps["neoforge"] = cfg["loader_version"]
    elif cfg["loader"] == "forge":
        deps["forge"] = cfg["loader_version"]
    elif cfg["loader"] == "fabric":
        deps["fabric-loader"] = cfg["loader_version"]
    index = {"formatVersion": 1, "game": "minecraft", "versionId": time.strftime("%Y.%m.%d-%H%M"),
             "name": f"Felix' Server – {name}", "summary": "Modpack zum Server von Felix",
             "files": files, "dependencies": deps}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
        z.writestr("overrides/servers-info.txt", "Server-Adresse bekommst du von Felix.\n")
        for m in embed:
            src = CACHE / "mods" / m["filename"]
            if src.exists():
                z.write(src, f"overrides/mods/{m['filename']}")
    log(f"Modpack exportiert: {out}")
    return out


def export_curseforge(name, log=print):
    """Zip im CurseForge-Format (manifest.json + Mod-Jars in overrides/mods).
    Importierbar in der CurseForge-App: Erstellen → Importieren."""
    cfg = load_cfg(name)
    mods = load_mods(name)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = EXPORTS / f"{name}-{cfg['mc']}-CurseForge.zip"
    loaders = []
    if cfg["loader"] in ("neoforge", "forge", "fabric"):
        loaders.append({"id": f"{cfg['loader']}-{cfg['loader_version']}", "primary": True})
    manifest = {"minecraft": {"version": cfg["mc"], "modLoaders": loaders},
                "manifestType": "minecraftModpack", "manifestVersion": 1,
                "name": f"Felix' Server - {name}", "version": time.strftime("%Y.%m.%d-%H%M"),
                "author": "Felix", "files": [], "overrides": "overrides"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for m in mods:
            if m["side"] == "server":
                continue
            src = CACHE / "mods" / m["filename"]
            if not src.exists():
                download(m["url"], src, log, sha512=m.get("sha512"))
            z.write(src, f"overrides/mods/{m['filename']}")
    log(f"CurseForge-Modpack exportiert: {out}")
    return out


# --------------------------------------------------------- Modpack-Import --
CF_WEB = "https://www.curseforge.com/api/v1/mods"


def _cf_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _sha(path):
    h1, h5 = hashlib.sha1(), hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h1.update(chunk)
            h5.update(chunk)
    return h1.hexdigest(), h5.hexdigest()


def _modrinth_by_hash(sha1s):
    """sha1 -> (version, project) für alle Dateien, die Modrinth kennt."""
    if not sha1s:
        return {}
    req = urllib.request.Request(f"{MODRINTH}/version_files", method="POST",
                                 data=json.dumps({"hashes": sha1s, "algorithm": "sha1"}).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        vers = json.loads(r.read())
    if not vers:
        return {}
    pids = sorted({v["project_id"] for v in vers.values()})
    projs = {}
    for i in range(0, len(pids), 100):
        q = urllib.parse.quote(json.dumps(pids[i:i + 100]))
        for p in http_json(f"{MODRINTH}/projects?ids={q}"):
            projs[p["id"]] = p
    return {h: (v, projs.get(v["project_id"])) for h, v in vers.items()}


def read_modpack(path):
    """Liest Format, Name, MC-Version und Loader aus .mrpack oder CurseForge-Zip."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "modrinth.index.json" in names:
            idx = json.loads(z.read("modrinth.index.json"))
            deps = idx["dependencies"]
            loader, lv = "vanilla", "-"
            for key, l in (("neoforge", "neoforge"), ("forge", "forge"), ("fabric-loader", "fabric")):
                if key in deps:
                    loader, lv = l, deps[key]
            if "quilt-loader" in deps:
                raise RuntimeError("Quilt-Modpacks werden nicht unterstützt")
            return {"format": "modrinth", "name": idx.get("name", Path(path).stem), "mc": deps["minecraft"],
                    "loader": loader, "loader_version": lv, "count": len(idx["files"])}
        if "manifest.json" in names:
            man = json.loads(z.read("manifest.json"))
            loader, lv = "vanilla", "-"
            for ml in man["minecraft"].get("modLoaders", []):
                if ml.get("primary", True):
                    l, _, v = ml["id"].partition("-")
                    if l not in ("neoforge", "forge", "fabric"):
                        raise RuntimeError(f"Loader „{l}“ wird nicht unterstützt")
                    loader, lv = l, v
            n_over = sum(1 for n in names if n.startswith(man.get("overrides", "overrides") + "/mods/")
                         and n.endswith(".jar"))
            return {"format": "curseforge", "name": man.get("name", Path(path).stem),
                    "mc": man["minecraft"]["version"], "loader": loader, "loader_version": lv,
                    "count": len(man.get("files", [])) + n_over}
    raise RuntimeError("Keine Modpack-Datei (weder .mrpack noch CurseForge-Zip)")


def _copy_tree_from_zip(z, prefix, targets, log):
    """Kopiert alles unter prefix/ (außer mods/*.jar) in die Ziel-Ordner. Gibt die Mod-Jars zurück."""
    jars = []
    for info in z.infolist():
        if info.is_dir() or not info.filename.startswith(prefix + "/"):
            continue
        rel = info.filename[len(prefix) + 1:]
        if not rel or ".." in Path(rel).parts:
            continue
        data = z.read(info)
        if rel.startswith("mods/") and rel.endswith(".jar") and rel.count("/") == 1:
            jars.append((Path(rel).name, data))
            continue
        for t in targets:
            dst = t / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
    return jars


def import_modpack(path, name, log=print):
    """Legt aus einem Modpack einen neuen Server + passende Prism-Instanz an."""
    info = read_modpack(path)
    log(f"Modpack „{info['name']}“: Minecraft {info['mc']}, {LOADER_NAMES[info['loader']]} "
        f"{info['loader_version']}, {info['count']} Mods")
    create_server(name, info["mc"], info["loader"], info["loader_version"], log=log)
    sdir, cdir = server_dir(name), prism_instance_dir(name) / "minecraft"
    (CACHE / "mods").mkdir(parents=True, exist_ok=True)
    pending = []  # (filename, side, url, title, cf_id)
    with zipfile.ZipFile(path) as z:
        if info["format"] == "modrinth":
            idx = json.loads(z.read("modrinth.index.json"))
            for f in idx["files"]:
                env = f.get("env", {})
                cl, sv = env.get("client") != "unsupported", env.get("server") != "unsupported"
                side = "both" if cl and sv else "client" if cl else "server"
                rel = f["path"]
                if ".." in Path(rel).parts:
                    continue
                if rel.startswith("mods/") and rel.endswith(".jar"):
                    dst = CACHE / "mods" / Path(rel).name
                    if not dst.exists():
                        log(f"Lade {Path(rel).name}")
                        download(f["downloads"][0], dst, sha512=f["hashes"].get("sha512"))
                    pending.append((dst.name, side, f["downloads"][0], None, None))
                else:  # Resourcepacks, Shader usw.
                    tmp = CACHE / "misc" / Path(rel).name
                    download(f["downloads"][0], tmp, sha512=f["hashes"].get("sha512"))
                    for ok, t in ((cl, cdir), (sv, sdir)):
                        if ok:
                            (t / rel).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(tmp, t / rel)
            jars = _copy_tree_from_zip(z, "overrides", [sdir, cdir], log)
            jars += [(n, d, "client") for n, d in _copy_tree_from_zip(z, "client-overrides", [cdir], log)]
            jars += [(n, d, "server") for n, d in _copy_tree_from_zip(z, "server-overrides", [sdir], log)]
        else:
            man = json.loads(z.read("manifest.json"))
            for f in man.get("files", []):
                pid, fid = f["projectID"], f["fileID"]
                try:
                    meta = _cf_get(f"{CF_WEB}/{pid}/files/{fid}")["data"]
                except Exception as e:  # noqa: BLE001
                    log(f"  CurseForge-Datei {pid}/{fid} nicht abrufbar: {e}")
                    continue
                gv = meta.get("gameVersions", [])
                side = "client" if "Client" in gv and "Server" not in gv else \
                       "server" if "Server" in gv and "Client" not in gv else "both"
                dst = CACHE / "mods" / meta["fileName"]
                url = f"{CF_WEB}/{pid}/files/{fid}/download"
                if not dst.exists():
                    log(f"Lade {meta['fileName']}")
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        dst.write_bytes(r.read())
                pending.append((dst.name, side, url, meta.get("displayName"), f"cf:{pid}"))
            jars = _copy_tree_from_zip(z, man.get("overrides", "overrides"), [sdir, cdir], log)
    for item in jars:
        n, data = item[0], item[1]
        (CACHE / "mods" / n).write_bytes(data)
        pending.append((n, item[2] if len(item) > 2 else None, "", None, None))

    # Modrinth kennt die meisten Dateien → richtige Namen, Seiten-Info und Updates
    hashes = {fn: _sha(CACHE / "mods" / fn) for fn, *_ in pending}
    log("Gleiche Mods mit Modrinth ab …")
    try:
        known = _modrinth_by_hash([h[0] for h in hashes.values()])
    except Exception:  # noqa: BLE001
        known = {}
    mods, unsure = [], []
    for fn, side, url, title, cfid in pending:
        sha1, sha512 = hashes[fn]
        v, p = known.get(sha1, (None, None))
        if p:
            fileobj = next((f for f in v["files"] if f["hashes"].get("sha1") == sha1), _primary(v))
            entry = {"project_id": p["id"], "slug": p["slug"], "title": p["title"], "version_id": v["id"],
                     "version": v["version_number"], "url": fileobj["url"], "side": side or side_of(p),
                     "icon": p.get("icon_url")}
            if side in (None, "both") or info["format"] == "curseforge":
                entry["side"] = side_of(p)  # Modrinth weiß, ob die Mod nur fürs Spiel ist
        else:
            entry = {"project_id": cfid or f"datei:{fn}", "slug": "", "title": title or fn,
                     "version_id": "", "version": "", "url": url, "side": side or "both", "icon": None}
            if not side or side == "both":
                unsure.append(entry["title"])
        entry |= {"filename": fn, "size": (CACHE / "mods" / fn).stat().st_size, "sha1": sha1,
                  "sha512": sha512, "dependency_of": None, "from_pack": info["name"]}
        if not any(m["filename"] == fn for m in mods):
            mods.append(entry)
    save_mods(name, mods)
    sync_files(name)
    log(f"Import fertig: {len(mods)} Mods.")
    return {"mods": len(mods), "unsure": unsure, "info": info}


# -------------------------------------------------------- BlueMap-Zusatz --
ADDON_JS = Path(__file__).parent / "bluemap" / "spieler3d.js"


def bluemap_webroot(name):
    d = server_dir(name)
    conf = d / "config/bluemap/webapp.conf"
    root = "bluemap/web"
    if conf.exists():
        m = re.search(r'^\s*webroot:\s*"([^"]+)"', conf.read_text(), re.M)
        if m:
            root = m.group(1)
    return d / root


def bluemap_addon(name):
    """3D-Spielerfiguren + flüssige Bewegung in BlueMap einhängen. True, wenn etwas geändert wurde."""
    d = server_dir(name)
    web = bluemap_webroot(name)
    if not web.exists():
        return False
    (web / "js").mkdir(exist_ok=True)
    (web / "skins").mkdir(exist_ok=True)
    changed = False
    dst = web / "js" / "spieler3d.js"
    if not dst.exists() or dst.read_bytes() != ADDON_JS.read_bytes():
        shutil.copy2(ADDON_JS, dst)
        changed = True
    conf0 = d / "config/bluemap/webapp.conf"
    if conf0.exists():  # größere Sichtweite für Detail-/Übersichtskarte
        t = conf0.read_text()
        t2 = re.sub(r"^hires-slider-default: 100\b", "hires-slider-default: 250", t, flags=re.M)
        t2 = re.sub(r"^lowres-slider-default: 2000\b", "lowres-slider-default: 7000", t2, flags=re.M)
        if t2 != t:
            conf0.write_text(t2)
            changed = True
    # Versionsnummer im Namen → Browser lädt nach Änderungen garantiert die neue Datei
    entry = f"js/spieler3d.js?v={int(ADDON_JS.stat().st_mtime)}"
    conf = d / "config/bluemap/webapp.conf"
    if conf.exists():
        t = conf.read_text()
        if entry not in t:
            t2 = re.sub(r'\n\s*"js/spieler3d\.js[^"]*"', "", t)
            t2 = re.sub(r"^(\s*scripts:\s*\[)", lambda m: m.group(1) + f'\n    "{entry}"', t2, count=1, flags=re.M)
            if t2 != t:
                conf.write_text(t2)
                changed = True
    sj = web / "settings.json"
    if sj.exists():
        s = json.loads(sj.read_text())
        scripts = [x for x in s.get("scripts", []) if not x.startswith("js/spieler3d.js")] + [entry]
        if scripts != s.get("scripts"):
            s["scripts"] = scripts
            sj.write_text(json.dumps(s))
            changed = True
    return changed


def fetch_skin(name, uuid):
    """Skin eines Spielers für die 3D-Karte nach <webroot>/skins/<uuid>.png holen."""
    import base64
    web = bluemap_webroot(name)
    if not web.exists():
        return None
    out = web / "skins" / f"{uuid}.png"
    if out.exists() and time.time() - out.stat().st_mtime < 86400:
        return out
    prof = http_json(f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid.replace('-', '')}")
    tex = json.loads(base64.b64decode(prof["properties"][0]["value"]))
    url = tex["textures"].get("SKIN", {}).get("url")
    if not url:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(http_get(url.replace("http://", "https://")))
    return out


def known_players(name):
    p = server_dir(name) / "usercache.json"
    try:
        return [(e["name"], e["uuid"]) for e in json.loads(p.read_text())]
    except Exception:
        return []


# --------------------------------------------------------------- Backup ---
def backup(name, log=print):
    d = server_dir(name)
    level = read_properties(name).get("level-name", "world")
    world = d / level
    if not world.exists():
        log("Noch keine Welt vorhanden – nichts zu sichern.")
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    out = BACKUPS / f"{name}-{level}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in world.rglob("*"):
            if p.is_file() and p.name != "session.lock":
                z.write(p, p.relative_to(d))
    log(f"Backup gespeichert: {out} ({out.stat().st_size // 1_048_576} MB)")
    return out


# -------------------------------------------------------------- Netzwerk ---
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?"


def public_ip():
    try:
        return http_get("https://api.ipify.org", 5).decode().strip()
    except Exception:
        return "?"


def tailscale_ip():
    try:
        return subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3).stdout.split()[0]
    except Exception:
        return None


def mojang_uuid(player):
    data = http_json(f"https://api.mojang.com/users/profiles/minecraft/{urllib.parse.quote(player)}")
    u = data["id"]
    return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}", data["name"]


# ------------------------------------------------ CurseForge (offizielle API) --
CF_API = "https://api.curseforge.com/v1"
CF_KEY_FILE = BASE / "curseforge-api-key.txt"
CF_LOADER_TYPE = {"forge": 1, "fabric": 4, "neoforge": 6}
CF_SORT = {"relevance": 2, "downloads": 6, "newest": 11, "updated": 3}  # Popularität, Downloads, Release, Update


def cf_key():
    try:
        return CF_KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def save_cf_key(key):
    CF_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    CF_KEY_FILE.write_text(key.strip() + "\n")
    CF_KEY_FILE.chmod(0o600)


def cf_api(path, params=None):
    key = cf_key()
    if not key:
        raise RuntimeError("Kein CurseForge-API-Schlüssel eingetragen.")
    url = f"{CF_API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"x-api-key": key, "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            if path.startswith("/mods/search"):
                raise RuntimeError("CurseForge hat die Suche für diesen API-Schlüssel noch nicht freigegeben "
                                   "(403). Der Schlüssel selbst funktioniert – Freischaltung bei CurseForge "
                                   "anfragen („Contact us“ auf console.curseforge.com).") from e
            raise RuntimeError("CurseForge lehnt den API-Schlüssel ab (403) – Schlüssel prüfen.") from e
        raise


def cf_side(game_versions):
    c, s = "Client" in game_versions, "Server" in game_versions
    if c and not s:
        return "client"
    if s and not c:
        return "server"
    return "both"


def cf_search(query, mc, loader, offset=0, limit=30, sort="relevance"):
    """Suche nur Mods, die es für diese MC-Version + diesen Loader gibt. Format wie früher bei Modrinth."""
    params = {"gameId": 432, "classId": 6, "gameVersion": mc, "modLoaderType": CF_LOADER_TYPE.get(loader, 0),
              "sortField": CF_SORT.get(sort, 2), "sortOrder": "desc", "index": offset, "pageSize": limit}
    if query:
        params["searchFilter"] = query
    data = cf_api("/mods/search", params)
    hits = []
    for m in data["data"]:
        lf = [f for f in m.get("latestFilesIndexes", []) if f.get("gameVersion") == mc]
        hits.append({"project_id": f"cf:{m['id']}", "cf_id": m["id"], "title": m["name"], "slug": m.get("slug", ""),
                     "description": m.get("summary", ""), "downloads": int(m.get("downloadCount", 0)),
                     "author": ", ".join(a["name"] for a in m.get("authors", [])[:2]),
                     "icon_url": (m.get("logo") or {}).get("thumbnailUrl"),
                     "url": (m.get("links") or {}).get("websiteUrl"), "has_file": bool(lf)})
    total = min(data["pagination"]["totalCount"], 10000 - limit)
    return {"hits": hits, "total_hits": total}


def cf_best_file(mod_id, mc, loader):
    data = cf_api(f"/mods/{mod_id}/files", {"gameVersion": mc, "modLoaderType": CF_LOADER_TYPE.get(loader, 0),
                                              "pageSize": 50})["data"]
    data = [f for f in data if f.get("fileName", "").endswith(".jar") and f.get("isAvailable", True)]
    if not data:
        return None
    data.sort(key=lambda f: f["fileDate"], reverse=True)
    rel = [f for f in data if f.get("releaseType") == 1]
    return (rel or data)[0]


def cf_install_mod(name, mod_id, log=print, _seen=None, dependency_of=None):
    """Installiert eine CurseForge-Mod samt Pflicht-Abhängigkeiten. Gibt Liste neu installierter Titel zurück."""
    cfg = load_cfg(name)
    mc, loader = cfg["mc"], cfg["loader"]
    mod_id = int(mod_id)
    _seen = _seen if _seen is not None else set()
    if mod_id in _seen:
        return []
    _seen.add(mod_id)
    mod = cf_api(f"/mods/{mod_id}")["data"]
    if any(m["project_id"] == f"cf:{mod_id}" for m in load_mods(name)):
        if dependency_of is None:
            log(f"„{mod['name']}“ ist schon installiert.")
        return []
    if mod.get("allowModDistribution") is False:
        raise RuntimeError(f"Der Autor von „{mod['name']}“ erlaubt keine Downloads durch andere Programme – "
                           f"bitte von Hand auf CurseForge laden: {(mod.get('links') or {}).get('websiteUrl', '')}")
    f = cf_best_file(mod_id, mc, loader)
    if f is None:
        raise RuntimeError(f"„{mod['name']}“ hat keine Datei für {LOADER_NAMES[loader]} {mc}")
    if any(m["filename"] == f["fileName"] for m in load_mods(name)):
        if dependency_of is None:
            log(f"„{mod['name']}“ ist schon installiert (aus anderer Quelle).")
        return []
    new = []
    for dep in f.get("dependencies", []):
        if dep.get("relationType") == 3:  # Pflicht-Abhängigkeit
            new += cf_install_mod(name, dep["modId"], log, _seen, dependency_of=mod["name"])
    side = cf_side(f.get("gameVersions", []))
    log(f"Installiere {mod['name']} – {f['fileName']} ({SIDE_NAMES[side]})"
        + (f" – benötigt von {dependency_of}" if dependency_of else ""))
    url = f"{CF_WEB}/{mod_id}/files/{f['id']}/download"
    cache_file = CACHE / "mods" / f["fileName"]
    if not cache_file.exists():
        tmp = cache_file.with_suffix(".part")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(f.get("downloadUrl") or url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        want = next((h["value"] for h in f.get("hashes", []) if h.get("algo") == 1), None)
        if want and _sha(tmp)[0] != want:
            tmp.unlink()
            raise RuntimeError(f"Prüfsumme falsch: {f['fileName']}")
        tmp.replace(cache_file)
    sha1, sha512 = _sha(cache_file)
    entry = {"project_id": f"cf:{mod_id}", "slug": mod.get("slug", ""), "title": mod["name"],
             "version_id": "", "version": f.get("displayName") or f["fileName"], "filename": f["fileName"],
             "url": url, "size": cache_file.stat().st_size, "sha1": sha1, "sha512": sha512, "side": side,
             "dependency_of": dependency_of, "icon": (mod.get("logo") or {}).get("thumbnailUrl"),
             "source": "curseforge"}
    mods = load_mods(name)
    mods.append(entry)
    save_mods(name, mods)
    sync_files(name)
    return new + [mod["name"]]
