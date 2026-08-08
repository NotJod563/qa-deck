const registryTypeHelp = {
  REG_DWORD: "Ціле число, наприклад 0, 1, 2 або 255",
  REG_QWORD: "Ціле 64-бітне число, наприклад 4294967296",
  REG_SZ: "Текстове значення, наприклад QA",
  REG_EXPAND_SZ: "Рядок зі змінними середовища, наприклад %LOCALAPPDATA%\\Vendor\\Product",
  REG_MULTI_SZ: "Один елемент на рядок",
  REG_BINARY: "Шістнадцяткові байти, наприклад 01 FF A0 2C",
};

const registryPresentationValue = (value) =>
  ({ visible: "Видима", hidden: "Прихована" })[value] || value;

function updatePresetEditor(editor) {
  const included = [];
  editor.querySelectorAll("[data-preset-target]").forEach((card) => {
    const checkbox = card.querySelector("[data-include-target]");
    const desired = card.querySelector("[data-desired-controls]");
    const controls = desired.querySelectorAll("select, input, textarea");
    controls.forEach((control) => {
      control.disabled = !checkbox.checked;
    });
    desired.classList.toggle("is-inactive", !checkbox.checked);
    desired.setAttribute("aria-disabled", String(!checkbox.checked));

    const typeSelect = card.querySelector("[data-registry-type]");
    const valueControl = card.querySelector("[data-registry-value]");
    const preview = card.querySelector("[data-desired-preview]");
    const help = card.querySelector("[data-type-help]");
    if (help && typeSelect) help.textContent = registryTypeHelp[typeSelect.value];
    if (preview && valueControl) {
      preview.textContent = registryPresentationValue(valueControl.value) || "—";
    }
    if (checkbox.checked && valueControl) {
      const current = card.dataset.currentState;
      if (typeSelect) {
        const transition = current ? `${current} → ` : "→ ";
        included.push(`${card.dataset.targetName}: ${transition}${valueControl.value || "—"} (${typeSelect.value})`);
      } else {
        const transition = current ? `${registryPresentationValue(current)} → ` : "→ ";
        included.push(`${card.dataset.targetName}: ${transition}${registryPresentationValue(valueControl.value) || "—"}`);
      }
    }
  });

  const name = editor.querySelector("[data-preset-name]").value.trim();
  editor.querySelector("[data-summary-name]").textContent = name || "Новий preset";
  const summary = editor.querySelector("[data-preset-summary]");
  summary.replaceChildren();
  if (included.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "У preset ще не включено ресурсів.";
    summary.append(empty);
  } else {
    included.forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      summary.append(item);
    });
  }
  const submit = editor.closest("form").querySelector('button[type="submit"]');
  if (submit) submit.disabled = included.length === 0;
}

document.querySelectorAll("[data-preset-editor]").forEach((editor) => {
  editor.addEventListener("input", () => updatePresetEditor(editor));
  editor.addEventListener("change", () => updatePresetEditor(editor));
  updatePresetEditor(editor);
});
