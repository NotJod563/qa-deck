"use strict";

for (const opener of document.querySelectorAll("[data-dialog-open]")) {
  const dialog = document.getElementById(opener.dataset.dialogOpen);
  opener.addEventListener("click", () => {
    if (!dialog) return;
    dialog.showModal();
    dialog.querySelector("[autofocus]")?.focus();
  });
  dialog?.addEventListener("close", () => opener.focus());
}

for (const closer of document.querySelectorAll("[data-dialog-close]")) {
  closer.addEventListener("click", () => closer.closest("dialog")?.close());
}

for (const dialog of document.querySelectorAll("dialog[data-dialog-auto-open]")) {
  const returnTarget = document.querySelector("[data-dialog-return]");
  dialog.addEventListener("close", () => returnTarget?.focus());
  dialog.showModal();
  dialog.querySelector("[autofocus]")?.focus();
}
