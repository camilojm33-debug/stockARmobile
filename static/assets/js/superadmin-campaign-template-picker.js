(function () {
  "use strict";

  const FIELD_OPTIONS = [
    ["contacto", "Nombre del contacto"],
    ["empresa", "Nombre de la empresa"],
    ["comercio", "Nombre del comercio"],
    ["rubro", "Rubro"],
    ["localidad", "Localidad"],
    ["producto", "Producto (StockArMobile)"],
    ["mensaje", "Texto de la campaña"],
    ["asunto", "Asunto / título"],
    ["trial_url", "Enlace para probar StockArMobile"]
  ];
  const FALLBACK_DEFAULT_FIELDS = ["contacto", "producto", "mensaje", "trial_url"];

  function showFeedback(node, message, kind) {
    if (!node) return;
    node.className = "alert small mb-0 " +
      (kind === "danger" ? "alert-warning" : kind === "success" ? "alert-success" : "alert-info");
    node.textContent = message;
  }

  function startPicker(form) {
    const endpoint = form.dataset.templateListUrl;
    const picker = form.querySelector("[data-whatsapp-template-picker]");
    const nameInput = form.querySelector("[data-whatsapp-template-name]");
    const languageInput = form.querySelector("[data-whatsapp-template-language]");
    const feedback = form.querySelector("[data-whatsapp-template-feedback]");
    const preview = form.querySelector("[data-whatsapp-template-preview]");
    const controls = form.querySelector("[data-whatsapp-parameter-controls]");
    const parametersInput = form.querySelector("[data-whatsapp-parameter-value]");
    const channelSelect = form.querySelector('[name="channel"]');
    if (!endpoint || !picker || !nameInput || !languageInput || !controls || !parametersInput) return;

    const configuredDefaultFields = (form.dataset.defaultFields || "")
      .split(",").map(value => value.trim().toLowerCase()).filter(Boolean);
    const defaultFields = configuredDefaultFields.length ? configuredDefaultFields : FALLBACK_DEFAULT_FIELDS;
    let templates = [];
    let currentTemplate = null;
    let loaded = false;
    let currentMapping = (form.dataset.currentFields || parametersInput.value || "")
      .split(",").map(value => value.trim().toLowerCase()).filter(Boolean);

    function channelNeedsWhatsApp() {
      return ["whatsapp", "both"].includes(String(channelSelect && channelSelect.value || "").toLowerCase());
    }

    function selectedTemplate() {
      return templates.find(item => picker.value === item.name + "||" + item.language) || null;
    }

    function writeMapping() {
      const values = Array.from(controls.querySelectorAll("[data-whatsapp-variable]"))
        .map(select => select.value);
      parametersInput.value = values.length ? values.join(",") : "none";
    }

    function renderVariables(template, preferredValues) {
      controls.textContent = "";
      const count = Number(template && template.variable_count || 0);
      if (!count) {
        parametersInput.value = "none";
        return;
      }
      for (let index = 0; index < count; index += 1) {
        const select = document.createElement("select");
        select.dataset.whatsappVariable = String(index + 1);
        FIELD_OPTIONS.forEach(([value, label]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          select.appendChild(option);
        });
        const fallback = defaultFields[index] || "mensaje";
        const preferred = preferredValues[index] || fallback;
        select.value = FIELD_OPTIONS.some(([value]) => value === preferred) ? preferred : fallback;
        select.addEventListener("change", writeMapping);
        controls.appendChild(select);
      }
      writeMapping();
    }

    function renderStatus() {
      if (!channelNeedsWhatsApp()) {
        showFeedback(feedback, "Esta campaña se enviará por Email. No necesitás configurar WhatsApp.", "success");
      } else if (!loaded) {
        showFeedback(feedback, "Estamos comprobando que WhatsApp esté listo para esta campaña.", "info");
      } else if (currentTemplate && currentTemplate.campaign_compatible) {
        showFeedback(feedback, "WhatsApp está listo. StockARmobile completa automáticamente la configuración del mensaje.", "success");
      } else {
        showFeedback(feedback, "WhatsApp todavía no está listo. Podés guardar el borrador; antes de aprobarlo habrá que revisar la conexión.", "danger");
      }
    }

    function populate(data) {
      templates = Array.isArray(data.templates) ? data.templates : [];
      picker.textContent = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Configuración automática";
      picker.appendChild(placeholder);
      templates.forEach(template => {
        const option = document.createElement("option");
        option.value = template.name + "||" + template.language;
        option.textContent = template.name;
        picker.appendChild(option);
      });

      const storedName = form.dataset.currentName || (data.selected && data.selected.name) || "";
      const storedLanguage = form.dataset.currentLanguage || (data.selected && data.selected.language) || "es_AR";
      const matched = templates.find(item =>
        item.name === storedName && item.language === storedLanguage && item.campaign_compatible
      );
      currentTemplate = matched || templates.find(item => item.campaign_compatible) || null;

      if (currentTemplate) {
        picker.value = currentTemplate.name + "||" + currentTemplate.language;
        nameInput.value = currentTemplate.name;
        languageInput.value = currentTemplate.language;
        renderVariables(currentTemplate, matched && currentMapping.length ? currentMapping : []);
        if (preview) preview.textContent = currentTemplate.body_text || "";
      } else {
        picker.value = "";
        nameInput.value = "";
        languageInput.value = "es_AR";
        parametersInput.value = defaultFields.join(",");
        controls.textContent = "";
        if (preview) preview.textContent = "";
      }
      loaded = true;
      picker.disabled = false;
      renderStatus();
    }

    async function loadTemplates() {
      loaded = false;
      renderStatus();
      picker.disabled = true;
      try {
        const response = await fetch(endpoint, {
          method: "GET",
          credentials: "same-origin",
          headers: { "Accept": "application/json" }
        });
        const data = await response.json();
        if (!response.ok || !data || !data.success) throw new Error("No fue posible verificar el canal.");
        populate(data);
      } catch (_error) {
        templates = [];
        currentTemplate = null;
        loaded = true;
        nameInput.value = "";
        languageInput.value = "es_AR";
        parametersInput.value = defaultFields.join(",");
        controls.textContent = "";
        picker.disabled = false;
        renderStatus();
        if (channelNeedsWhatsApp()) {
          showFeedback(feedback, "No pudimos comprobar WhatsApp ahora. Podés guardar el borrador y volver a revisar antes de aprobarlo.", "danger");
        }
      }
    }

    picker.addEventListener("change", () => {
      // Preserve support for a previously selected template; regular users never need to change it.
      currentTemplate = selectedTemplate();
      if (currentTemplate && currentTemplate.campaign_compatible) {
        nameInput.value = currentTemplate.name;
        languageInput.value = currentTemplate.language;
        renderVariables(currentTemplate, []);
      }
      renderStatus();
    });
    if (channelSelect) channelSelect.addEventListener("change", renderStatus);
    form.addEventListener("submit", () => {
      const chosen = selectedTemplate() || currentTemplate;
      if (chosen && chosen.campaign_compatible) writeMapping();
    });

    loadTemplates();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form.js-whatsapp-template-form").forEach(startPicker);
  });
})();
