const themeBtn = document.getElementById("themeBtn");
if (themeBtn) {
  themeBtn.addEventListener("click", () => {
    const r = document.documentElement;
    const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    r.dataset.theme = dark ? "light" : "dark";
  });
}

const tip = document.getElementById("tip");
document.querySelectorAll(".day[data-tip]").forEach((d) => {
  d.addEventListener("mousemove", (e) => {
    tip.textContent = d.dataset.tip;
    tip.style.opacity = 1;
    tip.style.left = Math.min(e.clientX + 14, innerWidth - 140) + "px";
    tip.style.top = (e.clientY - 14) + "px";
  });
  d.addEventListener("mouseleave", () => tip.style.opacity = 0);
});

function toast(msg) {
  const t = document.getElementById("toast");
  if (!t || !msg) return;
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3200);
}
if (window.__flash) toast(window.__flash);

const jobBox = document.querySelector("[data-job-box]");
if (jobBox) {
  async function pollJob() {
    const res = await fetch("/job/progress", { headers: { "Accept": "application/json" } });
    const job = await res.json();
    jobBox.textContent = `${job.state}${job.kind ? " · " + job.kind : ""} · ${job.done || 0}/${job.total || 0} · inserted ${job.inserted || 0}${job.error ? " · " + job.error : ""}`;
    if (job.state === "running") setTimeout(pollJob, 1600);
  }
  pollJob();
}
