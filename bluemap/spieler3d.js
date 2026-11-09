/* Spieler als 3D-Figuren mit Skin + flüssige Bewegung für BlueMap 5.x
 * (eingebunden über webapp.conf → scripts). Felix' Server-Manager.
 *
 * BlueMap liefert Spielerpositionen nur ca. 1x pro Sekunde. Wir fragen öfter nach
 * (genauere Zeitstempel) und spielen die Bewegung mit ~1 s Verzögerung linear
 * interpoliert ab – wie Netzwerkspiele es machen. Ergebnis: gleichmäßiges Laufen.
 */
(function () {
  'use strict';
  const POLL_MS = 200;      // so oft players.json holen
  const DELAY_MS = 1150;    // Abspiel-Verzögerung (> Server-Takt von 1 s)
  const PX = 0.9 / 16;      // Minecraft-Pixel → Blöcke (Figur 1,8 Blöcke hoch)

  const wait = setInterval(() => {
    const app = window.bluemap;
    if (window.BlueMap && app && app.mapViewer && app.playerMarkerManager) {
      clearInterval(wait);
      try { init(window.BlueMap, app); } catch (e) { console.error('[spieler3d]', e); }
    }
  }, 300);

  function init(BM, app) {
    const T = BM.Three;
    const Group = T.Group || T.Object3D;

    // Kopf-Symbol nur noch aus großer Entfernung zeigen
    const st = document.createElement('style');
    st.textContent = '.bm-marker-player[distance-data="near"] img{display:none}';
    document.head.appendChild(st);

    // 1) schneller abfragen
    const PMM = BM.PlayerMarkerManager.prototype;
    const origInterval = PMM.setAutoUpdateInterval;
    PMM.setAutoUpdateInterval = function (ms) {
      return origInterval.call(this, ms > 0 ? Math.min(ms, POLL_MS) : ms);
    };
    app.playerMarkerManager.setAutoUpdateInterval(POLL_MS);

    // 2) Positionen puffern statt springen
    const PM = BM.PlayerMarker.prototype;
    PM.updateFromData = function (e) {
      const p = e.position || {}, r = e.rotation || {}, now = performance.now();
      const s = { t: now, x: p.x || 0, y: p.y || 0, z: p.z || 0, yaw: r.yaw || 0, pitch: r.pitch || 0 };
      const b = this._buf || (this._buf = []);
      const l = b[b.length - 1];
      if (!l) {
        b.push(s);
        this.position.set(s.x, s.y + 1.8, s.z);
      } else if (l.x !== s.x || l.y !== s.y || l.z !== s.z || l.yaw !== s.yaw || l.pitch !== s.pitch) {
        if (Math.hypot(s.x - l.x, s.y - l.y, s.z - l.z) > 48) {
          b.length = 0;                       // Teleport → direkt hin
          b.push(s);
        } else {
          if (now - l.t > 1100) b.push(Object.assign({}, l, { t: now - 1000 })); // stand still → Start der Bewegung
          b.push(s);
        }
        if (b.length > 12) b.splice(0, b.length - 12);
      }
      const name = e.name || this.data.playerUuid;
      this.data.name = name;
      if (this.playerNameElement.innerHTML !== name) this.playerNameElement.innerHTML = name;
      this.data.foreign = e.foreign;
      if (!this._model) buildModel(this);
    };

    // 3) Figur bauen
    function buildModel(marker) {
      const holder = new Group();
      holder.position.y = -1.8;
      marker.add(holder);
      marker._model = holder;
      marker._phase = 0;
      marker._amp = 0;
      const uuid = marker.data.playerUuid;
      const loader = new T.TextureLoader();
      const tryUrls = ['skins/' + uuid + '.png', 'https://crafatar.com/skins/' + uuid];
      const next = () => {
        const url = tryUrls.shift();
        if (!url) { assemble(holder, null, 64, marker); return; }
        loader.load(url, tex => assemble(holder, tex, tex.image.height === 32 ? 32 : 64, marker), undefined, next);
      };
      next();
    }

    function assemble(holder, tex, th, marker) {
      let base, over;
      if (tex) {
        tex.magFilter = T.NearestFilter;
        tex.minFilter = T.NearestFilter;
        tex.generateMipmaps = false;
        base = new T.MeshBasicMaterial({ map: tex, alphaTest: 0.5 });
        over = new T.MeshBasicMaterial({ map: tex, alphaTest: 0.5, transparent: true, side: T.DoubleSide, depthWrite: false });
      } else {
        base = new T.MeshBasicMaterial({ color: 0x3c6eb4 });
      }
      const box = (w, h, d, u, v, mat, inf) => {
        inf = inf || 0;
        const g = new T.BoxGeometry((w + inf * 2) * PX, (h + inf * 2) * PX, (d + inf * 2) * PX);
        if (tex) {
          const uv = g.attributes.uv;
          const faces = [
            [u + d + w, v + d, d, h],     // +x  linke Seite der Figur
            [u, v + d, d, h],             // -x  rechte Seite
            [u + d, v, w, d],             // +y  oben
            [u + d + w, v, w, d],         // -y  unten
            [u + d, v + d, w, h],         // +z  vorne
            [u + d + w + d, v + d, w, h], // -z  hinten
          ];
          faces.forEach((f, i) => {
            const u0 = f[0] / 64, u1 = (f[0] + f[2]) / 64, v0 = 1 - f[1] / th, v1 = 1 - (f[1] + f[3]) / th;
            uv.setXY(i * 4, u0, v0); uv.setXY(i * 4 + 1, u1, v0);
            uv.setXY(i * 4 + 2, u0, v1); uv.setXY(i * 4 + 3, u1, v1);
          });
          uv.needsUpdate = true;
        }
        return new T.Mesh(g, mat);
      };
      const part = (px, py, pz, w, h, d, my, uvBase, uvOver, inf) => {
        const pivot = new Group();
        pivot.position.set(px * PX, py * PX, pz * PX);
        const m = box(w, h, d, uvBase[0], uvBase[1], base);
        m.position.y = my * PX;
        pivot.add(m);
        if (over && uvOver && th === 64) {
          const o = box(w, h, d, uvOver[0], uvOver[1], over, inf);
          o.position.y = my * PX;
          pivot.add(o);
        }
        holder.add(pivot);
        return pivot;
      };
      const legacy = th === 32;
      marker._parts = {
        head: part(0, 24, 0, 8, 8, 8, 4, [0, 0], [32, 0], 0.5),
        body: part(0, 18, 0, 8, 12, 4, 0, [16, 16], [16, 32], 0.25),
        rArm: part(-6, 22, 0, 4, 12, 4, -4, [40, 16], [40, 32], 0.25),
        lArm: part(6, 22, 0, 4, 12, 4, -4, legacy ? [40, 16] : [32, 48], [48, 48], 0.25),
        rLeg: part(-2, 12, 0, 4, 12, 4, -6, [0, 16], [0, 32], 0.25),
        lLeg: part(2, 12, 0, 4, 12, 4, -6, legacy ? [0, 16] : [16, 48], [0, 48], 0.25),
      };
      if (over) marker._parts.head.children.forEach(c => (c.renderOrder = 1));
      app.mapViewer.redraw();
    }

    // 4) jede Bildschirm-Aktualisierung: Position interpolieren + Laufanimation
    const lerpYaw = (a, b, f) => { let d = b - a; while (d > 180) d -= 360; while (d < -180) d += 360; return a + d * f; };
    let lastT = performance.now();
    function frame() {
      const now = performance.now(), dt = Math.min(0.1, (now - lastT) / 1000);
      lastT = now;
      let changed = false;
      try {
        const set = app.playerMarkerManager && app.playerMarkerManager.getPlayerMarkerSet(false);
        if (set) set.markers.forEach(m => { if (m.isPlayerMarker && m._buf) changed = step(m, now, dt) || changed; });
      } catch (e) { /* Kartenwechsel o. Ä. */ }
      if (window.__felixFollow) {
        try {
          const set2 = app.playerMarkerManager.getPlayerMarkerSet(false);
          let tm = null;
          if (set2) set2.markers.forEach(m => { if (m.data.playerUuid === window.__felixFollow) tm = m; });
          const ctrl = app.mapViewer.controlsManager;
          if (tm && ctrl) {
            ctrl.position.set(tm.position.x, tm.position.y - 1.8, tm.position.z);
            changed = true;
          }
        } catch (e) { /* egal */ }
      }
      if (changed) app.mapViewer.redraw();
      requestAnimationFrame(frame);
    }

    function step(m, now, dt) {
      const b = m._buf, rt = now - DELAY_MS;
      while (b.length > 2 && b[1].t <= rt) b.shift();
      let s;
      if (b.length === 1 || rt <= b[0].t) s = b[0];
      else if (rt >= b[b.length - 1].t) s = b[b.length - 1];
      else {
        const a = b[0], c = b[1], f = (rt - a.t) / (c.t - a.t);
        s = { x: a.x + (c.x - a.x) * f, y: a.y + (c.y - a.y) * f, z: a.z + (c.z - a.z) * f,
              yaw: lerpYaw(a.yaw, c.yaw, f), pitch: a.pitch + (c.pitch - a.pitch) * f };
      }
      const ox = m.position.x, oy = m.position.y, oz = m.position.z;
      m.position.set(s.x, s.y + 1.8, s.z);
      const dist = Math.hypot(s.x - ox, s.z - oz);
      let changed = dist > 1e-5 || Math.abs(s.y + 1.8 - oy) > 1e-5;
      if (m._model) {
        m._model.rotation.y = -s.yaw * Math.PI / 180;
        const speed = dt > 0 ? dist / dt : 0;
        const target = Math.min(speed / 4.3, 1);
        m._amp += (target - m._amp) * Math.min(1, dt * 8);
        m._phase += dist * 2.4;
        if (m._parts) {
          const sw = Math.sin(m._phase) * m._amp * 0.9;
          m._parts.rArm.rotation.x = sw;
          m._parts.lArm.rotation.x = -sw;
          m._parts.rLeg.rotation.x = -sw;
          m._parts.lLeg.rotation.x = sw;
          m._parts.head.rotation.x = s.pitch * Math.PI / 180;
        }
        // aus der Ferne größer zeichnen, damit man die Figur noch sieht
        // Zoom-Stufe der Karte nehmen, nicht den Kamera-Abstand: in der Draufsicht steht die
        // (orthografische) Kamera sehr weit oben, das machte die Figuren riesig.
        const ctrl = app.mapViewer.controlsManager;
        if (ctrl) {
          const cam = app.mapViewer.camera;
          const zoom = (cam && cam.ortho > 0.5) ? ctrl.distance : Math.min(ctrl.distance, cam ? cam.position.distanceTo(m.position) : ctrl.distance);
          const sc = Math.min(12, Math.max(1, zoom / 70));
          if (Math.abs(m._model.scale.x - sc) > 0.01) { m._model.scale.setScalar(sc); changed = true; }
        }
        if (m._amp > 0.005) changed = true;
      }
      return changed;
    }
    requestAnimationFrame(frame);
    initMobs(BM, app, T, Group);
    initAutoRefresh(app);
    initFollow(app);
    console.log('[spieler3d] aktiv');
  }

  /* ---------------- Mobs (Stand der letzten Welt-Speicherung) ---------------- */
  // Breite, Höhe, Farbe, Form: q = Vierbeiner, b = Zweibeiner, f = Flieger/klein, w = Wasser
  const MOB = {
    cow: [0.9, 1.4, 0x4a3426, 'q', 'Kuh'], mooshroom: [0.9, 1.4, 0xa3221c, 'q', 'Pilzkuh'],
    pig: [0.9, 0.9, 0xf0a5a2, 'q', 'Schwein'], sheep: [0.9, 1.3, 0xe8e8e8, 'q', 'Schaf'],
    chicken: [0.4, 0.7, 0xf4f4f4, 'f', 'Huhn'], horse: [1.3, 1.6, 0x8a5a2b, 'q', 'Pferd'],
    donkey: [1.3, 1.5, 0x7d6a58, 'q', 'Esel'], mule: [1.3, 1.6, 0x5a3e28, 'q', 'Maultier'],
    llama: [0.9, 1.87, 0xd8c8a0, 'q', 'Lama'], trader_llama: [0.9, 1.87, 0x3a5ec8, 'q', 'Händler-Lama'],
    goat: [0.9, 1.3, 0xdcd6c8, 'q', 'Ziege'], camel: [1.7, 2.3, 0xc8a064, 'q', 'Kamel'],
    wolf: [0.6, 0.85, 0xbfbfbf, 'q', 'Wolf'], fox: [0.6, 0.7, 0xe07020, 'q', 'Fuchs'],
    cat: [0.6, 0.7, 0x8a7050, 'q', 'Katze'], ocelot: [0.6, 0.7, 0xe0c060, 'q', 'Ozelot'],
    rabbit: [0.4, 0.5, 0xa08060, 'f', 'Hase'], panda: [1.3, 1.25, 0xf0f0f0, 'q', 'Panda'],
    polar_bear: [1.4, 1.4, 0xf8f8f8, 'q', 'Eisbär'], armadillo: [0.7, 0.65, 0xa06850, 'q', 'Gürteltier'],
    sniffer: [1.9, 1.75, 0x9c3a2a, 'q', 'Schnüffler'], frog: [0.5, 0.5, 0xd08030, 'f', 'Frosch'],
    turtle: [1.2, 0.4, 0x3a8a3a, 'w', 'Schildkröte'], strider: [0.9, 1.7, 0xa03030, 'b', 'Schreiter'],
    villager: [0.6, 1.95, 0x9a6a4a, 'b', 'Dorfbewohner'], wandering_trader: [0.6, 1.95, 0x3a5ec8, 'b', 'Fahrender Händler'],
    iron_golem: [1.4, 2.7, 0xd8d0c8, 'b', 'Eisengolem'], snow_golem: [0.7, 1.9, 0xffffff, 'b', 'Schneegolem'],
    allay: [0.35, 0.6, 0x60d0ff, 'f', 'Hilfsgeist'], bee: [0.7, 0.6, 0xf0c020, 'f', 'Biene'],
    parrot: [0.5, 0.9, 0xe02020, 'f', 'Papagei'], bat: [0.5, 0.9, 0x4a3a2a, 'f', 'Fledermaus'],
    squid: [0.8, 0.8, 0x3a4a6a, 'w', 'Tintenfisch'], glow_squid: [0.8, 0.8, 0x40c0b0, 'w', 'Leuchttintenfisch'],
    cod: [0.5, 0.3, 0xb09070, 'w', 'Kabeljau'], salmon: [0.7, 0.4, 0xa03030, 'w', 'Lachs'],
    tropical_fish: [0.5, 0.4, 0xf08020, 'w', 'Tropenfisch'], pufferfish: [0.7, 0.7, 0xe0c030, 'w', 'Kugelfisch'],
    dolphin: [0.9, 0.6, 0x7090b0, 'w', 'Delfin'], axolotl: [0.75, 0.42, 0xf0a0c0, 'w', 'Axolotl'],
    tadpole: [0.4, 0.3, 0x5a4030, 'w', 'Kaulquappe'],
    zombie: [0.6, 1.95, 0x3a7a3a, 'b', 'Zombie', 1], husk: [0.6, 1.95, 0xb0a070, 'b', 'Wüstenzombie', 1],
    drowned: [0.6, 1.95, 0x40a0a0, 'b', 'Ertrunkener', 1], zombie_villager: [0.6, 1.95, 0x5a8a3a, 'b', 'Zombiedorfbewohner', 1],
    skeleton: [0.6, 1.99, 0xc8c8c8, 'b', 'Skelett', 1], stray: [0.6, 1.99, 0x90a8a8, 'b', 'Eiswanderer', 1],
    bogged: [0.6, 1.99, 0x6a7a3a, 'b', 'Sumpfskelett', 1], wither_skeleton: [0.7, 2.4, 0x2a2a2a, 'b', 'Witherskelett', 1],
    creeper: [0.6, 1.7, 0x40c040, 'b', 'Creeper', 1], spider: [1.4, 0.9, 0x3a3030, 'q', 'Spinne', 1],
    cave_spider: [0.7, 0.5, 0x1a4a5a, 'q', 'Höhlenspinne', 1], enderman: [0.6, 2.9, 0x161616, 'b', 'Enderman', 1],
    witch: [0.6, 1.95, 0x5a3a7a, 'b', 'Hexe', 1], slime: [1.0, 1.0, 0x70c060, 'f', 'Schleim', 1],
    magma_cube: [1.0, 1.0, 0x8a2a10, 'f', 'Magmawürfel', 1], phantom: [0.9, 0.5, 0x3a4a8a, 'f', 'Phantom', 1],
    pillager: [0.6, 1.95, 0x5a5a5a, 'b', 'Plünderer', 1], vindicator: [0.6, 1.95, 0x4a4a5a, 'b', 'Diener', 1],
    evoker: [0.6, 1.95, 0x3a3a6a, 'b', 'Magier', 1], ravager: [1.95, 2.2, 0x4a4040, 'q', 'Verwüster', 1],
    blaze: [0.6, 1.8, 0xf0b020, 'b', 'Lohe', 1], ghast: [4.0, 4.0, 0xf0f0f0, 'f', 'Ghast', 1],
    piglin: [0.6, 1.95, 0xe0a080, 'b', 'Piglin'], piglin_brute: [0.6, 1.95, 0x8a6040, 'b', 'Piglin-Barbar', 1],
    zombified_piglin: [0.6, 1.95, 0xd08080, 'b', 'Zombifizierter Piglin'], hoglin: [1.4, 1.4, 0xa06040, 'q', 'Hoglin', 1],
    zoglin: [1.4, 1.4, 0xc08080, 'q', 'Zoglin', 1], shulker: [1.0, 1.0, 0x9a6a9a, 'f', 'Shulker', 1],
    guardian: [0.85, 0.85, 0x6aa090, 'w', 'Wächter', 1], elder_guardian: [2.0, 2.0, 0xc0c0b0, 'w', 'Großer Wächter', 1],
    silverfish: [0.4, 0.3, 0x8a8a8a, 'f', 'Silberfischchen', 1], endermite: [0.4, 0.3, 0x3a1a4a, 'f', 'Endermite', 1],
    vex: [0.4, 0.8, 0xa0b0c0, 'f', 'Plagegeist', 1], breeze: [0.6, 1.77, 0x9ab0e0, 'b', 'Böe', 1],
    warden: [0.9, 2.9, 0x0a3a4a, 'b', 'Wärter', 1], ender_dragon: [16, 8, 0x1a1a1a, 'f', 'Enderdrache', 1],
    wither: [0.9, 3.5, 0x1a1a1a, 'b', 'Wither', 1], creaking: [0.9, 2.7, 0x5a4a3a, 'b', 'Knarrer', 1],
  };
  const GERMAN_FALLBACK = t => t.replace(/^.*:/, '').replace(/_/g, ' ');


  /* -------- Echte Mob-Modelle (Minecraft-Modellkoordinaten: y nach unten, Boden = 24) --------
   * Teil: [pivotX, pivotY, pivotZ, rotX, rotY, rotZ, [[u, v, x, y, z, w, h, d, inflate], ...], [Kind-Teile]]
   * Kind-Teile haben ihren Drehpunkt relativ zum Eltern-Teil (wie in Minecraft). */
  const HP = Math.PI / 2, Q = Math.PI / 4, E = Math.PI / 8;
  const P = (px, py, pz, cubes, rx = 0, ry = 0, rz = 0, kids) => [px, py, pz, rx, ry, rz, cubes, kids];
  const LEG = (px, py, pz, u, v, x, y, z, w, h, d) => P(px, py, pz, [[u, v, x, y, z, w, h, d]]);
  const legs4 = (u, v, x, y, z, w, h, d, ps, uvs) => ps.map((p, i) =>
    LEG(p[0], p[1], p[2], uvs ? uvs[i][0] : u, uvs ? uvs[i][1] : v, x, y, z, w, h, d));

  const M_HUMAN = (armsForward, leftArm, leftLeg) => [
    P(0, 0, 0, [[0, 0, -4, -8, -4, 8, 8, 8], [32, 0, -4, -8, -4, 8, 8, 8, 0.5]]),
    P(0, 0, 0, [[16, 16, -4, 0, -2, 8, 12, 4]]),
    P(-5, 2, 0, [[40, 16, -3, -2, -2, 4, 12, 4]], armsForward ? -HP : 0),
    P(5, 2, 0, [[leftArm[0], leftArm[1], -1, -2, -2, 4, 12, 4]], armsForward ? -HP : 0),
    LEG(-1.9, 12, 0, 0, 16, -2, 0, -2, 4, 12, 4),
    LEG(1.9, 12, 0, leftLeg[0], leftLeg[1], -2, 0, -2, 4, 12, 4),
  ];
  const M_SKELETON = [
    P(0, 0, 0, [[0, 0, -4, -8, -4, 8, 8, 8], [32, 0, -4, -8, -4, 8, 8, 8, 0.5]]),
    P(0, 0, 0, [[16, 16, -4, 0, -2, 8, 12, 4]]),
    P(-5, 2, 0, [[40, 16, -1, -2, -1, 2, 12, 2]], -1.2, 0.1),
    P(5, 2, 0, [[40, 16, -1, -2, -1, 2, 12, 2]], -0.3),
    LEG(-2, 12, 0, 0, 16, -1, 0, -1, 2, 12, 2), LEG(2, 12, 0, 0, 16, -1, 0, -1, 2, 12, 2),
  ];
  const M_CREEPER = [
    P(0, 6, 0, [[0, 0, -4, -8, -4, 8, 8, 8]]), P(0, 6, 0, [[16, 16, -4, 0, -2, 8, 12, 4]]),
    ...legs4(0, 16, -2, 0, -2, 4, 6, 4, [[-2, 18, 4], [2, 18, 4], [-2, 18, -4], [2, 18, -4]]),
  ];
  const M_COW = [
    P(0, 4, -8, [[0, 0, -4, -4, -6, 8, 8, 6], [22, 0, -5, -5, -4, 1, 3, 1], [22, 0, 4, -5, -4, 1, 3, 1]]),
    P(0, 5, 2, [[18, 4, -6, -10, -7, 12, 18, 10], [52, 0, -2, 2, -8, 4, 6, 1]], HP),
    ...legs4(0, 16, -2, 0, -2, 4, 12, 4, [[-4, 12, 7], [4, 12, 7], [-4, 12, -6], [4, 12, -6]]),
  ];
  const M_PIG = [
    P(0, 12, -6, [[0, 0, -4, -4, -8, 8, 8, 8], [16, 16, -2, 0, -9, 4, 3, 1]]),
    P(0, 11, 2, [[28, 8, -5, -10, -7, 10, 16, 8]], HP),
    ...legs4(0, 16, -2, 0, -2, 4, 6, 4, [[-3, 18, 7], [3, 18, 7], [-3, 18, -5], [3, 18, -5]]),
  ];
  const M_SHEEP = [
    P(0, 6, -8, [[0, 0, -3, -4, -6, 6, 6, 8]]), P(0, 5, 2, [[28, 8, -4, -10, -7, 8, 16, 6]], HP),
    ...legs4(0, 16, -2, 0, -2, 4, 12, 4, [[-3, 12, 7], [3, 12, 7], [-3, 12, -5], [3, 12, -5]]),
  ];
  const M_SHEEP_FUR = [
    P(0, 6, -8, [[0, 0, -3, -4, -4, 6, 6, 6, 0.6]]), P(0, 5, 2, [[28, 8, -4, -10, -7, 8, 16, 6, 1.75]], HP),
    ...[[-3, 12, 7], [3, 12, 7], [-3, 12, -5], [3, 12, -5]].map(p => P(p[0], p[1], p[2], [[0, 16, -2, 0, -2, 4, 6, 4, 0.5]])),
  ];
  const M_CHICKEN = [
    P(0, 15, -4, [[0, 0, -2, -6, -2, 4, 6, 3], [14, 0, -2, -4, -4, 4, 2, 2], [14, 4, -1, -2, -3, 2, 2, 2]]),
    P(0, 16, 0, [[0, 9, -3, -4, -3, 6, 8, 6]], HP),
    LEG(-2, 19, 1, 26, 0, -1, 0, -3, 3, 5, 3), LEG(1, 19, 1, 26, 0, -1, 0, -3, 3, 5, 3),
    P(-4, 13, 0, [[24, 13, 0, 0, -3, 1, 4, 6]]), P(4, 13, 0, [[24, 13, -1, 0, -3, 1, 4, 6]]),
  ];
  const M_SPIDER = [
    P(0, 15, -3, [[32, 4, -4, -4, -8, 8, 8, 8]]), P(0, 15, 0, [[0, 0, -3, -3, -3, 6, 6, 6]]),
    P(0, 15, 9, [[0, 12, -5, -4, -6, 10, 8, 12]]),
    ...[[2, Q, -Q], [1, E, -0.58], [0, -E, -0.58], [-1, -Q, -Q]].flatMap(([z, ry, rz]) => [
      P(-4, 15, z, [[18, 0, -15, -1, -1, 16, 2, 2]], 0, ry, rz),
      P(4, 15, z, [[18, 0, -1, -1, -1, 16, 2, 2]], 0, -ry, -rz)]),
  ];
  const M_VILLAGER = (zombieArms) => [
    P(0, 0, 0, [[0, 0, -4, -10, -4, 8, 10, 8]]), P(0, -2, 0, [[24, 0, -1, -1, -6, 2, 4, 2]]),
    P(0, 0, 0, [[16, 20, -4, 0, -3, 8, 12, 6], [0, 38, -4, 0, -3, 8, 20, 6, 0.5]]),
    ...(zombieArms ? [P(-5, 2, 0, [[44, 22, -3, -2, -2, 4, 12, 4]], -HP), P(5, 2, 0, [[44, 22, -1, -2, -2, 4, 12, 4]], -HP)]
      : [P(0, 3, -1, [[44, 22, -8, -2, -2, 4, 8, 4], [44, 22, 4, -2, -2, 4, 8, 4], [40, 38, -4, 2, -2, 8, 4, 4]], -0.75)]),
    LEG(-2, 12, 0, 0, 22, -2, 0, -2, 4, 12, 4), LEG(2, 12, 0, 0, 22, -2, 0, -2, 4, 12, 4),
  ];
  const M_ENDERMAN = [
    P(0, -14, 0, [[0, 0, -4, -8, -4, 8, 8, 8]]), P(0, -14, 0, [[32, 16, -4, 0, -2, 8, 12, 4]]),
    P(-5, -12, 0, [[56, 0, -1, -2, -1, 2, 30, 2]]), P(5, -12, 0, [[56, 0, -1, -2, -1, 2, 30, 2]]),
    LEG(-2, -6, 0, 56, 0, -1, 0, -1, 2, 30, 2), LEG(2, -6, 0, 56, 0, -1, 0, -1, 2, 30, 2),
  ];
  const M_PIGLIN = (armsForward) => {
    const m = M_HUMAN(armsForward, [32, 48], [16, 48]);
    m[0] = P(0, 0, 0, [[0, 0, -5, -8, -4, 10, 8, 8], [31, 1, -2, -4, -5, 4, 4, 1], [2, 4, 2, -2, -5, 1, 2, 1], [2, 0, -3, -2, -5, 1, 2, 1]],
      0, 0, 0, [P(4.5, -6, 0, [[51, 6, 0, 0, -2, 1, 5, 4]], 0, 0, -0.52), P(-4.5, -6, 0, [[39, 6, -1, 0, -2, 1, 5, 4]], 0, 0, 0.52)]);
    return m;
  };
  const M_WOLF = [
    P(-1, 13.5, -7, [[0, 0, -2, -3, -2, 6, 6, 4], [16, 14, -2, -5, 0, 2, 2, 1], [16, 14, 2, -5, 0, 2, 2, 1], [0, 10, -0.5, -0.001, -5, 3, 3, 4]]),
    P(0, 14, 2, [[18, 14, -3, -2, -3, 6, 9, 6]], HP),
    P(-1, 14, -3, [[21, 0, -3, -3, -3, 8, 6, 7]], HP),
    ...legs4(0, 18, 0, 0, -1, 2, 8, 2, [[-2.5, 16, 7], [0.5, 16, 7], [-2.5, 16, -4], [0.5, 16, -4]]),
    P(-1, 12, 8, [[9, 18, 0, 0, -1, 2, 8, 2]], 0.63),
  ];
  const M_FOX = [
    P(-1, 16.5, -3, [[1, 5, -3, -2, -5, 8, 6, 6], [8, 1, -3, -4, -4, 2, 2, 1], [15, 1, 3, -4, -4, 2, 2, 1], [6, 18, -1, 2.01, -8, 4, 2, 3]]),
    P(0, 16, -6, [[24, 15, -3, 3.999, -3.5, 6, 11, 6]], HP, 0, 0, [P(-4, 15, -1, [[30, 0, 2, 0, -1, 4, 9, 5]], -0.05)]),
    ...legs4(0, 0, 2, 0.5, -1, 2, 6, 2, [[-5, 17.5, 7], [-1, 17.5, 7], [-5, 17.5, 0], [-1, 17.5, 0]], [[13, 24], [4, 24], [13, 24], [4, 24]]),
  ];
  const M_CAT = [
    P(0, 15, -9, [[0, 0, -2.5, -2, -3, 5, 4, 5], [0, 24, -1.5, -0.02, -4, 3, 2, 2], [0, 10, -2, -3, 0, 1, 1, 2], [6, 10, 1, -3, 0, 1, 1, 2]]),
    P(0, 12, -10, [[20, 0, -2, 3, -8, 4, 16, 6]], HP),
    P(0, 15, 8, [[0, 15, -0.5, 0, 0, 1, 8, 1]], 0.9), P(0, 20, 14, [[4, 15, -0.5, 0, 0, 1, 8, 1]], 1.7),
    LEG(1.1, 18, 5, 8, 13, -1, 0, 1, 2, 6, 2), LEG(-1.1, 18, 5, 8, 13, -1, 0, 1, 2, 6, 2),
    LEG(1.2, 14.1, -5, 40, 0, -1, 0, 0, 2, 10, 2), LEG(-1.2, 14.1, -5, 40, 0, -1, 0, 0, 2, 10, 2),
  ];
  const M_RABBIT = [
    P(0, 19, 8, [[0, 0, -3, -2, -10, 6, 5, 10]], -0.35),
    P(0, 16, -1, [[32, 0, -2.5, -4, -5, 5, 4, 5], [52, 0, -2.5, -9, -1, 2, 5, 1], [58, 0, 0.5, -9, -1, 2, 5, 1], [32, 9, -0.5, -2.5, -5.5, 1, 1, 1]]),
    P(0, 20, 7, [[52, 6, -1.5, -1.5, 0, 3, 3, 2]]),
    P(3, 17.5, 3.7, [[16, 15, -1, 0, 0, 2, 4, 5]], -0.35), P(-3, 17.5, 3.7, [[30, 15, -1, 0, 0, 2, 4, 5]], -0.35),
    P(3, 17.5, 3.7, [[26, 24, -1, 5.5, -3.7, 2, 1, 7]]), P(-3, 17.5, 3.7, [[8, 24, -1, 5.5, -3.7, 2, 1, 7]]),
    P(3, 17, -1, [[8, 15, -1, 0, -1, 2, 7, 2]], -0.17), P(-3, 17, -1, [[0, 15, -1, 0, -1, 2, 7, 2]], -0.17),
  ];
  const M_HORSE = [
    P(0, 11, 5, [[0, 32, -5, -8, -17, 10, 10, 22, 0.05]], 0, 0, 0, [P(0, -5, 2, [[42, 36, -1.5, 0, 0, 3, 14, 4]], Math.PI / 6)]),
    P(0, 4, -12, [[0, 35, -2, -11, -7, 4, 12, 7], [0, 13, -3, -11, -7, 6, 5, 7], [56, 36, -1, -11, 5.01, 2, 16, 2],
                  [0, 25, -2, -11, -12, 4, 5, 5], [19, 16, 0.55, -13, 4, 2, 3, 1], [19, 16, -2.55, -13, 4, 2, 3, 1]], Math.PI / 6),
    LEG(4, 14, 7, 48, 21, -3, -1.01, -1, 4, 11, 4), LEG(-4, 14, 7, 48, 21, -1, -1.01, -1, 4, 11, 4),
    LEG(4, 14, -12, 48, 21, -3, -1.01, -1.9, 4, 11, 4), LEG(-4, 14, -12, 48, 21, -1, -1.01, -1.9, 4, 11, 4),
  ];
  const M_LLAMA = [
    P(0, 7, -6, [[0, 0, -2, -14, -10, 4, 4, 9], [0, 14, -4, -16, -6, 8, 18, 6], [17, 0, -4, -19, -4, 3, 3, 2], [17, 0, 1, -19, -4, 3, 3, 2]]),
    P(0, 5, 2, [[29, 0, -6, -10, -7, 12, 18, 10]], HP),
    ...legs4(29, 29, -2, 0, -2, 4, 14, 4, [[-3.5, 10, 6], [3.5, 10, 6], [-3.5, 10, -5], [3.5, 10, -5]]),
  ];
  const M_GOAT = [
    P(1, 14, 0, [[12, 55, -0.01, -16, -10.5, 2, 7, 2], [12, 55, -2.99, -16, -10.5, 2, 7, 2], [2, 61, -6, -11, -10, 3, 2, 1], [2, 61, 2, -11, -10, 3, 2, 1]],
      0, 0, 0, [P(0, -8, -8, [[34, 46, -3, -4, -8, 5, 7, 10]], 0.9599)]),
    P(0, 24, 0, [[1, 1, -4, -17, -7, 9, 11, 16], [0, 28, -5, -18, -8, 11, 14, 11]]),
    LEG(1, 14, 4, 36, 29, 0, 4, 0, 3, 6, 3), LEG(-3, 14, 4, 49, 29, 0, 4, 0, 3, 6, 3),
    LEG(-3, 14, -6, 49, 2, 0, 0, 0, 3, 10, 3), LEG(1, 14, -6, 35, 2, 0, 0, 0, 3, 10, 3),
  ];
  const M_POLAR = [
    P(0, 10, -16, [[0, 0, -3.5, -3, -3, 7, 7, 7], [0, 44, -2.5, 1, -6, 5, 3, 3], [26, 0, -4.5, -4, -1, 2, 2, 1], [26, 0, 2.5, -4, -1, 2, 2, 1]]),
    P(-2, 9, 12, [[0, 19, -5, -13, -7, 14, 14, 11], [39, 0, -4, -25, -7, 12, 12, 10]], HP),
    LEG(-4.5, 14, 6, 50, 22, -2, 0, -2, 4, 10, 8), LEG(4.5, 14, 6, 50, 22, -2, 0, -2, 4, 10, 8),
    LEG(-3.5, 14, -8, 50, 40, -2, 0, -2, 4, 10, 6), LEG(3.5, 14, -8, 50, 40, -2, 0, -2, 4, 10, 6),
  ];
  const M_PANDA = [
    P(0, 11.5, -17, [[0, 6, -6.5, -5, -4, 13, 10, 9], [45, 16, -3.5, 0, -6, 7, 5, 2], [52, 25, -8.5, -8, -1, 5, 4, 1], [52, 25, 3.5, -8, -1, 5, 4, 1]]),
    P(0, 10, 0, [[0, 25, -9.5, -13, -6.5, 19, 26, 13]], HP),
    ...legs4(40, 0, -3, 0, -3, 6, 9, 6, [[-5.5, 15, 9], [5.5, 15, 9], [-5.5, 15, -9], [5.5, 15, -9]]),
  ];
  const M_IRON_GOLEM = [
    P(0, -7, -2, [[0, 0, -4, -12, -5.5, 8, 10, 8], [24, 0, -1, -5, -7.5, 2, 4, 2]]),
    P(0, -7, 0, [[0, 40, -9, -2, -6, 18, 12, 11], [0, 70, -4.5, 10, -3, 9, 5, 6, 0.5]]),
    P(0, -7, 0, [[60, 21, -13, -2.5, -3, 4, 30, 6]]), P(0, -7, 0, [[60, 58, 9, -2.5, -3, 4, 30, 6]]),
    LEG(-4, 11, 0, 37, 0, -3.5, -3, -3, 6, 16, 5), LEG(5, 11, 0, 60, 0, -3.5, -3, -3, 6, 16, 5),
  ];
  const M_SNOW_GOLEM = [
    P(0, 4, 0, [[0, 0, -4, -8, -4, 8, 8, 8, -0.5]]), P(0, 13, 0, [[0, 16, -5, -10, -5, 10, 10, 10, -0.5]]),
    P(0, 24, 0, [[0, 36, -6, -12, -6, 12, 12, 12, -0.5]]),
    P(5, 6, 1, [[32, 0, -1, 0, -1, 12, 2, 2, -0.5]], 0, 0, 1.0), P(-5, 6, -1, [[32, 0, -1, 0, -1, 12, 2, 2, -0.5]], 0, Math.PI, -1.0),
  ];
  const M_BEE = [
    P(0, 19, 0, [[0, 0, -3.5, -4, -5, 7, 7, 10], [26, 7, 0, -1, 5, 0, 1, 2], [2, 0, 1.5, -6, -8, 1, 2, 3], [2, 3, -2.5, -6, -8, 1, 2, 3]], 0, 0, 0, [
      P(-1.5, -4, -3, [[0, 18, -9, 0, 0, 9, 0, 6]], 0, -0.26, 0), P(1.5, -4, -3, [[0, 18, 0, 0, 0, 9, 0, 6]], 0, 0.26, 0),
      P(1.5, 3, 0, [[26, 1, -5, 0, 0, 7, 2, 0], [26, 3, -5, 0, 0, 7, 2, 0], [26, 5, -5, 0, 0, 7, 2, 0]].map((c, i) => (c[4] = -2 + i * 2, c)))]),
  ];
  const M_SQUID = [
    P(0, 8, 0, [[0, 0, -6, -8, -6, 12, 16, 12]]),
    ...[0, 1, 2, 3, 4, 5, 6, 7].map(i => { const a = i * Math.PI / 4;
      return P(Math.cos(a) * 5, 15, Math.sin(a) * 5, [[48, 0, -1, 0, -1, 2, 18, 2]], 0, i * -Math.PI / 4 + HP, 0); }),
  ];
  const M_SLIME = [P(0, 0, 0, [[0, 16, -3, 17, -3, 6, 6, 6], [32, 0, -3.25, 18, -3.5, 2, 2, 2], [32, 4, 1.25, 18, -3.5, 2, 2, 2], [32, 8, 0, 21, -3.5, 1, 1, 1]])];
  const M_SLIME_OUTER = [P(0, 0, 0, [[0, 0, -4, 16, -4, 8, 8, 8]])];
  const M_MAGMA = [P(0, 0, 0, [0, 1, 2, 3, 4, 5, 6, 7].map(i => [i === 2 || i === 3 ? 24 : 0, i === 2 ? 10 : i === 3 ? 19 : i, -4, 16 + i, -4, 8, 1, 8])
    .concat([[0, 16, -2, 18, -2, 4, 4, 4]]))];
  const M_BLAZE = [
    P(0, 0, 0, [[0, 0, -4, -4, -4, 8, 8, 8]]),
    ...[[9, -2], [7, 2], [5, 11]].flatMap(([r, y], ring) => [0, 1, 2, 3].map(k => { const a = k * HP + ring * 0.7;
      return P(Math.cos(a) * r, y, Math.sin(a) * r, [[0, 16, 0, 0, 0, 2, 8, 2]]); })),
  ];
  const M_GHAST = [
    P(0, 17.6, 0, [[0, 0, -8, -8, -8, 16, 16, 16]]),
    ...[[-3.8, -5, 11], [1.3, -5, 9], [6.3, -5, 13], [-6.3, 0, 10], [-1.3, 0, 12], [3.8, 0, 8], [-3.8, 5, 14], [1.3, 5, 10], [6.3, 5, 12]]
      .map(([x, z, l]) => P(x, 24.6, z, [[0, 0, -1, 0, -1, 2, l, 2]])),
  ];
  const M_COD = [
    P(0, 22, 0, [[0, 0, -1, -2, 0, 2, 4, 7], [11, 0, -1, -2, -3, 2, 4, 3]]), P(0, 22, -3, [[0, 0, -1, -2, -1, 2, 3, 1]]),
    P(0, 22, 7, [[22, 3, 0, -2, 0, 0, 4, 4]]), P(0, 20, 0, [[20, -6, 0, -1, -1, 0, 1, 6]]),
  ];
  const M_SALMON = [
    P(0, 20, 0, [[0, 0, -1.5, -2.5, 0, 3, 5, 8], [22, 0, -1, -2, -3, 2, 4, 3]]), P(0, 20, 8, [[0, 13, -1.5, -2.5, 0, 3, 5, 8]]),
    P(0, 20, 16, [[20, 10, 0, -2.5, 0, 0, 5, 6]]), P(0, 17.5, 2, [[4, 2, 0, -1, 0, 0, 2, 3]]),
  ];
  const M_PUFFER = [P(0, 22, 0, [[0, 27, -1.5, -2, -1.5, 3, 2, 3], [24, 6, -1.5, -1, -1.5, 3, 1, 1]])];
  const M_TROPICAL = [P(0, 22, 0, [[0, 0, -1, -1.5, -3, 2, 3, 6], [10, -5, 0, -4.5, -1, 0, 3, 6]]), P(0, 22, 3, [[22, -6, 0, -1.5, 0, 0, 3, 6]])];
  const M_DOLPHIN = [
    P(0, 22, -5, [[22, 0, -4, -7, 0, 8, 7, 13]], 0, 0, 0, [
      P(0, -4, -3, [[0, 0, -4, -3, -3, 8, 7, 6], [0, 13, -1, 2, -7, 2, 2, 4]]),
      P(0, 0, 11, [[0, 19, -2, -2.5, -1, 4, 5, 11]], 0, 0, 0, [P(0, 0, 9, [[19, 20, -5, -0.5, 0, 10, 1, 6]])]),
      P(0, -7, 4, [[51, 0, -0.5, 0, 0, 1, 4, 5]], Math.PI / 3)]),
  ];
  const M_TURTLE = [
    P(0, 19, -10, [[3, 0, -3, -1, -3, 6, 5, 6]]),
    P(0, 11, -10, [[7, 37, -9.5, 3, -10, 19, 20, 6], [31, 1, -5.5, 3, -13, 11, 18, 3]], HP),
    LEG(-3.5, 22, 11, 1, 23, -2, 0, 0, 4, 1, 10), LEG(3.5, 22, 11, 1, 12, -2, 0, 0, 4, 1, 10),
    LEG(-5, 21, -4, 27, 30, -13, 0, -2, 13, 1, 5), LEG(5, 21, -4, 27, 24, 0, 0, -2, 13, 1, 5),
  ];
  const M_HOGLIN = [
    P(0, 7, 0, [[1, 1, -8, -7, -13, 16, 14, 26]], 0, 0, 0, [P(0, -14, -5, [[90, 33, 0, 0, -9, 0, 10, 19, 0.001]])]),
    P(0, 2, -12, [[61, 1, -7, -3, -19, 14, 6, 19]], 0.87, 0, 0, [
      P(-6, -2, -3, [[1, 1, -6, -1, -2, 6, 1, 4]], 0, 0, -0.69), P(6, -2, -3, [[1, 6, 0, -1, -2, 6, 1, 4]], 0, 0, 0.69),
      P(-7, 2, -12, [[10, 13, -1, -11, -1, 2, 11, 2]]), P(7, 2, -12, [[1, 13, -1, -11, -1, 2, 11, 2]])]),
    LEG(-4, 10, -8.5, 66, 42, -3, 0, -3, 6, 14, 6), LEG(4, 10, -8.5, 41, 42, -3, 0, -3, 6, 14, 6),
    LEG(-5, 13, 10, 21, 45, -2.5, 0, -2.5, 5, 11, 5), LEG(5, 13, 10, 0, 45, -2.5, 0, -2.5, 5, 11, 5),
  ];
  const M_RAVAGER = [
    P(0, 1, 2, [[0, 55, -7, -10, -7, 14, 16, 20], [0, 91, -6, 6, -7, 12, 13, 18]], HP),
    P(0, -7, 5.5, [[68, 73, -5, -1, -18, 10, 10, 18]], 0, 0, 0, [
      P(0, 16, -17, [[0, 0, -8, -20, -14, 16, 20, 16], [0, 0, -2, -6, -18, 4, 8, 4]], 0, 0, 0, [
        P(-10, -14, -8, [[74, 55, 0, -14, -2, 2, 14, 4]], 1.0995), P(8, -14, -8, [[74, 55, 0, -14, -2, 2, 14, 4]], 1.0995),
        P(0, -2, 2, [[0, 36, -8, 0, -16, 16, 3, 16]])])]),
    LEG(-8, -13, 18, 96, 0, -4, 0, -4, 8, 37, 8), LEG(8, -13, 18, 96, 0, -4, 0, -4, 8, 37, 8),
    LEG(-8, -13, -4, 64, 0, -4, 0, -4, 8, 37, 8), LEG(8, -13, -4, 64, 0, -4, 0, -4, 8, 37, 8),
  ];
  const M_PHANTOM = [
    P(0, 0, 0, [[0, 8, -3, -2, -8, 5, 3, 9]], -0.1, 0, 0, [
      P(0, -2, 1, [[3, 20, -1.5, 0, 0, 3, 2, 6]], 0, 0, 0, [P(0, 0.5, 6, [[4, 29, -0.5, 0, 0, 1, 1, 6]])]),
      P(2, -2, -8, [[23, 12, 0, 0, 0, 6, 2, 9]], 0, 0, 0.1, [P(6, 0, 0, [[16, 24, 0, 0, 0, 13, 1, 9]], 0, 0, 0.1)]),
      P(-3, -2, -8, [[23, 12, -6, 0, 0, 6, 2, 9]], 0, 0, -0.1, [P(-6, 0, 0, [[16, 24, -13, 0, 0, 13, 1, 9]], 0, 0, -0.1)]),
      P(0, 1, -7, [[0, 0, -4, -2, -5, 7, 3, 5]], 0.2)]),
  ];
  const M_GUARDIAN = [
    P(0, 0, 0, [[0, 0, -6, 10, -8, 12, 12, 16], [0, 28, -8, 10, -6, 2, 12, 12], [0, 28, 6, 10, -6, 2, 12, 12],
                [16, 40, -6, 8, -6, 12, 2, 12], [16, 40, -6, 22, -6, 12, 2, 12]]),
    P(0, 0, -8.25, [[8, 0, -1, 15, 0, 2, 2, 1]]),
    P(0, 0, 0, [[40, 0, -2, 14, 7, 4, 4, 8]], 0, 0, 0, [P(-1.5, 0.5, 14, [[0, 54, 0, 14, 0, 3, 3, 7]], 0, 0, 0,
      [P(0.5, 1, 6, [[41, 32, 0, 14, 0, 2, 2, 6], [25, 19, 1, 10.5, 3, 1, 9, 9]])])]),
  ];
  const M_SHULKER = [P(0, 24, 0, [[0, 0, -8, -16, -8, 16, 12, 16]]), P(0, 24, 0, [[0, 28, -8, -8, -8, 16, 8, 16]]), P(0, 12, 0, [[0, 52, -3, 0, -3, 6, 6, 6]])];
  const segs = (sizes, uvs) => { let z = -3.5; return sizes.map((s, i) => { const p = P(0, 24 - s[1], z, [[uvs[i][0], uvs[i][1], -s[0] / 2, 0, -s[2] / 2, s[0], s[1], s[2]]]);
    if (i + 1 < sizes.length) z += (s[2] + sizes[i + 1][2]) * 0.5; return p; }); };
  const M_SILVERFISH = segs([[3, 2, 2], [4, 3, 2], [6, 4, 3], [3, 3, 3], [2, 2, 3], [2, 1, 2], [1, 1, 2]], [[0, 0], [0, 4], [0, 9], [0, 16], [0, 22], [11, 0], [13, 4]]);
  const M_ENDERMITE = segs([[4, 3, 2], [6, 4, 5], [3, 3, 1], [1, 2, 1]], [[0, 0], [0, 5], [0, 14], [0, 18]]);
  const M_ALLAY = [
    P(0, 19.5, 0, [[0, 0, -2.5, -5, -2.5, 5, 5, 5]]),
    P(0, 19.5, 0, [[0, 10, -1.5, 0, -1, 3, 4, 2], [0, 16, -1.5, 0, -1, 3, 5, 2, -0.2]], 0, 0, 0, [
      P(-1.75, 0.5, 0, [[23, 0, -0.75, -0.5, -1, 1, 4, 2, -0.01]]), P(1.75, 0.5, 0, [[23, 6, -0.25, -0.5, -1, 1, 4, 2, -0.01]]),
      P(-0.5, 0, 0.6, [[16, 14, 0, 1, 0, 0, 5, 8]], 0, -0.6), P(0.5, 0, 0.6, [[16, 14, 0, 1, 0, 0, 5, 8]], 0, 0.6)]),
  ];
  const M_WARDEN = [
    P(0, 3, 0, [[0, 0, -9, -13, -4, 18, 21, 11]], 0, 0, 0, [
      P(-7, -2, -4, [[90, 11, -2, -11, -0.1, 9, 21, 0]]), P(7, -2, -4, [[90, 11, -7, -11, -0.1, 9, 21, 0]]),
      P(0, -13, 0, [[0, 32, -8, -16, -5, 16, 16, 10]]),
      P(-13, -13, 1, [[44, 50, -4, 0, -4, 8, 28, 8]]), P(13, -13, 1, [[0, 58, -4, 0, -4, 8, 28, 8]])]),
    LEG(-5.9, 11, 0, 76, 48, -3.1, 0, -3, 6, 13, 6), LEG(5.9, 11, 0, 76, 76, -2.9, 0, -3, 6, 13, 6),
  ];
  const M_CAMEL = [
    P(0, 4, 9.5, [[0, 25, -7.5, -12, -23.5, 15, 12, 27]], 0, 0, 0, [
      P(0, -12, -10, [[74, 0, -4.5, -5, -5.5, 9, 5, 11]]), P(0, -9, 3.5, [[122, 0, -1.5, 0, 0, 3, 14, 0]]),
      P(0, -3, -19.5, [[60, 24, -3.5, -7, -15, 7, 8, 19], [21, 0, -3.5, -21, -15, 7, 14, 7], [50, 0, -2.5, -21, -21, 5, 5, 6],
                       [45, 0, 2.5, -20.5, -10.5, 3, 1, 2], [67, 0, -5.5, -20.5, -10.5, 3, 1, 2]])]),
    LEG(4.9, 1, 9.5, 58, 16, -2.5, 2, -2.5, 5, 21, 5), LEG(-4.9, 1, 9.5, 94, 16, -2.5, 2, -2.5, 5, 21, 5),
    LEG(4.9, 1, -10.5, 0, 0, -2.5, 2, -2.5, 5, 21, 5), LEG(-4.9, 1, -10.5, 0, 26, -2.5, 2, -2.5, 5, 21, 5),
  ];
  const M_SNIFFER = [
    P(0, 5, 0, [[62, 68, -12.5, -14, -20, 25, 29, 40], [62, 0, -12.5, -14, -20, 25, 24, 40, 0.5]], 0, 0, 0, [
      P(0, 6.5, -19.48, [[8, 15, -6.5, -7.5, -11.5, 13, 18, 11]], 0, 0, 0, [
        P(6.51, -7.5, -4.51, [[2, 0, 0, 0, -3, 1, 19, 7]]), P(-6.51, -7.5, -4.51, [[48, 0, -1, 0, -3, 1, 19, 7]]),
        P(0, -4.5, -11.5, [[10, 45, -6.5, -2, -9, 13, 2, 9]]), P(0, 2.5, -12.5, [[10, 57, -6.5, -7, -8, 13, 12, 9]])])]),
    LEG(-7.5, 15, -15, 32, 87, -3.5, -1, -4, 7, 10, 8), LEG(-7.5, 15, 0, 32, 105, -3.5, -1, -4, 7, 10, 8), LEG(-7.5, 15, 15, 32, 123, -3.5, -1, -4, 7, 10, 8),
    LEG(7.5, 15, -15, 0, 87, -3.5, -1, -4, 7, 10, 8), LEG(7.5, 15, 0, 0, 105, -3.5, -1, -4, 7, 10, 8), LEG(7.5, 15, 15, 0, 123, -3.5, -1, -4, 7, 10, 8),
  ];
  const M_ARMADILLO = [
    P(0, 21, 4, [[0, 20, -4, -7, -10, 8, 8, 12], [0, 40, -4, -7, -10, 8, 8, 12, 0.3]], 0, 0, 0, [
      P(0, -3, 1, [[44, 53, -0.5, -0.0865, 0.0933, 1, 6, 1]], 0.5061), P(0, -2, -11, [[43, 15, -1.5, -1, -1, 3, 5, 2]])]),
    LEG(-2, 21, 4, 51, 31, -1, 0, -1, 2, 3, 2), LEG(2, 21, 4, 42, 31, -1, 0, -1, 2, 3, 2),
    LEG(-2, 21, -4, 51, 43, -1, 0, -1, 2, 3, 2), LEG(2, 21, -4, 42, 43, -1, 0, -1, 2, 3, 2),
  ];
  const M_FROG = [
    P(0, 22, 4, [[3, 1, -3.5, -2, -8, 7, 3, 9], [23, 22, -3.5, -1, -8, 7, 0, 9]], 0, 0, 0, [
      P(0, -2, -1, [[23, 13, -3.5, -1, -7, 7, 0, 9], [0, 13, -3.5, -2, -7, 7, 3, 9]], 0, 0, 0, [
        P(-2, -3, -5, [[0, 0, -1.5, -1, -1.5, 3, 2, 3]]), P(2, -3, -5, [[14, 0, -1.5, -1, -1.5, 3, 2, 3]])]),
      P(4, -1, -6.5, [[0, 32, -1, 0, -1, 2, 3, 3]]), P(-4, -1, -6.5, [[0, 38, -1, 0, -1, 2, 3, 3]])]),
    P(3.5, 21, 4, [[14, 25, -1, 0, -2, 3, 3, 4]]), P(-3.5, 21, 4, [[0, 25, -2, 0, -2, 3, 3, 4]]),
  ];
  const M_TADPOLE = [P(0, 22, -3, [[0, 0, -1.5, -1, 0, 3, 2, 3]]), P(0, 22, 0, [[0, 0, 0, -1, 0, 0, 2, 7]])];
  const M_AXOLOTL = [
    P(0, 20, 5, [[0, 11, -4, -2, -9, 8, 4, 10], [2, 17, 0, -3, -8, 0, 5, 9]], 0, 0, 0, [
      P(0, 0, -9, [[0, 1, -4, -3, -5, 8, 5, 5]]), P(0, 0, 1, [[2, 19, 0, -3, 0, 0, 5, 12]])]),
    LEG(-3.5, 21, 4, 2, 13, -1, 0, 0, 3, 5, 0), LEG(3.5, 21, 4, 2, 13, -2, 0, 0, 3, 5, 0),
    LEG(-3.5, 21, -3, 2, 13, -1, 0, 0, 3, 5, 0), LEG(3.5, 21, -3, 2, 13, -2, 0, 0, 3, 5, 0),
  ];
  const M_STRIDER = [P(0, 1, 0, [[0, 0, -8, -6, -8, 16, 14, 16]]), LEG(-4, 8, 0, 0, 32, -2, 0, -2, 4, 16, 4), LEG(4, 8, 0, 0, 55, -2, 0, -2, 4, 16, 4)];
  const M_BAT = [
    P(0, 17, 0, [[0, 0, -1.5, 0, -1, 3, 5, 2], [16, 16, -1.5, 5, 0, 3, 2, 0]], 0, 0, 0, [
      P(-1.5, 0, 0, [[12, 0, -2, -2, 0, 2, 7, 0]], 0, 0, 0, [P(-2, 0, 0, [[16, 0, -6, -2, 0, 6, 8, 0]])]),
      P(1.5, 0, 0, [[12, 7, 0, -2, 0, 2, 7, 0]], 0, 0, 0, [P(2, 0, 0, [[16, 8, 0, -2, 0, 6, 8, 0]])])]),
    P(0, 17, 0, [[0, 7, -2, -3, -1, 4, 3, 2]]),
  ];
  const M_PARROT = [
    P(0, 16.5, -3, [[2, 8, -1.5, 0, -1.5, 3, 6, 3]], 0.4937),
    P(0, 21.07, 1.16, [[22, 1, -1.5, -1, -1, 3, 4, 1]], 1.015),
    P(1.5, 16.94, -2.76, [[19, 8, -0.5, 0, -1.5, 1, 5, 3]], -0.6981), P(-1.5, 16.94, -2.76, [[19, 8, -0.5, 0, -1.5, 1, 5, 3]], -0.6981),
    P(0, 15.69, -2.76, [[2, 2, -1, -1.5, -1, 2, 3, 2]], 0, 0, 0, [
      P(0, -2, -1, [[10, 0, -1, -0.5, -2, 2, 1, 4]]), P(0, -0.5, -1.5, [[11, 7, -0.5, -1, -0.5, 1, 2, 1]]),
      P(0, -1.75, -2.45, [[16, 7, -0.5, 0, -0.5, 1, 2, 1]]), P(0, -2.15, 0.15, [[2, 18, 0, -4, -2, 0, 5, 4]], -0.2214)]),
    LEG(1, 22, -1.05, 14, 18, -0.5, 0, -0.5, 1, 2, 1), LEG(-1, 22, -1.05, 14, 18, -0.5, 0, -0.5, 1, 2, 1),
  ];
  const M_WITHER = [
    P(0, 0, 0, [[0, 16, -10, 3.9, -0.5, 20, 3, 3]]),
    P(-2, 6.9, -0.5, [[0, 22, 0, 0, 0, 3, 10, 3], [24, 22, -4, 1.5, 0.5, 11, 2, 2], [24, 22, -4, 4, 0.5, 11, 2, 2], [24, 22, -4, 6.5, 0.5, 11, 2, 2]], 0.2),
    P(-2, 16.4, 1.6, [[12, 22, 0, 0, 0, 3, 6, 3]], 0.6),
    P(0, 0, 0, [[0, 0, -4, -4, -4, 8, 8, 8]]), P(-8, 4, 0, [[32, 0, -4, -4, -4, 6, 6, 6]]), P(10, 4, 0, [[32, 0, -4, -4, -4, 6, 6, 6]]),
  ];

  // Mob-ID → [Modell, Textur (Pfad unter textures/entity), Größe] – Textur darf eine Funktion (Mob-Daten) sein
  const nm = v => (typeof v === 'string' ? v.replace(/^.*:/, '') : v);
  const pick = (arr, v, d) => arr[v] !== undefined ? arr[v] : d;
  const HORSE_COL = ['white', 'creamy', 'chestnut', 'brown', 'black', 'gray', 'darkbrown'];
  const HORSE_MARK = [null, 'white', 'whitefield', 'whitedots', 'blackdots'];
  const MODELS = {
    zombie: [M_HUMAN(true, [32, 48], [16, 48]), 'zombie/zombie'], husk: [M_HUMAN(true, [32, 48], [16, 48]), 'zombie/husk', 1.0625],
    drowned: [M_HUMAN(true, [32, 48], [16, 48]), 'zombie/drowned'],
    skeleton: [M_SKELETON, 'skeleton/skeleton'], stray: [M_SKELETON, 'skeleton/stray'], bogged: [M_SKELETON, 'skeleton/bogged'],
    wither_skeleton: [M_SKELETON, 'skeleton/wither_skeleton', 1.2],
    creeper: [M_CREEPER, 'creeper/creeper'], cow: [M_COW, 'cow/cow'],
    mooshroom: [M_COW, m => 'cow/' + (nm(m.v) || 'red') + '_mooshroom'],
    pig: [M_PIG, 'pig/pig'], sheep: [M_SHEEP, 'sheep/sheep'], chicken: [M_CHICKEN, 'chicken'],
    spider: [M_SPIDER, 'spider/spider'], cave_spider: [M_SPIDER, 'spider/cave_spider', 0.7],
    villager: [M_VILLAGER(false), 'villager/villager'], wandering_trader: [M_VILLAGER(false), 'wandering_trader'],
    zombie_villager: [M_VILLAGER(true), 'zombie_villager/zombie_villager'], witch: [M_VILLAGER(false), 'witch'],
    pillager: [M_VILLAGER(false), 'illager/pillager'], vindicator: [M_VILLAGER(false), 'illager/vindicator'],
    evoker: [M_VILLAGER(false), 'illager/evoker'], illusioner: [M_VILLAGER(false), 'illager/illusioner'],
    enderman: [M_ENDERMAN, 'enderman/enderman'],
    piglin: [M_PIGLIN(false), 'piglin/piglin'], piglin_brute: [M_PIGLIN(false), 'piglin/piglin_brute'],
    zombified_piglin: [M_PIGLIN(true), 'piglin/zombified_piglin'],
    wolf: [M_WOLF, m => { const n = nm(m.v) || 'pale'; return 'wolf/wolf' + (n === 'pale' ? '' : '_' + n) + (m.tm ? '_tame' : ''); }],
    fox: [M_FOX, m => (m.v === 'snow' ? 'fox/snow_fox' : 'fox/fox')],
    cat: [M_CAT, m => 'cat/' + (nm(m.v) || 'tabby'), 0.8], ocelot: [M_CAT, 'cat/ocelot'],
    rabbit: [M_RABBIT, m => 'rabbit/' + pick({ 0: 'brown', 1: 'white', 2: 'black', 3: 'white_splotched', 4: 'gold', 5: 'salt', 99: 'caerbannog' }, m.v, 'brown'), 0.6],
    horse: [M_HORSE, m => 'horse/horse_' + pick(HORSE_COL, (m.v || 0) & 255, 'brown'), 1.1],
    donkey: [M_HORSE, 'horse/donkey', 0.87], mule: [M_HORSE, 'horse/mule', 0.92],
    skeleton_horse: [M_HORSE, 'horse/horse_skeleton'], zombie_horse: [M_HORSE, 'horse/horse_zombie'],
    llama: [M_LLAMA, m => 'llama/' + pick(['creamy', 'white', 'brown', 'gray'], m.v, 'creamy')],
    trader_llama: [M_LLAMA, m => 'llama/' + pick(['creamy', 'white', 'brown', 'gray'], m.v, 'creamy')],
    goat: [M_GOAT, 'goat/goat'], camel: [M_CAMEL, 'camel/camel'], polar_bear: [M_POLAR, 'bear/polarbear'],
    panda: [M_PANDA, 'panda/panda'], iron_golem: [M_IRON_GOLEM, 'iron_golem/iron_golem'], snow_golem: [M_SNOW_GOLEM, 'snow_golem'],
    bee: [M_BEE, 'bee/bee'], squid: [M_SQUID, 'squid/squid'], glow_squid: [M_SQUID, 'squid/glow_squid'],
    slime: [M_SLIME, 'slime/slime'], magma_cube: [M_MAGMA, 'slime/magmacube'], blaze: [M_BLAZE, 'blaze'],
    ghast: [M_GHAST, 'ghast/ghast', 4.5], cod: [M_COD, 'fish/cod'], salmon: [M_SALMON, 'fish/salmon'],
    pufferfish: [M_PUFFER, 'fish/pufferfish'], tropical_fish: [M_TROPICAL, 'fish/tropical_a'], dolphin: [M_DOLPHIN, 'dolphin'],
    turtle: [M_TURTLE, 'turtle/big_sea_turtle'], hoglin: [M_HOGLIN, 'hoglin/hoglin'], zoglin: [M_HOGLIN, 'hoglin/zoglin'],
    ravager: [M_RAVAGER, 'illager/ravager'], phantom: [M_PHANTOM, 'phantom'], guardian: [M_GUARDIAN, 'guardian'],
    elder_guardian: [M_GUARDIAN, 'guardian_elder', 2.35], shulker: [M_SHULKER, 'shulker/shulker'],
    silverfish: [M_SILVERFISH, 'silverfish'], endermite: [M_ENDERMITE, 'endermite'],
    allay: [M_ALLAY, 'allay/allay'], vex: [M_ALLAY, 'illager/vex'], warden: [M_WARDEN, 'warden/warden'],
    sniffer: [M_SNIFFER, 'sniffer/sniffer'], armadillo: [M_ARMADILLO, 'armadillo'],
    frog: [M_FROG, m => 'frog/' + (nm(m.v) || 'temperate') + '_frog'], tadpole: [M_TADPOLE, 'tadpole/tadpole'],
    axolotl: [M_AXOLOTL, m => 'axolotl/axolotl_' + pick(['lucy', 'wild', 'gold', 'cyan', 'blue'], m.v, 'lucy')],
    strider: [M_STRIDER, 'strider/strider'], bat: [M_BAT, 'bat'],
    parrot: [M_PARROT, m => 'parrot/parrot_' + pick(['red_blue', 'blue', 'green', 'yellow_blue', 'grey'], m.v, 'red_blue')],
    wither: [M_WITHER, 'wither/wither', 2],
  };
  const DYE = [0xf9fffe, 0xf9801d, 0xc74ebd, 0x3ab3da, 0xfed83d, 0x80c71f, 0xf38baa, 0x474f52,
               0x9d9d97, 0x169c9c, 0x8932b8, 0x3c44aa, 0x835432, 0x5e7c16, 0xb02e26, 0x1d1d21];
  function initMobs(BM, app, T, Group) {
    const group = new Group();
    group.name = 'felix-mobs';
    const mats = {}, geos = {};
    const mat = c => mats[c] || (mats[c] = new T.MeshBasicMaterial({ color: c }));
    const dark = c => { const r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
      return ((r * 0.7) << 16) | ((g * 0.7) << 8) | (b * 0.7); };
    const geo = (w, h, d) => { const k = w + '|' + h + '|' + d;
      return geos[k] || (geos[k] = new T.BoxGeometry(w, h, d)); };
    const hostileOutline = new T.MeshBasicMaterial({ color: 0xff2020, wireframe: true });
    let labels = [];

    const texCache = {};
    const loader = new T.TextureLoader();
    let SIZES = {};
    function texture(name) {
      if (!texCache[name]) {
        const t = loader.load('extra/tex/' + name + '.png', () => app.mapViewer.redraw());
        t.magFilter = T.NearestFilter; t.minFilter = T.NearestFilter; t.generateMipmaps = false;
        texCache[name] = t;
      }
      return texCache[name];
    }
    const texMats = {};
    function texMat(name, tint, translucent) {
      const k = name + '|' + (tint || '') + '|' + (translucent ? 1 : 0);
      if (!texMats[k]) texMats[k] = new T.MeshBasicMaterial(translucent
        ? { map: texture(name), transparent: true, opacity: 0.85, depthWrite: false, side: T.DoubleSide }
        : { map: texture(name), alphaTest: 0.5, side: T.DoubleSide, color: tint === undefined ? 0xffffff : tint });
      return texMats[k];
    }
    const geoCache = {};
    function cubeGeo(c, tw, th) {
      const [u, v, , , , w, h, d, inf = 0] = c;
      const k = c.join(',') + '|' + tw + 'x' + th;
      if (geoCache[k]) return geoCache[k];
      const g = new T.BoxGeometry(Math.max(w + inf * 2, 0.01) * PX, Math.max(h + inf * 2, 0.01) * PX, Math.max(d + inf * 2, 0.01) * PX);
      const uv = g.attributes.uv;
      const faces = [[u + d + w, v + d, d, h], [u, v + d, d, h], [u + d, v, w, d], [u + d + w, v, w, d],
                     [u + d, v + d, w, h], [u + d + w + d, v + d, w, h]];
      faces.forEach((f, i) => {
        const u0 = f[0] / tw, u1 = (f[0] + f[2]) / tw, v0 = 1 - f[1] / th, v1 = 1 - (f[1] + f[3]) / th;
        uv.setXY(i * 4, u0, v0); uv.setXY(i * 4 + 1, u1, v0); uv.setXY(i * 4 + 2, u0, v1); uv.setXY(i * 4 + 3, u1, v1);
      });
      return (geoCache[k] = g);
    }
    function addParts(parent, parts, mat, tw, th, root) {
      parts.forEach(([px, py, pz, rx, ry, rz, cubes, kids]) => {
        const pivot = new Group();
        pivot.position.set(px * PX, (root ? 24 - py : -py) * PX, -pz * PX);
        pivot.rotation.set(rx || 0, -(ry || 0), -(rz || 0));
        (cubes || []).forEach(c => {
          const mesh = new T.Mesh(cubeGeo(c, tw, th), mat);
          mesh.position.set((c[2] + c[5] / 2) * PX, -(c[3] + c[6] / 2) * PX, -(c[4] + c[7] / 2) * PX);
          pivot.add(mesh);
        });
        if (kids) addParts(pivot, kids, mat, tw, th, false);
        parent.add(pivot);
      });
    }
    function buildModel(parts, texName, tint, translucent) {
      const sz = SIZES[texName] || [64, 32];
      const g = new Group();
      addParts(g, parts, texMat(texName, tint, translucent), sz[0], sz[1], true);
      return g;
    }

    function makeMob(m) {
      const short = m.t.replace(/^minecraft:/, '');
      const mdl = MODELS[short];
      if (mdl) {
        const o = new Group();
        const tex = typeof mdl[1] === 'function' ? mdl[1](m) : mdl[1];
        o.add(buildModel(mdl[0], SIZES[tex] ? tex : (typeof mdl[1] === 'function' ? mdl[1]({}) : tex)));
        if (short === 'sheep' && !m.sh) o.add(buildModel(M_SHEEP_FUR, 'sheep/sheep_fur', DYE[m.c || 0]));
        if (short === 'slime') o.add(buildModel(M_SLIME_OUTER, 'slime/slime', undefined, true));
        if (short === 'horse') { const mk = HORSE_MARK[((m.v || 0) >> 8) & 255]; if (mk) o.add(buildModel(M_HORSE, 'horse/horse_markings_' + mk)); }
        let scale = (mdl[2] || 1) * (m.b ? 0.5 : 1);
        if (short === 'slime' || short === 'magma_cube') scale *= (m.sz || 0) + 1;
        // Mob-Modelle: 1 Pixel = 1/16 Block (PX ist für Spieler auf 0,9 verkleinert)
        o.scale.setScalar(scale / 0.9);
        o.position.set(m.x, m.y, m.z);
        o.rotation.y = -(m.r || 0) * Math.PI / 180;
        const def = MOB[short] || [];
        o.userData = { name: m.n || def[4] || GERMAN_FALLBACK(m.t), hostile: !!def[5] };
        return o;
      }
      const def = MOB[short] || [0.6, 1.0, m.t.includes(':') ? 0xb050d0 : 0x9a9a9a, 'b', GERMAN_FALLBACK(m.t)];
      let [w, h, col, shape] = def;
      const s = m.b ? 0.5 : 1;
      w *= s; h *= s;
      const o = new Group();
      if (shape === 'q') {            // Körper waagerecht + Kopf vorne + Beine
        const bh = h * 0.45, legH = h * 0.4, len = Math.max(w * 1.4, h * 0.9);
        const body = new T.Mesh(geo(w * 0.85, bh, len), mat(col)); body.position.y = legH + bh / 2; o.add(body);
        const hs = Math.min(w * 0.6, h * 0.45);
        const head = new T.Mesh(geo(hs, hs, hs), mat(dark(col))); head.position.set(0, legH + bh * 0.8, len / 2 + hs * 0.35); o.add(head);
        const lg = geo(w * 0.22, legH, w * 0.22);
        [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([a, b]) => { const l = new T.Mesh(lg, mat(dark(col)));
          l.position.set(a * w * 0.28, legH / 2, b * len * 0.35); o.add(l); });
      } else if (shape === 'b') {     // Zweibeiner: Beine, Körper, Kopf
        const hs = Math.min(w * 0.9, h * 0.25);
        const body = new T.Mesh(geo(w, h - hs, w * 0.55), mat(col)); body.position.y = (h - hs) / 2; o.add(body);
        const head = new T.Mesh(geo(hs, hs, hs), mat(dark(col))); head.position.y = h - hs / 2; o.add(head);
      } else {                        // klein / fliegend / Wasser: ein Block
        const b = new T.Mesh(geo(w, Math.max(h * 0.8, 0.25), shape === 'w' ? w * 1.6 : w), mat(col));
        b.position.y = h / 2; o.add(b);
      }
      if (def[5]) { const ol = new T.Mesh(geo(w * 1.05 + 0.05, h + 0.05, w * 1.05 + 0.05), hostileOutline);
        ol.position.y = h / 2; o.add(ol); }
      o.position.set(m.x, m.y, m.z);
      o.rotation.y = -(m.r || 0) * Math.PI / 180;
      o.userData = { name: m.n || def[4], hostile: !!def[5] };
      return o;
    }

    function attach() {
      const root = app.playerMarkerManager && app.playerMarkerManager.root;
      if (root && group.parent !== root) root.add(group);
    }

    let lastMap = null, lastSaved = 0;
    async function refresh() {
      try {
        attach();
        const map = app.mapViewer.map && app.mapViewer.map.data && app.mapViewer.map.data.id;
        if (!map) return;
        const r = await fetch('extra/mobs-' + map + '.json?' + Date.now());
        if (!r.ok) { if (map !== lastMap) { group.clear(); lastMap = map; } return; }
        const d = await r.json();
        if (d.saved === lastSaved && map === lastMap) return;
        lastSaved = d.saved; lastMap = map;
        group.clear(); labels.forEach(l => { l.removeFromParent(); if (l.element) l.element.remove(); }); labels = [];
        d.mobs.forEach(m => group.add(makeMob(m)));
        app.mapViewer.redraw();
      } catch (e) { console.warn('[spieler3d] Mobs', e); }
    }
    fetch('extra/tex/sizes.json?' + Date.now()).then(r => r.ok ? r.json() : {}).catch(() => ({}))
      .then(sz => { SIZES = sz || {}; refresh(); });
    setInterval(refresh, 30000);

    // Namen nur in der Nähe der Kamera einblenden
    setInterval(() => {
      try {
        const cam = app.mapViewer.camera;
        if (!cam || !BM.CSS2DObject) return;
        labels.forEach(l => { l.removeFromParent(); if (l.element) l.element.remove(); }); labels = [];
        // nur Mobs im Umkreis zeichnen (sonst viele tausend Teile → ruckelig)
        const ctrl = app.mapViewer.controlsManager;
        const mobile = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
        const R = mobile ? Math.min(220, Math.max(80, ctrl.distance)) : Math.min(700, Math.max(160, ctrl.distance * 1.6));
        group.children.forEach(o => {
          o.visible = Math.abs(o.position.x - ctrl.position.x) < R && Math.abs(o.position.z - ctrl.position.z) < R;
        });
        const near = [];
        group.children.forEach(o => { if (!o.visible) return; const d = cam.position.distanceTo(o.position); if (d < 45) near.push([d, o]); });
        near.sort((a, b) => a[0] - b[0]).slice(0, 40).forEach(([, o]) => {
          const el = document.createElement('div');
          el.textContent = o.userData.name;
          el.style.cssText = 'font:11px sans-serif;color:' + (o.userData.hostile ? '#ff8080' : '#fff') +
            ';background:rgba(0,0,0,.55);padding:1px 4px;border-radius:3px;pointer-events:none;white-space:nowrap';
          const lab = new BM.CSS2DObject(el);
          lab.position.set(0, 2.2, 0);
          o.add(lab); labels.push(lab);
        });
        app.mapViewer.redraw();
      } catch (e) { /* egal */ }
    }, 1500);
  }

  /* -------- Karte regelmäßig neu laden (neu gebaute/entdeckte Gebiete) -------- */
  function initAutoRefresh(app) {
    const EVERY_MS = 5 * 60 * 1000, IDLE_MS = 30 * 1000;
    let last = Date.now();
    setInterval(() => {
      const idle = Date.now() - (app.lastCameraMove || 0);
      if (Date.now() - last < EVERY_MS || idle < IDLE_MS || document.hidden) return;
      last = Date.now();
      try { app.updateMap(); console.log('[spieler3d] Karte aktualisiert'); } catch (e) { /* egal */ }
    }, 10000);
  }

  /* -------------------- Kamera dauerhaft einem Spieler folgen -------------------- */
  function initFollow(app) {
    const box = document.createElement('div');
    box.id = 'felix-follow';
    box.style.cssText = 'position:absolute;top:56px;left:8px;z-index:100;font:13px sans-serif;' +
      'background:rgba(30,30,30,.85);color:#eee;border-radius:4px;padding:6px 8px;min-width:150px;user-select:none';
    document.body.appendChild(box);
    const btnCss = 'display:flex;align-items:center;gap:6px;width:100%;margin:3px 0;padding:4px 6px;border:0;' +
      'border-radius:3px;cursor:pointer;font:13px sans-serif;text-align:left;';
    let lastKey = '';
    function render() {
      const set = app.playerMarkerManager && app.playerMarkerManager.getPlayerMarkerSet(false);
      const players = [];
      if (set) set.markers.forEach(m => { if (m.isPlayerMarker && !m.data.foreign) players.push([m.data.playerUuid, m.data.name]); });
      players.sort((a, b) => a[1].localeCompare(b[1]));
      if (window.__felixFollow && !players.some(p => p[0] === window.__felixFollow)) window.__felixFollow = null;
      const key = window.__felixFollow + '|' + players.map(p => p.join(':')).join(',');
      if (key === lastKey) return;
      lastKey = key;
      box.innerHTML = '<div style="font-weight:bold;margin-bottom:2px">📍 Folgen</div>';
      if (!players.length) { box.innerHTML += '<div style="opacity:.6">niemand online</div>'; return; }
      players.forEach(([uuid, name]) => {
        const on = window.__felixFollow === uuid;
        const b = document.createElement('button');
        b.style.cssText = btnCss + (on ? 'background:#2a7a2a;color:#fff' : 'background:#444;color:#eee');
        b.innerHTML = '<img src="maps/' + (app.mapViewer.map ? app.mapViewer.map.data.id : 'world') +
          '/assets/playerheads/' + uuid + '.png" style="width:16px;height:16px;image-rendering:pixelated" onerror="this.style.display=\'none\'">' +
          '<span>' + name + '</span><span style="margin-left:auto">' + (on ? '■ Stopp' : '▶') + '</span>';
        b.title = on ? 'Nicht mehr folgen' : 'Kamera folgt ' + name + ' dauerhaft';
        b.onclick = () => { window.__felixFollow = on ? null : uuid; lastKey = ''; render(); app.mapViewer.redraw(); };
        box.appendChild(b);
      });
    }
    render();
    setInterval(render, 1000);
  }
})();
