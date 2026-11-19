const $ = (s) => document.querySelector(s);
const pct = (v) => (v == null ? "n/a" : (v * 100).toFixed(1) + "%");
let chart;
let plantsById = {};

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

async function loadPlants() {
  const plants = await get("/api/plants");
  plantsById = Object.fromEntries(plants.map((p) => [p.id, p]));
  $("#plant").innerHTML = plants.map((p) => `<option value="${p.id}">${p.name} (${p.city})</option>`).join("");
  $("#plant").onchange = () => show($("#plant").value);
  show(plants[0].id);
}

async function show(id) {
  const [monthly, daily, inverters, alerts, quality] = await Promise.all([
    get(`/api/plants/${id}/monthly`), get(`/api/plants/${id}/daily`), get(`/api/plants/${id}/inverters`),
    get(`/api/alerts?plant=${id}&limit=50`), get("/api/data-quality"),
  ]);
  const energy = monthly.reduce((s, m) => s + m.energy_mwh, 0);
  const prs = daily.filter((d) => d.performance_ratio != null);
  const pr = prs.reduce((s, d) => s + d.energy_kwh, 0) / prs.reduce((s, d) => s + d.energy_kwh / d.performance_ratio, 0);
  const avail = daily.reduce((s, d) => s + d.availability, 0) / daily.length;
  const open = alerts.filter((a) => a.status !== "resolved");
  $("#cards").innerHTML = [
    ["Energy", energy.toFixed(1) + " MWh"], ["Performance ratio", pct(pr)], ["Availability", pct(avail)],
    ["Open alerts", plantsById[id].open_alerts], ["Days of data", daily.length],
  ].map(([l, v]) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  if (chart) chart.destroy();
  if (window.Chart) chart = new Chart($("#daily"), {
    data: {
      labels: daily.map((d) => d.day),
      datasets: [
        { type: "bar", label: "Energy (kWh)", data: daily.map((d) => d.energy_kwh), backgroundColor: "#9cc9ae", yAxisID: "y" },
        { type: "line", label: "Performance ratio", data: daily.map((d) => d.performance_ratio), borderColor: "#1f6f43",
          pointRadius: 0, yAxisID: "pr", spanGaps: true },
      ],
    },
    options: { animation: false, scales: { pr: { position: "right", min: 0, max: 1, grid: { display: false } } } },
  });

  $("#inverters tbody").innerHTML = inverters.map((i) =>
    `<tr><td>${i.inverter_id}</td><td>${i.energy_mwh}</td><td>${pct(i.availability)}</td></tr>`).join("");
  $("#quality").innerHTML = `<p>${quality.loaded.toLocaleString()} readings loaded, ${quality.rejected.toLocaleString()} rejected</p>` +
    quality.by_reason.map((r) => `<div>${r.reason}: ${r.rows}</div>`).join("");
  $("#alerts tbody").innerHTML = open.slice(0, 25).map((a) => `
    <tr><td>${a.device}</td><td>${a.kind}</td><td>${a.start_ts.slice(0, 16).replace("T", " ")}</td>
    <td>${a.lost_kwh}</td><td>${a.detail}</td><td><span class="tag ${a.status}">${a.status}</span></td>
    <td>${a.status === "open" ? `<button data-id="${a.id}">Acknowledge</button>` : ""}</td></tr>`).join("");
  document.querySelectorAll("#alerts button").forEach((b) => (b.onclick = async () => {
    await fetch(`/api/alerts/${b.dataset.id}`, { method: "PATCH", headers: { "content-type": "application/json" },
      body: JSON.stringify({ status: "acknowledged" }) });
    show(id);
  }));
  if (monthly.length) $("#month").value = monthly[monthly.length - 1].month;
}

$("#report").onsubmit = (e) => { e.preventDefault(); window.location = `/api/reports/monthly?month=${$("#month").value}`; };
loadPlants().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p style="padding:20px">${e.message}</p>`); });
