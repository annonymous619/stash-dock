const $ = selector => document.querySelector(selector);
const esc = (value = "") => String(value).replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[character]));
const lines = value => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
const message = (element, text, ok = false) => {
  element.textContent = text;
  element.classList.toggle("success", ok);
};
const bytes = value => {
  const amount = Number(value || 0);
  if (amount >= 1099511627776) return `${(amount / 1099511627776).toFixed(1)} TB`;
  if (amount >= 1073741824) return `${(amount / 1073741824).toFixed(1)} GB`;
  return `${(amount / 1048576).toFixed(1)} MB`;
};
const api = async (url, options = {}) => {
  const response = await fetch(url, options);
  let result = {};
  try { result = await response.json(); } catch { /* An empty response is valid for a few actions. */ }
  if (!response.ok) throw new Error(result.detail || result.message || `Request failed (${response.status}).`);
  return result;
};

let advanced = null;
let jobsCache = [];
let preflightResult = null;

// Themes are intentionally device-local. Server settings and download jobs remain shared.
const THEMES = new Set(["ops", "media", "command"]);
const THEME_COLORS = { ops: "#091018", media: "#0b0c16", command: "#0d0f0c" };
const applyTheme = requested => {
  const theme = THEMES.has(requested) ? requested : "ops";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("stashDockTheme", theme);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLORS[theme]);
  if ($("#theme-select")) $("#theme-select").value = theme;
  if ($("#theme-quick-select")) $("#theme-quick-select").value = theme;
  document.querySelectorAll(".theme-option").forEach(option => {
    const active = option.dataset.themeOption === theme;
    option.classList.toggle("active", active);
    option.setAttribute("aria-pressed", String(active));
  });
};
applyTheme(localStorage.getItem("stashDockTheme") || document.documentElement.dataset.theme);
$("#theme-select").addEventListener("change", event => applyTheme(event.target.value));
$("#theme-quick-select").addEventListener("change", event => applyTheme(event.target.value));
document.querySelectorAll(".theme-option").forEach(option => option.addEventListener("click", () => {
  applyTheme(option.dataset.themeOption);
}));
window.addEventListener("pageshow", () => applyTheme(localStorage.getItem("stashDockTheme") || "ops"));
window.addEventListener("storage", event => { if (event.key === "stashDockTheme") applyTheme(event.newValue); });

const viewLabels = {
  downloads: ["DOWNLOAD CONTROL", "Downloads"],
  manage: ["MEDIA CONTROL", "Library"],
  settings: ["CONFIGURATION", "Settings"],
  diagnostics: ["SUPPORT", "Diagnostics"]
};
document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item,.view").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  $(`#${button.dataset.view}-view`).classList.add("active");
  [$("#view-eyebrow").textContent, $("#view-title").textContent] = viewLabels[button.dataset.view];
  if (button.dataset.view === "settings") loadSettings();
  if (button.dataset.view === "diagnostics") loadDiagnostics();
  if (button.dataset.view === "manage") loadAdvanced();
}));

async function loadAdvanced() {
  try {
    advanced = await api("/api/advanced");
    $("#recipe").innerHTML = advanced.recipes.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    $("#library").innerHTML = advanced.libraries.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    const features = advanced.feature_toggles || {};
    [["downloads", "feature-downloads"], ["audio_mode", "feature-audio"],
      ["duplicate_review", "feature-duplicates"], ["storage_review", "feature-storage"], ["plugins", "feature-plugins"],
      ["webhooks", "feature-webhooks"], ["stash_sync", "feature-stash"]].forEach(([key, id]) => {
      const element = $(`#${id}`);
      if (element) element.checked = features[key] !== false;
    });
    $("#routing-rules").value = JSON.stringify(advanced.rules || [], null, 2);
    $("#cookie-profiles").value = JSON.stringify(advanced.cookie_profiles || [], null, 2);
    const audioChoice = document.querySelector('input[name="mode"][value="audio"]')?.closest("label");
    if (audioChoice) audioChoice.hidden = features.audio_mode === false;
    $("#analyze").disabled = features.downloads === false;
    $("#scan-duplicates").hidden = features.duplicate_review === false;
    $("#review-storage").hidden = features.storage_review === false;
    $("#load-plugins").hidden = features.plugins === false;
  } catch (error) {
    message($("#message"), `Could not load downloader settings: ${error.message}`);
  }
}

