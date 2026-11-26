const dashboard = document.querySelector("#welcome-dashboard");
const content = document.querySelector("#welcome-widgets");
const widgetArea = content.querySelector(".welcome-scroll");
const stage = document.querySelector(".lighter-stage");
const canvas = document.querySelector("#welcome-lighter");
const surfaceLine = stage.querySelector(".lighter-surface-line");
const toggle = document.querySelector("#lighter-toggle");
const ash = document.querySelector("#welcome-ash");
const ashContext = ash.getContext("2d");
const widgets = [...content.querySelectorAll(".welcome-hero, .dashboard-stat, .quick-start-heading, .quick-card, #welcome-create")];
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let active = false;
let burning = false;
let ignition = 0;
let frame = 0;
let previousTime = 0;
let model;
let loading = false;
let pointer;
let drag;
let suppressPointerClick = false;
let ashes = [];
// Outer flame, inner flame, core, and reflected light.
const warmFlame = ["#ff6a00", "#ffb703", "#ffedce", "#ff9b32"];
const coolFlame = ["#2563eb", "#60a5fa", "#86bfff", "#2563eb"];
let heatRatio = 0;

function blueCoverage(heat) {
  return heat <= .5 ? Math.max(0, (heat - .2) / .6) : heat;
}

function syncProjectCount() {
  const projectCount = Number(dashboard.dataset.projectCount) || 0;
  heatRatio = Math.min(Math.max(projectCount, 0) / 10, 1);
  const coverage = blueCoverage(heatRatio);
  for (const [index, name] of ["outer", "inner", "core"].entries()) {
    const cool = coolFlame[index];
    const warm = warmFlame[index];
    stage.style.setProperty(`--flame-warm-${name}`, warm);
    stage.style.setProperty(`--flame-cool-${name}`, cool);
  }
  stage.style.setProperty("--flame-glow", `color-mix(in srgb, ${coolFlame[3]} ${coverage * 100}%, ${warmFlame[3]})`);
  stage.style.setProperty("--flame-split", `${108 - coverage * 116}%`);
  scheduleFrame();
}

function disperseWidgets() {
  const bounds = dashboard.getBoundingClientRect();
  const viewport = widgetArea.getBoundingClientRect();
  const light = document.documentElement.dataset.theme === "light";
  const ratio = Math.min(devicePixelRatio, 2);
  ash.width = Math.max(1, Math.round(bounds.width * ratio));
  ash.height = Math.max(1, Math.round(bounds.height * ratio));
  ashContext.setTransform(ratio, 0, 0, ratio, 0, 0);
  for (const widget of widgets) {
    const rect = widget.getBoundingClientRect();
    const left = Math.max(rect.left, viewport.left);
    const right = Math.min(rect.right, viewport.right);
    const top = Math.max(rect.top, viewport.top);
    const bottom = Math.min(rect.bottom, viewport.bottom);
    if (right <= left || bottom <= top) continue;
    for (let i = 0; i < 54; i++) {
      ashes.push({
        x: left - bounds.left + Math.random() * (right - left),
        y: top - bounds.top + Math.random() * (bottom - top),
        vx: (Math.random() - .45) * 80,
        vy: -20 - Math.random() * 95,
        age: 0,
        life: .45 + Math.random() * 1.05,
        size: .6 + Math.random() * 2.6,
        shade: (light ? 52 : 123) + Math.floor(Math.random() * 75),
        phase: Math.random() * 6,
      });
    }
  }
}

function drawAsh(dt) {
  ashContext.clearRect(0, 0, ash.clientWidth, ash.clientHeight);
  ashes = ashes.filter(particle => particle.age < particle.life);
  for (const particle of ashes) {
    particle.age += dt;
    particle.x += (particle.vx + 19 * Math.sin(particle.phase + particle.age * 5)) * dt;
    particle.y += particle.vy * dt;
    const { x, y, size, shade } = particle;
    const alpha = .8 * Math.max(0, 1 - particle.age / particle.life) ** 1.3;
    ashContext.fillStyle = `rgba(${shade},${shade},${shade},${alpha})`;
    ashContext.fillRect(x, y, size, size * .65);
  }
}

