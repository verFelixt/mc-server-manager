"""Mod-Seite: kleine Webseite, über die Mitspieler sehen, welche Mods der Server braucht.

- http://<IP>:8101/ zeigt den aktiven Server: Modpack-Version, was neu ist, alle Mods mit Links,
  Download als CurseForge-Zip (.zip), Modrinth-Pack (.mrpack) und Prism-Instanz mit Auto-Update.
- Prism-Auto-Update: packwiz-installer gleicht vor jedem Spielstart die Mods mit
  http://<IP>:8101/<slug>/pack/pack.toml ab (fehlende laden, entfernte löschen).
- Jede Änderung der Mod-Liste (für Spieler relevant) erhöht die Modpack-Version (server.json „pack“).
"""
import hashlib
import html
import io
import json
import re
import shutil
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mcserver_core as core

PORT = 8101
OUT = core.BASE / "modseite"
BOOTSTRAP = core.CACHE / "tools" / "packwiz-installer-bootstrap.jar"
BOOTSTRAP_URL = ("https://github.com/packwiz/packwiz-installer-bootstrap/releases/download/"
                 "v0.0.3/packwiz-installer-bootstrap.jar")
_lock = threading.Lock()
active = {"name": None}   # von der GUI gesetzt: welcher Server unter „/“ erscheint


def slug(name):
    s = name.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "server"


def by_slug(s):
    return next((n for n in core.list_servers() if slug(n) == s), None)


def client_mods(name):
    seen = {}
    for m in core.load_mods(name):
        if m["side"] in ("both", "client"):
            seen.setdefault(m["filename"], m)  # gleiche Datei nur einmal
    return sorted(seen.values(), key=lambda m: m["title"].lower())


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


_meta_cache = {}


def parse_toml_meta(toml, manifest=""):
    """Haupt-Mod-ID und Version aus (neoforge.)mods.toml – per Regex, damit auch kaputte Dateien gehen."""
    blk = toml.split("[[mods]]", 1)[1] if "[[mods]]" in toml else toml
    mid = re.search(r"""modId\s*=\s*["']([^"']+)""", blk)
    ver = re.search(r"""(?m)^\s*version\s*=\s*["']([^"']+)""", blk) or \
        re.search(r"""\bversion\s*=\s*["']([^"']+)""", blk)
    v = ver.group(1) if ver else ""
    if "${" in v:
        iv = re.search(r"(?m)^Implementation-Version:\s*(\S+)", manifest)
        v = iv.group(1) if iv else ""
    return (mid.group(1) if mid else ""), v


def jar_meta(path):
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _meta_cache:
        mid, ver = "", ""
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                t = next((n for n in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if n in names), None)
                if t:
                    man = z.read("META-INF/MANIFEST.MF").decode("utf8", "replace") \
                        if "META-INF/MANIFEST.MF" in names else ""
                    mid, ver = parse_toml_meta(z.read(t).decode("utf8", "replace"), man)
        except (OSError, zipfile.BadZipFile):
            pass
        _meta_cache[key] = (mid, ver)
    return _meta_cache[key]


# ------------------------------------------------- neuere Versionen prüfen --
VCHECK_MAX_AGE = 6 * 3600
_vcheck_running = set()


def _mc_list(vs):
    mcs = [v for v in vs if re.fullmatch(r"\d+\.\d+(\.\d+)?", v)]
    mcs.sort(key=lambda v: [int(x) for x in v.split(".")], reverse=True)
    return mcs


def _newer_mc(vs, mc):
    """True, wenn eine der MC-Versionen neuer ist als die des Servers."""
    key = lambda v: [int(x) for x in v.split(".")]  # noqa: E731
    return any(key(v) > key(mc) for v in _mc_list(vs))


def _fuer(mcs, loaders):
    m = ", ".join(mcs[:3]) + (" …" if len(mcs) > 3 else "")
    return f"Minecraft {m}" + (f" ({', '.join(loaders)})" if loaders else "")


