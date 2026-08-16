(() => {
  const form = document.querySelector("[data-product-form]");
  if (!form) return;
  const executable = form.querySelector("[data-product-executable]");
  const name = form.querySelector("[data-product-name]");
  const workingDirectory = form.querySelector("[data-product-working-directory]");
  let derivedName = "";
  let derivedDirectory = "";

  executable.addEventListener("input", () => {
    const value = executable.value.trim().replace(/^(["'])(.*)\1$/, "$2");
    const separator = Math.max(value.lastIndexOf("\\"), value.lastIndexOf("/"));
    const filename = value.slice(separator + 1);
    const nextName = filename.replace(/\.[^.]*$/, "") || filename;
    const nextDirectory = separator >= 0 ? value.slice(0, separator) : "";
    if (!name.value.trim() || name.value === derivedName) name.value = nextName;
    if (!workingDirectory.value.trim() || workingDirectory.value === derivedDirectory) {
      workingDirectory.value = nextDirectory;
    }
    derivedName = nextName;
    derivedDirectory = nextDirectory;
  });
})();
