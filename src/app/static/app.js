// OurWorlds Quant — progressive-enhancement runtime.
// Loaded on every server-rendered page via <script src="/static/app.js" defer>.
// Contract: everything here is ADDITIVE. The server HTML works with JS disabled — every
// [data-metric] node already carries a `title` (plain-language fallback shown on hover /
// long-press). This script only UPGRADES those into tap/click/keyboard rich tooltips
// sourced from /api/glossary. No build step, no dependencies; CSP is script-src 'self'.
(function () {
  "use strict";

  var GLOSSARY = null;     // { key: {term, short, formula, unit, band} }
  var loading = null;      // in-flight fetch promise
  var activeTip = null;    // currently open tooltip element
  var activeNode = null;   // the [data-metric] node it belongs to

  function loadGlossary() {
    if (GLOSSARY) return Promise.resolve(GLOSSARY);
    if (loading) return loading;
    loading = fetch("/api/glossary", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { GLOSSARY = (data && data.metrics) || {}; return GLOSSARY; })
      .catch(function () { GLOSSARY = {}; return GLOSSARY; }); // title attr still works
    return loading;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function closeTip() {
    if (activeTip && activeTip.parentNode) activeTip.parentNode.removeChild(activeTip);
    if (activeNode) activeNode.setAttribute("aria-expanded", "false");
    activeTip = null;
    activeNode = null;
  }

  function buildTip(info) {
    var tip = document.createElement("div");
    tip.className = "owq-tip";
    tip.setAttribute("role", "tooltip");
    var html = "<h4>" + escapeHtml(info.term || "") + "</h4>";
    html += "<div>" + escapeHtml(info.short || "") + "</div>";
    if (info.formula) {
      html += '<div class="owq-tip-f">' + escapeHtml(info.formula);
      if (info.unit) html += "　·　单位 " + escapeHtml(info.unit);
      html += "</div>";
    }
    if (info.band) html += '<div class="owq-tip-b">' + escapeHtml(info.band) + "</div>";
    tip.innerHTML = html;
    return tip;
  }

  function positionTip(tip, node) {
    var r = node.getBoundingClientRect();
    document.body.appendChild(tip); // append before measuring width
    var tw = tip.offsetWidth;
    var top = window.scrollY + r.bottom + 8;
    var left = window.scrollX + r.left;
    var maxLeft = window.scrollX + document.documentElement.clientWidth - tw - 10;
    if (left > maxLeft) left = Math.max(window.scrollX + 8, maxLeft);
    tip.style.top = top + "px";
    tip.style.left = left + "px";
  }

  function openTip(node, info) {
    closeTip();
    var tip = buildTip(info);
    positionTip(tip, node);
    node.setAttribute("aria-expanded", "true");
    activeTip = tip;
    activeNode = node;
  }

  function toggle(node) {
    if (activeNode === node) { closeTip(); return; }
    loadGlossary().then(function (g) {
      var info = g[node.getAttribute("data-metric")];
      if (info) openTip(node, info); // unknown key -> leave native title as the fallback
    });
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var node = t.closest("[data-metric]");
    if (node) { e.preventDefault(); toggle(node); return; }
    if (activeTip && !t.closest(".owq-tip")) closeTip(); // outside click closes
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeTip(); return; }
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      var t = e.target;
      var node = t && t.closest ? t.closest("[data-metric]") : null;
      if (node) { e.preventDefault(); toggle(node); }
    }
  });

  window.addEventListener("resize", closeTip);
  window.addEventListener("scroll", closeTip, true);

  // Warm the cache so the first tap is instant; harmless if there are no metric nodes.
  if (document.querySelector("[data-metric]")) loadGlossary();

  // ---- Equity curve (hand-rolled SVG, no chart lib) ----------------------------------
  function drawEquityCurve(container, points) {
    var n = points.length;
    if (n < 2) return false;
    var W = 640, H = 200, pad = 30;
    var eq = points.map(function (pt) { return pt.equity; });
    var lo = Math.min.apply(null, eq), hi = Math.max.apply(null, eq);
    if (hi === lo) hi = lo + 1;
    var X = function (i) { return pad + i * (W - 2 * pad) / (n - 1); };
    var Y = function (v) { return H - pad - (v - lo) / (hi - lo) * (H - 2 * pad); };
    // Max drawdown: track running peak, find the deepest peak->trough drop.
    var peak = eq[0], curPeakI = 0, ddPeakI = 0, ddTroughI = 0, worst = 0;
    for (var i = 0; i < n; i++) {
      if (eq[i] > peak) { peak = eq[i]; curPeakI = i; }
      var dd = eq[i] / peak - 1;
      if (dd < worst) { worst = dd; ddTroughI = i; ddPeakI = curPeakI; }
    }
    var base = eq[0];
    var poly = points.map(function (pt, i) { return X(i).toFixed(1) + "," + Y(pt.equity).toFixed(1); }).join(" ");
    var stroke = eq[n - 1] >= base ? "var(--green)" : "var(--red)";
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="资产净值曲线">';
    if (worst < -0.0001 && ddTroughI > ddPeakI) {
      svg += '<rect x="' + X(ddPeakI).toFixed(1) + '" y="' + pad + '" width="' +
        (X(ddTroughI) - X(ddPeakI)).toFixed(1) + '" height="' + (H - 2 * pad) +
        '" fill="rgba(220,38,38,0.09)"></rect>';
    }
    svg += '<line x1="' + pad + '" y1="' + Y(base).toFixed(1) + '" x2="' + (W - pad) +
      '" y2="' + Y(base).toFixed(1) + '" stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1" opacity="0.5"></line>';
    svg += '<polyline fill="none" stroke="' + stroke + '" stroke-width="2" stroke-linejoin="round" points="' + poly + '"></polyline>';
    svg += "</svg>";
    svg += '<p class="muted" style="margin:6px 0 0;font-size:12px">区间最大回撤 ' + (worst * 100).toFixed(1) +
      "% · " + points[0].date + " → " + points[n - 1].date + "(虚线=初始本金)</p>";
    container.innerHTML = svg;
    return true;
  }

  function initEquityCurve() {
    var container = document.querySelector("[data-equity-curve]");
    if (!container) return;
    fetch("/api/equity-curve", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.points && drawEquityCurve(container, data.points)) {
          var section = container.closest("[data-equity-section]");
          if (section) section.removeAttribute("hidden"); // reveal only once drawn
        }
      })
      .catch(function () { /* no chart; the metric cards above still tell the story */ });
  }
  initEquityCurve();
})();
