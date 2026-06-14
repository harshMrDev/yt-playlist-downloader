/* =============================================================
   YTFetch — Frontend Logic
   ============================================================= */

// Global error handlers to display JS errors as UI toast notifications
window.addEventListener("error", (event) => {
  showToast(`JS Error: ${event.message} at ${event.filename.split('/').pop()}:${event.lineno}`, "error");
});
window.addEventListener("unhandledrejection", (event) => {
  showToast(`Promise Error: ${event.reason}`, "error");
});


// ── State ──────────────────────────────────────────────────
let allVideos = [];          // [{id, title, url, duration, thumbnail, channel, view_count}]
let selectedIds = new Set();
let currentQuality = "1080p";
let currentFormat = "mp4";
let downloadFolder = "";
let concurrentLimit = 3;
let queueItems = {};         // video_id -> {title, thumbnail, status, percent, speed, eta}
let sseSource = null;

// ── Init ────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", async () => {
  // Load default download dir
  try {
    const r = await fetch("/api/default-dir");
    const d = await r.json();
    downloadFolder = d.dir;
    document.getElementById("folderPath").value = downloadFolder;
  } catch (e) {
    downloadFolder = "";
    document.getElementById("folderPath").value = "";
  }

  // Check ffmpeg availability
  try {
    const r = await fetch("/api/system-info");
    const info = await r.json();
    if (!info.ffmpeg) {
      // Show warning banner
      document.getElementById("ffmpegBanner").style.display = "flex";
      // Dim ffmpeg-required quality pills
      document.getElementById("qualityPills").classList.add("no-ffmpeg");
      // Auto-select 720p as the safe default
      const safe = document.querySelector('.quality-pill[data-quality="720p"]');
      if (safe) {
        document.querySelectorAll(".quality-pill").forEach(b => b.classList.remove("active"));
        safe.classList.add("active");
        currentQuality = "720p";
      }
    }
  } catch (e) { /* server not yet ready */ }

  // URL input events
  const urlInput = document.getElementById("urlInput");
  const clearBtn = document.getElementById("clearUrlBtn");
  urlInput.addEventListener("input", () => {
    clearBtn.style.display = urlInput.value ? "flex" : "none";
  });
  clearBtn.addEventListener("click", () => {
    urlInput.value = "";
    clearBtn.style.display = "none";
    urlInput.focus();
  });
  urlInput.addEventListener("keydown", e => {
    if (e.key === "Enter") fetchPlaylist();
  });

  // Start SSE
  connectSSE();
});

// ── SSE ─────────────────────────────────────────────────────
function connectSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource("/api/progress");
  sseSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "progress") {
      handleProgressEvent(msg.data);
    }
  };
  sseSource.onerror = () => {
    // Reconnect after 3s
    setTimeout(connectSSE, 3000);
  };
}

function handleProgressEvent(data) {
  const { video_id, status, percent, speed, eta, downloaded, total, error } = data;
  if (!video_id) return;

  if (!queueItems[video_id]) return;

  queueItems[video_id].status = status || queueItems[video_id].status;
  if (percent !== undefined) queueItems[video_id].percent = percent;
  if (speed) queueItems[video_id].speed = speed;
  if (eta) queueItems[video_id].eta = eta;
  if (downloaded) queueItems[video_id].downloaded = downloaded;
  if (total) queueItems[video_id].total = total;
  if (error) queueItems[video_id].error = error;

  renderQueueItem(video_id);
  updateHeaderStats();
}

