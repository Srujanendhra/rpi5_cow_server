let map, animalMap, animalMarkers = {}, pathLine, currentAnimal = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function showDashboard() {
  $("dashboard").classList.remove("hidden");
  $("animalPage").classList.add("hidden");
  currentAnimal = null;
  loadAnimals();
}

async function loadAnimals() {
  const animals = await getJSON("/api/animals");
  $("animalList").innerHTML = animals.length ? animals.map(a => `
    <div class="animal-row" onclick="openAnimal('${esc(a.animal_id)}')">
      <div class="animal-name">${esc(a.name || a.animal_id)}</div>
      <div class="muted">${esc(a.species)} · Collar ${esc(a.collar_address ?? "—")}</div>
      <div>
        ${esc(a.behavior || "No telemetry")}
        ${a.temperature != null ? " · " + Number(a.temperature).toFixed(1) + " °C" : ""}
        ${a.last_seen ? " · " + esc(a.last_seen) : ""}
      </div>
    </div>`).join("") : "<p class='muted'>No animals yet. Waiting for LoRa telemetry.</p>";
}

function initMap() {
  map = L.map("map").setView([17.45, 78.38], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution:"© OpenStreetMap contributors"
  }).addTo(map);
}

async function refreshMap() {
  const data = await getJSON("/api/map");
  for (const k of Object.keys(animalMarkers)) map.removeLayer(animalMarkers[k]);
  animalMarkers = {};

  data.animals.forEach(a => {
    if (a.latitude == null || a.longitude == null) return;
    const marker = L.marker([a.latitude, a.longitude]).addTo(map);
    marker.bindPopup(`<b>${esc(a.animal_id)}</b><br>${esc(a.behavior)}<br>${Number(a.temperature ?? 0).toFixed(1)} °C`);
    animalMarkers[a.animal_id] = marker;
  });
  $("liveCount").textContent = `${data.animals.length} animal(s) reporting`;
}

async function openAnimal(id) {
  currentAnimal = id;
  $("dashboard").classList.add("hidden");
  $("animalPage").classList.remove("hidden");

  const d = await getJSON(`/api/animals/${encodeURIComponent(id)}`);
  const t = await getJSON(`/api/animals/${encodeURIComponent(id)}/telemetry?limit=1000`);
  $("animalHeader").innerHTML = `<h2>${esc(d.animal.name || d.animal.animal_id)}</h2>
    <span class="muted">${esc(d.animal.species)} · Collar ${esc(d.animal.collar_address ?? "—")}</span>`;
  $("animalInfo").innerHTML = `
    <p><b>ID:</b> ${esc(d.animal.animal_id)}</p>
    <p><b>Breed:</b> ${esc(d.animal.breed || "—")}</p>
    <p><b>Sex:</b> ${esc(d.animal.sex || "—")}</p>
    <p><b>Birth date:</b> ${esc(d.animal.birth_date || "—")}</p>
    <p><b>Notes:</b> ${esc(d.animal.notes || "—")}</p>`;

  renderCurrent(t.at(-1));
  renderHistory(d);
  renderTelemetry(t);
  await drawAnimalPath(id);
}

function renderCurrent(t) {
  if (!t) {
    $("currentTelemetry").innerHTML = "<p class='muted'>No telemetry received yet.</p>";
    return;
  }
  const metric = (label, value, unit="") => `<div class="metric">${label}<b>${esc(value)} ${unit}</b></div>`;
  $("currentTelemetry").innerHTML =
    metric("Temperature", t.temperature?.toFixed?.(2) ?? "—", "°C") +
    metric("Behavior", t.behavior ?? "—") +
    metric("RSSI", t.rssi ?? "—", "dBm") +
    metric("Latitude", t.latitude?.toFixed?.(6) ?? "—") +
    metric("Longitude", t.longitude?.toFixed?.(6) ?? "—") +
    metric("Last packet", t.received_at ?? "—");
}

function renderHistory(d) {
  $("vaccinations").innerHTML = d.vaccinations.length ? d.vaccinations.map(v =>
    `<div class="record"><b>${esc(v.vaccine)}</b><br>${esc(v.date)} → next due ${esc(v.next_due || "—")}<br><span class="muted">${esc(v.notes || "")}</span></div>`
  ).join("") : "<p class='muted'>No vaccination records.</p>";

  $("diagnoses").innerHTML = d.diagnoses.length ? d.diagnoses.map(x =>
    `<div class="record"><b>${esc(x.disease)}</b> · ${esc(x.date)}<br>${esc(x.diagnosis || "")}<br><span class="muted">${esc(x.treatment || "")}</span></div>`
  ).join("") : "<p class='muted'>No disease/diagnostic records.</p>";
}

function renderTelemetry(rows) {
  const cols = ["received_at","packet_type","temperature","latitude","longitude","behavior","ax_mean","ay_mean","az_mean","ax_std","ay_std","az_std","rssi","snr"];
  $("telemetryTable").innerHTML =
    `<thead><tr>${cols.map(c=>`<th>${c}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.slice().reverse().map(r =>
      `<tr>${cols.map(c=>`<td>${esc(typeof r[c] === "number" ? r[c].toFixed?.(3) ?? r[c] : r[c])}</td>`).join("")}</tr>`
    ).join("")}</tbody>`;
}

async function drawAnimalPath(id) {
  const p = await getJSON(`/api/animals/${encodeURIComponent(id)}/path`);
  if (animalMap) animalMap.remove();
  animalMap = L.map("animalMap").setView([17.45,78.38], 15);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {attribution:"© OpenStreetMap contributors"}).addTo(animalMap);
  const points = p.filter(x => x.latitude != null && x.longitude != null).map(x => [x.latitude,x.longitude]);
  if (points.length) {
    pathLine = L.polyline(points).addTo(animalMap);
    animalMap.fitBounds(pathLine.getBounds(), {padding:[20,20]});
    L.marker(points.at(-1)).addTo(animalMap).bindPopup(id).openPopup();
  }
}

async function addVaccination() {
  const vaccine = prompt("Vaccine name:");
  if (!vaccine) return;
  const date = prompt("Date (YYYY-MM-DD):", new Date().toISOString().slice(0,10));
  const next_due = prompt("Next due date (YYYY-MM-DD):", "");
  await getJSON(`/api/animals/${encodeURIComponent(currentAnimal)}/vaccinations`, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({vaccine,date,next_due})
  });
  openAnimal(currentAnimal);
}

async function addDiagnosis() {
  const disease = prompt("Disease / condition:");
  if (!disease) return;
  const date = prompt("Date (YYYY-MM-DD):", new Date().toISOString().slice(0,10));
  const diagnosis = prompt("Diagnosis / findings:", "");
  const treatment = prompt("Treatment:", "");
  await getJSON(`/api/animals/${encodeURIComponent(currentAnimal)}/diagnoses`, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({disease,date,diagnosis,treatment})
  });
  openAnimal(currentAnimal);
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $("status").textContent = "Live";
  ws.onclose = () => { $("status").textContent = "Disconnected — retrying"; setTimeout(connectWS, 2000); };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type !== "telemetry") return;
    if (currentAnimal === m.animal_id) {
      renderCurrent(m);
      getJSON(`/api/animals/${encodeURIComponent(currentAnimal)}/telemetry?limit=1000`).then(renderTelemetry);
      drawAnimalPath(currentAnimal);
    }
    loadAnimals();
    refreshMap();
  };
}

window.addEventListener("load", () => {
  initMap(); loadAnimals(); refreshMap(); connectWS();
  setInterval(refreshMap, 5000);
});