function animate(now) {
  frame = 0;
  const dt = Math.min((now - (previousTime || now)) / 1000, .035);
  previousTime = now;
  if (ignition && now >= ignition) {
    ignition = 0;
    stage.setAttribute("data-burning", "");
  }
  const lit = burning && !ignition;
  const moving = model?.draw(now / 1000, dt, burning, lit);
  if (ashes.length) drawAsh(dt);
  if (moving || ignition || ashes.length) frame = requestAnimationFrame(animate);
}

function scheduleFrame() {
  if (active && !frame) {
    previousTime = 0;
    frame = requestAnimationFrame(animate);
  }
}

function setHomeLighterOpen(open, animateChange = false) {
  burning = open;
  toggle.setAttribute("aria-expanded", String(burning));
  stage.toggleAttribute("data-open", burning);
  ignition = burning && animateChange && !reducedMotion.matches ? performance.now() + 190 : 0;
  stage.toggleAttribute("data-burning", burning && !ignition);
  if (!burning && animateChange && !content.inert && !reducedMotion.matches && ashContext) {
    disperseWidgets();
  }
  content.classList.toggle("revealed", burning);
  content.inert = !burning;
  scheduleFrame();
}

setHomeLighterOpen(toggle.getAttribute("aria-expanded") === "true");
toggle.addEventListener("lighterrestore", ({detail: open}) => setHomeLighterOpen(open));
toggle.addEventListener("click", event => {
  if (event.detail && suppressPointerClick) return;
  if (event.detail && model && !model.hitTest({ x: event.clientX, y: event.clientY }, burning)) return;
  setHomeLighterOpen(!burning, true);
  toggle.dispatchEvent(new CustomEvent("lighterchange", {detail: burning}));
});

toggle.addEventListener("pointerdown", event => {
  if (drag || event.button !== 0) return;
  suppressPointerClick = false;
  const { clientX: x, clientY: y, pointerId, timeStamp } = event;
  if (event.pointerType !== "mouse" || !model?.hitTest({ x, y }, burning)) return;
  drag = { pointerId, x, y, lastX: x, timeStamp, speed: 0, moved: false };
  model.stopRotation();
  toggle.setPointerCapture(pointerId);
});

function updateDrag(event) {
  const { clientX: x, clientY: y, timeStamp } = event;
  if (!drag.moved && Math.hypot(x - drag.x, y - drag.y) < 5) return;
  drag.moved = true;
  const dx = x - drag.lastX;
  if (dx) {
    drag.speed = dx / Math.max(8, timeStamp - drag.timeStamp);
    drag.lastX = x;
    drag.timeStamp = timeStamp;
    model?.rotate(dx);
  }
}

toggle.addEventListener("pointermove", event => {
  if (drag?.pointerId === event.pointerId) updateDrag(event);
});
toggle.addEventListener("pointerup", event => {
  if (drag?.pointerId !== event.pointerId) return;
  updateDrag(event);
  suppressPointerClick = drag.moved;
  if (drag.moved && !reducedMotion.matches) {
    const momentum = Math.max(0, 90 - (event.timeStamp - drag.timeStamp));
    model?.rotate(Math.max(-3, Math.min(3, drag.speed)) * momentum);
  }
  drag = undefined;
});
toggle.addEventListener("lostpointercapture", event => {
  if (drag?.pointerId !== event.pointerId) return;
  suppressPointerClick = true;
  drag = undefined;
  scheduleFrame();
});

window.addEventListener("pointermove", event => {
  if (!active || event.pointerType === "touch") return;
  const onToggle = event.target === toggle;
  if (!burning && !onToggle && !pointer) return;
  pointer = burning || onToggle ? { x: event.clientX, y: event.clientY, onToggle } : undefined;
  if (!model && burning) {
    const { x, y } = fallbackTip();
    const distance = Math.hypot(pointer.x - x, pointer.y - y);
    stage.style.setProperty("--lighter-bend", `${-(pointer.x - x) * .3 * Math.max(0, 1 - distance / 100)}deg`);
  }
  scheduleFrame();
}, { passive: true });
document.documentElement.addEventListener("pointerleave", () => {
  pointer = undefined;
  stage.style.removeProperty("--lighter-bend");
  scheduleFrame();
});

