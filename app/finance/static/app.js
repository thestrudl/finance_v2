(function () {
  "use strict";

  // Forms with data-confirm ask before submitting (deletes, merges).
  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  document.querySelectorAll("[data-autosubmit]").forEach(function (input) {
    input.addEventListener("change", function () { input.form.submit(); });
  });

  if (!window.Chart) return;

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  var MONO = token("--font-label");
  function eur(value) {
    return "€" + Number(value).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function eurShort(value) {
    var abs = Math.abs(value);
    if (abs >= 1000) return "€" + (value / 1000).toLocaleString("en", { maximumFractionDigits: 1 }) + "k";
    return "€" + value;
  }

  // Each chart reads {type, labels, series: [{name, values (cents or null), color (CSS token)}], net?}
  // from the JSON <script> its canvas names in data-chart.
  //   bar      grouped bars          stacked  bars stacked per label
  //   line     2px lines; a series coloured --ref is the grey reference
  // A series with kind "line" inside a bar chart is drawn as a line over the bars.
  // compact: true trims ticks for small multiples.
  function dataset(type, s, surface) {
    var values = s.values.map(function (v) { return v === null ? null : v / 100; });
    var color = token(s.color);
    if (s.kind === "points") {  // markers only, e.g. balance checks on an estimate line
      return {
        type: "line", label: s.name, data: values, showLine: false,
        borderColor: color, backgroundColor: color, pointRadius: 4, pointHoverRadius: 6, pointHitRadius: 12,
        pointStyle: "rect", order: 0
      };
    }
    if (type === "line" || s.kind === "line") {
      var reference = s.color === "--ref";
      return {
        type: "line",
        label: s.name, data: values, borderColor: color, backgroundColor: color,
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12,
        tension: 0, spanGaps: false,
        // Lower order draws on top: over bars a reference line goes first, in a line chart last.
        order: type === "line" ? (reference ? 2 : 1) : 0
      };
    }
    return {
      label: s.name, data: values, backgroundColor: color, borderColor: surface,
      // A 2px surface gap between neighbouring bars and stacked segments; square
      // ends, like every corner in the blog's design.
      borderWidth: type === "stacked" ? { top: 2 } : { left: 1, right: 1 },
      borderRadius: 0,
      borderSkipped: "start",
      maxBarThickness: 26, categoryPercentage: 0.72, barPercentage: 0.92, order: 1
    };
  }

  function config(data) {
    var surface = token("--surface");
    var type = data.type || "bar";
    var stacked = type === "stacked";
    return {
      type: type === "line" ? "line" : "bar",
      data: {
        labels: data.labels,
        datasets: data.series.map(function (s) { return dataset(type, s, surface); })
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: data.series.length > 1,
            position: "top",
            align: "start",
            labels: {
              color: token("--ink-2"), boxWidth: 10, boxHeight: 10,
              font: { family: MONO, size: 11 },
              // Series order, not drawing order (a reference line is drawn first so it sits on top).
              sort: function (a, b) { return a.datasetIndex - b.datasetIndex; }
            }
          },
          tooltip: {
            backgroundColor: token("--surface"),
            titleColor: token("--ink"),
            bodyColor: token("--ink-2"),
            footerColor: token("--ink"),
            borderColor: token("--border"),
            borderWidth: 1,
            cornerRadius: 0,
            padding: 10,
            titleFont: { family: MONO, size: 11, weight: "500" },
            filter: function (item) { return item.parsed.y !== null; },
            callbacks: {
              label: function (ctx) { return " " + ctx.dataset.label + ": " + eur(ctx.parsed.y); },
              footer: function (items) {
                if (stacked && items.length > 1) {
                  return "Total: " + eur(items.reduce(function (sum, i) { return sum + i.parsed.y; }, 0));
                }
                if (!data.net || items.length < 2) return "";
                return "Net: " + eur(items[0].parsed.y - items[1].parsed.y);
              }
            }
          }
        },
        scales: {
          x: {
            stacked: stacked,
            grid: { display: false }, border: { color: token("--axis") },
            ticks: {
              color: token("--muted"), autoSkip: true, maxRotation: 0, font: { family: MONO, size: 10.5 },
              // Daily series (holdings) have hundreds of labels: keep a readable handful.
              maxTicksLimit: data.labels.length > 40 ? 8 : undefined
            }
          },
          y: {
            stacked: stacked,
            beginAtZero: true,
            grid: { color: token("--grid") },
            border: { display: false },
            ticks: {
              color: token("--muted"), callback: eurShort, maxTicksLimit: data.compact ? 3 : 6,
              font: { family: MONO, size: 10.5 }
            }
          }
        }
      }
    };
  }

  Chart.defaults.font.family = token("--font-body");
  var charts = [];
  function render() {
    charts.forEach(function (chart) { chart.destroy(); });
    charts = [];
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var data = JSON.parse(document.getElementById(canvas.getAttribute("data-chart")).textContent);
      charts.push(new Chart(canvas, config(data)));
    });
  }
  // Draw once the self-hosted fonts are in, so axis labels measure right.
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(render);
})();