// ── Fetch Playlist ──────────────────────────────────────────
async function fetchPlaylist() {
  const url = document.getElementById("urlInput").value.trim();
  if (!url) {
    showError("Please enter a YouTube URL.");
    return;
  }

  hideError();
  showLoading("Fetching playlist info...");

  try {
    const r = await fetch("/api/fetch-playlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    const data = await r.json();
    hideLoading();

    if (data.error) {
      showError(data.error);
      return;
    }

    allVideos = data.videos || [];
    selectedIds = new Set(allVideos.map(v => v.id));

    // Show playlist title
    const titleEl = document.getElementById("playlistTitle");
    titleEl.innerHTML = `<span class="section-icon">📋</span>${escHtml(data.title || "Playlist")}`;
    document.getElementById("playlistMeta").textContent =
      `${data.count} video${data.count !== 1 ? "s" : ""}`;

    // Show sections
    document.getElementById("settingsSection").style.display = "block";
    document.getElementById("playlistSection").style.display = "block";

    renderVideoGrid();
    updateSelectionCount();
    updateDownloadBtn();
    updateHeaderStats();
    showToast("✅ Playlist loaded!", "success");

  } catch (e) {
    hideLoading();
    showError("Failed to connect to the server. Is it running?");
  }
}

// ── Video Grid ──────────────────────────────────────────────
function renderVideoGrid() {
  const query = document.getElementById("searchInput").value.toLowerCase();
  const grid = document.getElementById("videoGrid");
  grid.innerHTML = "";

  const filtered = allVideos.filter(v =>
    v.title.toLowerCase().includes(query) ||
    (v.channel || "").toLowerCase().includes(query)
  );

  filtered.forEach((video, i) => {
    const card = document.createElement("div");
    card.className = "video-card" + (selectedIds.has(video.id) ? " selected" : "");
    card.id = `vcard-${video.id}`;
    card.onclick = () => toggleVideo(video.id);

    card.innerHTML = `
      <div class="video-thumb">
        <img src="${escHtml(video.thumbnail)}" alt="" loading="lazy"
             onerror="this.style.display='none'">
        <div class="video-duration">${escHtml(video.duration)}</div>
        <div class="video-check">
          <svg viewBox="0 0 14 14" fill="none">
            <path d="M2 7l4 4 6-6" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
      </div>
      <div class="video-info">
        <div class="video-title" title="${escHtml(video.title)}">${escHtml(video.title)}</div>
        <div class="video-meta">
          <span>${escHtml(video.channel || "")}</span>
          <span class="video-num">#${allVideos.indexOf(video) + 1}</span>
        </div>
        ${video.view_count ? `<div class="video-meta" style="margin-top:2px"><span>${escHtml(video.view_count)}</span></div>` : ""}
      </div>
    `;
    grid.appendChild(card);
  });

  if (filtered.length === 0) {
    grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:40px 0">No videos found</div>`;
  }
}

function toggleVideo(id) {
  if (selectedIds.has(id)) {
    selectedIds.delete(id);
  } else {
    selectedIds.add(id);
  }
  const card = document.getElementById(`vcard-${id}`);
  if (card) card.classList.toggle("selected", selectedIds.has(id));
  updateSelectionCount();
  updateDownloadBtn();
}

function selectAll() {
  allVideos.forEach(v => selectedIds.add(v.id));
  document.querySelectorAll(".video-card").forEach(c => c.classList.add("selected"));
  updateSelectionCount();
  updateDownloadBtn();
}

function selectNone() {
  selectedIds.clear();
  document.querySelectorAll(".video-card").forEach(c => c.classList.remove("selected"));
  updateSelectionCount();
  updateDownloadBtn();
}

function filterVideos() {
  renderVideoGrid();
}

function updateSelectionCount() {
  document.getElementById("selectionCount").textContent = `${selectedIds.size} selected`;
}

function updateDownloadBtn() {
  const btn = document.getElementById("downloadBtn");
  const text = document.getElementById("downloadBtnText");
  const n = selectedIds.size;
  btn.disabled = n === 0;
  text.textContent = n > 0 ? `Download ${n} Video${n !== 1 ? "s" : ""}` : "Download Selected";
}

// ── Settings ────────────────────────────────────────────────
function selectQuality(btn) {
  document.querySelectorAll(".quality-pill").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  currentQuality = btn.dataset.quality;
}

function selectFormat(btn) {
  document.querySelectorAll(".format-pill").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  currentFormat = btn.dataset.format;

  // Hide quality if audio-only
  const qualityGroup = document.getElementById("qualityPills").parentElement;
  qualityGroup.style.opacity = currentFormat === "mp3" ? "0.4" : "1";
  qualityGroup.style.pointerEvents = currentFormat === "mp3" ? "none" : "auto";
}

function changeConcurrent(delta) {
  concurrentLimit = Math.max(1, Math.min(10, concurrentLimit + delta));
  document.getElementById("concurrentVal").textContent = concurrentLimit;
}

async function browseFolder() {
  try {
    const r = await fetch("/api/browse-folder");
    const d = await r.json();
    if (d.folder) {
      downloadFolder = d.folder;
      document.getElementById("folderPath").value = downloadFolder;
    }
  } catch (e) {
    showToast("Could not open folder picker", "error");
  }
}

async function openFolder() {
  const currentPath = document.getElementById("folderPath").value.trim();
  try {
    await fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: currentPath })
    });
  } catch (e) {
    showToast("Could not open folder", "error");
  }
}

async function setQuickFolder(type) {
  try {
    const r = await fetch(`/api/quick-dir?type=${type}`);
    const d = await r.json();
    if (d.dir) {
      downloadFolder = d.dir;
      document.getElementById("folderPath").value = downloadFolder;
      showToast(`📁 Output set to ${type}`, "success");
    }
  } catch (e) {
    showToast("Failed to set output directory", "error");
  }
}

// ── Download ─────────────────────────────────────────────────
async function startDownload() {
  if (selectedIds.size === 0) return;

  const currentPath = document.getElementById("folderPath").value.trim();
  const cookiesSource = document.getElementById("cookiesSourceSelect").value;
  downloadFolder = currentPath;

  const selected = allVideos.filter(v => selectedIds.has(v.id));

  // Add to queue UI
  selected.forEach(video => {
    queueItems[video.id] = {
      title: video.title,
      thumbnail: video.thumbnail,
      status: "pending",
      percent: 0,
      speed: "—",
      eta: "—",
    };
    renderQueueItem(video.id);
  });

  document.getElementById("queueSection").style.display = "block";
  document.getElementById("queueSection").scrollIntoView({ behavior: "smooth", block: "start" });

  // Batch downloads according to concurrentLimit
  const batches = [];
  for (let i = 0; i < selected.length; i += concurrentLimit) {
    batches.push(selected.slice(i, i + concurrentLimit));
  }

  for (const batch of batches) {
    try {
      await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          videos: batch,
          quality: currentQuality,
          format: currentFormat,
          output_dir: currentPath,
          cookies_source: cookiesSource,
        })
      });
    } catch (e) {
      showToast("Failed to start download batch", "error");
    }
    // Wait a moment between batches
    if (batches.indexOf(batch) < batches.length - 1) {
      await new Promise(r => setTimeout(r, 300));
    }
  }

  showToast(`⬇️ Downloading ${selected.length} video${selected.length !== 1 ? "s" : ""}...`, "info");
}