function syncVisibility() {
  const onDashboard = !dashboard.closest(".hidden");
  if (!onDashboard) stage.removeAttribute("data-webgl");
  const visible = !document.hidden && onDashboard;
  if (active === visible) return;
  active = visible;
  if (active) {
    if (!loading) {
      loading = true;
      import("./vendor/three.module.min.js").then(THREE => {
        if (active) {
          model = createLighter(THREE);
          stage.toggleAttribute("data-webgl-fallback", !model);
        } else loading = false;
        scheduleFrame();
      }).catch(error => {
        stage.setAttribute("data-webgl-fallback", "");
        console.warn("Loomis lighter:", error);
      });
    }
    scheduleFrame();
    return;
  }
  cancelAnimationFrame(frame);
  frame = 0;
  if (drag) {
    const { pointerId } = drag;
    drag = undefined;
    suppressPointerClick = true;
    if (toggle.hasPointerCapture(pointerId)) toggle.releasePointerCapture(pointerId);
  }
  if (onDashboard) return;
  pointer = undefined;
  ashes = [];
  ashContext?.clearRect(0, 0, ash.width, ash.height);
  stage.removeAttribute("data-hit");
  stage.style.removeProperty("--lighter-bend");
  if (!burning) model?.reset();
}

const visibility = new MutationObserver(syncVisibility);
for (const element of [dashboard, dashboard.closest("main"), dashboard.closest(".app-screen")]) {
  visibility.observe(element, { attributes: true, attributeFilter: ["class"] });
}
document.addEventListener("visibilitychange", syncVisibility);
new ResizeObserver(scheduleFrame).observe(stage);
new MutationObserver(scheduleFrame).observe(document.documentElement, {
  attributes: true, attributeFilter: ["data-theme"],
});
new MutationObserver(syncProjectCount).observe(dashboard, {
  attributes: true, attributeFilter: ["data-project-count"],
});
syncProjectCount();
reducedMotion.addEventListener("change", scheduleFrame);
syncVisibility();

