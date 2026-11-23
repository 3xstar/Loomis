const dashboard = document.querySelector("#welcome-dashboard");
const content = document.querySelector("#welcome-widgets");
const stage = document.querySelector(".lighter-stage");
const canvas = document.querySelector("#welcome-lighter");
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
let ashes = [];
let animations = [];

function stopAnimations() {
  animations.forEach(animation => animation.cancel());
  animations = [];
}

function showWidgets(origin) {
  content.inert = false;
  content.classList.add("revealed");
  if (reducedMotion.matches) return;
  animations = widgets.map((widget, index) => {
    const rect = widget.getBoundingClientRect();
    const x = origin.x - rect.left - rect.width / 2;
    const y = origin.y - rect.top - rect.height / 2;
    return widget.animate([
      { transform: `translate(${x}px, ${y}px) scale(.04)`, opacity: 0, filter: "blur(15px)" },
      { transform: "translateY(-9px) scale(1.025)", opacity: 1, filter: "blur(0)", offset: .72 },
      { transform: "translateY(3px) scale(.994)", offset: .87 },
      { transform: "none", opacity: 1, filter: "blur(0)" },
    ], { duration: 620, delay: index * 16, easing: "cubic-bezier(.18,.67,.34,1)", fill: "backwards" });
  });
}