def version_check(name):
    """Für jede Mod: kompatibles Update vorhanden? Neuere, aber inkompatible Version vorhanden?"""
    cfg = core.load_cfg(name)
    mc, loader = cfg["mc"], cfg["loader"]
    res = {}
    for m in client_mods(name) + [x for x in core.load_mods(name) if x["side"] == "server"]:
        info = {}
        try:
            if m["project_id"].startswith("cf:"):
                pid = m["project_id"][3:]
                fm = re.search(r"/files/(\d+)", m.get("url", ""))
                cur = core._cf_get(f"{core.CF_WEB}/{pid}/files/{fm.group(1)}")["data"] if fm else None
                data = core._cf_get(f"{core.CF_WEB}/{pid}/files?pageIndex=0&pageSize=50&sort=dateCreated"
                                    "&sortDescending=true")["data"]
                rel = [f for f in data if f.get("releaseType", 1) == 1 and f.get("fileName", "").endswith(".jar")]
                if cur:
                    rel = [f for f in rel if f["dateCreated"] > cur["dateCreated"]]
                lname = core.CF_LOADER.get(loader)
                comp = [f for f in rel if mc in f["gameVersions"] and lname in f["gameVersions"]]
                inc = [f for f in rel if lname in f["gameVersions"] and mc not in f["gameVersions"]
                       and _newer_mc(f["gameVersions"], mc)]
                if comp:
                    info["update"] = comp[0]["fileName"]
                if inc:
                    f = inc[0]
                    lds = [g for g in f["gameVersions"] if g in ("NeoForge", "Forge", "Fabric", "Quilt")]
                    info["neuer"] = {"v": f["fileName"], "fuer": _fuer(_mc_list(f["gameVersions"]), lds)}
            elif m.get("version_id"):
                vs = core.http_json(f"{core.MODRINTH}/project/{m['project_id']}/version")
                cur = next((v for v in vs if v["id"] == m["version_id"]), None)
                rel = [v for v in vs if v["version_type"] == "release"]
                if cur:
                    rel = [v for v in rel if v["date_published"] > cur["date_published"]]
                rel.sort(key=lambda v: v["date_published"], reverse=True)
                comp = [v for v in rel if mc in v["game_versions"] and loader in v["loaders"]]
                inc = [v for v in rel if loader in v["loaders"] and mc not in v["game_versions"]
                       and _newer_mc(v["game_versions"], mc)]
                if comp:
                    info["update"] = comp[0]["version_number"]
                if inc:
                    v = inc[0]
                    info["neuer"] = {"v": v["version_number"],
                                     "fuer": _fuer(_mc_list(v["game_versions"]),
                                                   [core.LOADER_NAMES.get(x, x).split(" ")[0] for x in v["loaders"]])}
        except Exception as e:  # noqa: BLE001
            info["fehler"] = str(e)
        res[m["filename"]] = info
    d = OUT / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    (OUT / f"{slug(name)}-versionen.json").write_text(json.dumps({"zeit": time.time(), "mods": res}))
    return res


def version_info(name):
    """Zwischengespeichertes Ergebnis; stößt im Hintergrund eine neue Prüfung an, wenn älter als 6 h."""
    f = OUT / f"{slug(name)}-versionen.json"
    data = None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        pass
    if (not data or time.time() - data["zeit"] > VCHECK_MAX_AGE) and name not in _vcheck_running:
        _vcheck_running.add(name)

        def run():
            try:
                version_check(name)
            finally:
                _vcheck_running.discard(name)
        threading.Thread(target=run, daemon=True).start()
    return data


def pack_state(name):
    """Aktueller Modpack-Stand; erhöht die Version, wenn sich die Mod-Liste geändert hat."""
    cfg = core.load_cfg(name)
    mods = client_mods(name)
    files = sorted(m["filename"] for m in mods)
    fp = hashlib.sha1(json.dumps([cfg["mc"], cfg["loader"], cfg.get("loader_version"), files])
                      .encode()).hexdigest()[:12]
    st = cfg.get("pack") or {}
    if st.get("hash") != fp:
        old = st.get("files", [])
        st = {"hash": fp, "version": st.get("version", 0) + 1, "changed": time.strftime("%d.%m.%Y %H:%M"),
              "files": files,
              "added": sorted(set(files) - set(old)) if old else [],
              "removed": sorted(set(old) - set(files)) if old else []}
        cfg["pack"] = st
        core.save_cfg(name, cfg)
    return st


