"""Static HTML/CSS/JS shell for the loopback-only read-only web viewer (Task 14).

This module is a deliberate dead-end leaf: it imports ONLY the standard
library, never ``gmail_tidy``, so it can never leak data, reach Gmail, or grow
write side effects. It owns the client side of the viewer:

- separated ``SHELL_CSS`` / ``SHELL_JS`` / ``SHELL_HTML`` constants so each
  asset is reviewable and testable on its own;
- ``html_shell() -> bytes`` assembles them into the full ``<!doctype html>``
  document served at ``GET /`` (the HTTP/server side lives in ``gmail_tidy.web``
  which delegates its ``_html_shell()`` here).

Client contract (pinned by ``tests/test_webapp_ux.py``):

* hash routing only, eight fixed views, unknown hashes fall back to overview;
* consumes ONLY the existing relative endpoints listed in ``gmail_tidy.web``
  (no new routes, no external assets, no network calls);
* every fetched string is rendered with ``textContent`` / DOM nodes — no
  unsafe ``innerHTML`` interpolation, ``eval``, ``document.write``,
  ``javascript:`` URLs, or remote URLs;
* privacy posture matches the server: aggregate views show no ids; detail
  views show only existing local run/audit ids and actions, never bodies,
  senders, subjects, tokens, secrets, or page tokens.
"""

from __future__ import annotations

# The eight user-approved views, in nav order. Hash routing is client-only:
# unknown hashes fall back to "overview".
VIEWS = (
    "overview",   # default
    "runs",
    "run",        # #/run/<run_id>
    "audit",
    "rules",
    "checkpoint",
    "setup",
    "privacy",
)

# Fixed route strings the client may consume. Kept here so a single edit
# cannot drift the client away from the server's route table in gmail_tidy.web.
API_STATUS = "/api/v1/status"
API_CONFIG = "/api/v1/config"
API_RUNS = "/api/v1/runs"
API_RUN_PREFIX = "/api/v1/runs/"
API_AUDIT_SUMMARY = "/api/v1/audit/summary"
API_AUDIT_LIMIT = "/api/v1/audit?limit=200"
API_CHECKPOINT = "/api/v1/checkpoint"