function jobLabel(job) {
  const source = job.url === "stash://manual-sync" ? "Manual Stash synchronization" : job.url;
  return `${job.status.toUpperCase()} · ${source}`;
}

function renderJobs() {
  $("#job-list").innerHTML = jobsCache.length ? jobsCache.map(job => `
    <article class="job">
      <span class="status ${esc(job.status)}">${esc(job.status)}</span>
      <div><p class="job-url">${esc(job.url)}</p><p class="meta">${esc(job.engine)} · ${esc(job.host)} · ${new Date(job.created_at * 1000).toLocaleString()}</p>${job.error ? `<p class="message">${esc(job.error)}</p>` : ""}</div>
      <div class="job-actions">${["queued", "running", "scheduled"].includes(job.status) ? `<button class="quiet cancel" data-id="${esc(job.id)}">Cancel</button>` : ""}<button class="quiet view-log" data-id="${esc(job.id)}">View logs</button></div>
    </article>`).join("") : '<p class="empty">No downloads yet. Paste a link above to begin.</p>';

  const selected = $("#log-job").value;
  $("#log-job").innerHTML = jobsCache.length
    ? jobsCache.map(job => `<option value="${esc(job.id)}">${esc(jobLabel(job))}</option>`).join("")
    : '<option value="">Latest job</option>';
  if (jobsCache.some(job => job.id === selected)) $("#log-job").value = selected;
  renderLog();
}

function filteredLog(job) {
  const all = String(job?.log || "Waiting for output…").split(/\r?\n/);
  const filter = $("#log-filter").value;
  const patterns = {
    warnings: /warning|error|failed|forbidden|429|403|timeout|unsupported/i,
    download: /download|fragment|destination|hls|extract|yt-dlp|gallery-dl/i,
    stash: /stash|scan|performer|scene|gallery|metadata|synchron/i
  };
  return filter === "all" ? all.join("\n") : all.filter(line => patterns[filter].test(line)).join("\n") || "No matching log lines yet.";
}

function renderLog() {
  const id = $("#log-job").value || jobsCache[0]?.id;
  const job = jobsCache.find(item => item.id === id);
  if (!job) {
    $("#log-state").textContent = "Waiting for a job…";
    $("#log-content").textContent = "Logs will appear here while downloads and Stash synchronization run.";
    return;
  }
  $("#log-state").textContent = `${job.status.toUpperCase()} · ${job.engine} · ${job.host} · ${new Date(job.created_at * 1000).toLocaleString()}`;
  $("#log-content").textContent = filteredLog(job);
  if (document.body.classList.contains("logs-open")) $("#log-content").scrollTop = $("#log-content").scrollHeight;
}

async function loadJobs() {
  try {
    const [jobs, health] = await Promise.all([api("/api/jobs"), api("/api/health")]);
    jobsCache = jobs;
    $("#health").textContent = health.stash_configured ? "Stash connected" : "Setup needed";
    $("#health").className = `health ${health.stash_configured ? "ok" : "warn"}`;
    renderJobs();
  } catch {
    $("#health").textContent = "Disconnected";
    $("#health").className = "health";
  }
}

const openLogs = jobId => {
  if (jobId && jobsCache.some(job => job.id === jobId)) $("#log-job").value = jobId;
  document.body.classList.add("logs-open");
  $("#log-drawer").setAttribute("aria-hidden", "false");
  $("#toggle-logs").setAttribute("aria-expanded", "true");
  renderLog();
};
const closeLogs = () => {
  document.body.classList.remove("logs-open");
  $("#log-drawer").setAttribute("aria-hidden", "true");
  $("#toggle-logs").setAttribute("aria-expanded", "false");
};
[$("#toggle-logs"), $("#mobile-log-toggle"), $("#open-logs")].forEach(button => button.addEventListener("click", () => openLogs()));
[$("#close-logs"), $("#log-backdrop")].forEach(button => button.addEventListener("click", closeLogs));
$("#log-job").addEventListener("change", renderLog);
$("#log-filter").addEventListener("change", renderLog);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeLogs(); });