def build(name, log=print):
    """Erzeugt alle Download-Dateien für den aktuellen Stand (nur wenn nötig)."""
    with _lock:
        st = pack_state(name)
        d = OUT / slug(name)
        stamp = d / ".stand"
        if stamp.exists() and stamp.read_text() == st["hash"]:
            return d
        log(f"Mod-Seite: baue Modpack v{st['version']} für „{name}“ …")
        tmp = d.with_name(d.name + ".neu")
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "pack" / "mods").mkdir(parents=True)
        cfg = core.load_cfg(name)
        mods = client_mods(name)
        # Mod-Jars (Hardlink aus dem Cache, sonst Kopie)
        index = ['hash-format = "sha256"', ""]
        for m in mods:
            src = core.CACHE / "mods" / m["filename"]
            if not src.exists():
                core.download(m["url"], src, log, sha512=m.get("sha512"))
            dst = tmp / "pack" / "mods" / m["filename"]
            try:
                dst.hardlink_to(src)
            except OSError:
                shutil.copy2(src, dst)
            index += ["[[files]]", f'file = "mods/{m["filename"]}"', f'hash = "{_sha256(src)}"', ""]
        idx = "\n".join(index)
        (tmp / "pack" / "index.toml").write_text(idx)
        loader_line = ""
        if cfg["loader"] in ("neoforge", "forge"):
            loader_line = f'{cfg["loader"]} = "{cfg["loader_version"]}"\n'
        elif cfg["loader"] == "fabric":
            loader_line = f'fabric = "{cfg["loader_version"]}"\n'
        (tmp / "pack" / "pack.toml").write_text(
            f'name = "Felix\' Server - {name}"\nauthor = "Felix"\nversion = "{st["version"]}"\n'
            'pack-format = "packwiz:1.1.0"\n\n[index]\nfile = "index.toml"\nhash-format = "sha256"\n'
            f'hash = "{hashlib.sha256(idx.encode()).hexdigest()}"\n\n[versions]\n'
            f'minecraft = "{cfg["mc"]}"\n{loader_line}')
        # CurseForge-Zip und .mrpack über die vorhandenen Export-Funktionen
        quiet = lambda *_: None  # noqa: E731
        shutil.copy2(core.export_curseforge(name, quiet), tmp / f"{slug(name)}-v{st['version']}-CurseForge.zip")
        shutil.copy2(core.export_mrpack(name, quiet), tmp / f"{slug(name)}-v{st['version']}.mrpack")
        (tmp / "stand.json").write_text(json.dumps({"server": name, "version": st["version"],
                                                    "changed": st["changed"], "hash": st["hash"]}))
        (tmp / ".stand").write_text(st["hash"])
        shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
        log(f"Mod-Seite: Modpack v{st['version']} bereit ({len(mods)} Mods).")
        return d


