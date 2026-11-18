/* Gantt chart of the renovation plan.

   Reads the plan from the JSON <script> named by #gantt[data-source] and redraws
   from the server's answer after every change, so whatever waits for a moved
   task moves with it: the schedule is worked out on the server (plan.py).

   Rows: each big item (goal), then the tasks under it. Left column sticks while
   the timeline scrolls; the header sticks while the rows scroll.
     drag a bar           move it (sets "not before"; in progress: when it started)
     drag its right edge  change how many days it takes
     drag its end dot     onto another bar: that one waits for this one
     drag a deadline ◆    move the big item's deadline
     click                edit; click an arrow to remove that link
*/
(function () {
  "use strict";

  document.querySelectorAll("[data-show-chart]").forEach(function (button) {
    button.addEventListener("click", function () { document.body.classList.add("show-chart"); });
  });

  var root = document.getElementById("gantt");
  if (!root) return;

  var csrf = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var data = JSON.parse(document.getElementById(root.getAttribute("data-source")).textContent);
  var statusLine = document.querySelector(".gantt-status");

  var ZOOMS = { day: 30, week: 12, month: 4 };
  var ROW = 34, SIDE = 300, DAY_MS = 86400000;
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var zoom = "week";
  try { zoom = localStorage.getItem("gantt-zoom") || zoom; } catch (e) { /* private mode */ }
  if (!ZOOMS[zoom]) zoom = "week";

  // ── dates as day numbers (days since 1970-01-01, UTC: no DST surprises) ──
  function dayNum(s) { var p = s.split("-"); return Math.round(Date.UTC(+p[0], +p[1] - 1, +p[2]) / DAY_MS); }
  function iso(n) { return new Date(n * DAY_MS).toISOString().slice(0, 10); }
  function date(n) { return new Date(n * DAY_MS); }
  function label(n, withYear) {
    var d = date(n);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + (withYear ? " " + d.getUTCFullYear() : "");
  }
  function weekday(n) { return (n + 3) % 7; }  // 0 = Monday (1 Jan 1970 was a Thursday)
  function plural(n, word) { n = Math.abs(n); return n + " " + word + (n === 1 ? "" : "s"); }
  function eur(cents) {
    var whole = cents % 100 === 0;
    return "€" + (cents / 100).toLocaleString("en", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 });
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function shorten(text) { return text.length > 400 ? text.slice(0, 400) + "…" : text; }
  function notesMark(notes) {
    return notes ? '<span class="g-notes" title="' + esc(shorten(notes)) + '" aria-label="Has notes">≡</span>' : "";
  }

  function say(message, bad) {
    if (!statusLine) return;
    statusLine.textContent = message || "";
    statusLine.classList.toggle("bad", !!bad);
  }

  // ── layout ──
  var view;  // {origin, last, px, rows, tasks, rowOf}

  function layout() {
    var today = dayNum(data.today);
    var first = today, last = today;
    data.goals.forEach(function (g) {
      var deadline = dayNum(g.deadline);
      if (g.start) first = Math.min(first, dayNum(g.start));
      last = Math.max(last, deadline, g.end ? dayNum(g.end) : deadline);
    });
    var px = ZOOMS[zoom];
    var origin = first - 7;
    origin -= weekday(origin);  // start on a Monday so weekend shading lines up
    var pad = zoom === "month" ? 90 : zoom === "week" ? 42 : 21;
    var rows = [], tasks = {}, rowOf = {};
    data.tasks.forEach(function (t) { tasks[t.id] = t; });
    data.goals.forEach(function (g) {
      rows.push({ kind: "goal", goal: g, count: 0 });
      var head = rows[rows.length - 1];
      data.tasks.forEach(function (t) {
        if (t.goal_id !== g.id) return;
        rowOf[t.id] = rows.length;
        rows.push({ kind: "task", task: t, goal: g });
        head.count += 1;
      });
    });
    return { today: today, origin: origin, last: last + pad, px: px, rows: rows, tasks: tasks, rowOf: rowOf };
  }

  function x(n) { return (n - view.origin) * view.px; }

  function header(width) {
    var months = [], ticks = [];
    var d = date(view.origin);
    var m = Math.round(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / DAY_MS);
    while (m <= view.last) {
      var md = date(m);
      var next = Math.round(Date.UTC(md.getUTCFullYear(), md.getUTCMonth() + 1, 1) / DAY_MS);
      var left = Math.max(x(m), 0), right = Math.min(x(next), width);
      var name = MONTHS[md.getUTCMonth()] + (zoom === "month" ? " " + String(md.getUTCFullYear()).slice(2) : " " + md.getUTCFullYear());
      months.push('<div class="g-month" style="left:' + left + "px;width:" + (right - left) + 'px"><span>' + name + "</span></div>");
      m = next;
    }
    if (zoom !== "month") {
      var step = zoom === "day" ? 1 : 7;
      for (var n = view.origin; n <= view.last; n += step) {
        var cls = "g-tick" + (zoom === "day" && weekday(n) >= 5 ? " weekend" : "") + (n === view.today ? " is-today" : "");
        ticks.push('<div class="' + cls + '" style="left:' + x(n) + "px;width:" + step * view.px + 'px">' + date(n).getUTCDate() + "</div>");
      }
    }
    return '<div class="g-head"><div class="g-corner"><span>Big item / task</span><span>Estimate</span></div>' +
      '<div class="g-scale" style="width:' + width + 'px"><div class="g-months">' + months.join("") + "</div>" +
      '<div class="g-ticks">' + ticks.join("") + "</div></div></div>";
  }

  function trackStyle(width) {
    var px = view.px;
    if (zoom === "month") return "width:" + width + "px";
    // Mon-Fri clear, Sat-Sun shaded; origin is a Monday.
    return "width:" + width + "px;background-image:repeating-linear-gradient(to right,transparent 0," +
      "transparent " + 5 * px + "px,var(--weekend) " + 5 * px + "px,var(--weekend) " + 7 * px + "px)";
  }

  function taskTitle(t) {
    var lines = [t.title, label(dayNum(t.start), true) + " – " + label(dayNum(t.end), true) + " (" + plural(t.days, "day") + ")"];
    if (t.status === "doing") lines.push("In progress");
    if (t.status === "done") lines.push("Done");
    if (t.who) lines.push(t.who);
    if (t.cost) lines.push(eur(t.cost) + (t.paid ? ", " + eur(t.paid) + " paid" : ""));
    if (t.status !== "done") {
      lines.push("Must start by " + label(dayNum(t.latest_start), true) +
        (t.slack < 0 ? " (" + plural(t.slack, "day") + " late)" : " (" + plural(t.slack, "day") + " to spare)"));
    }
    if (t.blocked_by.length) {
      lines.push("Waiting for: " + t.blocked_by.map(function (id) { return view.tasks[id].title; }).join(", "));
    }
    if (t.not_before) lines.push("Not before " + label(dayNum(t.not_before), true));
    if (t.notes) lines.push("", shorten(t.notes));
    return lines.join("\n");
  }

  function goalRow(g, width, rowIndex) {
    var late = g.late ? " is-late" : g.tight ? " is-tight" : "";
    var meta = "by " + label(dayNum(g.deadline)) + (g.total ? " · " + g.done + "/" + g.total + " done" : " · no tasks yet");
    var bar = "";
    if (g.start && g.end) {
      var left = x(dayNum(g.start)), w = (dayNum(g.end) - dayNum(g.start) + 1) * view.px;
      bar = '<div class="g-summary' + late + '" style="left:' + left + "px;width:" + w + 'px"></div>';
    }
    var dl = dayNum(g.deadline) + 1;
    var diamond = '<button type="button" class="g-deadline' + (g.late ? " is-late" : "") + '" data-goal="' + g.id +
      '" style="left:' + (x(dl) - 7) + 'px" title="Deadline: done by ' + esc(label(dayNum(g.deadline), true)) +
      (g.end ? (g.late ? " — planned to finish " + plural(g.slack, "day") + " late" : " — " + plural(g.slack, "day") + " to spare") : "") +
      '" aria-label="Deadline of ' + esc(g.title) + '"></button>';
    return '<div class="g-row g-row-goal' + late + (g.finished ? " is-finished" : "") + '" id="goal-' + g.id + '">' +
      '<div class="g-label"><div class="g-label-main"><a href="/goals/' + g.id + '?next=/" class="g-goal-title">' + esc(g.title) + "</a>" +
      notesMark(g.notes) + '<span class="g-meta">' + esc(meta) + "</span></div>" +
      '<span class="g-cost">' + (g.estimate ? eur(g.estimate) : "") + "</span>" +
      '<a class="g-add" href="/tasks/new?goal=' + g.id + '&amp;next=/" title="Add a task to ' + esc(g.title) + '" aria-label="Add a task">+</a></div>' +
      '<div class="g-track" style="' + trackStyle(width) + '">' + bar + diamond + "</div></div>";
  }

  function taskRow(t, width) {
    var start = dayNum(t.start), end = dayNum(t.end);
    var left = x(start), w = (end - start + 1) * view.px;
    var cls = "g-bar status-" + t.status + (t.flag ? " flag-" + t.flag : "") + (t.status === "done" ? " locked" : "");
    var progress = "";
    if (t.status === "doing") {
      var share = Math.min(Math.max((view.today - start + 1) / (end - start + 1), 0), 1);
      progress = '<span class="g-progress" style="width:' + (share * 100).toFixed(1) + '%"></span>';
    }
    var labelOut = w < t.title.length * 7 + 18 ? " label-out" : "";
    var handles = t.status === "done" ? "" :
      '<span class="g-handle" data-action="resize" title="Drag to change how long it takes"></span>';
    var dot = '<span class="g-dot" data-action="link" title="Drag onto the task that has to wait for this one"></span>';
    var flag = t.flag === "late" ? '<span class="g-flag late" title="Misses a deadline">!</span>' :
      t.flag === "tight" ? '<span class="g-flag tight" title="Little room for delays">•</span>' : "";
    return '<div class="g-row g-row-task status-' + t.status + '" id="task-' + t.id + '">' +
      '<div class="g-label"><div class="g-label-main">' + flag + '<a href="/tasks/' + t.id + '?next=/" class="g-task-title">' + esc(t.title) + "</a>" +
      notesMark(t.notes) + (t.who ? '<span class="g-meta">' + esc(t.who) + "</span>" : "") + "</div>" +
      '<span class="g-cost">' + (t.cost ? eur(t.cost) : "") + "</span></div>" +
      '<div class="g-track" style="' + trackStyle(width) + '">' +
      '<a class="' + cls + labelOut + '" data-id="' + t.id + '" href="/tasks/' + t.id + '?next=/" draggable="false" ' +
      'style="left:' + left + "px;width:" + w + 'px" title="' + esc(taskTitle(t)) + '">' + progress +
      '<span class="g-bar-label">' + esc(t.title) + "</span>" + handles + dot + "</a></div></div>";
  }

  function links() {
    var paths = [];
    data.tasks.forEach(function (t) {
      t.needs.forEach(function (needId) {
        var n = view.tasks[needId];
        if (!n || view.rowOf[needId] === undefined) return;
        var x1 = x(dayNum(n.end) + 1), y1 = view.rowOf[needId] * ROW + ROW / 2;
        var x2 = x(dayNum(t.start)), y2 = view.rowOf[t.id] * ROW + ROW / 2;
        var d;
        if (x2 - x1 >= 14) {
          var mid = x1 + 7;
          d = "M" + x1 + " " + y1 + "H" + mid + "V" + y2 + "H" + (x2 - 2);
        } else {
          // Not enough room: go round, between the rows.
          var between = y2 > y1 ? y2 - ROW / 2 : y2 + ROW / 2;
          d = "M" + x1 + " " + y1 + "H" + (x1 + 7) + "V" + between + "H" + (x2 - 9) + "V" + y2 + "H" + (x2 - 2);
        }
        var cls = "g-link" + (t.blocked_by.indexOf(needId) >= 0 ? "" : " met") + (t.flag === "late" ? " late" : "");
        paths.push('<g class="' + cls + '" data-task="' + t.id + '" data-needs="' + needId + '">' +
          '<title>' + esc(t.title) + " waits for " + esc(n.title) + " (click to remove)</title>" +
          '<path class="g-link-hit" d="' + d + '"/><path class="g-link-line" d="' + d + '" marker-end="url(#g-arrow)"/></g>');
      });
    });
    return paths.join("");
  }

  function deadlineLines() {
    var lines = [];
    view.rows.forEach(function (row, i) {
      if (row.kind !== "goal") return;
      var g = row.goal;
      lines.push('<div class="g-deadline-line' + (g.late ? " is-late" : "") + '" style="left:' + x(dayNum(g.deadline) + 1) +
        "px;top:" + i * ROW + "px;height:" + (row.count + 1) * ROW + 'px"></div>');
    });
    return lines.join("");
  }

  function render(keepScroll) {
    var scroller = root.querySelector(".g-scroll");
    var scroll = scroller && keepScroll ? { left: scroller.scrollLeft, top: scroller.scrollTop } : null;
    view = layout();
    var width = (view.last - view.origin + 1) * view.px;
    var height = view.rows.length * ROW;
    var rows = view.rows.map(function (row, i) {
      return row.kind === "goal" ? goalRow(row.goal, width, i) : taskRow(row.task, width);
    }).join("");
    root.innerHTML =
      '<div class="g-scroll"><div class="g-grid" style="width:' + (SIDE + width) + 'px">' + header(width) +
      '<div class="g-body" style="height:' + height + 'px">' + rows +
      '<div class="g-overlay" style="left:' + SIDE + "px;width:" + width + "px;height:" + height + 'px">' +
      deadlineLines() +
      '<div class="g-today" style="left:' + (x(view.today) + view.px / 2) + 'px" title="Today"></div>' +
      '<svg class="g-links" width="' + width + '" height="' + height + '">' +
      '<defs><marker id="g-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">' +
      '<path d="M0 0L8 4L0 8z"/></marker></defs>' + links() +
      '<line class="g-drag-line" x1="0" y1="0" x2="0" y2="0"/></svg></div></div></div></div>';
    document.querySelectorAll("[data-zoom]").forEach(function (a) {
      if (a.getAttribute("data-zoom") === zoom) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
    scroller = root.querySelector(".g-scroll");
    if (scroll) {
      scroller.scrollLeft = scroll.left;
      scroller.scrollTop = scroll.top;
    } else {
      scrollToToday();
    }
  }

  function scrollToToday() {
    var scroller = root.querySelector(".g-scroll");
    if (scroller) scroller.scrollLeft = Math.max(x(view.today) - 7 * view.px, 0);
  }

  // ── talking to the server ──
  function post(url, body, okMessage) {
    say("Saving…");
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return { error: "Something went wrong (" + response.status + ")." }; })
        .then(function (json) {
          if (!response.ok || json.error) throw new Error(json.error || "Something went wrong.");
          data = json;
          render(true);
          say(okMessage);
        });
    }).catch(function (error) {
      render(true);
      say(error.message, true);
    });
  }

  // ── dragging ──
  var drag = null;
  var suppressClick = false;

  function overlayPoint(event) {
    var overlay = root.querySelector(".g-overlay").getBoundingClientRect();
    return { x: event.clientX - overlay.left, y: event.clientY - overlay.top };
  }

  root.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    var diamond = event.target.closest(".g-deadline");
    var bar = event.target.closest(".g-bar");
    if (!diamond && !bar) return;
    var action = event.target.getAttribute("data-action") || "move";
    if (bar && bar.classList.contains("locked") && action !== "link") return;
    event.preventDefault();
    var el = diamond || bar;
    drag = {
      el: el, mode: diamond ? "deadline" : action, startX: event.clientX, startY: event.clientY, moved: false,
      left: parseFloat(el.style.left), width: parseFloat(el.style.width || 0),
      id: diamond ? +diamond.getAttribute("data-goal") : +bar.getAttribute("data-id")
    };
    if (drag.mode === "link") {
      var p = overlayPoint(event);
      drag.from = p;
    }
    el.setPointerCapture(event.pointerId);
    el.classList.add("dragging");
  });

  root.addEventListener("pointermove", function (event) {
    if (!drag) return;
    var dx = event.clientX - drag.startX;
    drag.moved = drag.moved || Math.abs(dx) > 3 || Math.abs(event.clientY - drag.startY) > 3;
    var days = Math.round(dx / view.px);
    if (drag.mode === "move") {
      var t = view.tasks[drag.id];
      drag.el.style.left = drag.left + days * view.px + "px";
      say(t.title + ": from " + label(dayNum(t.start) + days, true));
    } else if (drag.mode === "resize") {
      var task = view.tasks[drag.id];
      var newDays = Math.max(task.days + days, 1);
      drag.el.style.width = newDays * view.px + "px";
      say(task.title + ": " + plural(newDays, "day") + ", until " + label(dayNum(task.start) + newDays - 1, true));
    } else if (drag.mode === "deadline") {
      var g = data.goals.filter(function (goal) { return goal.id === drag.id; })[0];
      drag.el.style.left = drag.left + days * view.px + "px";
      say(g.title + ": done by " + label(dayNum(g.deadline) + days, true));
    } else if (drag.mode === "link") {
      var p = overlayPoint(event);
      var line = root.querySelector(".g-drag-line");
      line.setAttribute("x1", drag.from.x); line.setAttribute("y1", drag.from.y);
      line.setAttribute("x2", p.x); line.setAttribute("y2", p.y);
      line.classList.add("on");
      var over = document.elementFromPoint(event.clientX, event.clientY);
      var target = over && over.closest(".g-bar");
      root.querySelectorAll(".g-bar.link-target").forEach(function (b) { b.classList.remove("link-target"); });
      if (target && +target.getAttribute("data-id") !== drag.id) {
        target.classList.add("link-target");
        say(view.tasks[+target.getAttribute("data-id")].title + " waits for " + view.tasks[drag.id].title);
      } else {
        say("Drop on the task that has to wait for " + view.tasks[drag.id].title);
      }
    }
  });

  function endDrag(event, cancelled) {
    if (!drag) return;
    var d = drag;
    drag = null;
    d.el.classList.remove("dragging");
    var days = Math.round((event.clientX - d.startX) / view.px);
    if (!d.moved || cancelled) {
      if (d.mode === "link") { render(true); say(""); }
      if (d.moved) render(true);
      return;
    }
    suppressClick = true;
    setTimeout(function () { suppressClick = false; }, 0);
    if (d.mode === "move") {
      if (!days) return render(true);
      var t = view.tasks[d.id];
      post("/api/tasks/" + d.id + "/move", { start: iso(dayNum(t.start) + days) }, "Moved " + t.title + ".");
    } else if (d.mode === "resize") {
      var task = view.tasks[d.id];
      var newDays = Math.max(task.days + days, 1);
      if (newDays === task.days) return render(true);
      post("/api/tasks/" + d.id + "/resize", { days: newDays }, task.title + " takes " + plural(newDays, "day") + ".");
    } else if (d.mode === "deadline") {
      if (!days) return render(true);
      var g = data.goals.filter(function (goal) { return goal.id === d.id; })[0];
      post("/api/goals/" + d.id + "/deadline", { deadline: iso(dayNum(g.deadline) + days) }, "Moved the deadline of " + g.title + ".");
    } else if (d.mode === "link") {
      var over = document.elementFromPoint(event.clientX, event.clientY);
      var target = over && over.closest(".g-bar");
      var targetId = target ? +target.getAttribute("data-id") : null;
      if (!targetId || targetId === d.id) { render(true); say(""); return; }
      post("/api/tasks/" + targetId + "/needs", { needs_id: d.id },
        view.tasks[targetId].title + " now waits for " + view.tasks[d.id].title + ".");
    }
  }

  root.addEventListener("pointerup", function (event) { endDrag(event, false); });
  root.addEventListener("pointercancel", function (event) { endDrag(event, true); });

  root.addEventListener("click", function (event) {
    if (suppressClick || event.target.closest(".g-dot, .g-handle")) {
      event.preventDefault();
      return;
    }
    var diamond = event.target.closest(".g-deadline");
    if (diamond) {
      window.location.href = "/goals/" + diamond.getAttribute("data-goal") + "?next=/";
      return;
    }
    var link = event.target.closest(".g-link");
    if (link) {
      var task = view.tasks[+link.getAttribute("data-task")], needs = view.tasks[+link.getAttribute("data-needs")];
      if (window.confirm("Remove the link? " + task.title + " would no longer wait for " + needs.title + ".")) {
        post("/api/tasks/" + task.id + "/needs", { needs_id: needs.id, remove: true },
          task.title + " no longer waits for " + needs.title + ".");
      }
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && drag) {
      drag.el.classList.remove("dragging");
      drag = null;
      render(true);
      say("");
    }
  });

  // ── toolbar ──
  document.querySelectorAll("[data-zoom]").forEach(function (a) {
    a.addEventListener("click", function (event) {
      event.preventDefault();
      zoom = a.getAttribute("data-zoom");
      try { localStorage.setItem("gantt-zoom", zoom); } catch (e) { /* private mode */ }
      render(false);
    });
  });
  document.querySelectorAll("[data-scroll-today]").forEach(function (button) {
    button.addEventListener("click", scrollToToday);
  });

  render(false);
})();
