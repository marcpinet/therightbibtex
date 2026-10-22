"use strict";
/* Préférences d'affichage de cet appareil (thème, largeur de la barre latérale),
   appliquées avant le premier affichage pour éviter un éclair du mauvais thème. */
(() => {
  const read = (k) => { try { return JSON.parse(localStorage.getItem("cite." + k)); } catch { return null; } };
  const theme = read("theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  const w = read("sideW");
  if (typeof w === "number" && w >= 200 && w <= 480) document.documentElement.style.setProperty("--side-w", w + "px");
})();
