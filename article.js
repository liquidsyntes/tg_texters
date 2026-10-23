document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-text]");
  if (!button) return;

  const original = button.textContent;
  const ukrainian = document.documentElement.lang === "uk";
  try {
    await navigator.clipboard.writeText(button.dataset.copyText);
    button.textContent = ukrainian ? "Скопійовано" : "Скопировано";
  } catch {
    button.textContent = ukrainian ? "Не вдалося скопіювати" : "Не удалось скопировать";
  }
  window.setTimeout(() => { button.textContent = original; }, 1800);
});