function createLighter(THREE, completionStage) {
  const cinematic = Boolean(completionStage);
  const stage = completionStage || document.querySelector(".lighter-stage");
  const canvas = stage.querySelector("canvas");
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  } catch {
    return undefined;
  }
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const lighterGroup = new THREE.Group();
  lighterGroup.rotation.set(0, 0, 0, "YXZ");
  lighterGroup.position.set(0, .12, 0);
  scene.add(lighterGroup);
  const camera = new THREE.OrthographicCamera();
  const projection = new THREE.Vector3();
  const raycaster = new THREE.Raycaster();
  const rayPoint = new THREE.Vector2();
  let width = 0;
  let height = 0;
  let surfaceOffset = 0;
  let yawTarget = 0;
  let angle = cinematic || burning ? THREE.MathUtils.degToRad(105) : 0;
  let velocity = 0;
  let fire = cinematic ? 1 : 0;
  let bend = 0;
  let depth = 0;
  let sparkTimer = 0;
  let theme;
  let temperature = heatRatio;
  const warm = warmFlame.map(color => new THREE.Color(color));
  const cool = coolFlame.map(color => new THREE.Color(color));

  function resize() {
    const { width: nextWidth, height: nextHeight } = canvas.getBoundingClientRect();
    const nextOffset = cinematic ? 0 : parseFloat(getComputedStyle(toggle).bottom) + 8;
    if (!nextWidth || !nextHeight || (width === nextWidth && height === nextHeight && surfaceOffset === nextOffset)) return;
    width = nextWidth;
    height = nextHeight;
    surfaceOffset = nextOffset;
    const viewHeight = cinematic ? 2.7 : Math.min(3.85, Math.max(3.2, height * 3.85 / 326));
    const halfWidth = viewHeight * width / height / 2;
    const verticalOffset = cinematic ? 1.83 : viewHeight * (.5 - surfaceOffset / height) - lighterGroup.position.y;
    Object.assign(camera, { left: -halfWidth, right: halfWidth, top: verticalOffset + viewHeight / 2, bottom: verticalOffset - viewHeight / 2, near: .1, far: 30 });
    camera.position.z = 7;
    camera.position.y = lighterGroup.position.y;
    camera.lookAt(lighterGroup.position);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
    renderer.setSize(width, height, false);
    if (cinematic) return;
    body.updateWorldMatrix(true, false);
    const baseY = new THREE.Box3().setFromObject(body).min.y;
    const projectedBase = projection.set(0, baseY, 0).project(camera);
    surfaceLine.style.top = `${(1 - projectedBase.y) * height / 2}px`;
  }

  const studio = document.createElement("canvas");
  studio.width = 256;
  studio.height = 128;
  const studioContext = studio.getContext("2d");
  const gradient = studioContext.createLinearGradient(0, 0, studio.width, 0);
  for (const [at, color] of [[0, "#24262d"], [.19, "#e8ebf4"], [.28, "#3a3c43"], [.48, "#777b86"], [.72, "#15171b"], [.83, "#c1c4cc"], [1, "#24262d"]]) {
    gradient.addColorStop(at, color);
  }
  studioContext.fillStyle = gradient;
  studioContext.fillRect(0, 0, studio.width, studio.height);
  const environment = new THREE.CanvasTexture(studio);
  environment.mapping = THREE.EquirectangularReflectionMapping;
  environment.colorSpace = THREE.SRGBColorSpace;
  scene.environment = environment;
  scene.add(new THREE.HemisphereLight(0xf2f4ff, 0x343139, 1.55));

  const metal = new THREE.MeshPhysicalMaterial({
    roughness: 0.22,
    metalness: 0.78,
    clearcoat: 0.85,
    clearcoatRoughness: 0.12,
    flatShading: false,
  });
  metal.onBeforeCompile = shader => {
    shader.vertexShader = `varying vec2 metalPosition;\n${shader.vertexShader}`
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nmetalPosition = position.xy;");
    shader.fragmentShader = `varying vec2 metalPosition;\n${shader.fragmentShader}`
      .replace("#include <color_fragment>", `
        #include <color_fragment>
        float sheet = metalPosition.x + metalPosition.y * .24;
        float stripe = (sheet + .27) * 3.65;
        float highlight = exp(-stripe * stripe);
        float graphite = smoothstep(-.05, .52, sheet);
        diffuseColor.rgb *= .93 + highlight * .58 - graphite * .61;
      `);
  };
  const steel = new THREE.MeshStandardMaterial({ color: 0xb1b4bc, metalness: .8, roughness: .29 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x18191e, roughness: .85 });
  const fireLight = new THREE.PointLight(cinematic ? 0xffffff : warm[3], 0, 3);
  fireLight.position.set(0, 1.93, .29);
  lighterGroup.add(fireLight);

  function roundedBox(w, h, d, radius, bevel, material) {
    const shape = new THREE.Shape();
    const x = w / 2 - bevel;
    const y = h / 2 - bevel;
    shape.moveTo(-x + radius, -y);
    shape.lineTo(x - radius, -y);
    shape.quadraticCurveTo(x, -y, x, -y + radius);
    shape.lineTo(x, y - radius);
    shape.quadraticCurveTo(x, y, x - radius, y);
    shape.lineTo(-x + radius, y);
    shape.quadraticCurveTo(-x, y, -x, y - radius);
    shape.lineTo(-x, -y + radius);
    shape.quadraticCurveTo(-x, -y, -x + radius, -y);
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: d - bevel * 2, bevelEnabled: true, bevelSize: bevel,
      bevelThickness: bevel, bevelSegments: 8, curveSegments: 24, steps: 1,
    });
    const { position, normal } = geometry.attributes;
    const corners = Array.from({ length: 3 }, () => new THREE.Vector3());
    const edgeA = new THREE.Vector3();
    const edgeB = new THREE.Vector3();
    const faceNormal = new THREE.Vector3();
    const normalsByPosition = new Map();
    const vertexNormals = [];
    // ExtrudeGeometry duplicates vertices per triangle. Average their normals
    // by corner angle so the cap triangulation does not skew the bevel.
    for (let i = 0; i < position.count; i += 3) {
      corners.forEach((corner, j) => corner.fromBufferAttribute(position, i + j));
      faceNormal.fromBufferAttribute(normal, i);
      corners.forEach((corner, j) => {
        const key = corner.toArray().join(",");
        if (!normalsByPosition.has(key)) normalsByPosition.set(key, new THREE.Vector3());
        const smoothNormal = normalsByPosition.get(key);
        edgeA.subVectors(corners[(j + 1) % 3], corner);
        edgeB.subVectors(corners[(j + 2) % 3], corner);
        smoothNormal.addScaledVector(faceNormal, edgeA.angleTo(edgeB));
        vertexNormals.push(smoothNormal);
      });
    }
    for (const smoothNormal of normalsByPosition.values()) smoothNormal.normalize();
    vertexNormals.forEach(({ x, y, z }, i) => normal.setXYZ(i, x, y, z));
    geometry.translate(0, 0, -d / 2 + bevel);
    return new THREE.Mesh(geometry, material);
  }

  const body = roundedBox(1.10, 1.32, .43, .095, .04, metal);
  body.position.y = .67;
  lighterGroup.add(body);
  const pivot = new THREE.Group();
  pivot.position.set(-.54, 1.36, 0);
  const lid = roundedBox(1.10, .50, .43, .09, .04, metal);
  lid.position.set(.54, .255, 0);
  pivot.add(lid);
  pivot.rotation.z = angle;
  lighterGroup.add(pivot);

  const contourMaterial = new THREE.LineBasicMaterial({
    color: 0xe5e5e5, opacity: 0, transparent: true, depthWrite: false, toneMapped: false,
  });
  const contours = cinematic ? [body, lid].map(mesh => {
    const contour = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 5), contourMaterial);
    contour.scale.setScalar(1.001);
    contour.visible = false;
    mesh.add(contour);
    return contour;
  }) : [];

  const hinge = new THREE.Mesh(new THREE.CylinderGeometry(.067, .067, .36, 20), steel);
  hinge.rotation.x = Math.PI / 2;
  hinge.position.copy(pivot.position);
  lighterGroup.add(hinge);
  const chimney = roundedBox(.52, .35, .32, .03, .025, steel);
  chimney.position.set(.10, 1.49, 0);
  lighterGroup.add(chimney);
  const hole = new THREE.CircleGeometry(.032, 12);
  for (const z of [-.161, .161]) {
    for (const x of [-.06, .10, .26]) {
      for (const y of [1.45, 1.56]) {
        const vent = new THREE.Mesh(hole, dark);
        vent.position.set(x, y, z);
        if (z < 0) vent.rotation.y = Math.PI;
        lighterGroup.add(vent);
      }
    }
  }
  const wheel = new THREE.Mesh(new THREE.CylinderGeometry(.09, .09, .21, 14), dark);
  wheel.rotation.x = Math.PI / 2;
  wheel.position.set(-.30, 1.43, 0);
  lighterGroup.add(wheel);
  const wick = new THREE.Mesh(new THREE.CylinderGeometry(.032, .039, .10, 12), dark);
  wick.position.set(.10, 1.70, 0);
  lighterGroup.add(wick);

  const mark = document.createElement("canvas");
  mark.width = 256;
  mark.height = 64;
  const markContext = mark.getContext("2d");
  markContext.font = "700 38px Segoe UI, sans-serif";
  markContext.textAlign = "center";
  markContext.fillStyle = "white";
  markContext.fillText("L O O M I S", 128, 43);
  const markTexture = new THREE.CanvasTexture(mark);
  markTexture.colorSpace = THREE.SRGBColorSpace;
  const badge = new THREE.Mesh(new THREE.PlaneGeometry(.78, .195),
    new THREE.MeshBasicMaterial({ map: markTexture, transparent: true, opacity: .82, depthWrite: false }));
  badge.position.set(0, .40, .217);
  lighterGroup.add(badge);

  const flame = new THREE.Group();
  flame.position.set(.10, 1.73, 0);
  lighterGroup.add(flame);
  const flameGeometry = new THREE.SphereGeometry(1, 32, 32);
  const layers = [];
  const flameUniforms = {
    time: { value: 0 }, bend: { value: 0 }, depth: { value: 0 }, strength: { value: 0 }, blue: { value: 0 },
  };
  const silver = [0xa4a4a4, 0xe0e0e0, 0xffffff];
  for (const [radius, length, opacity, bottom] of [
    [.23, .84, .39, .00],
    [.165, .70, .72, .01],
    [.092, .44, .97, .025],
  ]) {
    const material = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: {
        ...flameUniforms,
        radius: { value: radius }, length: { value: length }, opacity: { value: opacity },
        color: { value: cinematic ? new THREE.Color(silver[layers.length]) : warm[layers.length] },
        coolColor: { value: cool[layers.length] }, bottom: { value: bottom },
      },
      vertexShader: `
        uniform float time, bend, depth, radius, length, bottom;
        varying float rise;
        varying vec3 surfaceNormal;
        void main() {
          rise = position.y * .5 + .5;
          float wobble = sin(time * 9. + rise * 13.) + .42 * sin(time * 17. - rise * 19.);
          vec3 p = position;
          p.xz *= radius * (1.12 - .64 * rise + .10 * wobble);
          p.y = bottom + rise * length * (1. + .10 * sin(time * 9.));
          p.x += bend * pow(rise, 1.6) + rise * wobble * .038;
          p.z += depth * pow(rise, 1.3);
          surfaceNormal = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.);
        }`,
      fragmentShader: `
        uniform float strength, opacity, blue;
        uniform vec3 color, coolColor;
        varying float rise;
        varying vec3 surfaceNormal;
        void main() {
          float edge = smoothstep(.0, .72, abs(surfaceNormal.z));
          float fade = smoothstep(0., .10, rise) * (1. - smoothstep(.66, 1., rise));
          float coolMix = smoothstep(-.12, .12, blue - rise)
            * smoothstep(0., .12, blue);
          coolMix = mix(coolMix, 1., smoothstep(.88, 1., blue));
          vec3 flameColor = mix(color, coolColor, coolMix);
          gl_FragColor = vec4(mix(flameColor, vec3(1.), .31 * (1. - rise) * (1. - coolMix)),
            edge * fade * opacity * strength);
          #include <colorspace_fragment>
        }`,
    });
    const layer = new THREE.Mesh(flameGeometry, material);
    layer.frustumCulled = false;
    layer.renderOrder = layers.length + 1;
    flame.add(layer);
    layers.push(layer);
  }

  const sparks = Array.from({ length: 12 }, () => ({
    life: 0,
    mesh: new THREE.Mesh(new THREE.SphereGeometry(.014, 6, 4),
      new THREE.MeshBasicMaterial({ color: 0xffd5a4, transparent: true, depthWrite: false })),
  }));
  for (const { mesh } of sparks) {
    mesh.visible = false;
    lighterGroup.add(mesh);
  }

  const hitMeshes = [body, lid, hinge, chimney, wheel, wick];

  // Keep the closed lid's silhouette during the peek so the lid cannot move out from under the pointer.
  const closedLid = lid.clone();
  closedLid.position.add(pivot.position);
  closedLid.visible = false;
  lighterGroup.add(closedLid);
  const peekMeshes = [...hitMeshes, closedLid];

  function hitTest(point, open) {
    const rect = canvas.getBoundingClientRect();
    rayPoint.set((point.x - rect.left) * 2 / rect.width - 1, 1 - (point.y - rect.top) * 2 / rect.height);
    raycaster.setFromCamera(rayPoint, camera);
    return raycaster.intersectObjects(cinematic ? [body] : open ? hitMeshes : peekMeshes, false).length > 0;
  }

  function projectPoint(x, y, z) {
    const rect = canvas.getBoundingClientRect();
    projection.set(flame.position.x + x, flame.position.y + y, flame.position.z + z)
      .applyMatrix4(lighterGroup.matrixWorld).project(camera);
    return {
      x: rect.left + (projection.x + 1) * rect.width / 2,
      y: rect.top + (1 - projection.y) * rect.height / 2,
    };
  }

  function reset() {
    velocity = bend = depth = sparkTimer = 0;
    angle = cinematic ? THREE.MathUtils.degToRad(105) : 0;
    fire = Number(cinematic);
    yawTarget = lighterGroup.rotation.y = 0;
    pivot.rotation.z = angle;
    for (const spark of sparks) {
      spark.life = 0;
      spark.mesh.visible = false;
    }
  }

  canvas.addEventListener("webglcontextlost", event => {
    event.preventDefault();
    if (!cinematic) surfaceLine.style.removeProperty("top");
    stage.removeAttribute("data-webgl");
    stage.setAttribute("data-webgl-fallback", "");
    if (!cinematic) {
      model = undefined;
      scheduleFrame();
    }
  });
  canvas.addEventListener("webglcontextrestored", () => {
    width = 0;
    if (!cinematic) {
      model = lighter;
      scheduleFrame();
    }
  });
  const lighter = {
    hitTest, reset,
    stopRotation() {
      yawTarget = lighterGroup.rotation.y;
    },
    rotate(dx) {
      yawTarget += dx * (2 * Math.PI / 240);
      scheduleFrame();
    },
    draw(time, dt, open, lit, contourGlow = 0) {
      resize();
      const light = document.documentElement.dataset.theme === "light";
      if (theme !== light) {
        theme = light;
        metal.color.setHex(light ? 0xc2c7d0 : 0x53565f);
        contourMaterial.color.setHex(light ? 0xf7f7f7 : 0xdcdcdc);
        badge.material.color.setHex(light ? 0x252932 : 0xe5e8f1);
      }
      const still = reducedMotion.matches;
      const yaw = lighterGroup.rotation.y;
      const rotating = Math.abs(yawTarget - yaw) > .001;
      lighterGroup.rotation.y = still || !rotating ? yawTarget : THREE.MathUtils.damp(yaw, yawTarget, 20, dt);
      if (cinematic) {
        contourMaterial.opacity = contourGlow;
        for (const contour of contours) contour.visible = contourGlow > .001;
      } else {
        temperature = still ? heatRatio : THREE.MathUtils.damp(temperature, heatRatio, 8, dt);
        if (Math.abs(temperature - heatRatio) < .001) temperature = heatRatio;
        flameUniforms.blue.value = blueCoverage(temperature);
        fireLight.color.lerpColors(warm[3], cool[3], flameUniforms.blue.value);
      }
      scene.updateMatrixWorld();
      const hit = Boolean(!cinematic && !drag?.moved && pointer?.onToggle && hitTest(pointer, open));
      stage.toggleAttribute("data-hit", hit);
      const goal = THREE.MathUtils.degToRad(open ? 105 : hit ? 15 : 0);
      velocity += ((goal - angle) * 130 - velocity * 17) * dt;
      angle += velocity * dt;
      if (still) {
        angle = goal;
        velocity = 0;
      }
      pivot.rotation.z = THREE.MathUtils.clamp(angle, 0, THREE.MathUtils.degToRad(105));
      fire = still ? Number(lit) : THREE.MathUtils.damp(fire, Number(lit), lit ? 10 : 20, dt);

      const source = projectPoint(0, .29, 0);
      const dx = !cinematic && pointer ? pointer.x - source.x : 0;
      const dy = !cinematic && pointer ? pointer.y - source.y : 0;
      const distance = !cinematic && pointer ? Math.hypot(dx, dy) : Infinity;
      const influence = Math.max(0, 1 - distance / 102);
      const proximity = Math.max(0, 1 - distance / 23);
      bend = THREE.MathUtils.damp(bend, dx * influence * .017, 8, dt);
      depth = THREE.MathUtils.damp(depth, proximity * .48, 10, dt);
      flame.visible = fire > .008;
      flameUniforms.time.value = still ? 0 : time;
      flameUniforms.bend.value = still ? 0 : bend;
      flameUniforms.depth.value = still ? 0 : depth;
      flameUniforms.strength.value = fire;
      flame.scale.setScalar(.36 + .64 * fire);
      fireLight.intensity = fire * (still ? .7 : .7 + .09 * Math.sin(time * 21.));

      sparkTimer -= dt;
      if (lit && !still && influence > 0 && sparkTimer <= 0) {
        const spark = sparks.find(item => item.life <= 0);
        if (spark) {
          spark.life = .49;
          spark.vx = bend * 2;
          spark.mesh.position.copy(flame.position).add(new THREE.Vector3(bend * .55, .38, depth * .5));
        }
        sparkTimer = .11 + Math.random() * .19;
      }
      for (const spark of sparks) {
        if (still) spark.life = 0;
        spark.life = Math.max(0, spark.life - dt);
        spark.mesh.visible = spark.life > 0;
        if (!spark.mesh.visible) continue;
        spark.mesh.position.x += spark.vx * dt;
        spark.mesh.position.y += dt * .86;
        spark.mesh.material.opacity = Math.min(1, spark.life * 3);
      }
      renderer.render(scene, camera);
      if (!renderer.getContext().isContextLost() && !stage.hasAttribute("data-webgl")) {
        // Resolve the hidden canvas style before starting the first transition.
        getComputedStyle(canvas).opacity;
        stage.removeAttribute("data-webgl-fallback");
        stage.setAttribute("data-webgl", "");
      }
      return !still && (rotating || lit || fire > .002 || Math.abs(angle - goal) > .0005 ||
        Math.abs(velocity) > .001 || temperature !== heatRatio ||
        sparks.some(spark => spark.life > 0));
    },
  };
  return lighter;
}