function disperseWidgets() {
  const bounds = dashboard.getBoundingClientRect();
  const viewport = content.getBoundingClientRect();
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

function flameOrigin() {
  if (model) return model.origin();
  const bounds = stage.getBoundingClientRect();
  return { x: bounds.left + bounds.width / 2, y: bounds.bottom - 103 };
}

function animate(now) {
  frame = 0;
  const dt = Math.min((now - (previousTime || now)) / 1000, .035);
  previousTime = now;
  if (ignition && now >= ignition) {
    ignition = 0;
    stage.toggleAttribute("data-burning", true);
    showWidgets(flameOrigin());
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

toggle.addEventListener("click", () => {
  burning = !burning;
  toggle.setAttribute("aria-expanded", String(burning));
  if (burning) {
    stopAnimations();
    content.classList.remove("revealed");
    ignition = performance.now() + (reducedMotion.matches ? 0 : 190);
  } else {
    ignition = 0;
    stage.removeAttribute("data-burning");
    const dissolve = !content.inert && !reducedMotion.matches && ashContext;
    if (dissolve) disperseWidgets();
    stopAnimations();
    content.inert = true;
    if (dissolve) {
      animations = widgets.map(widget => widget.animate([
        { opacity: 1, filter: "blur(0)" },
        { opacity: 0, transform: "translateY(-8px) scale(.98)", filter: "blur(4px)" },
      ], { duration: 160, easing: "ease-out", fill: "forwards" }));
      animations[0].finished.then(() => {
        content.classList.remove("revealed");
        stopAnimations();
      }, () => {});
    } else {
      content.classList.remove("revealed");
    }
  }
  stage.toggleAttribute("data-open", burning);
  scheduleFrame();
});

window.addEventListener("pointermove", event => {
  if (!active || !burning || event.pointerType === "touch") return;
  pointer = { x: event.clientX, y: event.clientY };
  if (!model) {
    const origin = flameOrigin();
    const distance = Math.hypot(pointer.x - origin.x, pointer.y - origin.y);
    stage.style.setProperty("--lighter-bend", `${-(pointer.x - origin.x) * .3 * Math.max(0, 1 - distance / 100)}deg`);
  }
}, { passive: true });
document.documentElement.addEventListener("pointerleave", () => {
  pointer = undefined;
  stage.style.removeProperty("--lighter-bend");
});

function syncVisibility() {
  const visible = !document.hidden && !dashboard.closest(".hidden");
  if (active === visible) return;
  active = visible;
  if (active) {
    if (!loading) {
      loading = true;
      import("./vendor/three.module.min.js").then(THREE => {
        if (active) model = createLighter(THREE);
        else loading = false;
        scheduleFrame();
      }).catch(error => console.warn("Loomis lighter:", error));
    }
    scheduleFrame();
    return;
  }
  cancelAnimationFrame(frame);
  frame = 0;
  if (!dashboard.closest(".hidden")) return;
  burning = false;
  ignition = 0;
  pointer = undefined;
  ashes = [];
  ashContext?.clearRect(0, 0, ash.width, ash.height);
  toggle.setAttribute("aria-expanded", "false");
  stage.removeAttribute("data-open");
  stage.removeAttribute("data-burning");
  content.inert = true;
  content.classList.remove("revealed");
  stopAnimations();
  model?.reset();
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
reducedMotion.addEventListener("change", scheduleFrame);
syncVisibility();

function createLighter(THREE) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  } catch {
    return undefined;
  }
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera();
  const viewHeight = 3.85;
  const target = new THREE.Vector3();
  const projection = new THREE.Vector3();
  let width = 0;
  let height = 0;
  let floorOffset = 0;
  let angle = 0;
  let velocity = 0;
  let fire = 0;
  let bend = 0;
  let depth = 0;
  let sparkTimer = 0;
  let theme;

  function resize() {
    const nextWidth = canvas.clientWidth;
    const nextHeight = canvas.clientHeight;
    const nextOffset = parseFloat(getComputedStyle(stage).getPropertyValue("--lighter-floor"));
    if (!nextWidth || !nextHeight || (width === nextWidth && height === nextHeight && floorOffset === nextOffset)) return;
    width = nextWidth;
    height = nextHeight;
    floorOffset = nextOffset;
    const halfWidth = viewHeight * width / height / 2;
    Object.assign(camera, { left: -halfWidth, right: halfWidth, top: viewHeight / 2, bottom: -viewHeight / 2, near: .1, far: 30 });
    target.y = viewHeight * (.5 - floorOffset / height) / .981;
    camera.position.copy(target).add(new THREE.Vector3(1.8, 1.45, 7));
    camera.lookAt(target);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setSize(width, height, false);
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

  const metal = new THREE.MeshPhysicalMaterial({ metalness: .85, roughness: .24, clearcoat: .65 });
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
  const fireLight = new THREE.PointLight(0xffb878, 0, 3);
  fireLight.position.set(0, 1.93, .29);
  scene.add(fireLight);

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
      bevelThickness: bevel, bevelSegments: 4, curveSegments: 8, steps: 1,
    });
    geometry.translate(0, 0, -d / 2 + bevel);
    return new THREE.Mesh(geometry, material);
  }

  const body = roundedBox(1.10, 1.32, .43, .095, .04, metal);
  body.position.y = .67;
  scene.add(body);
  const pivot = new THREE.Group();
  pivot.position.set(-.54, 1.36, 0);
  const lid = roundedBox(1.10, .50, .43, .09, .04, metal);
  lid.position.set(.54, .255, 0);
  pivot.add(lid);
  scene.add(pivot);

  const hinge = new THREE.Mesh(new THREE.CylinderGeometry(.067, .067, .36, 20), steel);
  hinge.rotation.x = Math.PI / 2;
  hinge.position.copy(pivot.position);
  scene.add(hinge);
  const chimney = roundedBox(.52, .35, .32, .03, .025, steel);
  chimney.position.set(.10, 1.49, 0);
  scene.add(chimney);
  const hole = new THREE.CircleGeometry(.032, 12);
  for (const z of [-.161, .161]) {
    for (const x of [-.06, .10, .26]) {
      for (const y of [1.45, 1.56]) {
        const vent = new THREE.Mesh(hole, dark);
        vent.position.set(x, y, z);
        if (z < 0) vent.rotation.y = Math.PI;
        scene.add(vent);
      }
    }
  }
  const wheel = new THREE.Mesh(new THREE.CylinderGeometry(.09, .09, .21, 14), dark);
  wheel.rotation.x = Math.PI / 2;
  wheel.position.set(-.30, 1.43, 0);
  scene.add(wheel);
  const wick = new THREE.Mesh(new THREE.CylinderGeometry(.032, .039, .10, 12), dark);
  wick.position.set(.10, 1.70, 0);
  scene.add(wick);

  const mark = document.createElement("canvas");
  mark.width = 256;
  mark.height = 64;
  const markContext = mark.getContext("2d");
  markContext.font = "600 33px Segoe UI, sans-serif";
  markContext.textAlign = "center";
  markContext.fillStyle = "white";
  markContext.fillText("L O O M I S", 128, 43);
  const markTexture = new THREE.CanvasTexture(mark);
  markTexture.colorSpace = THREE.SRGBColorSpace;
  const badge = new THREE.Mesh(new THREE.PlaneGeometry(.78, .195),
    new THREE.MeshBasicMaterial({ map: markTexture, transparent: true, opacity: .82, depthWrite: false }));
  badge.position.set(0, .40, .217);
  scene.add(badge);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(5, 3.2), new THREE.ShaderMaterial({
    transparent: true, depthWrite: false,
    uniforms: { glow: { value: 0 }, tint: { value: new THREE.Color() } },
    vertexShader: `
      varying vec2 floorUv;
      void main() {
        floorUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.);
      }`,
    fragmentShader: `
      varying vec2 floorUv;
      uniform float glow;
      uniform vec3 tint;
      void main() {
        vec2 p = (floorUv - .5) * 2.;
        float pool = exp(-dot(p * vec2(2.9, 2.6), p * vec2(2.9, 2.6)));
        vec2 mirror = vec2((p.x + .055 * p.y) * 4.5, (p.y + .25) * 3.);
        float silhouette = (1. - smoothstep(.70, 1.15, abs(mirror.x)))
          * (1. - smoothstep(.2, 1.1, abs(mirror.y))) * smoothstep(.07, .2, -p.y);
        float alpha = (pool * (.11 + glow * .52) + silhouette * .19)
          * (1. - smoothstep(.68, 1., length(p)));
        gl_FragColor = vec4(mix(tint, vec3(1., .72, .47), glow * .72), alpha);
        #include <colorspace_fragment>
      }`,
  }));
  floor.rotation.x = -Math.PI / 2;
  scene.add(floor);

  const flame = new THREE.Group();
  flame.position.set(.10, 1.73, 0);
  scene.add(flame);
  const flameGeometry = new THREE.SphereGeometry(1, 32, 32);
  const layers = [];
  const flameUniforms = {
    time: { value: 0 }, bend: { value: 0 }, depth: { value: 0 }, strength: { value: 0 },
  };
  for (const [radius, length, opacity, color, bottom] of [
    [.23, .84, .39, 0xffa65f, .00],
    [.165, .70, .72, 0xffdcab, .01],
    [.092, .44, .97, 0xfff6e5, .025],
  ]) {
    const material = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: {
        ...flameUniforms,
        radius: { value: radius }, length: { value: length }, opacity: { value: opacity },
        color: { value: new THREE.Color(color) }, bottom: { value: bottom },
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
        uniform float strength, opacity;
        uniform vec3 color;
        varying float rise;
        varying vec3 surfaceNormal;
        void main() {
          float edge = smoothstep(.0, .72, abs(surfaceNormal.z));
          float fade = smoothstep(0., .10, rise) * (1. - smoothstep(.66, 1., rise));
          gl_FragColor = vec4(mix(color, vec3(1.), .31 * (1. - rise)),
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
    scene.add(mesh);
  }

  function origin() {
    const rect = canvas.getBoundingClientRect();
    projection.copy(flame.position);
    projection.y += .29;
    projection.project(camera);
    return {
      x: rect.left + (projection.x + 1) * rect.width / 2,
      y: rect.top + (1 - projection.y) * rect.height / 2,
    };
  }

  function reset() {
    angle = velocity = fire = bend = depth = sparkTimer = 0;
    for (const spark of sparks) {
      spark.life = 0;
      spark.mesh.visible = false;
    }
  }

  canvas.addEventListener("webglcontextlost", event => {
    event.preventDefault();
    stage.removeAttribute("data-webgl");
    model = undefined;
  });
  canvas.addEventListener("webglcontextrestored", () => {
    stage.setAttribute("data-webgl", "");
    model = lighter;
    scheduleFrame();
  });
  resize();
  stage.setAttribute("data-webgl", "");

  const lighter = {
    origin, reset,
    draw(time, dt, open, lit) {
      resize();
      const light = document.documentElement.dataset.theme === "light";
      if (theme !== light) {
        theme = light;
        metal.color.setHex(light ? 0xc2c7d0 : 0x53565f);
        metal.metalness = light ? .64 : .88;
        metal.roughness = light ? .39 : .23;
        badge.material.color.setHex(light ? 0x252932 : 0xe5e8f1);
        floor.material.uniforms.tint.value.setHex(light ? 0x747986 : 0xa2a9b8);
      }
      const still = reducedMotion.matches;
      const goal = open ? THREE.MathUtils.degToRad(105) : 0;
      velocity += ((goal - angle) * 130 - velocity * 17) * dt;
      angle += velocity * dt;
      pivot.rotation.z = still ? goal : Math.max(-.008, angle);
      fire = still ? Number(lit) : THREE.MathUtils.damp(fire, Number(lit), lit ? 10 : 20, dt);

      const source = origin();
      const dx = pointer ? pointer.x - source.x : 0;
      const dy = pointer ? pointer.y - source.y : 0;
      const distance = pointer ? Math.hypot(dx, dy) : Infinity;
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
      floor.material.uniforms.glow.value = fireLight.intensity;

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
      return !still && (lit || fire > .002 || Math.abs(angle - goal) > .0005 ||
        Math.abs(velocity) > .001 || sparks.some(spark => spark.life > 0));
    },
  };
  return lighter;
}