SHELL_CSS = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    margin: 0; line-height: 1.5; color: #1a1a1a; background: #fff;
  }
  header.site {
    border-bottom: 1px solid #ccc; padding: .75rem 1rem;
    display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem 1rem;
  }
  header.site h1 { font-size: 1.1rem; margin: 0; }
  nav ul {
    list-style: none; display: flex; flex-wrap: wrap;
    gap: .25rem .5rem; margin: 0; padding: 0;
  }
  nav a {
    color: #1a1a1a; text-decoration: none; display: block;
    min-height: 2.75rem; padding: .65rem .5rem;
  }
  nav a:hover { text-decoration: underline; }
  nav a[aria-current] { font-weight: 700; border-bottom: 2px solid #222; }
  a:focus-visible, button:focus-visible {
    outline: 2px solid #0056b3; outline-offset: 2px;
  }
  main { padding: 1rem; max-width: 72rem; margin: 0 auto; }
  h1, h2, h3 { line-height: 1.25; }
  h2 { font-size: 1.15rem; margin: 1.4rem 0 .5rem; }
  h3 { font-size: 1rem; margin: 1rem 0 .35rem; }
  a { color: #0056b3; }
  table {
    border-collapse: collapse; width: 100%; max-width: 68rem;
    margin: .35rem 0 1rem; font-size: .95rem;
  }
  caption { text-align: left; font-weight: 600; padding: .3rem 0; }
  th, td { text-align: left; padding: .3rem .55rem; border-bottom: 1px solid #ddd; }
  th { font-weight: 600; }
  td { word-break: break-word; }
  .num { text-align: right; }
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
    gap: .6rem; max-width: 56rem; margin: .6rem 0 1rem;
  }
  .card { border: 1px solid #ccc; border-radius: .45rem; padding: .55rem .7rem; }
  .card b { display: block; font-size: 1.35rem; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  code { background: #f0f0f0; padding: .08rem .3rem; border-radius: .25rem; }
  pre { background: #f6f6f6; border: 1px solid #ddd; border-radius: .35rem; padding: .6rem .8rem; overflow-x: auto; }
  .muted { color: #666; }
  .ok { color: #1a7f37; font-weight: 600; }
  .warn { color: #9a6700; font-weight: 600; }
  .err { color: #cf222e; font-weight: 600; }
  .state, .notice {
    border-left: .3rem solid #999; padding: .5rem .75rem; margin: .6rem 0;
    max-width: 68rem;
  }
  .state.loading { border-color: #999; }
  .state.error, .notice.error { border-color: #cf222e; }
  .notice.warn { border-color: #9a6700; }
  .notice.info { border-color: #0056b3; }
  .skip {
    position: absolute; left: -9999px; top: auto; width: 1px; height: 1px; overflow: hidden;
  }
  .skip:focus {
    position: static; left: 0; width: auto; height: auto; overflow: visible;
    background: #fff; padding: .4rem .6rem; border: 2px solid #0056b3;
  }
  footer.site {
    border-top: 1px solid #ccc; margin-top: 2rem; padding: .6rem 1rem;
    font-size: .9rem; color: #666;
  }
  button {
    font: inherit; border: 1px solid #888; border-radius: .3rem;
    min-height: 2.75rem; padding: .4rem .8rem;
    background: #f0f0f0; cursor: pointer;
  }
  button:hover { background: #e2e2e2; }
  @media (max-width: 640px) {
    main { padding: .5rem; }
    header.site { padding: .5rem .6rem; }
    table { display: block; overflow-x: auto; white-space: nowrap; }
    th, td { padding: .25rem .4rem; }
    .cards { grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); }
  }
  @media (prefers-color-scheme: dark) {
    body { background: #111; color: #eee; }
    header.site, footer.site { border-color: #333; }
    nav a { color: #ccc; }
    nav a[aria-current] { border-bottom-color: #fff; }
    a { color: #7fb2ff; }
    th, td { border-bottom-color: #333; }
    .card, pre { border-color: #333; }
    code { background: #2a2a2a; }
    pre { background: #1c1c1c; border-color: #333; }
    .muted { color: #aaa; }
    .ok { color: #6cc47c; }
    .warn { color: #d2a75e; }
    .err { color: #ff7b72; }
    button { background: #2a2a2a; color: #eee; border-color: #555; }
    a:focus-visible, button:focus-visible { outline: 2px solid #fff; }
    .skip:focus { background: #111; }
  }
"""

SHELL_JS = """
"use strict";
// Static hash-routing client shell. Every server-derived string is rendered
// via textContent / DOM nodes only; this file never uses innerHTML, eval,
// document.write, javascript: URLs, or any external/remote URL.

var VIEWS = ["overview", "runs", "run", "audit", "rules", "checkpoint",
             "setup", "privacy"];
var TITLES = {
  overview: "Overview",
  runs: "Runs",
  run: "Run",
  audit: "Audit",
  rules: "Rules",
  checkpoint: "Checkpoint",
  setup: "Setup",
  privacy: "Privacy"
};
var PAGES = {};
var API = {
  status: "/api/v1/status",
  config: "/api/v1/config",
  runs: "/api/v1/runs",
  runPrefix: "/api/v1/runs/",
  auditSummary: "/api/v1/audit/summary",
  audit: "/api/v1/audit?limit=200",
  checkpoint: "/api/v1/checkpoint"
};
var RE_RUN = /^[0-9a-f]{12}$/;
var state = { view: "overview", runId: null };

// Task 22: navigation epoch. Bumped once per navigation (first statement of
// route()); async view renders capture it at render start and drop their own
// then/catch handlers if it has moved on, so a slow in-flight fetch can never
// clobber the currently shown view.
var myEpoch = 0;

function $id(name) { return document.getElementById(name); }

var FETCH_TIMEOUT_MS = 10000;

function jget(url) {
  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); },
                         FETCH_TIMEOUT_MS);
  return fetch(url, { cache: "no-store", signal: controller.signal })
    .then(function (r) {
      clearTimeout(timer);
      if (r.status === 404) { return null; }
      if (!r.ok) { throw new Error("Request failed (" + r.status + ")"); }
      return r.json();
    })
    .catch(function (err) {
      clearTimeout(timer);
      if (err && err.name === "AbortError") {
        throw new Error("Request timed out after " + FETCH_TIMEOUT_MS + "ms");
      }
      throw err;
    });
}

// --- DOM building helpers (text-only; never markup) -----------------------
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text !== undefined && text !== null) { e.textContent = String(text); }
  return e;
}
function thCell(text) {
  var th = el("th", null, text);
  th.setAttribute("scope", "col");
  return th;
}
function rowCell(value) {
  var th = el("th", null, value === null || value === undefined ? "" : String(value));
  th.setAttribute("scope", "row");
  return th;
}
function textCell(value) {
  return el("td", null, value === null || value === undefined ? "" : String(value));
}
function linkCell(href, text) {
  var td = el("td");
  var a = el("a", null, text);
  a.href = href;
  td.appendChild(a);
  return td;
}
function mkTable(captionText, columns, rows, linkCol) {
  // columns: header labels. rows: array of row-arrays; a cell in column
  // `linkCol` may be {href, text} to render an anchor. All other cells are
  // plain text. First column cells render as scope="row" headers.
  var table = el("table");
  var cap = el("caption", null, captionText);
  table.appendChild(cap);
  var thead = el("thead");
  var head = el("tr");
  columns.forEach(function (label) { head.appendChild(thCell(label)); });
  thead.appendChild(head);
  table.appendChild(thead);
  var tbody = el("tbody");
  rows.forEach(function (cells) {
    var tr = el("tr");
    cells.forEach(function (cell, i) {
      if (i === linkCol && cell && typeof cell === "object" && cell.href) {
        tr.appendChild(linkCell(cell.href, cell.text));
      } else if (i === 0) {
        tr.appendChild(rowCell(cell));
      } else {
        tr.appendChild(textCell(cell));
      }
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}
function addLink(container, href, text) {
  var a = el("a", null, text);
  a.href = href;
  container.appendChild(a);
}
function fmtDate(ts) {
  if (!ts) { return ""; }
  var d = new Date(ts * 1000);
  if (isNaN(d.getTime())) { return String(ts); }
  return d.toISOString().replace("T", " ").slice(0, 19);
}
function actionsText(actions) {
  if (!actions) { return ""; }
  var parts = [];
  if (actions.add_label && actions.add_label.length) {
    parts.push("+label: " + actions.add_label.join(", "));
  }
  if (actions.remove_label && actions.remove_label.length) {
    parts.push("-label: " + actions.remove_label.join(", "));
  }
  if (actions.archive) { parts.push("archive"); }
  return parts.join("; ");
}
function criteriaText(criteria) {
  if (!criteria || !criteria.length) { return "(no criteria)"; }
  return criteria.map(function (c) {
    var v = Array.isArray(c.value) ? c.value.join(", ") : String(c.value);
    return c.name + " = " + v;
  }).join("; ");
}

// --- live status region (loading / errors, announced to AT) ---------------
var live = el("div", null);
live.id = "live-status";
live.setAttribute("role", "status");
live.setAttribute("aria-live", "polite");

function setLive(message, kind) {
  live.className = "state " + (kind || "loading");
  live.textContent = message;
}
function clearLive() {
  live.className = "";
  live.textContent = "";
}
function notice(message, kind) {
  return el("div", "notice " + (kind || "info"), message);
}
function errState(message, retry) {
  var box = el("div", "state error");
  box.appendChild(el("p", "err", message));
  if (retry) {
    var btn = el("button", null, "Retry");
    btn.addEventListener("click", retry);
    box.appendChild(btn);
  }
  return box;
}

// --- view renderers (each receives its own container) ---------------------

// Task 22: an async render started under an older epoch is stale; its handler
// must bail out before touching the DOM or the live status region. The start
// value is captured per-render inside each PAGES.* function and passed in,
// because a module-scope helper cannot see a page-local variable.
function _epoch(start) { return myEpoch === start; }

PAGES.overview = function (container) {
  var myEpochStart = myEpoch;
  setLive("Loading overview...", "loading");
  Promise.all([
    jget(API.status), jget(API.checkpoint), jget(API.config),
    jget(API.auditSummary), jget(API.runs)
  ]).then(function (results) {
    if (!_epoch(myEpochStart)) { return; }
    var st = results[0]; var ck = results[1]; var cfg = results[2];
    var au = results[3]; var runs = results[4];
    container.textContent = "";
    container.appendChild(el("h2", null, "Overview"));

    var cards = el("div", "cards");
    var rc = el("div", "card");
    rc.appendChild(el("b", null, String(st.runs_count)));
    rc.appendChild(el("span", "muted", "scan runs"));
    cards.appendChild(rc);

    var cfgLabel = !st.config_present ? "missing"
      : (st.config_valid ? "valid" : "invalid");
    var cc = el("div", "card");
    cc.appendChild(el("b", st.config_valid ? "ok" : "warn", cfgLabel));
    cc.appendChild(el("span", "muted", "config"));
    cards.appendChild(cc);

    var tc = el("div", "card");
    tc.appendChild(el("b", null, st.token_present ? "present" : "absent"));
    tc.appendChild(el("span", "muted", "token"));
    cards.appendChild(tc);

    var kc = el("div", "card");
    kc.appendChild(el("b", null, st.checkpoint_present ? "yes" : "no"));
    kc.appendChild(el("span", "muted", "checkpoint"));
    cards.appendChild(kc);
    container.appendChild(cards);

    if (!st.config_present) {
      container.appendChild(notice(
        "config.yaml not found. Create it and authenticate with: gmail-tidy init " +
        "(see the Setup view).", "warn"));
    } else if (!st.config_valid) {
      container.appendChild(notice(
        "config.yaml is present but invalid. Fix it via the CLI; the next scan " +
        "restarts from page 1.", "warn"));
    }
    if (st.checkpoint_present && ck && !ck.fingerprint) {
      container.appendChild(notice(
        "Checkpoint is empty or unreadable. The next scan restarts from page 1.", "warn"));
    } else if (ck && ck.fingerprint) {
      var ckRules = ck.rules || {};
      if (!Object.keys(ckRules).length) {
        container.appendChild(notice(
          "Checkpoint has no per-rule state yet. Run gmail-tidy scan to record progress.", "info"));
      }
    }

    var ckSec = el("section");
    ckSec.appendChild(el("h2", null, "Checkpoint"));
    if (ck && ck.rules && Object.keys(ck.rules).length) {
      var ckRows = Object.keys(ck.rules).map(function (rid) {
        return [rid, ck.rules[rid] === "exhausted" ? "exhausted" : "in-progress"];
      });
      ckSec.appendChild(mkTable("Per-rule scan state", ["rule", "state"], ckRows, -1));
      ckSec.appendChild(el("p", "muted", "fingerprint: " + String(ck.fingerprint || "")));
    } else {
      ckSec.appendChild(el("p", "muted", "No per-rule checkpoint state on disk yet."));
    }
    addLink(ckSec, "#/checkpoint", "View checkpoint details");
    container.appendChild(ckSec);

    var auSec = el("section");
    auSec.appendChild(el("h2", null, "Audit summary"));
    var byAction = (au && au.by_action) || {};
    if (Object.keys(byAction).length) {
      auSec.appendChild(mkTable("Actions applied (counts only)",
        ["action", "count"],
        Object.keys(byAction).map(function (k) { return [k, byAction[k]]; }), -1));
    } else {
      auSec.appendChild(el("p", "muted", "No audit entries yet."));
    }
    addLink(auSec, "#/audit", "View audit log");
    container.appendChild(auSec);

    var runSec = el("section");
    runSec.appendChild(el("h2", null, "Recent runs"));
    var runIds = (runs && runs.runs) ? runs.runs.slice(-5).reverse() : [];
    if (runIds.length) {
      runSec.appendChild(mkTable("Run ids", ["run id"],
        runIds.map(function (rid) { return [{ href: "#/run/" + rid, text: rid }]; }), 0));
    } else {
      runSec.appendChild(el("p", "muted", "No runs yet. Run gmail-tidy scan to create one."));
    }
    addLink(runSec, "#/runs", "All runs");
    container.appendChild(runSec);
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load overview.", "error");
  });
};

PAGES.runs = function (container) {
  var myEpochStart = myEpoch;
  setLive("Loading runs...");
  jget(API.runs).then(function (data) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(el("h2", null, "Runs"));
    var ids = (data && data.runs) ? data.runs.slice().reverse() : [];
    if (!ids.length) {
      container.appendChild(el("p", "muted", "No runs yet. Run gmail-tidy scan to create one."));
    } else {
      container.appendChild(mkTable("Scan runs (newest first)", ["run id"],
        ids.map(function (rid) { return [{ href: "#/run/" + rid, text: rid }]; }), 0));
    }
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load runs.", "error");
  });
};

PAGES.run = function (container, runId) {
  if (!RE_RUN.test(runId)) {
    container.textContent = "";
    container.appendChild(el("h2", null, "Run"));
    container.appendChild(errState("Invalid run id.", route));
    setLive("Invalid run id.", "error");
    return;
  }
  var myEpochStart = myEpoch;
  setLive("Loading run details...");
  jget(API.runPrefix + runId).then(function (data) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(el("h2", null, "Run " + runId));
    if (!data) {
      container.appendChild(el("p", "muted", "Run not found. It may have been removed."));
      addLink(container, "#/runs", "Back to runs");
      clearLive();
      return;
    }
    var stats = data.stats;
    if (data.stats === null || data.stats === undefined) {
      // Task 19: explicit state for runs recorded without scan statistics.
      container.appendChild(el("p", "muted", "Scan stats not recorded for this run."));
    } else {
      var sRows = [];
      ["evaluated", "excluded", "noop", "candidates"].forEach(function (k) {
        if (stats[k] !== undefined && stats[k] !== null) { sRows.push([k, stats[k]]); }
      });
      if (sRows.length) {
        container.appendChild(mkTable("Scan statistics", ["metric", "count"], sRows, -1));
      }
    }
    var cands = data.candidates || [];
    if (!cands.length) {
      container.appendChild(el("p", "muted", "No candidates in this run."));
    } else {
      container.appendChild(mkTable("Candidates (" + cands.length + ")",
        ["message id", "thread id", "rule", "actions", "before labels", "in inbox"],
        cands.map(function (c) {
          return [c.message_id, c.thread_id, c.rule_id,
                  actionsText(c.actions),
                  (c.before_labels || []).join(", "),
                  String(Boolean(c.in_inbox))];
        }), -1));
    }
    addLink(container, "#/runs", "Back to runs");
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load run.", "error");
  });
};

PAGES.audit = function (container) {
  var myEpochStart = myEpoch;
  setLive("Loading audit log...");
  jget(API.audit).then(function (data) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(el("h2", null, "Audit log"));
    var entries = (data && data.entries) ? data.entries : [];
    if (!entries.length) {
      container.appendChild(el("p", "muted",
        "No audit entries yet. Run gmail-tidy scan/apply to record actions."));
    } else {
      container.appendChild(el("p", "muted", "Showing the most recent " +
        entries.length + " entries."));
      container.appendChild(mkTable("Audit entries",
        ["time", "run id", "rule", "action", "payload", "kind"],
        entries.map(function (e) {
          return [fmtDate(e.ts), e.run_id, e.rule_id, e.action,
                  e.payload === null || e.payload === undefined ? "" : String(e.payload),
                  e.kind];
        }), -1));
    }
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load audit log.", "error");
  });
};

PAGES.rules = function (container) {
  var myEpochStart = myEpoch;
  setLive("Loading rules...");
  jget(API.config).then(function (data) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(el("h2", null, "Rules (criteria only)"));
    if (!data || !data.rules || !data.rules.length) {
      container.appendChild(el("p", "muted",
        "No rules configured. Add rules to config.yaml via the CLI workflow (see Setup)."));
    } else {
      container.appendChild(mkTable("Configured rules (match criteria only)",
        ["rule", "criteria"],
        data.rules.map(function (r) { return [r.id, criteriaText(r.criteria)]; }), -1));
    }
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load rules.", "error");
  });
};

PAGES.checkpoint = function (container) {
  var myEpochStart = myEpoch;
  setLive("Loading checkpoint...");
  jget(API.checkpoint).then(function (ck) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(el("h2", null, "Checkpoint"));
    if (!ck || (!ck.fingerprint && !Object.keys(ck.rules || {}).length)) {
      container.appendChild(el("p", "muted",
        "No checkpoint on disk yet. Run gmail-tidy scan to record scan progress."));
    } else {
      container.appendChild(el("p", "muted", "Fingerprint: " + String(ck.fingerprint || "")));
      container.appendChild(mkTable("Per-rule scan state", ["rule", "state"],
        Object.keys(ck.rules || {}).map(function (rid) {
          return [rid, ck.rules[rid] === "exhausted" ? "exhausted" : "in-progress"];
        }), -1));
      jget(API.status).then(function (st) {
        if (st && st.config_present && !st.config_valid) {
          container.appendChild(notice(
            "config.yaml is invalid, so this checkpoint may be stale. " +
            "The next scan restarts from page 1.", "warn"));
        } else if (st && !st.config_present) {
          container.appendChild(notice(
            "config.yaml is missing; checkpoint progress cannot be trusted. " +
            "Run gmail-tidy init to set up.", "warn"));
        }
      }).catch(function () {
        // The status fetch is advisory only (stale/missing-config warning).
        // A rejection must not propagate to the outer handler, which blanks
        // the already-rendered checkpoint view.
      });
    }
    clearLive();
  }).catch(function (err) {
    if (!_epoch(myEpochStart)) { return; }
    container.textContent = "";
    container.appendChild(errState(err.message, route));
    setLive("Could not load checkpoint.", "error");
  });
};

PAGES.setup = function (container) {
  container.textContent = "";
  container.appendChild(el("h2", null, "Setup"));
  container.appendChild(el("p", null,
    "This web viewer is strictly read-only. Every change to config, rules, " +
    "and scan progress happens through the gmail-tidy command-line tool."));
  var items = [
    ["gmail-tidy init", "create config.yaml template and authenticate read-only"],
    ["gmail-tidy auth status", "show token presence and OAuth scopes"],
    ["gmail-tidy scan", "build a candidate plan (read-only) and write it to the local run journal"],
    ["gmail-tidy preview --compact", "review proposed actions before applying"],
    ["gmail-tidy apply --yes", "apply the latest run's actions"],
    ["gmail-tidy undo --yes", "undo the latest apply"],
    ["gmail-tidy status", "account, scopes, run history, audit path"]
  ];
  var list = el("ul");
  items.forEach(function (pair) {
    var li = el("li");
    li.appendChild(el("code", null, pair[0]));
    li.appendChild(document.createTextNode(" — " + pair[1]));
    list.appendChild(li);
  });
  container.appendChild(list);
  container.appendChild(el("p", "muted",
    "Tip: edit config.yaml by hand, then re-run gmail-tidy scan. Invalid config " +
    "is flagged in the overview before any scan restarts."));
};

PAGES.privacy = function (container) {
  container.textContent = "";
  container.appendChild(el("h2", null, "Privacy"));
  var items = [
    "This page only talks to this viewer on 127.0.0.1 (loopback). It never sends anything to the network.",
    "Aggregate views show counts and groupings only; they never show message ids.",
    "Detail views show only existing local run and audit ids and action names.",
    "Message bodies, senders, and subjects are never fetched, stored, or shown.",
    "OAuth tokens and client secrets are never read by the viewer.",
    "Checkpoint page tokens are never displayed.",
    "No cookies, no tracking, no external assets, no third-party requests."
  ];
  var list = el("ul");
  items.forEach(function (t) { list.appendChild(el("li", null, t)); });
  container.appendChild(list);
  container.appendChild(el("p", "muted",
    "This viewer cannot change anything. Every write still happens through the CLI."));
};

// --- router (client-only hash routing) --------------------------------------
function parseHash() {
  var h = location.hash || "";
  if (h.indexOf("#/run/") === 0) {
    return { view: "run", runId: h.slice(6) };
  }
  var name = h.slice(2).split("/")[0]; // strip "#/" then take first segment
  if (VIEWS.indexOf(name) === -1) { name = "overview"; }
  return { view: name, runId: null };
}
function route() {
  myEpoch++; // Task 22: invalidate every in-flight async render from before.
  var t = parseHash();
  state.view = t.view;
  state.runId = t.runId;
  var title = "gmail-tidy — " + (TITLES[t.view] || TITLES.overview);
  if (t.view === "run" && RE_RUN.test(t.runId)) {
    title += " — " + t.runId;
  }
  document.title = title;
  var section = $id("view");
  section.textContent = "";
  // The run detail view (#/run/<id>) is not itself a nav entry; it maps onto
  // the "runs" nav link so that link carries aria-current while viewing a run.
  var navView = (t.view === "run") ? "runs" : t.view;
  var links = document.querySelectorAll("nav a[data-view]");
  Array.prototype.forEach.call(links, function (a) {
    if (a.getAttribute("data-view") === navView) { a.setAttribute("aria-current", "page"); }
    else { a.removeAttribute("aria-current"); }
  });
  var fn = PAGES[t.view] || PAGES.overview;
  fn(section, t.runId);
}
window.addEventListener("hashchange", route);

// --- boot ---------------------------------------------------------------------
(function () {
  var main = $id("main");
  main.insertBefore(live, main.firstChild);
  route();
})();
"""

# SHELL_HTML stitches the three assets together. Plain string concatenation
# (never format()), so the embedded braces in CSS/JS are safe.
SHELL_HTML = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>gmail-tidy — local viewer</title>\n"
    "<style>\n"
    + SHELL_CSS +
    "\n</style>\n"
    "</head>\n"
    "<body>\n"
    '<a class="skip" href="#main">Skip to content</a>\n'
    '<header class="site">\n'
    "  <h1>gmail-tidy local viewer</h1>\n"
    "  <nav aria-label=\"Viewer\">\n"
    "    <ul>\n"
    '      <li><a href="#/overview" data-view="overview">Overview</a></li>\n'
    '      <li><a href="#/runs" data-view="runs">Runs</a></li>\n'
    '      <li><a href="#/audit" data-view="audit">Audit</a></li>\n'
    '      <li><a href="#/rules" data-view="rules">Rules</a></li>\n'
    '      <li><a href="#/checkpoint" data-view="checkpoint">Checkpoint</a></li>\n'
    '      <li><a href="#/setup" data-view="setup">Setup</a></li>\n'
    '      <li><a href="#/privacy" data-view="privacy">Privacy</a></li>\n'
    "    </ul>\n"
    "  </nav>\n"
    "</header>\n"
    '<main id="main">\n'
    '  <section id="view"></section>\n'
    "</main>\n"
    '<footer class="site">\n'
    "  <span class=\"muted\">Read-only local viewer. Data stays on this machine.</span>\n"
    "</footer>\n"
    "<script>\n"
    + SHELL_JS +
    "\n</script>\n"
    "</body>\n"
    "</html>\n"
)


def html_shell() -> bytes:
    """Return the complete HTML document (UTF-8 bytes) served at ``GET /``."""
    return SHELL_HTML.encode("utf-8")


__all__ = [
    "SHELL_CSS", "SHELL_JS", "SHELL_HTML", "VIEWS", "html_shell",
    "API_STATUS", "API_CONFIG", "API_RUNS", "API_RUN_PREFIX",
    "API_AUDIT_SUMMARY", "API_AUDIT_LIMIT", "API_CHECKPOINT",
]
