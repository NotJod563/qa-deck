"use strict";

for (const scope of document.querySelectorAll("[data-selection-scope]")) {
  const checkboxes = Array.from(
    scope.querySelectorAll("input[type='checkbox'][data-selectable-item]"),
  );
  const count = scope.querySelector("[data-selected-count]");
  const submit = scope.querySelector("[data-selection-submit]");
  const empty = scope.querySelector("[data-selection-empty]");

  const updateCard = (checkbox) => {
    const card = checkbox.closest(".setup-configuration-card");
    if (!card) return;
    card.classList.toggle("setup-card-unselected", !checkbox.checked);
    for (const field of card.querySelectorAll("input:not([data-selectable-item])")) {
      field.disabled = !checkbox.checked;
    }
  };

  const update = () => {
    const selected = checkboxes.filter((item) => item.checked).length;
    if (count) count.textContent = String(selected);
    if (submit) submit.disabled = selected === 0;
    if (empty) empty.hidden = selected !== 0;
    checkboxes.forEach(updateCard);
  };

  scope.querySelector("[data-select-all]")?.addEventListener("click", () => {
    checkboxes.forEach((item) => { item.checked = true; });
    update();
  });
  scope.querySelector("[data-clear-selection]")?.addEventListener("click", () => {
    checkboxes.forEach((item) => { item.checked = false; });
    update();
  });
  checkboxes.forEach((item) => item.addEventListener("change", update));
  update();
}
