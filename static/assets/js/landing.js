(function () {
  const root = document.documentElement;
  const toggle = document.getElementById("themeToggle");

  const getPreferredTheme = function () {
    const savedTheme = localStorage.getItem("stockarmobile-theme");
    if (savedTheme === "light" || savedTheme === "dark") {
      return savedTheme;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };

  const applyTheme = function (theme) {
    root.setAttribute("data-theme", theme);
    localStorage.setItem("stockarmobile-theme", theme);
  };

  applyTheme(getPreferredTheme());

  toggle?.addEventListener("click", function () {
    const nextTheme = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(nextTheme);
  });

  const revealItems = document.querySelectorAll(".reveal");
  const revealObserver = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          const delay = entry.target.getAttribute("data-delay") || "0";
          entry.target.style.setProperty("--delay", delay + "ms");
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.16 }
  );

  revealItems.forEach(function (item) {
    revealObserver.observe(item);
  });

  const counterObserver = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) {
          return;
        }
        const counter = entry.target;
        const value = parseFloat(counter.dataset.counter || "0");
        const suffix = counter.dataset.suffix || "";
        const duration = 1300;
        const start = performance.now();
        const from = 0;

        const step = function (now) {
          const progress = Math.min((now - start) / duration, 1);
          const eased = 1 - Math.pow(1 - progress, 3);
          const current = from + (value - from) * eased;
          const decimals = value % 1 === 0 ? 0 : 1;
          counter.textContent = current.toFixed(decimals) + suffix;
          if (progress < 1) {
            requestAnimationFrame(step);
          }
        };

        requestAnimationFrame(step);
        counterObserver.unobserve(counter);
      });
    },
    { threshold: 0.5 }
  );

  document.querySelectorAll("[data-counter]").forEach(function (counter) {
    counterObserver.observe(counter);
  });

  /* ── Landing comercial 2.0: conversion-focused, non-destructive layer ── */
  if (!document.getElementById("landing-commercial-v2")) {
    const style = document.createElement("style");
    style.id = "landing-commercial-v2-styles";
    style.textContent = `
      .landing-v2-bridge{position:relative;z-index:2;padding:1rem 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:var(--surface-strong)}
      .landing-v2-bridge__inner{display:flex;align-items:center;justify-content:center;gap:.7rem;flex-wrap:wrap;text-align:center}
      .landing-v2-bridge__item{display:inline-flex;align-items:center;gap:.4rem;color:var(--muted);font-size:.86rem;font-weight:600}
      .landing-v2-bridge__item i{color:var(--accent)}
      .landing-v2-section{padding:4rem 0}
      .landing-v2-problem{border:1px solid var(--border);border-radius:24px;padding:1.5rem;background:var(--surface-strong);box-shadow:var(--shadow-sm)}
      .landing-v2-problem h3{font-size:1.25rem;font-weight:800;margin-bottom:1rem}
      .landing-v2-list{display:grid;gap:.65rem;margin:0;padding:0;list-style:none}
      .landing-v2-list li{display:flex;align-items:flex-start;gap:.6rem;color:var(--muted);line-height:1.55}
      .landing-v2-list i{flex:0 0 auto;margin-top:.15rem}
      .landing-v2-list.problem i{color:#dc3545}.landing-v2-list.solution i{color:var(--accent)}
      .landing-v2-solution{border-color:rgba(15,98,254,.18);background:linear-gradient(145deg,rgba(15,98,254,.06),rgba(18,185,129,.05))}
      .landing-v2-kicker{display:inline-flex;align-items:center;gap:.45rem;border-radius:999px;padding:.4rem .75rem;margin-bottom:.8rem;background:rgba(15,98,254,.09);color:var(--primary);font-size:.76rem;font-weight:800;text-transform:uppercase;letter-spacing:.07em}
      .landing-v2-title{font-size:clamp(2rem,4vw,3rem);line-height:1.08;font-weight:800;letter-spacing:-.02em;margin-bottom:.75rem}
      .landing-v2-subtitle{max-width:760px;margin:0 auto;color:var(--muted);font-size:1.05rem;line-height:1.7}
      .landing-v2-flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.8rem;margin-top:1.7rem}
      .landing-v2-flow__step{position:relative;padding:1rem;border:1px solid var(--border);border-radius:16px;background:var(--surface-strong)}
      .landing-v2-flow__num{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;margin-bottom:.65rem;background:rgba(15,98,254,.1);color:var(--primary);font-weight:900}
      .landing-v2-flow__step strong{display:block;margin-bottom:.25rem}.landing-v2-flow__step span{display:block;color:var(--muted);font-size:.83rem;line-height:1.5}
      .landing-v2-inline-cta{margin-top:1.4rem;display:flex;justify-content:center;gap:.75rem;flex-wrap:wrap}
      .landing-v2-sticky{display:none}
      [data-theme="dark"] .landing-v2-bridge{background:var(--surface-strong)}
      [data-theme="dark"] .landing-v2-problem{background:var(--surface-strong)}
      @media(max-width:767px){
        .landing-v2-section{padding:3rem 0}.landing-v2-flow{grid-template-columns:1fr 1fr}.landing-v2-sticky{position:fixed;z-index:1000;left:12px;right:12px;bottom:12px;display:flex;align-items:center;gap:.65rem;padding:.65rem;border:1px solid var(--border);border-radius:18px;background:var(--surface-muted);box-shadow:0 18px 45px rgba(2,8,23,.18);backdrop-filter:blur(18px)}
        .landing-v2-sticky__copy{min-width:0;flex:1}.landing-v2-sticky__copy strong{display:block;font-size:.78rem;line-height:1.2}.landing-v2-sticky__copy span{display:block;color:var(--muted);font-size:.68rem;margin-top:.12rem}.landing-v2-sticky .btn{white-space:nowrap;font-weight:800}
        body{padding-bottom:78px}
      }
      @media(max-width:420px){.landing-v2-flow{grid-template-columns:1fr}.landing-v2-sticky__copy span{display:none}.landing-v2-sticky .btn{padding-inline:.8rem;font-size:.78rem}}
    `;
    document.head.appendChild(style);

    const hero = document.querySelector(".hero-section");
    const main = document.querySelector("main");
    const videoSection = document.getElementById("video-demo");

    if (hero && main && videoSection) {
      const bridge = document.createElement("section");
      bridge.className = "landing-v2-bridge";
      bridge.setAttribute("aria-label", "Beneficios principales");
      bridge.innerHTML = `
        <div class="container landing-v2-bridge__inner">
          <span class="landing-v2-bridge__item"><i class="bi bi-check-circle-fill"></i> Prueba gratis 10 días</span>
          <span class="landing-v2-bridge__item"><i class="bi bi-credit-card"></i> Sin tarjeta para empezar</span>
          <span class="landing-v2-bridge__item"><i class="bi bi-phone"></i> PC, celular y tablet</span>
          <span class="landing-v2-bridge__item"><i class="bi bi-headset"></i> Soporte por WhatsApp</span>
        </div>`;
      hero.insertAdjacentElement("afterend", bridge);

      const problem = document.createElement("section");
      problem.id = "landing-commercial-v2";
      problem.className = "landing-v2-section";
      problem.innerHTML = `
        <div class="container">
          <div class="text-center mb-4">
            <span class="landing-v2-kicker"><i class="bi bi-arrow-repeat"></i> Menos trabajo manual</span>
            <h2 class="landing-v2-title">Dejá de controlar tu comercio a ciegas</h2>
            <p class="landing-v2-subtitle">Centralizá ventas, stock, caja, clientes y presupuestos. Todo en un solo lugar y preparado para usar desde el celular.</p>
          </div>
          <div class="row g-3">
            <div class="col-md-6">
              <div class="landing-v2-problem h-100">
                <h3><i class="bi bi-x-circle text-danger me-2"></i>Cuando todo está separado</h3>
                <ul class="landing-v2-list problem">
                  <li><i class="bi bi-x-circle-fill"></i><span>Stock repartido entre planillas y memoria.</span></li>
                  <li><i class="bi bi-x-circle-fill"></i><span>Ventas y caja difíciles de seguir durante el día.</span></li>
                  <li><i class="bi bi-x-circle-fill"></i><span>Presupuestos enviados por WhatsApp que quedan sin seguimiento.</span></li>
                  <li><i class="bi bi-x-circle-fill"></i><span>Poco tiempo para saber qué vender, reponer o cobrar.</span></li>
                </ul>
              </div>
            </div>
            <div class="col-md-6">
              <div class="landing-v2-problem landing-v2-solution h-100">
                <h3><i class="bi bi-check-circle text-success me-2"></i>Con StockARmobile</h3>
                <ul class="landing-v2-list solution">
                  <li><i class="bi bi-check-circle-fill"></i><span>POS, stock, caja y clientes conectados.</span></li>
                  <li><i class="bi bi-check-circle-fill"></i><span>Presupuesto → aceptación del cliente → conversión a venta.</span></li>
                  <li><i class="bi bi-check-circle-fill"></i><span>Alertas para productos críticos y tareas pendientes.</span></li>
                  <li><i class="bi bi-check-circle-fill"></i><span>Reportes para decidir con información real.</span></li>
                </ul>
                <div class="landing-v2-inline-cta">
                  <a class="btn btn-primary btn-pill" href="/auth/register?selected_plan=trial">Probá gratis 10 días</a>
                  <a class="btn btn-outline-primary btn-pill" href="#video-demo">Ver cómo funciona</a>
                </div>
              </div>
            </div>
          </div>
          <div class="landing-v2-flow">
            <div class="landing-v2-flow__step"><div class="landing-v2-flow__num">1</div><strong>Registrate</strong><span>Creá tu cuenta y empezá sin tarjeta.</span></div>
            <div class="landing-v2-flow__step"><div class="landing-v2-flow__num">2</div><strong>Cargá tu negocio</strong><span>Productos, clientes, stock y usuarios.</span></div>
            <div class="landing-v2-flow__step"><div class="landing-v2-flow__num">3</div><strong>Vendé</strong><span>Usá el POS desde PC o celular y registrá la caja.</span></div>
            <div class="landing-v2-flow__step"><div class="landing-v2-flow__num">4</div><strong>Tomá decisiones</strong><span>Consultá reportes y alertas sin perder tiempo.</span></div>
          </div>
        </div>`;
      videoSection.insertAdjacentElement("beforebegin", problem);

      const mobileCta = document.createElement("div");
      mobileCta.className = "landing-v2-sticky";
      mobileCta.innerHTML = `
        <div class="landing-v2-sticky__copy"><strong>Probá StockARmobile gratis</strong><span>10 días · sin tarjeta</span></div>
        <a class="btn btn-primary btn-pill" href="/auth/register?selected_plan=trial"><i class="bi bi-rocket-takeoff me-1"></i>Probar gratis</a>`;
      document.body.appendChild(mobileCta);

      const heroTitle = hero.querySelector("h1");
      const heroLead = hero.querySelector("p.lead");
      if (heroTitle) {
        heroTitle.textContent = "Controlá tu comercio desde cualquier lugar";
      }
      if (heroLead) {
        heroLead.textContent = "Vendé, controlá stock, manejá caja, clientes y presupuestos desde una sola plataforma. Probalo gratis durante 10 días y seguí tu negocio desde la PC o el celular.";
      }

      const installButton = document.getElementById("pwaInstallBtn");
      if (installButton) {
        installButton.querySelector("i")?.classList.remove("bi-download");
        installButton.querySelector("i")?.classList.add("bi-phone");
        const textNode = Array.from(installButton.childNodes).find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
        if (textNode) {
          textNode.textContent = " Instalar como app (opcional)";
        }
      }
    }
  }

  const calcPlan = document.getElementById("calcPlan");
  const calcClients = document.getElementById("calcClients");
  const calcRevenue = document.getElementById("calcRevenue");
  const calcCommission = document.getElementById("calcCommission");
  const calcTotal = document.getElementById("calcTotal");
  const calculatorRate = document.querySelector("[data-commission-percent]");

  const formatARS = function (value) {
    const number = Number.isFinite(value) ? value : 0;
    return "ARS " + number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
  };

  const refreshCalculator = function () {
    if (!calcPlan || !calcClients || !calcRevenue || !calcCommission || !calcTotal || !calculatorRate) {
      return;
    }

    const selected = calcPlan.options[calcPlan.selectedIndex];
    const planPrice = parseFloat(selected?.dataset.price || "0");
    const clientsCount = Math.max(1, parseInt(calcClients.value || "1", 10));
    const commissionPercent = parseFloat(calculatorRate.dataset.commissionPercent || "0");

    const monthlyGenerated = planPrice * clientsCount;
    const commission = monthlyGenerated * commissionPercent;
    const estimatedTotal = commission * 12;

    calcRevenue.textContent = formatARS(monthlyGenerated);
    calcCommission.textContent = formatARS(commission);
    calcTotal.textContent = formatARS(estimatedTotal);
  };

  calcPlan?.addEventListener("change", refreshCalculator);
  calcClients?.addEventListener("input", refreshCalculator);
  refreshCalculator();

  const installBtn = document.getElementById("pwaInstallBtn");
  const installHint = document.getElementById("pwaInstallHint");
  let deferredInstallPrompt = null;

  const isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  if (isStandalone) {
    return;
  }

  const showHint = function (message) {
    if (!installHint) {
      return;
    }
    installHint.textContent = message;
    installHint.hidden = false;
  };

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installBtn) {
      installBtn.hidden = false;
    }
    showHint("Instala StockArmobile en tu dispositivo para abrirla como app nativa.");
  });

  installBtn?.addEventListener("click", async function () {
    if (!deferredInstallPrompt) {
      const isiOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent || "");
      if (isiOS) {
        showHint("En iPhone: toca Compartir y luego 'Añadir a pantalla de inicio'.");
      } else {
        showHint("Desde el navegador, abre el menú y selecciona 'Instalar aplicación'.");
      }
      return;
    }

    deferredInstallPrompt.prompt();
    const choice = await deferredInstallPrompt.userChoice;
    if (choice && choice.outcome === "accepted") {
      showHint("Instalación iniciada. Busca StockArmobile en tu pantalla principal.");
      installBtn.hidden = true;
    }
    deferredInstallPrompt = null;
  });

  window.addEventListener("appinstalled", function () {
    if (installBtn) {
      installBtn.hidden = true;
    }
    showHint("StockArmobile se instaló correctamente en tu dispositivo.");
  });

  const isiOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent || "");
  if (isiOS && installBtn) {
    installBtn.hidden = false;
    showHint("En iPhone puedes instalarla desde Compartir > Añadir a pantalla de inicio.");
  }
})();
