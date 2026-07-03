// Animated background: drifting gradient blobs + connected particle network.
(function () {
  const canvas = document.getElementById("bg");
  const ctx = canvas.getContext("2d");
  let w, h, dpr;
  const mouse = { x: -999, y: -999 };

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.width = innerWidth * dpr;
    h = canvas.height = innerHeight * dpr;
    canvas.style.width = innerWidth + "px";
    canvas.style.height = innerHeight + "px";
  }
  window.addEventListener("resize", resize);
  resize();

  addEventListener("mousemove", (e) => {
    mouse.x = e.clientX * dpr;
    mouse.y = e.clientY * dpr;
  });

  // soft color blobs
  const blobs = [
    { x: 0.2, y: 0.3, r: 0.5, c: "108,140,255", vx: 0.00006, vy: 0.00004 },
    { x: 0.8, y: 0.2, r: 0.45, c: "180,108,255", vx: -0.00005, vy: 0.00006 },
    { x: 0.6, y: 0.85, r: 0.55, c: "87,230,195", vx: 0.00004, vy: -0.00005 },
  ];

  // particles
  const N = Math.min(90, Math.floor(innerWidth / 16));
  const parts = Array.from({ length: N }, () => ({
    x: Math.random(), y: Math.random(),
    vx: (Math.random() - 0.5) * 0.0006,
    vy: (Math.random() - 0.5) * 0.0006,
  }));

  let t = 0;
  function draw() {
    t += 1;
    ctx.clearRect(0, 0, w, h);

    // blobs
    for (const b of blobs) {
      b.x += Math.sin(t * 0.01) * b.vx + b.vx;
      b.y += Math.cos(t * 0.011) * b.vy + b.vy;
      if (b.x < 0 || b.x > 1) b.vx *= -1;
      if (b.y < 0 || b.y > 1) b.vy *= -1;
      const g = ctx.createRadialGradient(
        b.x * w, b.y * h, 0, b.x * w, b.y * h, b.r * w
      );
      g.addColorStop(0, `rgba(${b.c},0.16)`);
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }

    // particles + links
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > 1) p.vx *= -1;
      if (p.y < 0 || p.y > 1) p.vy *= -1;
      const px = p.x * w, py = p.y * h;
      // repel from mouse
      const dx = px - mouse.x, dy = py - mouse.y;
      const d2 = dx * dx + dy * dy;
      if (d2 < (160 * dpr) ** 2) {
        const d = Math.sqrt(d2) || 1;
        p.vx += (dx / d) * 0.00003;
        p.vy += (dy / d) * 0.00003;
      }
      ctx.beginPath();
      ctx.arc(px, py, 1.6 * dpr, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(180,200,255,.55)";
      ctx.fill();
    }
    const linkDist = 130 * dpr;
    for (let i = 0; i < parts.length; i++) {
      for (let j = i + 1; j < parts.length; j++) {
        const a = parts[i], b = parts[j];
        const dx = (a.x - b.x) * w, dy = (a.y - b.y) * h;
        const d = Math.hypot(dx, dy);
        if (d < linkDist) {
          ctx.strokeStyle = `rgba(108,140,255,${(1 - d / linkDist) * 0.18})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x * w, a.y * h);
          ctx.lineTo(b.x * w, b.y * h);
          ctx.stroke();
        }
      }
    }
    requestAnimationFrame(draw);
  }
  draw();
})();
