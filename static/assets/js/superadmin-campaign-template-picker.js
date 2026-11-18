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
  const DEFAULT_FIELDS = ["contacto", "producto", "mensaje", "trial_url"];

  function showFeedback(node, message, kind) {
    if (!node) return;
    node.className = "alert small mb-2 " + (kind === "danger" ? "alert-danger" : kind === "success" ? "alert-success" : "alert-info");
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
    const defaultsInput = form.querySelector("[data-whatsapp-defaults]");
    const goalSelect = form.querySelector("[data-campaign-goal]");
    const refreshButton = form.querySelector("[data-whatsapp-template-refresh]");
    const channelSelect = form.querySelector('[name="channel"]');
    if (!endpoint || !picker || !nameInput || !languageInput || !controls || !parametersInput) return;

    let templates = [];
    let loaded = false;
    let currentMapping = (form.dataset.currentFields || parametersInput.value || "")
      .split(",").map(value => value.trim().toLowerCase()).filter(Boolean);
    let currentDefaults = {};
    try { currentDefaults = JSON.parse(defaultsInput && defaultsInput.value || "{}") || {}; } catch (_error) { currentDefaults = {}; }

    function goalPreset() {
      const option = goalSelect && goalSelect.selectedOptions[0];
      let mapping = [], defaults = {};
      try { mapping = JSON.parse(option && option.dataset.defaultFields || "[]"); } catch (_error) { mapping = []; }
      try { defaults = JSON.parse(option && option.dataset.defaultValues || "{}"); } catch (_error) { defaults = {}; }
      return {mapping: Array.isArray(mapping) ? mapping : [], defaults: defaults && typeof defaults === "object" ? defaults : {}};
    }

    function channelNeedsWhatsApp() {
      return ["whatsapp", "both"].includes(String(channelSelect && channelSelect.value || "").toLowerCase());
    }

    function updateRequired() {
      picker.required = channelNeedsWhatsApp();
      if (!picker.value && channelNeedsWhatsApp()) {
        picker.setCustomValidity("Seleccioná una plantilla aprobada y compatible de WhatsApp.");
      } else {
        picker.setCustomValidity("");
      }
    }

    function selectedTemplate() {
      const value = picker.value;
      return templates.find(item => value === item.name + "||" + item.language) || null;
    }

    function writeMapping() {
      const selects = Array.from(controls.querySelectorAll("[data-whatsapp-variable]"));
      const values = selects.map(select => select.value);
      const defaults = {};
      selects.forEach(select => {
        const input = controls.querySelector('[data-whatsapp-variable-default="' + select.dataset.whatsappVariable + '"]');
        const value = String(input && input.value || "").trim();
        if (value) defaults[select.value] = value;
      });
      parametersInput.value = values.length ? values.join(",") : "none";
      if (defaultsInput) defaultsInput.value = JSON.stringify(defaults);
    }

    function renderVariables(template, preferredValues) {
      controls.textContent = "";
      const count = Number(template && template.variable_count || 0);
      if (!count) {
        parametersInput.value = "none";
        const note = document.createElement("div");
        note.className = "small text-muted";
        note.textContent = "Esta plantilla no requiere variables dinámicas.";
        controls.appendChild(note);
        return;
      }

      const heading = document.createElement("div");
      heading.className = "fw-semibold small mb-2";
      heading.textContent = "Datos que se insertarán en cada variable de la plantilla";
      controls.appendChild(heading);

      for (let index = 0; index < count; index += 1) {
        const row = document.createElement("div");
        row.className = "row g-2 align-items-center mb-2";

        const labelWrap = document.createElement("div");
        labelWrap.className = "col-sm-3";
        const label = document.createElement("label");
        label.className = "form-label small mb-0";
        label.textContent = "Variable {{" + (index + 1) + "}}";
        labelWrap.appendChild(label);

        const selectWrap = document.createElement("div");
        selectWrap.className = "col-sm-9";
        const select = document.createElement("select");
        select.className = "form-select form-select-sm";
        select.dataset.whatsappVariable = String(index + 1);
        FIELD_OPTIONS.forEach(([value, title]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = title;
          select.appendChild(option);
        });
        const preset = goalPreset();
        const defaultValue = (preset.mapping && preset.mapping[index]) || DEFAULT_FIELDS[index] || "mensaje";
        select.value = preferredValues[index] || defaultValue;
        if (!FIELD_OPTIONS.some(([value]) => value === select.value)) {
          select.value = defaultValue;
        }
        const defaultInput = document.createElement("input");
        defaultInput.type = "text";
        defaultInput.className = "form-control form-control-sm mt-2";
        defaultInput.maxLength = 300;
        defaultInput.placeholder = "Valor predeterminado si el prospecto no tiene este dato";
        defaultInput.dataset.whatsappVariableDefault = String(index + 1);
        defaultInput.value = currentDefaults[select.value] || (preset.defaults && preset.defaults[select.value]) || "";
        select.addEventListener("change", function () {
          defaultInput.value = currentDefaults[select.value] || (goalPreset().defaults && goalPreset().defaults[select.value]) || "";
          writeMapping();
        });
        defaultInput.addEventListener("input", writeMapping);
        selectWrap.appendChild(select);
        selectWrap.appendChild(defaultInput);
        row.appendChild(labelWrap);
        row.appendChild(selectWrap);
        controls.appendChild(row);
      }
      writeMapping();
    }

    function renderSelected(template, preserveMapping, clearHiddenWhenEmpty) {
      if (!template) {
        if (clearHiddenWhenEmpty !== false) {
          nameInput.value = "";
          languageInput.value = "";
          parametersInput.value = "";
        }
        controls.textContent = "";
        if (preview) preview.textContent = "Elegí una plantilla MARKETING aprobada y compatible de tu cuenta de Meta.";
        updateRequired();
        return;
      }

      nameInput.value = template.name;
      languageInput.value = template.language;
      if (preview) {
        const state = template.campaign_compatible
          ? "Aprobada y compatible · " + Number(template.variable_count || 0) + " variable(s)"
          : (template.compatibility_reason || "No compatible con campañas.");
        preview.textContent = template.name + " (" + template.language + ") · " + state;
        if (template.body_text) preview.textContent += "\n" + template.body_text;
      }
      if (template.campaign_compatible) {
        renderVariables(template, preserveMapping ? currentMapping : []);
      } else {
        controls.textContent = "";
        parametersInput.value = "";
      }
      updateRequired();
    }

    function populate(data) {
      templates = Array.isArray(data.templates) ? data.templates : [];
      picker.textContent = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = templates.length ? "Seleccioná una plantilla..." : "No hay plantillas disponibles";
      picker.appendChild(placeholder);

      templates.forEach(template => {
        const option = document.createElement("option");
        option.value = template.name + "||" + template.language;
        option.textContent = template.name + " · " + template.language + " · " +
          (template.category || "SIN CATEGORÍA") + " · " + (template.status || "ESTADO DESCONOCIDO") +
          (template.campaign_compatible ? "" : " · No compatible");
        option.disabled = !template.campaign_compatible;
        picker.appendChild(option);
      });

      const storedName = form.dataset.currentName || (data.selected && data.selected.name) || "";
      const storedLanguage = form.dataset.currentLanguage || (data.selected && data.selected.language) || "es_AR";
      const matched = templates.find(item => item.name === storedName && item.language === storedLanguage && item.campaign_compatible);
      const firstCompatible = templates.find(item => item.campaign_compatible);
      const chosen = matched || (!form.dataset.currentName ? firstCompatible : null);

      if (chosen) {
        picker.value = chosen.name + "||" + chosen.language;
        renderSelected(chosen, matched !== null && currentMapping.length > 0);
        if (!matched) currentMapping = [];
      } else {
        picker.value = "";
        renderSelected(null, false, false);
        if (form.dataset.currentName) {
          showFeedback(feedback, "La plantilla guardada ya no aparece como aprobada y compatible en Meta. Seleccioná otra para poder enviar.", "danger");
        } else if (!templates.length) {
          showFeedback(feedback, "Meta no devolvió plantillas para esta cuenta comercial.", "danger");
        } else {
          showFeedback(feedback, "Hay plantillas en Meta, pero ninguna está aprobada y es compatible con campañas del CRM.", "danger");
        }
      }

      const incompatible = templates.filter(item => !item.campaign_compatible).length;
      if (chosen) {
        showFeedback(
          feedback,
          "Plantillas sincronizadas desde la cuenta de WhatsApp Comercial. " +
            (incompatible ? incompatible + " plantilla(s) no se pueden usar en campañas por sus variables o categoría." : "Todas las plantillas listadas son compatibles."),
          "success"
        );
      }
      loaded = true;
      picker.disabled = false;
      updateRequired();
    }

    async function loadTemplates() {
      picker.disabled = true;
      showFeedback(feedback, "Consultando plantillas aprobadas directamente en Meta...", "info");
      try {
        const response = await fetch(endpoint, {
          method: "GET",
          credentials: "same-origin",
          headers: { "Accept": "application/json" }
        });
        const data = await response.json();
        if (!response.ok || !data || !data.success) {
          throw new Error((data && data.error) || "No se pudieron consultar las plantillas de WhatsApp.");
        }
        populate(data);
      } catch (error) {
        templates = [];
        picker.textContent = "";
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No se pudo cargar la lista";
        picker.appendChild(option);
        picker.disabled = false;
        loaded = false;
        renderSelected(null, false, false);
        showFeedback(feedback, error.message || "No se pudo conectar con Meta.", "danger");
      }
    }

    picker.addEventListener("change", () => {
      currentMapping = [];
      renderSelected(selectedTemplate(), false);
      updateRequired();
    });
    if (refreshButton) refreshButton.addEventListener("click", loadTemplates);
    if (channelSelect) channelSelect.addEventListener("change", updateRequired);
    if (goalSelect) goalSelect.addEventListener("change", function () {
      const preset = goalPreset();
      currentMapping = preset.mapping || [];
      currentDefaults = Object.assign({}, preset.defaults || {});
      renderSelected(selectedTemplate(), false);
    });
    form.addEventListener("submit", event => {
      const chosen = selectedTemplate();
      if (loaded && chosen && chosen.campaign_compatible) writeMapping();
      updateRequired();
      if (channelNeedsWhatsApp() && (!loaded || !chosen || !chosen.campaign_compatible)) {
        event.preventDefault();
        picker.reportValidity();
        if (feedback) feedback.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });

    loadTemplates();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form.js-whatsapp-template-form").forEach(startPicker);
  });
})();