// ── Queue Rendering ──────────────────────────────────────────
function renderQueueItem(video_id) {
  const item = queueItems[video_id];
  if (!item) return;

  const queueList = document.getElementById("queueList");
  let el = document.getElementById(`qi-${video_id}`);
  if (!el) {
    el = document.createElement("div");
    el.id = `qi-${video_id}`;
    el.className = "queue-item";
    queueList.prepend(el);
  }

  const statusClass = `status-${item.status}`;
  el.className = `queue-item ${statusClass}`;

  const pct = Math.min(100, item.percent || 0);
  const fillClass = item.status === "done" ? "done" : item.status === "error" ? "error" : "";

  let badgeClass = "badge-pending";
  let badgeLabel = "Pending";
  if (item.status === "downloading") { badgeClass = "badge-downloading"; badgeLabel = "Downloading"; }
  else if (item.status === "processing") { badgeClass = "badge-processing"; badgeLabel = "Processing"; }
  else if (item.status === "done") { badgeClass = "badge-done"; badgeLabel = "Done ✓"; }
  else if (item.status === "error") { badgeClass = "badge-error"; badgeLabel = "Error"; }
  else if (item.status === "cancelled") { badgeClass = "badge-cancelled"; badgeLabel = "Cancelled"; }

  let statsHtml = "";
  if (item.status === "downloading") {
    statsHtml = `<div class="queue-stats">
      <span>⚡ ${item.speed || "—"}</span>
      <span>⏳ ${item.eta || "—"}</span>
      ${item.downloaded ? `<span>📦 ${item.downloaded}${item.total ? " / " + item.total : ""}</span>` : ""}
    </div>`;
  } else if (item.status === "error") {
    statsHtml = `<div class="queue-stats" style="color:var(--red)">❌ ${escHtml(item.error || "Download failed")}</div>`;
  }

  el.innerHTML = `
    <img class="queue-thumb" src="${escHtml(item.thumbnail || "")}" alt="" onerror="this.style.display='none'">
    <div class="queue-info">
      <div class="queue-title" title="${escHtml(item.title)}">${escHtml(item.title)}</div>
      <div class="queue-progress-wrap">
        <div class="progress-bar-bg">
          <div class="progress-bar-fill ${fillClass}" style="width:${pct}%"></div>
        </div>
        <div class="progress-pct">${item.status === "done" ? "100%" : item.status === "pending" ? "" : pct + "%"}</div>
      </div>
      ${statsHtml}
    </div>
    <div class="status-badge ${badgeClass}">${badgeLabel}</div>
  `;
}

async function clearCompleted() {
  try {
    const r = await fetch("/api/clear-completed", { method: "POST" });
    const d = await r.json();
    const cleared = d.cleared || 0;

    // Remove from local state and DOM
    Object.keys(queueItems).forEach(id => {
      const s = queueItems[id].status;
      if (["done", "error", "cancelled"].includes(s)) {
        delete queueItems[id];
        const el = document.getElementById(`qi-${id}`);
        if (el) el.remove();
      }
    });

    if (cleared > 0) showToast(`🧹 Cleared ${cleared} item${cleared !== 1 ? "s" : ""}`, "info");
    else showToast("Nothing to clear", "info");
  } catch (e) {
    showToast("Failed to clear", "error");
  }
}

// ── Header Stats ─────────────────────────────────────────────
function updateHeaderStats() {
  const total = Object.keys(queueItems).length;
  const done = Object.values(queueItems).filter(i => i.status === "done").length;

  if (total > 0) {
    document.getElementById("headerStats").style.display = "flex";
    document.getElementById("statTotalNum").textContent = total;
    document.getElementById("statDoneNum").textContent = done;
  } else {
    document.getElementById("headerStats").style.display = "none";
  }
}

// ── Helpers ──────────────────────────────────────────────────
function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showLoading(text = "Loading...") {
  document.getElementById("loadingText").textContent = text;
  document.getElementById("loadingOverlay").style.display = "flex";
}
function hideLoading() {
  document.getElementById("loadingOverlay").style.display = "none";
}
function showError(msg) {
  const el = document.getElementById("fetchError");
  el.textContent = msg;
  el.style.display = "block";
}
function hideError() {
  document.getElementById("fetchError").style.display = "none";
}

let toastTimeout = {};
function showToast(msg, type = "info") {
  const container = document.getElementById("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.transition = "opacity 0.3s";
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}
