(function () {
  "use strict";

  function field(form, name) {
    return name ? form.querySelector('[name="' + name + '"]') : null;
  }

  function start(select) {
    const form = select.closest("form");
    if (!form) return;
    select.addEventListener("change", function () {
      const option = select.selectedOptions[0];
      if (!option || !option.value) return;

      const goal = field(form, select.dataset.goalField);
      if (goal && option.dataset.goal && goal.value !== option.dataset.goal) {
        goal.value = option.dataset.goal;
        goal.dispatchEvent(new Event("change", {bubbles: true}));
      }

      const subject = field(form, select.dataset.subjectField);
      const textBody = field(form, select.dataset.textField);
      const htmlBody = field(form, select.dataset.htmlField);
      if (subject && option.dataset.subject) {
        subject.value = option.dataset.subject;
        subject.dispatchEvent(new Event("input", {bubbles: true}));
      }
      if (textBody && option.dataset.text !== undefined) {
        textBody.value = option.dataset.text;
        textBody.dispatchEvent(new Event("input", {bubbles: true}));
      }
      if (htmlBody && option.dataset.html !== undefined && option.dataset.html) {
        htmlBody.value = option.dataset.html;
        htmlBody.dispatchEvent(new Event("input", {bubbles: true}));
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-campaign-copy-picker]").forEach(start);
  });
})();
