const $ = selector => document.querySelector(selector);
const esc = (value = "") => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const lines = value => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
const message = (element, text, ok = false) => {
  element.textContent = text;
  element.classList.toggle("success", ok);
};

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item,.view").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  $(`#${button.dataset.view}-view`).classList.add("active");
  if (button.dataset.view === "settings") loadSettings();
  if (button.dataset.view === "diagnostics") loadDiagnostics();
  if (button.dataset.view === "manage") loadAdvanced();
}));

let advanced = null;
async function loadAdvanced() {
  advanced = await (await fetch("/api/advanced")).json();
  $("#recipe").innerHTML = advanced.recipes.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
  $("#library").innerHTML = advanced.libraries.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
}

async function loadJobs() {
  try {
    const [jobsResponse, healthResponse] = await Promise.all([fetch("/api/jobs"), fetch("/api/health")]);
    const jobs = await jobsResponse.json();
    const health = await healthResponse.json();
    $("#health").textContent = health.stash_configured ? "Stash connected" : "Setup needed";
    $("#health").className = `health ${health.stash_configured ? "ok" : "warn"}`;
    $("#job-list").innerHTML = jobs.length ? jobs.map(job => `
      <article class="job">
        <span class="status ${esc(job.status)}">${esc(job.status)}</span>
        <div><p class="job-url">${esc(job.url)}</p><p class="meta">${esc(job.engine)} · ${esc(job.host)} · ${new Date(job.created_at * 1000).toLocaleString()}</p>${job.error ? `<p class="message">${esc(job.error)}</p>` : ""}</div>
        ${["queued","running"].includes(job.status) ? `<button class="quiet cancel" data-id="${esc(job.id)}">Cancel</button>` : ""}
        <details><summary>View log</summary><pre>${esc(job.log || "Waiting for output…")}</pre></details>
      </article>`).join("") : '<p class="empty">No downloads yet. Paste a link above to begin.</p>';
  } catch {
    $("#health").textContent = "Disconnected";
    $("#health").className = "health";
  }
}

$("#download-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("[type=submit]");
  button.disabled = true;
  try {
    const response = await fetch("/api/jobs", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
      url: $("#url").value, mode: new FormData(event.currentTarget).get("mode"), authorized: $("#authorized").checked,
      recipe_id: $("#recipe").value || "original", library_id: $("#library").value || "stash"
    })});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not start the download.");
    message($("#message"), `Queued with ${result.engine}.`, true);
    $("#url").value = "";
    await loadJobs();
  } catch (error) { message($("#message"), error.message); }
  finally { button.disabled = false; }
});

$("#job-list").addEventListener("click", async event => {
  const button = event.target.closest(".cancel");
  if (!button) return;
  button.disabled = true;
  await fetch(`/api/jobs/${button.dataset.id}/cancel`, {method:"POST"});
  await loadJobs();
});

async function loadSettings() {
  const settings = await (await fetch("/api/settings")).json();
  $("#stash-url").value = settings.stash_url;
  $("#api-key").value = "";
  $("#key-state").textContent = settings.api_key_configured ? "API key saved" : "No API key saved";
  $("#sync-enabled").checked = settings.sync_enabled;
  $("#scan-wait").value = settings.scan_wait_seconds;
  $("#folder-layout").value = settings.folder_layout;
  $("#unknown-creator").value = settings.unknown_creator_label;
  $("#gallery-hosts").value = settings.gallery_hosts.join("\n");
  $("#video-hosts").value = settings.video_hosts.join("\n");
  $("#site-labels").value = Object.entries(settings.site_labels).map(([key,value]) => `${key}=${value}`).join("\n");
  $("#integration-key-state").textContent = settings.integration_api_configured
    ? `Active key ending in ${settings.integration_api_key_last_four}`
    : "No integration key generated";
  $("#revoke-integration-key").disabled = !settings.integration_api_configured;
}

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const labels = Object.fromEntries(lines($("#site-labels").value).map(row => {
    const [key, ...rest] = row.split("=");
    return [key.trim(), rest.join("=").trim()];
  }).filter(([key,value]) => key && value));
  const response = await fetch("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
    stash_url:$("#stash-url").value, api_key:$("#api-key").value, sync_enabled:$("#sync-enabled").checked,
    scan_wait_seconds:Number($("#scan-wait").value), folder_layout:$("#folder-layout").value,
    unknown_creator_label:$("#unknown-creator").value, gallery_hosts:lines($("#gallery-hosts").value),
    video_hosts:lines($("#video-hosts").value), site_labels:labels
  })});
  const result = await response.json();
  if (!response.ok) return message($("#settings-message"), result.detail || "Could not save settings.");
  message($("#settings-message"), "Settings saved.", true);
  loadSettings(); loadJobs();
});

$("#test-stash").addEventListener("click", async () => {
  message($("#settings-message"), "Testing…");
  const response = await fetch("/api/settings/test", {method:"POST"});
  const result = await response.json();
  message($("#settings-message"), response.ok ? `Connected: ${result.performers} performers, ${result.scenes} scenes, ${result.galleries} galleries.` : result.detail, response.ok);
});