$("#job-list").addEventListener("click", async event => {
  const logButton = event.target.closest(".view-log");
  if (logButton) return openLogs(logButton.dataset.id);
  const button = event.target.closest(".cancel");
  if (!button) return;
  button.disabled = true;
  try { await api(`/api/jobs/${button.dataset.id}/cancel`, { method: "POST" }); }
  catch (error) { message($("#message"), error.message); }
  await loadJobs();
});

function invalidatePreflight() {
  preflightResult = null;
  $("#preflight-card").hidden = true;
  $("#queue-download").disabled = true;
}
[$("#url"), $("#recipe"), $("#library")].forEach(element => element.addEventListener("input", invalidatePreflight));
document.querySelectorAll('input[name="mode"]').forEach(element => element.addEventListener("change", invalidatePreflight));

function fact(label, value) {
  return `<div class="fact"><span>${esc(label)}</span><b title="${esc(value || "Unknown")}">${esc(value || "Unknown")}</b></div>`;
}

function renderPreflight(result) {
  $("#preflight-card").hidden = false;
  $("#preflight-state").textContent = result.ready ? "READY" : "BLOCKED";
  $("#preflight-state").className = `status ${result.ready ? "completed" : "failed"}`;
  if (result.ready) {
    $("#preflight-title").textContent = result.creator || result.title || `${result.site} media`;
    $("#preflight-facts").innerHTML = [
      fact("Site", result.site), fact("Creator", result.creator || "Not reported"),
      fact("Items", result.item_count == null ? "Unknown" : `${result.item_count}${result.count_limited ? "+" : ""}`),
      fact("Engine", result.engine), fact("Type", result.content_kind),
      fact("Free space", bytes(result.free_bytes)), fact("Destination", result.destination),
      fact("Routing rule", result.rule_applied ? "Applied" : "Default")
    ].join("");
  } else {
    const failure = result.failure || {};
    $("#preflight-title").textContent = failure.message || "This link is not ready";
    $("#preflight-facts").innerHTML = [fact("Site", result.site), fact("Engine", result.engine), fact("Code", failure.code)].join("");
  }
  const warnings = [...(result.warnings || [])];
  if (!result.ready && result.details) warnings.push(result.details);
  $("#preflight-warning").hidden = warnings.length === 0;
  $("#preflight-warning").innerHTML = warnings.map(item => `<p>${esc(item)}</p>`).join("");
  $("#queue-download").disabled = !(result.ready && $("#authorized").checked);
}

$("#download-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#analyze");
  button.disabled = true;
  message($("#message"), "Inspecting the link…");
  try {
    preflightResult = await api("/api/preflight", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: $("#url").value, mode: new FormData(event.currentTarget).get("mode"),
        recipe_id: $("#recipe").value || "original", library_id: $("#library").value || "stash"
      })
    });
    renderPreflight(preflightResult);
    message($("#message"), preflightResult.ready ? `Ready with ${preflightResult.engine}. Review the scope below.` : "Preflight found a problem. Nothing was queued.", preflightResult.ready);
  } catch (error) {
    invalidatePreflight();
    message($("#message"), error.message);
  } finally {
    button.disabled = advanced?.feature_toggles?.downloads === false;
  }
});

$("#max-items").addEventListener("change", event => {
  $("#custom-limit-wrap").hidden = event.target.value !== "custom";
});
$("#authorized").addEventListener("change", () => {
  $("#queue-download").disabled = !(preflightResult?.ready && $("#authorized").checked);
});

