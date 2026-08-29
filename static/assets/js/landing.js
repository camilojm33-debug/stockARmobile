(function () {
  const root = document.documentElement;
  const toggle = document.getElementById("themeToggle");

  const getPreferredTheme = function () {
    const savedTheme = localStorage.getItem("stockarmobile-theme");
    if (savedTheme === "light" || savedTheme === "dark") return savedTheme;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const applyTheme = function (theme) {
    root.setAttribute("data-theme", theme);
    localStorage.setItem("stockarmobile-theme", theme);
  };
  applyTheme(getPreferredTheme());
  toggle?.addEventListener("click", function () {
    applyTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
  });

  const revealItems = document.querySelectorAll(".reveal");
  const revealObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.style.setProperty("--delay", (entry.target.getAttribute("data-delay") || "0") + "ms");
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.16 });
  revealItems.forEach(function (item) { revealObserver.observe(item); });

  const counterObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      const counter = entry.target;
      const value = parseFloat(counter.dataset.counter || "0");
      const suffix = counter.dataset.suffix || "";
      const start = performance.now();
      const step = function (now) {
        const progress = Math.min((now - start) / 1300, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        counter.textContent = (value * eased).toFixed(value % 1 === 0 ? 0 : 1) + suffix;
        if (progress < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
      counterObserver.unobserve(counter);
    });
  }, { threshold: 0.5 });
  document.querySelectorAll("[data-counter]").forEach(function (counter) { counterObserver.observe(counter); });

  /* Landing comercial 2.0: capa no destructiva sobre la landing existente. */
  if (!document.getElementById("landing-commercial-v2")) {
    const style = document.createElement("style");
    style.id = "landing-commercial-v2-styles";
    style.textContent = `
      .landing-v2-bridge{position:relative;z-index:2;padding:1rem 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:var(--surface-strong)}
      .landing-v2-bridge__inner{display:flex;align-items:center;justify-content:center;gap:.7rem;flex-wrap:wrap;text-align:center}.landing-v2-bridge__item{display:inline-flex;align-items:center;gap:.4rem;color:var(--muted);font-size:.86rem;font-weight:600}.landing-v2-bridge__item i{color:var(--accent)}
      .landing-v2-section{padding:4rem 0}.landing-v2-problem{border:1px solid var(--border);border-radius:24px;padding:1.5rem;background:var(--surface-strong);box-shadow:var(--shadow-sm)}.landing-v2-problem h3{font-size:1.25rem;font-weight:800;margin-bottom:1rem}
      .landing-v2-list{display:grid;gap:.65rem;margin:0;padding:0;list-style:none}.landing-v2-list li{display:flex;align-items:flex-start;gap:.6rem;color:var(--muted);line-height:1.55}.landing-v2-list i{flex:0 0 auto;margin-top:.15rem}.landing-v2-list.problem i{color:#dc3545}.landing-v2-list.solution i{color:var(--accent)}
      .landing-v2-solution{border-color:rgba(15,98,254,.18);background:linear-gradient(145deg,rgba(15,98,254,.06),rgba(18,185,129,.05))}.landing-v2-kicker{display:inline-flex;align-items:center;gap:.45rem;border-radius:999px;padding:.4rem .75rem;margin-bottom:.8rem;background:rgba(15,98,254,.09);color:var(--primary);font-size:.76rem;font-weight:800;text-transform:uppercase;letter-spacing:.07em}
      .landing-v2-title{font-size:clamp(2rem,4vw,3rem);line-height:1.08;font-weight:800;letter-spacing:-.02em;margin-bottom:.75rem}.landing-v2-subtitle{max-width:780px;margin:0 auto;color:var(--muted);font-size:1.05rem;line-height:1.7}.landing-v2-flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.8rem;margin-top:1.7rem}
      .landing-v2-flow__step{padding:1rem;border:1px solid var(--border);border-radius:16px;background:var(--surface-strong)}.landing-v2-flow__num{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;margin-bottom:.65rem;background:rgba(15,98,254,.1);color:var(--primary);font-weight:900}.landing-v2-flow__step strong{display:block;margin-bottom:.25rem}.landing-v2-flow__step span{display:block;color:var(--muted);font-size:.83rem;line-height:1.5}
      .landing-v2-inline-cta{margin-top:1.4rem;display:flex;justify-content:center;gap:.75rem;flex-wrap:wrap}.landing-v2-sticky{display:none}
      .landing-v2-proof{border:1px solid rgba(15,98,254,.14);border-radius:24px;padding:1rem;background:linear-gradient(145deg,rgba(15,98,254,.06),rgba(18,185,129,.05));box-shadow:var(--shadow-sm)}
      .landing-v2-proof__image{display:block;width:100%;height:100%;min-height:330px;object-fit:cover;object-position:center;border-radius:18px;border:1px solid var(--border);background:var(--surface-strong)}
      .landing-v2-proof__copy{padding:1rem}.landing-v2-proof__steps{display:grid;gap:.7rem;margin-top:1rem}.landing-v2-proof__step{display:flex;gap:.7rem;align-items:flex-start}.landing-v2-proof__num{display:grid;place-items:center;flex:0 0 auto;width:30px;height:30px;border-radius:50%;background:rgba(15,98,254,.1);color:var(--primary);font-weight:900;font-size:.8rem}.landing-v2-proof__step strong{display:block;font-size:.92rem}.landing-v2-proof__step span{display:block;color:var(--muted);font-size:.78rem;line-height:1.45;margin-top:.15rem}
      .landing-v2-quote{border-radius:26px;padding:1.6rem;background:linear-gradient(145deg,rgba(15,98,254,.08),rgba(18,185,129,.07));border:1px solid rgba(15,98,254,.12)}
      .landing-v2-quote__badge{display:inline-flex;align-items:center;gap:.45rem;padding:.4rem .72rem;border-radius:999px;background:rgba(255,255,255,.72);color:var(--primary);font-size:.76rem;font-weight:800}
      .landing-v2-quote__card{height:100%;padding:1.1rem;border:1px solid var(--border);border-radius:16px;background:var(--surface-strong)}.landing-v2-quote__icon{font-size:1.35rem;color:var(--primary)}
      @media(max-width:767px){.landing-v2-section{padding:3rem 0}.landing-v2-flow{grid-template-columns:1fr 1fr}.landing-v2-sticky{position:fixed;z-index:1000;left:12px;right:12px;bottom:12px;display:flex;align-items:center;gap:.65rem;padding:.65rem;border:1px solid var(--border);border-radius:18px;background:var(--surface-muted);box-shadow:0 18px 45px rgba(2,8,23,.18);backdrop-filter:blur(18px)}.landing-v2-sticky__copy{min-width:0;flex:1}.landing-v2-sticky__copy strong{display:block;font-size:.78rem;line-height:1.2}.landing-v2-sticky__copy span{display:block;color:var(--muted);font-size:.68rem;margin-top:.12rem}.landing-v2-sticky .btn{white-space:nowrap;font-weight:800}.landing-v2-proof__image{min-height:250px}.landing-v2-proof__copy{padding:.8rem}.landing-v2-quote{padding:1.15rem}body{padding-bottom:78px}}
      @media(max-width:420px){.landing-v2-flow{grid-template-columns:1fr}.landing-v2-sticky__copy span{display:none}.landing-v2-sticky .btn{padding-inline:.8rem;font-size:.78rem}}
      [data-theme="dark"] .landing-v2-bridge{background:var(--surface-strong)}[data-theme="dark"] .landing-v2-problem{background:var(--surface-strong)}[data-theme="dark"] .landing-v2-proof__image{background:#0f172a}
    `;
    document.head.appendChild(style);

    const hero = document.querySelector(".hero-section");
    const main = document.querySelector("main");
    const videoSection = document.getElementById("video-demo");
    const trialLink = hero?.querySelector('a[href*="selected_plan=trial"]');
    const trialUrl = trialLink ? trialLink.href : "/auth/register?selected_plan=trial";

    if (hero && main && videoSection) {
      const bridge = document.createElement("section");
      bridge.className = "landing-v2-bridge";
      bridge.setAttribute("aria-label", "Beneficios principales");
      bridge.innerHTML = `<div class="container landing-v2-bridge__inner"><span class="landing-v2-bridge__item"><i class="bi bi-check-circle-fill"></i> Prueba gratis 10 días</span><span class="landing-v2-bridge__item"><i class="bi bi-credit-card"></i> Sin tarjeta para empezar</span><span class="landing-v2-bridge__item"><i class="bi bi-phone"></i> PC, celular y tablet</span><span class="landing-v2-bridge__item"><i class="bi bi-headset"></i> Soporte por WhatsApp</span></div>`;
      hero.insertAdjacentElement("afterend", bridge);

      const problem = document.createElement("section");
      problem.id = "landing-commercial-v2";
      problem.className = "landing-v2-section";
      problem.innerHTML = `
        <div class="container">
          <div class="text-center mb-4"><span class="landing-v2-kicker"><i class="bi bi-arrow-repeat"></i> Menos trabajo manual</span><h2 class="landing-v2-title">Dejá de controlar tu comercio a ciegas</h2><p class="landing-v2-subtitle">Centralizá ventas, stock, caja, clientes y presupuestos. Todo en un solo lugar y preparado para usar desde el celular.</p></div>
          <div class="row g-3">
            <div class="col-md-6"><div class="landing-v2-problem h-100"><h3><i class="bi bi-x-circle text-danger me-2"></i>Cuando todo está separado</h3><ul class="landing-v2-list problem"><li><i class="bi bi-x-circle-fill"></i><span>Stock repartido entre planillas y memoria.</span></li><li><i class="bi bi-x-circle-fill"></i><span>Ventas y caja difíciles de seguir durante el día.</span></li><li><i class="bi bi-x-circle-fill"></i><span>Presupuestos enviados por WhatsApp que quedan sin seguimiento.</span></li><li><i class="bi bi-x-circle-fill"></i><span>Poco tiempo para saber qué vender, reponer o cobrar.</span></li></ul></div></div>
            <div class="col-md-6"><div class="landing-v2-problem landing-v2-solution h-100"><h3><i class="bi bi-check-circle text-success me-2"></i>Con StockARmobile</h3><ul class="landing-v2-list solution"><li><i class="bi bi-check-circle-fill"></i><span>POS, stock, caja y clientes conectados.</span></li><li><i class="bi bi-check-circle-fill"></i><span>Presupuesto → aceptación del cliente → conversión a venta.</span></li><li><i class="bi bi-check-circle-fill"></i><span>Alertas para productos críticos y tareas pendientes.</span></li><li><i class="bi bi-check-circle-fill"></i><span>Reportes para decidir con información real.</span></li></ul><div class="landing-v2-inline-cta"><a class="btn btn-primary btn-pill" href="${trialUrl}">Probá gratis 10 días</a><a class="btn btn-outline-primary btn-pill" href="#video-demo">Ver cómo funciona</a></div></div></div>
          </div>
          <div class="landing-v2-flow"><div class="landing-v2-flow__step"><div class="landing-v2-flow__num">1</div><strong>Registrate</strong><span>Creá tu cuenta y empezá sin tarjeta.</span></div><div class="landing-v2-flow__step"><div class="landing-v2-flow__num">2</div><strong>Cargá tu negocio</strong><span>Productos, clientes, stock y usuarios.</span></div><div class="landing-v2-flow__step"><div class="landing-v2-flow__num">3</div><strong>Vendé</strong><span>Usá el POS desde PC o celular y registrá la caja.</span></div><div class="landing-v2-flow__step"><div class="landing-v2-flow__num">4</div><strong>Tomá decisiones</strong><span>Consultá reportes y alertas sin perder tiempo.</span></div></div>
        </div>`;
      videoSection.insertAdjacentElement("beforebegin", problem);

      const quoteSection = document.createElement("section");
      quoteSection.id = "presupuestos-como-venta";
      quoteSection.className = "landing-v2-section section-soft";
      quoteSection.innerHTML = `
        <div class="container">
          <div class="landing-v2-quote">
            <div class="text-center mb-4"><span class="landing-v2-kicker"><i class="bi bi-file-earmark-check"></i> Presupuestos que venden</span><h2 class="landing-v2-title">No mandes un presupuesto y te olvides</h2><p class="landing-v2-subtitle">Seguí el presupuesto desde el envío hasta la venta, con un flujo claro para vos y simple para tu cliente.</p></div>
            <div class="row g-3">
              <div class="col-lg-7"><div class="landing-v2-proof h-100"><img class="landing-v2-proof__image" src="/static/assets/images/landing-demo-poster.jpg" alt="Vista real de StockARmobile" loading="lazy"></div></div>
              <div class="col-lg-5"><div class="landing-v2-proof h-100"><div class="landing-v2-proof__copy"><span class="landing-v2-quote__badge"><i class="bi bi-arrow-right-circle"></i> Flujo comercial</span><div class="landing-v2-proof__steps"><div class="landing-v2-proof__step"><span class="landing-v2-proof__num">1</span><div><strong>Creás el presupuesto</strong><span>Con productos, cantidades, precios y cliente.</span></div></div><div class="landing-v2-proof__step"><span class="landing-v2-proof__num">2</span><div><strong>Lo recibe el cliente</strong><span>Lo abre desde un enlace y puede revisarlo desde el celular.</span></div></div><div class="landing-v2-proof__step"><span class="landing-v2-proof__num">3</span><div><strong>Lo acepta online</strong><span>La aceptación queda registrada en StockARmobile.</span></div></div><div class="landing-v2-proof__step"><span class="landing-v2-proof__num">4</span><div><strong>Lo convertís en venta</strong><span>La operación pasa al circuito del POS para cobrar y descontar stock.</span></div></div></div><div class="landing-v2-inline-cta"><a class="btn btn-primary btn-pill w-100" href="${trialUrl}"><i class="bi bi-rocket-takeoff me-1"></i>Probá este flujo gratis</a></div></div></div></div>
            </div>
          </div>
        </div>`;
      problem.insertAdjacentElement("afterend", quoteSection);

      const mobileCta = document.createElement("div");
      mobileCta.className = "landing-v2-sticky";
      mobileCta.innerHTML = `<div class="landing-v2-sticky__copy"><strong>Probá StockARmobile gratis</strong><span>10 días · sin tarjeta</span></div><a class="btn btn-primary btn-pill" href="${trialUrl}"><i class="bi bi-rocket-takeoff me-1"></i>Probar gratis</a>`;
      document.body.appendChild(mobileCta);

      const heroTitle = hero.querySelector("h1");
      const heroLead = hero.querySelector("p.lead");
      if (heroTitle) heroTitle.textContent = "Controlá tu comercio desde cualquier lugar";
      if (heroLead) heroLead.textContent = "Vendé, controlá stock, manejá caja, clientes y presupuestos desde una sola plataforma. Probalo gratis durante 10 días y seguí tu negocio desde la PC o el celular.";

      /* Reemplazá el mockup abstracto por una captura real del producto disponible en el repositorio. */
      const mockup = hero.querySelector(".hero-mockup");
      if (mockup && !mockup.querySelector(".landing-v2-hero-real")) {
        mockup.innerHTML = `<div class="landing-v2-hero-real" style="position:relative;overflow:hidden;border-radius:22px;border:1px solid var(--border);background:var(--surface-strong)"><img src="/static/assets/images/landing-demo-poster.jpg" alt="Captura real de StockARmobile" style="display:block;width:100%;height:auto;min-height:360px;object-fit:cover;object-position:center"><div style="position:absolute;left:14px;bottom:14px;right:14px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:14px;background:rgba(2,8,23,.78);color:#fff;backdrop-filter:blur(8px)"><span style="font-size:.75rem;font-weight:700">StockARmobile · vista real</span><span style="font-size:.7rem;opacity:.9">Ventas · Stock · Caja</span></div></div>`;
      }

      const installButton = document.getElementById("pwaInstallBtn");
      if (installButton) {
        installButton.querySelector("i")?.classList.remove("bi-download");
        installButton.querySelector("i")?.classList.add("bi-phone");
        const textNode = Array.from(installButton.childNodes).find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
        if (textNode) textNode.textContent = " Instalar como app (opcional)";
      }
    }
  }

  const calcPlan = document.getElementById("calcPlan");
  const calcClients = document.getElementById("calcClients");
  const calcRevenue = document.getElementById("calcRevenue");
  const calcCommission = document.getElementById("calcCommission");
  const calcTotal = document.getElementById("calcTotal");
  const calculatorRate = document.querySelector("[data-commission-percent]");
  const formatARS = function (value) { return "ARS " + (Number.isFinite(value) ? value : 0).toLocaleString("es-AR", { maximumFractionDigits: 2 }); };
  const refreshCalculator = function () {
    if (!calcPlan || !calcClients || !calcRevenue || !calcCommission || !calcTotal || !calculatorRate) return;
    const selected = calcPlan.options[calcPlan.selectedIndex];
    const planPrice = parseFloat(selected?.dataset.price || "0");
    const clientsCount = Math.max(1, parseInt(calcClients.value || "1", 10));
    const commissionPercent = parseFloat(calculatorRate.dataset.commissionPercent || "0");
    const monthlyGenerated = planPrice * clientsCount;
    const commission = monthlyGenerated * commissionPercent;
    calcRevenue.textContent = formatARS(monthlyGenerated);
    calcCommission.textContent = formatARS(commission);
    calcTotal.textContent = formatARS(commission * 12);
  };
  calcPlan?.addEventListener("change", refreshCalculator);
  calcClients?.addEventListener("input", refreshCalculator);
  refreshCalculator();

  const installBtn = document.getElementById("pwaInstallBtn");
  const installHint = document.getElementById("pwaInstallHint");
  let deferredInstallPrompt = null;
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  if (isStandalone) return;
  const showHint = function (message) { if (installHint) { installHint.textContent = message; installHint.hidden = false; } };
  window.addEventListener("beforeinstallprompt", function (event) { event.preventDefault(); deferredInstallPrompt = event; if (installBtn) installBtn.hidden = false; showHint("Instala StockArmobile en tu dispositivo para abrirla como app nativa."); });
  installBtn?.addEventListener("click", async function () {
    if (!deferredInstallPrompt) {
      const isiOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent || "");
      showHint(isiOS ? "En iPhone: toca Compartir y luego 'Añadir a pantalla de inicio'." : "Desde el navegador, abre el menú y selecciona 'Instalar aplicación'.");
      return;
    }
    deferredInstallPrompt.prompt();
    const choice = await deferredInstallPrompt.userChoice;
    if (choice && choice.outcome === "accepted") { showHint("Instalación iniciada. Busca StockArmobile en tu pantalla principal."); installBtn.hidden = true; }
    deferredInstallPrompt = null;
  });
  window.addEventListener("appinstalled", function () { if (installBtn) installBtn.hidden = true; showHint("StockArmobile se instaló correctamente en tu dispositivo."); });
  if (/iphone|ipad|ipod/i.test(window.navigator.userAgent || "") && installBtn) { installBtn.hidden = false; showHint("En iPhone puedes instalarla desde Compartir > Añadir a pantalla de inicio."); }
})();
