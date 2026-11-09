"""Liest Mobs aus den Welt-Speicherdateien (entities/r.X.Z.mca) für die BlueMap-Karte.

Minecraft speichert Mobs beim Autosave (alle 5 Minuten) bzw. wenn Chunks entladen werden.
Die Karte zeigt also den Stand der letzten Speicherung.
"""
import json
import struct
import time
import zlib
from pathlib import Path

DIMS = {"world": "", "world_the_nether": "DIM-1", "world_the_end": "DIM1"}

SKIP = {"item", "experience_orb", "arrow", "spectral_arrow", "trident", "snowball", "egg", "ender_pearl",
        "fireball", "small_fireball", "dragon_fireball", "wither_skull", "shulker_bullet", "llama_spit",
        "area_effect_cloud", "marker", "interaction", "block_display", "item_display", "text_display",
        "falling_block", "tnt", "lightning_bolt", "firework_rocket", "painting", "item_frame",
        "glow_item_frame", "leash_knot", "evoker_fangs", "eye_of_ender", "fishing_bobber", "potion",
        "experience_bottle", "wind_charge", "breeze_wind_charge", "ominous_item_spawner"}
SKIP_PARTS = ("contraption", "seat", "display", "projectile", "bullet", "sub_level", "physics", "carriage",
              "gantry", "super_glue", "plunger", "rope", "minecart", "boat", "raft")


# ------------------------------------------------------------ NBT ------
class _R:
    def __init__(self, b):
        self.b, self.i = b, 0

    def take(self, n):
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def u(self, fmt):
        s = struct.calcsize(fmt)
        return struct.unpack(fmt, self.take(s))[0]


def _payload(r, t):
    if t == 1: return r.u(">b")
    if t == 2: return r.u(">h")
    if t == 3: return r.u(">i")
    if t == 4: return r.u(">q")
    if t == 5: return r.u(">f")
    if t == 6: return r.u(">d")
    if t == 7: n = r.u(">i"); r.take(n); return None
    if t == 8: n = r.u(">H"); return r.take(n).decode("utf-8", "replace")
    if t == 9:
        et, n = r.u(">b"), r.u(">i")
        return [_payload(r, et) for _ in range(n)]
    if t == 10:
        d = {}
        while True:
            ct = r.u(">b")
            if ct == 0:
                return d
            name = r.take(r.u(">H")).decode("utf-8", "replace")
            d[name] = _payload(r, ct)
    if t == 11: n = r.u(">i"); r.take(4 * n); return None
    if t == 12: n = r.u(">i"); r.take(8 * n); return None
    raise ValueError(f"NBT-Typ {t}")


def read_nbt(data):
    r = _R(data)
    t = r.u(">b")
    r.take(r.u(">H"))
    return _payload(r, t)


def region_chunks(path):
    data = Path(path).read_bytes()
    if len(data) < 8192:
        return
    for i in range(1024):
        off = int.from_bytes(data[i * 4:i * 4 + 3], "big") * 4096
        if not off or off + 5 > len(data):
            continue
        length = int.from_bytes(data[off:off + 4], "big")
        comp = data[off + 4]
        raw = data[off + 5:off + 4 + length]
        try:
            if comp == 2:
                yield read_nbt(zlib.decompress(raw))
            elif comp == 1:
                import gzip
                yield read_nbt(gzip.decompress(raw))
            elif comp == 3:
                yield read_nbt(raw)
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------- Mobs -------
def _name(e):
    cn = e.get("CustomName")
    if isinstance(cn, str) and cn:
        try:
            j = json.loads(cn)
            return j.get("text") if isinstance(j, dict) else str(j)
        except Exception:  # noqa: BLE001
            return cn
    return None


def collect(world_dir, dim_sub):
    base = Path(world_dir) / dim_sub / "entities" if dim_sub else Path(world_dir) / "entities"
    out = []
    if not base.exists():
        return out
    for reg in base.glob("r.*.*.mca"):
        for ch in region_chunks(reg):
            for e in ch.get("Entities", []) or []:
                eid = e.get("id", "")
                ns, _, short = eid.partition(":")
                if short in SKIP or any(p in short for p in SKIP_PARTS):
                    continue
                pos = e.get("Pos")
                if not pos or len(pos) < 3:
                    continue
                rot = e.get("Rotation") or [0, 0]
                m = {"t": eid if ns != "minecraft" else short, "x": round(pos[0], 2), "y": round(pos[1], 2),
                     "z": round(pos[2], 2), "r": round(rot[0], 1)}
                n = _name(e)
                if n:
                    m["n"] = n
                if e.get("IsBaby") or (isinstance(e.get("Age"), int) and e["Age"] < 0):
                    m["b"] = 1
                for key in ("variant", "Variant", "Type", "RabbitType"):
                    if key in e and isinstance(e[key], (int, str)):
                        m["v"] = e[key]
                        break
                if e.get("Owner") is not None:
                    m["tm"] = 1
                if short in ("slime", "magma_cube") and isinstance(e.get("Size"), int):
                    m["sz"] = e["Size"]
                if short == "sheep":
                    m["c"] = e.get("Color", 0) or 0
                    if e.get("Sheared"):
                        m["sh"] = 1
                out.append(m)
    return out


def export_textures(server_dir, webroot):
    """Alle Mob-Texturen aus der von BlueMap geladenen Minecraft-Datei nach <webroot>/extra/tex
    kopieren (Pfade wie unter textures/entity) und ihre Größen in sizes.json schreiben."""
    import struct
    import zipfile
    jars = sorted((Path(server_dir) / "bluemap").glob("minecraft-client-*.jar"))
    if not jars:
        return 0
    out = Path(webroot) / "extra" / "tex"
    sizes_file = out / "sizes.json"
    if sizes_file.exists() and sizes_file.stat().st_mtime >= jars[-1].stat().st_mtime:
        return 0
    prefix = "assets/minecraft/textures/entity/"
    sizes = {}
    with zipfile.ZipFile(jars[-1]) as z:
        for n in z.namelist():
            if not (n.startswith(prefix) and n.endswith(".png")):
                continue
            rel = n[len(prefix):-4]
            data = z.read(n)
            dst = out / f"{rel}.png"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            sizes[rel] = list(struct.unpack(">II", data[16:24]))
    sizes_file.write_text(json.dumps(sizes, separators=(",", ":")))
    return len(sizes)


def export(server_dir, webroot, level="world"):
    """Schreibt <webroot>/extra/mobs-<map>.json, wenn sich Speicherdateien geändert haben."""
    world = Path(server_dir) / level
    extra = Path(webroot) / "extra"
    extra.mkdir(parents=True, exist_ok=True)
    try:
        export_textures(server_dir, webroot)
    except Exception:  # noqa: BLE001
        pass
    total = 0
    for mapid, sub in DIMS.items():
        base = world / sub / "entities" if sub else world / "entities"
        if not base.exists():
            continue
        newest = max((p.stat().st_mtime for p in base.glob("r.*.*.mca")), default=0)
        out = extra / f"mobs-{mapid}.json"
        if out.exists() and out.stat().st_mtime >= newest:
            continue
        mobs = collect(world, sub)
        total += len(mobs)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": int(newest), "mobs": mobs}, separators=(",", ":")))
        tmp.replace(out)
    return total


if __name__ == "__main__":
    import sys
    t = time.time()
    print(export(sys.argv[1], sys.argv[2]), "Mobs", round(time.time() - t, 2), "s")