$("#queue-download").addEventListener("click", async () => {
  const selectedLimit = $("#max-items").value;
  const maxItems = selectedLimit === "custom" ? Number($("#custom-limit").value) : (selectedLimit ? Number(selectedLimit) : null);
  const button = $("#queue-download");
  button.disabled = true;
  try {
    const result = await api("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: $("#url").value, mode: new FormData($("#download-form")).get("mode"), authorized: $("#authorized").checked,
        recipe_id: $("#recipe").value || "original", library_id: $("#library").value || "stash",
        scheduled_at: null,
        max_items: maxItems, date_after: $("#date-after").value, date_before: $("#date-before").value
      })
    });
    message($("#message"), `Queued with ${result.engine}.`, true);
    $("#url").value = "";
    $("#authorized").checked = false;
    invalidatePreflight();
    await loadJobs();
  } catch (error) {
    message($("#message"), error.message);
    button.disabled = !(preflightResult?.ready && $("#authorized").checked);
  }
});

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    $("#stash-url").value = settings.stash_url;
    $("#api-key").value = "";
    $("#key-state").textContent = settings.api_key_configured ? "API key saved" : "No API key saved";
    $("#sync-enabled").checked = settings.sync_enabled;
    $("#scan-wait").value = settings.scan_wait_seconds;
    $("#folder-layout").value = settings.folder_layout;
    $("#unknown-creator").value = settings.unknown_creator_label;
    $("#gallery-hosts").value = settings.gallery_hosts.join("\n");
    $("#video-hosts").value = settings.video_hosts.join("\n");
    $("#site-labels").value = Object.entries(settings.site_labels).map(([key, value]) => `${key}=${value}`).join("\n");
    $("#integration-key-state").textContent = settings.integration_api_configured
      ? `Active key ending in ${settings.integration_api_key_last_four}` : "No integration key generated";
    $("#revoke-integration-key").disabled = !settings.integration_api_configured;
  } catch (error) { message($("#settings-message"), error.message); }
}

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const labels = Object.fromEntries(lines($("#site-labels").value).map(row => {
    const [key, ...rest] = row.split("=");
    return [key.trim(), rest.join("=").trim()];
  }).filter(([key, value]) => key && value));
  let rules;
  let profiles;
  try {
    rules = JSON.parse($("#routing-rules").value || "[]");
    profiles = JSON.parse($("#cookie-profiles").value || "[]");
    if (!Array.isArray(rules) || !Array.isArray(profiles)) throw new Error();
  } catch { return message($("#community-message"), "Rules and cookie profiles must be valid JSON lists."); }
  try {
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      stash_url: $("#stash-url").value, api_key: $("#api-key").value, sync_enabled: $("#sync-enabled").checked,
      scan_wait_seconds: Number($("#scan-wait").value), folder_layout: $("#folder-layout").value,
      unknown_creator_label: $("#unknown-creator").value, gallery_hosts: lines($("#gallery-hosts").value),
      video_hosts: lines($("#video-hosts").value), site_labels: labels
    }) });
    const advancedPayload = { ...advanced, rules, cookie_profiles: profiles, feature_toggles: {
      downloads: $("#feature-downloads").checked, audio_mode: $("#feature-audio").checked,
      schedules: false, duplicate_review: $("#feature-duplicates").checked,
      storage_review: $("#feature-storage").checked, plugins: $("#feature-plugins").checked,
      webhooks: $("#feature-webhooks").checked, stash_sync: $("#feature-stash").checked
    } };
    advanced = await api("/api/advanced", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(advancedPayload) });
    message($("#settings-message"), "Settings saved.", true);
    message($("#community-message"), "Community settings saved.", true);
    await Promise.all([loadSettings(), loadJobs(), loadAdvanced()]);
  } catch (error) { message($("#settings-message"), error.message); }
});

$("#config-import").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const bundle = JSON.parse(await file.text());
    await api("/api/config/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bundle }) });
    message($("#community-message"), "Configuration imported without secrets. Review and save your settings.", true);
    await Promise.all([loadAdvanced(), loadSettings()]);
  } catch (error) { message($("#community-message"), error.message || "That file is not valid JSON."); }
  event.target.value = "";
});

$("#test-stash").addEventListener("click", async () => {
  message($("#settings-message"), "Testing…");
  try {
    const result = await api("/api/settings/test", { method: "POST" });
    message($("#settings-message"), `Connected: ${result.performers} performers, ${result.scenes} scenes, ${result.galleries} galleries.`, true);
  } catch (error) { message($("#settings-message"), error.message); }
});

$("#sync-now").addEventListener("click", async () => {
  try {
    const result = await api("/api/stash/sync", { method: "POST" });
    message($("#settings-message"), `Synchronization queued as ${result.id}.`, true);
    await loadJobs();
  } catch (error) { message($("#settings-message"), error.message); }
});

