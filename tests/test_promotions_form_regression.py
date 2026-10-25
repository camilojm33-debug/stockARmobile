import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from promotions import _validate_payload


def _promotion_rule_script():
    template = Path("templates/promociones/index.html").read_text(encoding="utf-8")
    for script in re.findall(r"<script>(.*?)</script>", template, flags=re.DOTALL):
        if "syncPromotionRule" in script and "promotionType" in script:
            return script
    raise AssertionError("No se encontró el controlador JavaScript de las reglas de promoción.")


def test_promotion_rule_script_parses_and_switches_to_percentage_fields():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js no está disponible para validar el JavaScript de promociones.")

    script = _promotion_rule_script()
    result = subprocess.run(
        [node, "--check", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = SCRIPT_SOURCE;

class FakeElement {
  constructor(id, hidden = false) {
    this.id = id;
    this.hidden = hidden;
    this.value = "";
    this.handlers = {};
    this.inputs = [{disabled: false}, {disabled: false}];
    this.classList = {
      toggle: (className, force) => {
        if (className === "d-none") this.hidden = Boolean(force);
      }
    };
  }
  querySelectorAll() { return this.inputs; }
  addEventListener(event, callback) { this.handlers[event] = callback; }
}

const promotionType = new FakeElement("promotionType");
promotionType.value = "bogo";
const elements = {
  promotionType,
  ruleBogo: new FakeElement("ruleBogo", false),
  rulePercent: new FakeElement("rulePercent", true),
  ruleFixed: new FakeElement("ruleFixed", true)
};
const document = {getElementById: id => elements[id]};
vm.runInNewContext(source, {document});

function choose(type) {
  promotionType.value = type;
  promotionType.handlers.change();
  for (const [id, expectedType] of [
    ["ruleBogo", "bogo"],
    ["rulePercent", "percent_quantity"],
    ["ruleFixed", "fixed_quantity"]
  ]) {
    const rule = elements[id];
    const enabled = type === expectedType;
    assert.equal(rule.hidden, !enabled, id + " visibility");
    assert.ok(rule.inputs.every(input => input.disabled === !enabled), id + " input state");
  }
}

choose("bogo");
choose("percent_quantity");
choose("fixed_quantity");
choose("percent_quantity");
""".replace("SCRIPT_SOURCE", json.dumps(script))

    executed = subprocess.run(
        [node, "-e", harness],
        text=True,
        capture_output=True,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity"])
def test_promotion_controller_rejects_non_finite_quantity_or_percent(bad_value):
    with pytest.raises(ValueError, match="valores numéricos"):
        _validate_payload({
            "type": "percent_quantity",
            "category": "Bebidas",
            "min_quantity": bad_value,
            "discount_percent": "15",
            "name": "Promoción QA",
            "active": "1",
            "priority": "100",
        })

    with pytest.raises(ValueError, match="valores numéricos"):
        _validate_payload({
            "type": "percent_quantity",
            "category": "Bebidas",
            "min_quantity": "3",
            "discount_percent": bad_value,
            "name": "Promoción QA",
            "active": "1",
            "priority": "100",
        })
