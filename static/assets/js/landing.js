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
})();