const completionScene = document.querySelector("#completion-scene");
const completionStage = document.querySelector("#completion-lighter-stage");
const extinguish = document.querySelector("#completion-extinguish");
const finalActions = document.querySelector("#completion-actions");
let completionModel;
let completionLoad;
let completionProject;
let completionFrame = 0;
let previousCompletionTime = 0;
let entranceEnd = Infinity;
let closedAt = Infinity;

function drawCompletion(now) {
  completionFrame = 0;
  if (completionScene.inert || document.hidden) return;
  const dt = Math.min(.033, (now - previousCompletionTime) / 1000) || .016;
  previousCompletionTime = now;
  const open = closedAt === Infinity;
  const breathing = Math.max(0, (now - entranceEnd - 5000) / 1000);
  const glow = open && !reducedMotion.matches ? .20 * (1 - Math.cos(breathing * 1.8)) : 0;
  completionModel?.draw(now / 1000, dt, open, open, glow);
  if (open || now - closedAt < 1400) completionFrame = requestAnimationFrame(drawCompletion);
}

function syncCompletion() {
  const id = completionScene.dataset.project;
  if (id !== completionProject) {
    completionProject = id;
    cancelAnimationFrame(completionFrame);
    completionFrame = 0;
    previousCompletionTime = 0;
    entranceEnd = Infinity;
    closedAt = Infinity;
    completionModel?.reset();
  }
  if (!id || document.hidden || completionScene.classList.contains("completion-closed")) return;
  completionLoad ??= import("./vendor/three.module.min.js").then(THREE => {
    completionModel = createLighter(THREE, completionStage);
  }).catch(error => console.warn("Loomis completion lighter:", error));
  completionLoad.then(() => {
    if (completionScene.dataset.project !== id || document.hidden || closedAt !== Infinity) return;
    if (!completionScene.classList.contains("completion-lit")) {
      completionScene.classList.add("completion-lit");
      if (reducedMotion.matches) entranceEnd = performance.now();
    }
    if (!completionFrame) completionFrame = requestAnimationFrame(drawCompletion);
  });
}

completionStage.addEventListener("animationend", (event) => {
  if (event.animationName !== "completion-rise") return;
  entranceEnd = performance.now();
  const focused = document.activeElement;
  if (!extinguish.closest("[inert]") && !extinguish.disabled &&
      (focused === document.body || focused.matches('.project-button[aria-current="true"]'))) {
    extinguish.focus({preventScroll: true});
  }
});

extinguish.addEventListener("click", (event) => {
  if (completionScene.inert || closedAt !== Infinity) return;
  if (event.detail && completionStage.hasAttribute("data-webgl") &&
      !completionModel.hitTest({x: event.clientX, y: event.clientY}, true)) return;
  closedAt = performance.now();
  extinguish.disabled = true;
  completionScene.classList.add("completion-out");
  completionScene.dispatchEvent(new Event("lighterclosed"));
});

finalActions.addEventListener("animationend", (event) => {
  if (event.target !== finalActions || !completionScene.classList.contains("completion-out")) return;
  finalActions.inert = false;
  finalActions.querySelector("button").focus({preventScroll: true});
});

new MutationObserver(syncCompletion).observe(completionScene, {
  attributes: true, attributeFilter: ["data-project"],
});
document.addEventListener("visibilitychange", syncCompletion);
syncCompletion();
