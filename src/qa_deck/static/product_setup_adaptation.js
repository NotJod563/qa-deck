"use strict";

const joinPortablePath = (base, relative) => {
  if (!base || !relative) return "";
  if (relative === ".") return base.replace(/[\\/]+$/, "");
  const separator = base.includes("\\") ? "\\" : "/";
  const normalizedBase = base.replace(/[\\/]+$/, "");
  return `${normalizedBase}${separator}${relative.replaceAll("/", separator)}`;
};

for (const button of document.querySelectorAll("[data-derive-product-paths]")) {
  button.addEventListener("click", () => {
    const install = document.getElementById(button.dataset.installInput)?.value.trim();
    const executable = document.getElementById(button.dataset.executableInput);
    const working = document.getElementById(button.dataset.workingInput);
    if (!install || !executable) return;
    executable.value = joinPortablePath(install, button.dataset.executableRelative);
    if (working && button.dataset.workingRelative) {
      working.value = joinPortablePath(install, button.dataset.workingRelative);
    }
  });
}
