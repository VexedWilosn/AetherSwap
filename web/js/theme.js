const Theme = {
  get() {
    return localStorage.getItem("theme") || "dark";
  },
  set(mode) {
    localStorage.setItem("theme", mode);
    this.apply(mode);
  },
  apply(mode) {
    const root = document.documentElement;
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const m = mode === "system" ? (prefersDark ? "dark" : "light") : mode;
    root.dataset.theme = m;
  },
  cycle() {
    const cur = this.get();
    const next = cur === "dark" ? "light" : cur === "light" ? "system" : "dark";
    this.set(next);
    toast("主题已切换", next === "system" ? "跟随系统" : next === "dark" ? "深色" : "浅色");
  },
};