$("#generate-integration-key").addEventListener("click", async () => {
  try {
    const result = await api("/api/settings/integration-key", { method: "POST" });
    $("#generated-key").value = result.api_key;
    $("#generated-key-wrap").hidden = false;
    message($("#settings-message"), "New integration key generated. Copy it now.", true);
    await loadSettings();
  } catch (error) { message($("#settings-message"), error.message); }
});
$("#copy-integration-key").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#generated-key").value);
    message($("#settings-message"), "Integration key copied.", true);
  } catch { message($("#settings-message"), "Copy failed. Select the key and copy it manually."); }
});
$("#revoke-integration-key").addEventListener("click", async () => {
  try {
    await api("/api/settings/integration-key", { method: "DELETE" });
    $("#generated-key").value = "";
    $("#generated-key-wrap").hidden = true;
    message($("#settings-message"), "Integration key revoked.", true);
    await loadSettings();
  } catch (error) { message($("#settings-message"), error.message); }
});

async function loadDiagnostics() {
  try {
    const result = await api("/api/diagnostics");
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
    $("#diagnostic-list").innerHTML = checks.map(([name, value, ok]) => `<article class="diagnostic"><span class="indicator ${ok ? "good" : "bad"}"></span><div><h4>${esc(name)}</h4><p>${esc(value)}</p></div></article>`).join("");
  } catch (error) { $("#diagnostic-list").innerHTML = `<p class="message">${esc(error.message)}</p>`; }
}

$("#import-library").addEventListener("click", async () => {
  message($("#manage-message"), "Indexing media…");
  try {
    const result = await api("/api/library/import", { method: "POST" });
    message($("#manage-message"), `Indexed ${result.indexed} media files. Found ${result.exact_matches} exact and ${result.probable_matches} probable matches.`, true);
  } catch (error) { message($("#manage-message"), error.message); }
});
$("#scan-duplicates").addEventListener("click", async () => {
  try {
    const result = await api("/api/duplicates");
    $("#manage-results").innerHTML = result.groups.length ? result.groups.map(group => `<article class="review-card"><span class="status ${group.kind === "exact" ? "failed" : "queued"}">${esc(group.kind)}</span><h3>${group.files.length} matching files</h3><p class="hint">${group.kind === "exact" ? `${bytes(group.reclaimable_bytes)} safely reviewable` : "Same normalized title and nearly identical size"}</p><ul>${group.files.map(file => `<li>${esc(file.path)} · ${bytes(file.size)}</li>`).join("")}</ul></article>`).join("") : '<p class="empty">No duplicate groups found.</p>';
  } catch (error) { message($("#manage-message"), error.message); }
});
$("#review-storage").addEventListener("click", async () => {
  try {
    const result = await api("/api/storage/candidates");
    $("#manage-results").innerHTML = `<article class="review-card safety"><h3>Review-only storage policy</h3><p>${esc(result.note)}</p></article>` + (result.candidates.length ? result.candidates.map(item => `<article class="review-card"><h3>${esc(item.path)}</h3><p>${bytes(item.size)} · ${esc(item.reason)}</p></article>`).join("") : '<p class="empty">No files match the current policy.</p>');
  } catch (error) { message($("#manage-message"), error.message); }
});
$("#load-plugins").addEventListener("click", async () => {
  try {
    const result = await api("/api/plugins");
    $("#manage-results").innerHTML = `<article class="review-card safety"><h3>Safe plugin model</h3><p>${esc(result.format)}</p></article>` + (result.plugins.length ? result.plugins.map(item => `<article class="review-card"><h3>${esc(item.name || item.file)}</h3><p>${esc(item.description || item.error || "Ready")}</p></article>`).join("") : '<p class="empty">No plugin manifests in /config/plugins yet.</p>');
  } catch (error) { message($("#manage-message"), error.message); }
});

$("#refresh").addEventListener("click", loadJobs);
$("#reload-diagnostics").addEventListener("click", loadDiagnostics);
Promise.all([loadAdvanced(), loadJobs()]);
setInterval(loadJobs, 3000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
