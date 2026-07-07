// 3D WebGL background (three.js): a rotating aperture-style wireframe "gem"
// inside a gold/white particle starfield, with subtle mouse parallax.
// Falls back to the 2D canvas animation (background.js) if three.js or WebGL
// is unavailable.
(function () {
  const canvas = document.getElementById("bg");
  if (!canvas) return;

  function loadFallback() {
    const s = document.createElement("script");
    s.src = "/static/background.js";
    document.body.appendChild(s);
  }

  if (!window.THREE) { loadFallback(); return; }
  const THREE = window.THREE;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  } catch (e) {
    loadFallback();
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(innerWidth, innerHeight);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0a0a0c, 0.055);

  const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.1, 100);
  camera.position.z = 14;

  const GOLD = 0xfdb913;
  const AMBER = 0xc9880b;

  // ---- central aperture gem: nested rotating wireframe polyhedra ----
  const gemGroup = new THREE.Group();
  scene.add(gemGroup);

  const g1 = new THREE.IcosahedronGeometry(4, 0);
  const gem = new THREE.LineSegments(
    new THREE.EdgesGeometry(g1),
    new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.55 })
  );
  gemGroup.add(gem);

  const g2 = new THREE.OctahedronGeometry(2.4, 0);
  const gemInner = new THREE.LineSegments(
    new THREE.EdgesGeometry(g2),
    new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.35 })
  );
  gemGroup.add(gemInner);

  // faint solid core for depth
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.2, 0),
    new THREE.MeshBasicMaterial({ color: AMBER, transparent: true, opacity: 0.12 })
  );
  gemGroup.add(core);

  // ---- soft round sprite texture for glowing particles ----
  function makeDotTexture() {
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const ctx = c.getContext("2d");
    const grd = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0, "rgba(255,255,255,1)");
    grd.addColorStop(0.25, "rgba(253,220,150,0.9)");
    grd.addColorStop(1, "rgba(253,185,19,0)");
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(c);
  }
  const dotTex = makeDotTexture();

  // ---- particle starfield ----
  const COUNT = 1600;
  const positions = new Float32Array(COUNT * 3);
  const colors = new Float32Array(COUNT * 3);
  const goldC = new THREE.Color(GOLD);
  const whiteC = new THREE.Color(0xffffff);
  for (let i = 0; i < COUNT; i++) {
    const r = 10 + Math.random() * 30;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    positions[i * 3 + 2] = r * Math.cos(phi);
    const col = Math.random() < 0.7 ? goldC : whiteC;
    colors[i * 3] = col.r; colors[i * 3 + 1] = col.g; colors[i * 3 + 2] = col.b;
  }
  const pGeo = new THREE.BufferGeometry();
  pGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  pGeo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const particles = new THREE.Points(pGeo, new THREE.PointsMaterial({
    size: 0.5, map: dotTex, vertexColors: true, transparent: true,
    opacity: 0.9, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  scene.add(particles);

  // ---- interaction + animation ----
  const mouse = { x: 0, y: 0, tx: 0, ty: 0 };
  addEventListener("mousemove", (e) => {
    mouse.tx = (e.clientX / innerWidth - 0.5);
    mouse.ty = (e.clientY / innerHeight - 0.5);
  });
  addEventListener("resize", () => {
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
  });

  let t = 0;
  function animate() {
    t += 0.005;
    mouse.x += (mouse.tx - mouse.x) * 0.05;
    mouse.y += (mouse.ty - mouse.y) * 0.05;

    gemGroup.rotation.x = t * 0.6;
    gemGroup.rotation.y = t * 0.9;
    gemInner.rotation.z = -t * 1.4;
    const pulse = 1 + Math.sin(t * 2) * 0.04;
    gemGroup.scale.set(pulse, pulse, pulse);

    particles.rotation.y = t * 0.15;
    particles.rotation.x = t * 0.05;

    // parallax: nudge camera toward mouse
    camera.position.x += (mouse.x * 6 - camera.position.x) * 0.04;
    camera.position.y += (-mouse.y * 6 - camera.position.y) * 0.04;
    camera.lookAt(scene.position);

    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }
  animate();
})();