def prism_zip(name, host):
    """Prism-Instanz (zum Importieren) mit Auto-Update vor jedem Start."""
    cfg = core.load_cfg(name)
    if not BOOTSTRAP.exists():
        core.download(BOOTSTRAP_URL, BOOTSTRAP)
    comps = [{"important": True, "uid": "net.minecraft", "version": cfg["mc"]}]
    if cfg["loader"] == "neoforge":
        comps.append({"uid": "net.neoforged", "version": cfg["loader_version"]})
    elif cfg["loader"] == "forge":
        comps.append({"uid": "net.minecraftforge", "version": cfg["loader_version"]})
    elif cfg["loader"] == "fabric":
        comps.append({"uid": "net.fabricmc.intermediary", "version": cfg["mc"], "dependencyOnly": True})
        comps.append({"uid": "net.fabricmc.fabric-loader", "version": cfg["loader_version"]})
    port = core.read_properties(name).get("server-port", "25565")
    ip = host.split(":")[0]
    pack_url = f"http://{host}/{slug(name)}/pack/pack.toml"
    icfg = ("[General]\nConfigVersion=1.3\nInstanceType=OneSix\nAutomaticJava=true\n"
            f"name=Felix' Server - {name}\niconKey=default\n"
            "OverrideMemory=true\nMinMemAlloc=2048\nMaxMemAlloc=8192\n"
            "OverrideCommands=true\n"
            f'PreLaunchCommand="$INST_JAVA" -jar packwiz-installer-bootstrap.jar {pack_url}\n'
            "JoinServerOnLaunch=true\n"
            f"JoinServerOnLaunchAddress={ip}:{port}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("instance.cfg", icfg)
        z.writestr("mmc-pack.json", json.dumps({"components": comps, "formatVersion": 1}, indent=4))
        z.write(BOOTSTRAP, "minecraft/packwiz-installer-bootstrap.jar")
    return buf.getvalue()


def motd(name, host):
    """Zweite MOTD-Zeile mit Modpack-Version und Adresse der Mod-Seite."""
    st = pack_state(name)
    first = core.read_properties(name).get("motd", "Felix' Minecraft-Server")
    first = first.split("\\n")[0]
    return f"{first}\\n\\u00a7eModpack v{st['version']} \\u00a77\\u00b7 Mods: \\u00a7b{host}"


# ------------------------------------------------------------------ HTML ---
CSS = """
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#e3e6eb;--acc:#2f7d32;--acc2:#1565c0;
--new:#e8f5e9;--old:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e7eaf0;--mut:#9aa3b2;
--line:#2c323c;--acc:#66bb6a;--acc2:#64b5f6;--new:#1e3323;--old:#3a1f1f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:0 0 12px}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px;margin-top:16px}
.badge{display:inline-block;background:var(--acc);color:#fff;border-radius:999px;padding:2px 12px;
font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.dl{display:block;border:1px solid var(--line);border-radius:8px;padding:14px;text-decoration:none;color:var(--fg)}
.dl:hover{border-color:var(--acc2)}.dl b{color:var(--acc2)}
ol{margin:6px 0 0;padding-left:20px}li{margin:3px 0}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{font-size:13px;color:var(--mut);font-weight:600}
td a{color:var(--acc2);text-decoration:none;margin-right:10px;white-space:nowrap}
.tag{font-size:12px;padding:1px 7px;border-radius:5px;background:var(--line)}
.n{background:var(--new)}.r{background:var(--old)}code{background:var(--line);padding:1px 5px;border-radius:4px}
@media (max-width:600px){.hide-s{display:none}}
.hinweis{font-size:13px;color:var(--mut)}
"""


ABGLEICH = """
<div class="card" id="abgleich"><h2>🔍 Meine Mods abgleichen</h2>
<p>Wähle deinen <b>mods</b>-Ordner aus. Die Seite vergleicht dann, was dir fehlt, was veraltet ist und was zu viel ist.
<span class="mut">Es wird nichts hochgeladen: Der Browser liest nur Namen und Versionen der Mods auf deinem PC.
Falls der Browser fragt „Dateien hochladen?“: Das ist nur sein Standardtext, einfach bestätigen.</span></p>
<details><summary class="mut">Wo ist mein mods-Ordner?</summary><ul>
<li><b>CurseForge (Windows):</b> CurseForge-App → Profil → ⋯ → <i>Ordner öffnen</i>, darin <code>mods</code>.
Meist <code>C:\\Users\\NAME\\curseforge\\minecraft\\Instances\\PROFIL\\mods</code></li>
<li><b>Prism Launcher:</b> Instanz rechtsklicken → <i>Ordner</i> → <code>minecraft\\mods</code></li>
<li><b>Modrinth App:</b> Profil → ⋯ → <i>Ordner öffnen</i> → <code>mods</code></li></ul></details>
<p><button id="nochmal" class="dl" style="display:none;cursor:pointer;font:inherit;background:none;text-align:left;margin-bottom:8px"></button>
<label id="waehlen" class="dl" style="display:inline-block;cursor:pointer"><b>📁 mods-Ordner auswählen und abgleichen</b>
<input id="ordner" type="file" webkitdirectory directory multiple style="display:none"></label></p>
<p id="merkhinweis" class="mut" style="font-size:13px"></p>
<div id="ergebnis"></div></div>
<script>
const PACK = __DATA__, SLUG = "__SLUG__";
const esc = t => t.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// --- Jar (Zip) im Browser lesen: nur die Mod-Beschreibung, nicht die ganze Datei ---
async function eintrag(file, wanted) {
  const tail = Math.min(file.size, 70000);
  const buf = new DataView(await file.slice(file.size - tail).arrayBuffer());
  let e = -1;
  for (let i = tail - 22; i >= 0; i--) if (buf.getUint32(i, true) === 0x06054b50) { e = i; break; }
  if (e < 0) return {};
  const cdSize = buf.getUint32(e + 12, true), cdOff = buf.getUint32(e + 16, true);
  const cd = new DataView(await file.slice(cdOff, cdOff + cdSize).arrayBuffer());
  const dec = new TextDecoder(), res = {};
  for (let p = 0; p + 46 <= cd.byteLength && cd.getUint32(p, true) === 0x02014b50;) {
    const meth = cd.getUint16(p + 10, true), csize = cd.getUint32(p + 20, true);
    const nl = cd.getUint16(p + 28, true), xl = cd.getUint16(p + 30, true), cl = cd.getUint16(p + 32, true);
    const loc = cd.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(cd.buffer, p + 46, nl));
    if (wanted.includes(name)) {
      const lh = new DataView(await file.slice(loc, loc + 30).arrayBuffer());
      const start = loc + 30 + lh.getUint16(26, true) + lh.getUint16(28, true);
      let blob = file.slice(start, start + csize);
      if (meth === 8) blob = await new Response(blob.stream().pipeThrough(new DecompressionStream("deflate-raw"))).blob();
      res[name] = await blob.text();
    }
    p += 46 + nl + xl + cl;
  }
  return res;
}
function tomlMeta(toml, man) {
  const blk = toml.includes("[[mods]]") ? toml.split("[[mods]]")[1] : toml;
  const mid = blk.match(/modId\\s*=\\s*["']([^"']+)/);
  const ver = blk.match(/^\\s*version\\s*=\\s*["']([^"']+)/m) || blk.match(/\\bversion\\s*=\\s*["']([^"']+)/);
  let v = ver ? ver[1] : "";
  if (v.includes("${")) { const iv = (man || "").match(/^Implementation-Version:\\s*(\\S+)/m); v = iv ? iv[1] : ""; }
  return {id: mid ? mid[1] : "", v};
}
async function jarMeta(file) {
  try {
    const r = await eintrag(file, ["META-INF/neoforge.mods.toml", "META-INF/mods.toml", "META-INF/MANIFEST.MF"]);
    const t = r["META-INF/neoforge.mods.toml"] || r["META-INF/mods.toml"];
    return t ? tomlMeta(t, r["META-INF/MANIFEST.MF"]) : {id: "", v: ""};
  } catch (err) { return {id: "", v: ""}; }
}
// --- Ordner merken (nur über https möglich: File System Access API + IndexedDB) ---
function idb(mode, fn) {
  return new Promise((ok, err) => {
    const r = indexedDB.open("modabgleich", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("h");
    r.onsuccess = () => { const tx = r.result.transaction("h", mode); const q = fn(tx.objectStore("h"));
      tx.oncomplete = () => ok(q && q.result); tx.onerror = () => err(tx.error); };
    r.onerror = () => err(r.error);
  });
}
const merken = h => idb("readwrite", st => st.put(h, SLUG));
const gemerkt = () => idb("readonly", st => st.get(SLUG)).catch(() => null);
async function ausHandle(h) {
  const files = [];
  for await (const e of h.values()) if (e.kind === "file" && /\\.jar$/i.test(e.name)) files.push(await e.getFile());
  return files;
}
const btnNochmal = document.getElementById("nochmal"), lblWaehlen = document.getElementById("waehlen");
function zeigeNochmal(h) {
  btnNochmal.style.display = "block";
  btnNochmal.innerHTML = "<b>🔄 Erneut abgleichen</b> <span class='mut'>– Ordner „" + esc(h.name) + "“</span>";
  btnNochmal.onclick = async () => {
    if ((await h.requestPermission({mode: "read"})) !== "granted") return;
    auswerten(await ausHandle(h));
  };
  lblWaehlen.querySelector("b").textContent = "📁 Anderen Ordner wählen";
}
if (window.showDirectoryPicker) {
  document.getElementById("merkhinweis").textContent = "Der Ordner wird in diesem Browser gemerkt – beim nächsten Mal reicht ein Klick.";
  lblWaehlen.addEventListener("click", async ev => {
    ev.preventDefault();
    try { const h = await showDirectoryPicker({id: "mods", mode: "read"}); await merken(h); zeigeNochmal(h); auswerten(await ausHandle(h)); }
    catch (e) { if (e.name !== "AbortError") document.getElementById("ergebnis").textContent = "Fehler: " + e.message; }
  });
  gemerkt().then(h => { if (h) zeigeNochmal(h); });
} else {
  document.getElementById("merkhinweis").textContent = "Hinweis: Über diese Adresse (ohne https) darf der Browser sich den Ordner nicht merken – der Auswahldialog startet aber meist im zuletzt gewählten Ordner.";
}
document.getElementById("ordner").addEventListener("change", ev =>
  auswerten([...ev.target.files].filter(f => f.webkitRelativePath.split("/").length === 2)));
async function auswerten(files) {
  const jars = files.filter(f => /\\.jar$/i.test(f.name));
  const out = document.getElementById("ergebnis");
  if (!jars.length) { out.innerHTML = "<p class='r' style='padding:8px'>In diesem Ordner liegen keine Mods (.jar). Ist das wirklich der <b>mods</b>-Ordner?</p>"; return; }
  out.innerHTML = "<p class='mut'>Lese " + jars.length + " Mods …</p>";
  const meine = await Promise.all(jars.map(async f => Object.assign({f: f.name, s: f.size}, await jarMeta(f))));
  const byName = new Map(meine.map(x => [x.f, x])), byId = new Map();
  for (const x of meine) if (x.id) (byId.get(x.id) || byId.set(x.id, []).get(x.id)).push(x);
  const fehlt = [], anders = [], ok = [], benutzt = new Set();
  for (const m of PACK) {
    const n = byName.get(m.f);
    if (n && n.s === m.s) { ok.push(m); benutzt.add(n.f); continue; }
    const kand = (m.id && byId.get(m.id)) || (n ? [n] : []);
    const gleich = kand.find(x => x.v && x.v === m.v);
    if (gleich) { ok.push(m); benutzt.add(gleich.f); continue; }
    if (kand.length) { anders.push({m, alt: kand}); kand.forEach(x => benutzt.add(x.f)); continue; }
    fehlt.push(m);
  }
  const fremd = meine.filter(x => !benutzt.has(x.f));
  const laden = fehlt.concat(anders.map(a => a.m));
  let h = "";
  if (!laden.length)
    h += "<p class='n' style='padding:10px;border-radius:8px'>✅ <b>Alles passt!</b> Alle " + PACK.length + " Mods sind in der richtigen Version da.</p>";
  else
    h += "<p class='r' style='padding:10px;border-radius:8px'>⚠️ <b>" + laden.length + " Mods fehlen oder haben eine andere Version</b> – " + ok.length + " von " + PACK.length + " passen schon.</p>";
  if (laden.length) {
    const q = laden.map(m => "f=" + encodeURIComponent(m.f)).join("&");
    h += "<p><a class='dl' style='display:inline-block' href='/" + SLUG + "/fehlende.zip?" + q + "'><b>⬇ Alle " + laden.length + " benötigten als Zip</b></a> " +
      "<span class='mut'>Zip entpacken, .jar-Dateien in den mods-Ordner legen.</span></p>";
  }
  if (fehlt.length)
    h += "<h2 style='margin-top:14px'>⬇ Fehlt (" + fehlt.length + ")</h2><ul>" +
      fehlt.map(m => "<li><b>" + esc(m.t) + "</b> " + esc(m.v) + " <a href='/" + SLUG + "/pack/mods/" + encodeURIComponent(m.f) + "'>einzeln</a></li>").join("") + "</ul>";
  if (anders.length)
    h += "<h2 style='margin-top:14px'>🔄 Andere Version – alte Datei löschen, neue rein (" + anders.length + ")</h2><ul>" +
      anders.map(a => "<li><b>" + esc(a.m.t) + "</b>: du hast " + a.alt.map(x => esc(x.v || "?") + " <span class='mut'>(" + esc(x.f) + ")</span>").join(", ") +
      " → Server hat <b>" + esc(a.m.v || "?") + "</b> <a href='/" + SLUG + "/pack/mods/" + encodeURIComponent(a.m.f) + "'>laden</a></li>").join("") + "</ul>";
  if (fremd.length)
    h += "<h2 style='margin-top:14px'>❔ Nicht im Server-Modpack (" + fremd.length + ")</h2>" +
      "<p class='mut'>Reine Optik-Mods (Shader, Minimap usw.) dürfen bleiben. Kommt beim Verbinden ein Fehler, diese herausnehmen.</p><ul>" +
      fremd.map(x => "<li>" + esc(x.f) + (x.id ? " <span class='mut'>(" + esc(x.id) + " " + esc(x.v) + ")</span>" : "") + "</li>").join("") + "</ul>";
  h += "<p class='mut'>Danach Minecraft neu starten und gerne noch einmal abgleichen.</p>";
  out.innerHTML = h;
  out.scrollIntoView({behavior: "smooth"});
}
</script>
"""


def zip_stream(wfile, d, names):
    """Schreibt eine Zip der gewünschten Mod-Dateien direkt in den Antwort-Stream."""
    with zipfile.ZipFile(wfile, "w", zipfile.ZIP_STORED) as z:
        for fn in names:
            f = d / "pack" / "mods" / fn
            if f.is_file() and f.parent == d / "pack" / "mods":
                z.write(f, fn)


def _mod_links(m):
    pid = m["project_id"]
    if pid.startswith("cf:"):
        if m.get("slug"):
            return f'<a href="https://www.curseforge.com/minecraft/mc-mods/{urllib.parse.quote(m["slug"])}">CurseForge</a>'
        return f'<a href="https://www.curseforge.com/projects/{pid[3:]}">CurseForge</a>'
    if m.get("slug"):
        return f'<a href="https://modrinth.com/mod/{urllib.parse.quote(m["slug"])}">Modrinth</a>'
    return ""


def page(name, host):
    d = build(name)
    st = pack_state(name)
    cfg = core.load_cfg(name)
    mods = client_mods(name)
    s = slug(name)
    e = html.escape
    loader = core.LOADER_NAMES.get(cfg["loader"], cfg["loader"])
    port = core.read_properties(name).get("server-port", "25565")
    addr = host.split(":")[0] + ("" if port == "25565" else f":{port}")
    cf = next(d.glob("*-CurseForge.zip")).name
    mr = next(d.glob("*.mrpack")).name
    added = set(st.get("added", []))
    vdata = version_info(name)
    vinfo = vdata["mods"] if vdata else None
    rows = []
    for m in mods:
        cls = ' class="n"' if m["filename"] in added else ""
        neu = ' <span class="tag">neu</span>' if m["filename"] in added else ""
        side = '<span class="tag">nur Spieler</span>' if m["side"] == "client" else ""
        vi = (vinfo or {}).get(m["filename"], {})
        if vi.get("neuer"):
            side += (f"<br><span class='hinweis'>ℹ️ Neuere Version <b>{e(vi['neuer']['v'])}</b> gibt es nur für "
                     f"{e(vi['neuer']['fuer'])} – passt nicht zum Server, deshalb bleibt diese Version.</span>")
        if vi.get("update"):
            side += (f"<br><span class='hinweis'>⬆ Passendes Update <b>{e(vi['update'])}</b> verfügbar – "
                     "noch nicht auf dem Server installiert.</span>")
        rows.append(f"<tr{cls}><td><b>{e(m['title'])}</b>{neu} {side}<br><span class='mut'>{e(m['filename'])}"
                    f"</span></td><td>{_mod_links(m)}<a href='/{s}/pack/mods/{urllib.parse.quote(m['filename'])}'>"
                    "Datei ⬇</a></td></tr>")
    changes = ""
    if added or st.get("removed"):
        items = "".join(f"<li class='n'>＋ {e(f)}</li>" for f in sorted(added))
        items += "".join(f"<li class='r'>－ {e(f)}</li>" for f in st.get("removed", []))
        changes = (f"<div class='card'><h2>Neu in v{st['version']}</h2>"
                   f"<ul style='list-style:none;padding:0;margin:0'>{items}</ul></div>")
    plist = []
    for m in mods:
        jf = d / "pack" / "mods" / m["filename"]
        mid, ver = jar_meta(jf)
        plist.append({"f": m["filename"], "t": m["title"], "s": jf.stat().st_size, "id": mid, "v": ver})
    abgleich = ABGLEICH.replace("__DATA__", json.dumps(plist).replace("</", "<\\/")).replace("__SLUG__", s)
    if vdata:
        n_inc = sum(1 for x in vinfo.values() if x.get("neuer"))
        vstand = (f"<p class='mut'>Versionen geprüft {time.strftime('%d.%m. %H:%M', time.localtime(vdata['zeit']))}"
                  + (f" · {n_inc} Mods haben eine neuere Version, die (noch) nicht zu Minecraft {e(cfg['mc'])} / "
                     f"{e(loader)} passt – die stehen mit ℹ️ markiert." if n_inc else "") + "</p>")
    else:
        vstand = "<p class='mut'>Neuere Versionen werden gerade geprüft – Seite in einer Minute neu laden.</p>"
    others = [n for n in core.list_servers() if n != name]
    other_html = ""
    if others:
        other_html = ("<p class='mut'>Andere Server: " + ", ".join(
            f"<a href='/{slug(n)}/'>{e(n)}</a>" for n in others) + "</p>")
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mods – {e(name)}</title>
<style>{CSS}</style></head><body><main>
<h1>{e(name)}</h1>
<p class="mut">Minecraft {e(cfg['mc'])} · {e(loader)} {e(cfg.get('loader_version') or '')} ·
Server-Adresse <code>{e(addr)}</code></p>
<p><span class="badge">Modpack v{st['version']}</span> <span class="mut">Stand {e(st['changed'])} ·
{len(mods)} Mods</span></p>
<p class="mut">Wenn in der Serverliste eine höhere Modpack-Version steht als bei dir, oder beim Verbinden
„fehlende Mods“ kommt: hier das aktuelle Paket holen.</p>
{abgleich}
{changes}
<div class="card"><h2>Herunterladen</h2><div class="grid">
<a class="dl" href="/{s}/{urllib.parse.quote(cf)}"><b>⬇ CurseForge-App</b><br>
<span class="mut">{e(cf)}</span><ol>
<li>CurseForge-App → <b>Minecraft</b> → <b>Erstellen</b> (Create Custom Profile)</li>
<li>Oben „<b>Importieren</b>“ → diese Zip wählen</li>
<li>Bei einem Update: altes Profil löschen, neue Zip importieren (Welt liegt auf dem Server, geht nicht verloren)</li>
</ol></a>
<a class="dl" href="/{s}/prism.zip"><b>⬇ Prism Launcher – automatisch aktuell</b><br>
<span class="mut">einmal importieren, danach nie wieder</span><ol>
<li>Prism → <b>Instanz hinzufügen</b> → <b>Importieren</b> → diese Zip</li>
<li>Bei jedem Start werden fehlende Mods geladen und alte gelöscht</li>
<li>Startet direkt auf den Server</li></ol></a>
<a class="dl" href="/{s}/{urllib.parse.quote(mr)}"><b>⬇ Modrinth App / Prism (einmalig)</b><br>
<span class="mut">{e(mr)}</span><ol><li>In der Modrinth App oder Prism importieren</li>
<li>Kein Auto-Update</li></ol></a>
</div></div>
<div class="card"><h2>Alle Mods</h2>{vstand}<table><tr><th>Mod</th><th>Links</th></tr>
{''.join(rows)}</table></div>
{other_html}
</main></body></html>"""


def index_page(host):
    srv = core.list_servers()
    items = "".join(f"<li><a href='/{slug(n)}/'>{html.escape(n)}</a></li>" for n in srv)
    return (f"<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Felix' Server</title><style>{CSS}</style>"
            f"</head><body><main><h1>Felix' Server</h1><div class='card'><ul>{items}</ul></div></main></body></html>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        host = self.headers.get("Host") or f"localhost:{PORT}"
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        try:
            if not parts:
                name = active["name"]
                if name not in core.list_servers():
                    try:
                        name = json.loads((core.BASE / "gui.json").read_text()).get("last")
                    except (OSError, ValueError):
                        name = None
                if name not in core.list_servers():
                    name = None
                if name:
                    return self._send(page(name, host).encode(), "text/html; charset=utf-8")
                return self._send(index_page(host).encode(), "text/html; charset=utf-8")
            name = by_slug(parts[0])
            if not name:
                return self._send(b"Nicht gefunden", "text/plain; charset=utf-8", 404)
            if len(parts) == 1:
                return self._send(page(name, host).encode(), "text/html; charset=utf-8")
            if parts[1:] == ["fehlende.zip"]:
                d = build(name)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("f", [])
                names = [Path(x).name for x in q if x.endswith(".jar")]
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="fehlende-mods.zip"')
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    zip_stream(self.wfile, d, names)
                return
            if parts[1:] == ["prism.zip"]:
                fn = f"{slug(name)}-prism.zip"
                return self._send(prism_zip(name, host), "application/zip",
                                  extra={"Content-Disposition": f'attachment; filename="{fn}"'})
            d = build(name).resolve()
            f = (d / "/".join(parts[1:])).resolve()
            if d not in f.parents or not f.is_file() or f.name.startswith("."):
                return self._send(b"Nicht gefunden", "text/plain; charset=utf-8", 404)
            ctype = "text/plain; charset=utf-8" if f.suffix in (".toml", ".json") else "application/octet-stream"
            extra = {} if f.suffix in (".toml", ".json") else {
                "Content-Disposition": f'attachment; filename="{f.name}"'}
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(f.stat().st_size))
            self.send_header("Cache-Control", "no-cache")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                with open(f, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 1 << 20)
        except Exception as ex:  # noqa: BLE001
            self._send(f"Fehler: {ex}".encode(), "text/plain; charset=utf-8", 500)


_server = None


def start(port=PORT):
    """Startet die Mod-Seite im Hintergrund (einmalig)."""
    global _server
    if _server:
        return _server
    _server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return _server


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        active["name"] = sys.argv[1]
    start()
    print(f"Mod-Seite läuft auf http://localhost:{PORT}/")
    threading.Event().wait()