$("#sync-now").addEventListener("click", async () => {
  const response = await fetch("/api/stash/sync", {method:"POST"});
  const result = await response.json();
  message($("#settings-message"), response.ok ? `Synchronization queued as ${result.id}.` : result.detail, response.ok);
  loadJobs();
});

$("#generate-integration-key").addEventListener("click", async () => {
  const response = await fetch("/api/settings/integration-key", {method:"POST"});
  const result = await response.json();
  if (!response.ok) return message($("#settings-message"), result.detail || "Could not generate a key.");
  $("#generated-key").value = result.api_key;
  $("#generated-key-wrap").hidden = false;
  message($("#settings-message"), "New integration key generated. Copy it now.", true);
  loadSettings();
});

$("#copy-integration-key").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#generated-key").value);
  message($("#settings-message"), "Integration key copied.", true);
});

$("#revoke-integration-key").addEventListener("click", async () => {
  const response = await fetch("/api/settings/integration-key", {method:"DELETE"});
  if (!response.ok) return message($("#settings-message"), "Could not revoke the key.");
  $("#generated-key").value = "";
  $("#generated-key-wrap").hidden = true;
  message($("#settings-message"), "Integration key revoked.", true);
  loadSettings();
});

async function loadDiagnostics() {
  const result = await (await fetch("/api/diagnostics")).json();
  const checks = [
    ["Stash API", result.stash_configured ? "Configured" : "Missing key", result.stash_configured],
    ["Automatic sync", result.sync_enabled ? "Enabled" : "Disabled", result.sync_enabled],
    ["Integration API", result.integration_api_configured ? "Key active" : "No key", result.integration_api_configured],
    ["Metadata handoff", `${result.metadata_manifests} manifests`, true],
    ["Downloads volume", result.downloads_writable ? "Writable" : "Read only", result.downloads_writable],
    ["Config volume", result.config_writable ? "Writable" : "Read only", result.config_writable],
    ["gallery-dl", result.gallery_dl, !result.gallery_dl.startsWith("unavailable")],
    ["yt-dlp", result.yt_dlp, !result.yt_dlp.startsWith("unavailable")],
    ["FFmpeg", result.ffmpeg, !result.ffmpeg.startsWith("unavailable")],
    ["Queues", `${result.queue} downloads · ${result.stash_queue} Stash`, result.queue === 0 && result.stash_queue === 0]
  ];
  $("#diagnostic-list").innerHTML = checks.map(([name,value,ok]) => `<article class="diagnostic"><span class="indicator ${ok?"good":"bad"}"></span><div><h4>${esc(name)}</h4><p>${esc(value)}</p></div></article>`).join("");
}

$("#refresh").addEventListener("click", loadJobs);
$("#reload-diagnostics").addEventListener("click", loadDiagnostics);
function bytes(value) { return value > 1073741824 ? `${(value/1073741824).toFixed(1)} GB` : `${(value/1048576).toFixed(1)} MB`; }
$("#import-library").addEventListener("click", async () => {
  message($("#manage-message"), "Indexing media…");
  const result = await (await fetch("/api/library/import", {method:"POST"})).json();
  message($("#manage-message"), `Indexed ${result.indexed} media files. Found ${result.exact_matches} exact and ${result.probable_matches} probable matches.`, true);
});
$("#scan-duplicates").addEventListener("click", async () => {
  const result = await (await fetch("/api/duplicates")).json();
  $("#manage-results").innerHTML = result.groups.length ? result.groups.map(group => `<article class="review-card"><span class="status ${group.kind==="exact"?"failed":"queued"}">${esc(group.kind)}</span><h3>${group.files.length} matching files</h3><p class="hint">${group.kind==="exact" ? `${bytes(group.reclaimable_bytes)} safely reviewable` : "Same normalized title and nearly identical size"}</p><ul>${group.files.map(file=>`<li>${esc(file.path)} · ${bytes(file.size)}</li>`).join("")}</ul></article>`).join("") : '<p class="empty">No duplicate groups found.</p>';
});
$("#review-storage").addEventListener("click", async () => {
  const result = await (await fetch("/api/storage/candidates")).json();
  $("#manage-results").innerHTML = `<article class="review-card safety"><h3>Review-only storage policy</h3><p>${esc(result.note)}</p></article>` + (result.candidates.length ? result.candidates.map(item=>`<article class="review-card"><h3>${esc(item.path)}</h3><p>${bytes(item.size)} · ${esc(item.reason)}</p></article>`).join("") : '<p class="empty">No files match the current policy.</p>');
});
$("#load-plugins").addEventListener("click", async () => {
  const result = await (await fetch("/api/plugins")).json();
  $("#manage-results").innerHTML = `<article class="review-card safety"><h3>Safe plugin model</h3><p>${esc(result.format)}</p></article>` + (result.plugins.length ? result.plugins.map(item=>`<article class="review-card"><h3>${esc(item.name || item.file)}</h3><p>${esc(item.description || item.error || "Ready")}</p></article>`).join("") : '<p class="empty">No plugin manifests in /config/plugins yet.</p>');
});
loadAdvanced();
loadJobs();
setInterval(loadJobs, 4000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
