// The iterplay viewer (HLD §7): a run's version timeline with a score sparkline, v1 and the
// selected version's playtests side by side, its verified issues and its patch notes.
//
// Every input is a static file served by `iterplay view`:
//   runs/index.json                       RunIndex
//   runs/<id>/summary.json                RunSummary
//   runs/<id>/vN/score.json               ScoreCard
//   runs/<id>/vN/critique.verified.json   VerifiedCritique
//   runs/<id>/vN/changelog.md             patch notes
//   runs/<id>/vN/playtest/<bot>.webm      recordings
//
// Much of that text comes from the model, so it is untrusted. It reaches the page only through
// textContent and other DOM APIs that never parse markup; innerHTML is never used.
//
// The URL hash (#run=<id>&v=<n>&bot=<bot>) keeps the selection across reloads.

const BOTS = ["random", "speedrunner", "newbie"];
const DEFAULT_BOT = "speedrunner";
const OUTCOMES = ["accepted", "reverted", "build_failed"];
const SEVERITIES = ["critical", "high", "medium", "low"];
const ISSUE_GROUPS = [
  {
    status: "confirmed",
    title: "Confirmed",
    hint: "Telemetry or console output corroborates these claims.",
  },
  {
    status: "unverifiable",
    title: "Unverifiable",
    hint: "Telemetry cannot settle these claims, so they were kept at a lower priority.",
  },
  {
    status: "contradicted",
    title: "Caught hallucinations",
    hint: "Telemetry disproves these claims, so they were dropped and never sent to the Fixer.",
  },
];
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const TIMESTAMP = /^(\d{2}):(\d{2})$/;
const SVG_NS = "http://www.w3.org/2000/svg";

class LoadError extends Error {}

const page = {
  status: byId("status"),
  run: byId("run"),
  runSelect: byId("run-select"),
  runMeta: byId("run-meta"),
  sparkline: byId("sparkline"),
  versions: byId("versions"),
  botSelect: byId("bot-select"),
  playBoth: byId("play-both"),
  issues: byId("issue-list"),
  patchNotes: byId("patch-notes-body"),
  score: byId("score-body"),
};

const slots = { first: videoSlot("slot-first"), current: videoSlot("slot-current") };

const state = {
  runIds: [],
  runId: null,
  summary: null,
  scores: new Map(),
  version: null,
  bot: DEFAULT_BOT,
  runToken: 0,
  versionToken: 0,
};

start().catch((error) => showStatus(`The viewer could not start: ${error.message}`, true));

// Loading and selection

async function start() {
  page.botSelect.replaceChildren(...BOTS.map((bot) => option(bot, botLabel(bot))));
  page.botSelect.value = state.bot;
  page.runSelect.addEventListener("change", () => report(selectRun(page.runSelect.value)));
  page.botSelect.addEventListener("change", () => setBot(page.botSelect.value));
  page.playBoth.addEventListener("click", togglePlayBoth);
  window.addEventListener("hashchange", () => report(applyHash()));
  document.addEventListener("keydown", stepWithArrowKeys);

  const index = await loadJson("runs/index.json");
  if (index === null) {
    showStatus(
      "No runs to show: runs/index.json was not found. Create a run with iterplay new, then " +
        "start iterplay view, which indexes the runs directory.",
    );
    return;
  }
  if (!isObject(index) || !Array.isArray(index.run_ids)) {
    showStatus("runs/index.json is not a run index: it has no run_ids list.", true);
    return;
  }
  state.runIds = index.run_ids.filter(isRunId).reverse();
  if (state.runIds.length === 0) {
    showStatus("No runs yet. Create one with iterplay new, then restart iterplay view.");
    return;
  }
  page.runSelect.replaceChildren(...state.runIds.map((runId) => option(runId, runId)));
  page.runSelect.disabled = false;
  await applyHash();
}

/** Select what the URL hash names, falling back to the latest run and its best version. */
async function applyHash() {
  if (state.runIds.length === 0) return;
  const wanted = readHash();
  if (BOTS.includes(wanted.bot)) {
    state.bot = wanted.bot;
    page.botSelect.value = wanted.bot;
  }
  const runId = state.runIds.includes(wanted.run) ? wanted.run : state.runIds[0];
  if (runId !== state.runId || state.summary === null) {
    await selectRun(runId, wanted.version);
  } else if (hasVersion(wanted.version) && wanted.version !== state.version) {
    selectVersion(wanted.version);
  } else {
    renderVideos();
    writeHash();
  }
}

async function selectRun(runId, wantedVersion = null) {
  const token = ++state.runToken;
  state.versionToken += 1;
  Object.assign(state, { runId, summary: null, scores: new Map(), version: null });
  page.runSelect.value = runId;
  page.runMeta.textContent = "";
  page.run.hidden = true;
  document.title = `${runId} · iterplay viewer`;
  for (const slot of Object.values(slots)) clearSlot(slot);
  showStatus(`Loading ${runId}…`);
  writeHash(wantedVersion);

  let raw;
  try {
    raw = await loadJson(runPath(runId, "summary.json"));
  } catch (error) {
    if (token === state.runToken) showStatus(error.message, true);
    return;
  }
  if (token !== state.runToken) return;
  if (raw === null) {
    showStatus(`${runId} has no summary.json yet, so no version has finished. Reload later.`);
    return;
  }
  const summary = readSummary(raw);
  if (summary.versions.length === 0) {
    showStatus(`${runId} has no finished versions yet. Reload later.`);
    return;
  }
  state.summary = summary;
  page.status.hidden = true;
  page.run.hidden = false;
  renderRunMeta();
  renderTimeline();
  selectVersion(hasVersion(wantedVersion) ? wantedVersion : defaultVersion(summary));
  void loadScores(token);
}

function selectVersion(version) {
  state.version = version;
  writeHash();
  for (const button of page.versions.querySelectorAll("button.version")) {
    button.setAttribute("aria-pressed", String(button.dataset.version === String(version)));
  }
  renderSparkline();
  renderVideos();
  renderScore();
  void loadVersionDetails(version);
}

async function loadScores(token) {
  const runId = state.runId;
  const versions = state.summary.versions.map((record) => record.version);
  const results = await Promise.allSettled(
    versions.map((version) => loadJson(versionPath(runId, version, "score.json"))),
  );
  if (token !== state.runToken) return;
  versions.forEach((version, index) => {
    const result = results[index];
    state.scores.set(
      version,
      result.status === "fulfilled" ? { card: result.value } : { error: result.reason },
    );
  });
  renderSparkline();
  renderScore();
}

async function loadVersionDetails(version) {
  const token = ++state.versionToken;
  const runId = state.runId;
  page.issues.replaceChildren(message("Loading issues…"));
  page.patchNotes.replaceChildren(message("Loading patch notes…"));
  const [verified, changelog] = await Promise.allSettled([
    loadJson(versionPath(runId, version, "critique.verified.json")),
    loadText(versionPath(runId, version, "changelog.md")),
  ]);
  if (token !== state.versionToken) return;
  const record = findRecord(version);
  page.issues.replaceChildren(...issuesView(record, verified));
  page.patchNotes.replaceChildren(...patchNotesView(record, changelog));
}

function setBot(bot) {
  if (!BOTS.includes(bot)) return;
  state.bot = bot;
  page.botSelect.value = bot;
  if (state.summary === null) return;
  writeHash();
  renderVideos();
}

function readSummary(raw) {
  const versions = asArray(raw?.versions)
    .filter((record) => isObject(record) && Number.isInteger(record.version))
    .sort((a, b) => a.version - b.version);
  return {
    raw: isObject(raw) ? raw : {},
    versions,
    best: Number.isInteger(raw?.best_version) ? raw.best_version : null,
  };
}

function defaultVersion(summary) {
  return findRecord(summary.best) ? summary.best : summary.versions.at(-1).version;
}

function findRecord(version) {
  return state.summary?.versions.find((record) => record.version === version);
}

function hasVersion(version) {
  return version !== null && findRecord(version) !== undefined;
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const version = params.get("v") ?? "";
  return {
    run: params.get("run"),
    version: /^\d{1,9}$/.test(version) ? Number(version) : null,
    bot: params.get("bot"),
  };
}

function writeHash(version = state.version) {
  const params = new URLSearchParams();
  if (state.runId !== null) params.set("run", state.runId);
  if (version !== null) params.set("v", String(version));
  params.set("bot", state.bot);
  const hash = `#${params}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function stepWithArrowKeys(event) {
  const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
  if (step === undefined || state.summary === null) return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.target instanceof Element && event.target.closest("input, select, textarea, video")) {
    return;
  }
  const versions = state.summary.versions.map((record) => record.version);
  const next = versions[versions.indexOf(state.version) + step];
  if (next === undefined) return;
  event.preventDefault();
  selectVersion(next);
  page.versions.querySelector(`button[data-version="${next}"]`)?.focus();
}

// The run header, version timeline and sparkline

function renderRunMeta() {
  const { raw, versions, best } = state.summary;
  const parts = [`${versions.length} ${versions.length === 1 ? "version" : "versions"}`];
  const bestRecord = findRecord(best);
  if (bestRecord !== undefined) parts.push(`best v${best} (${percent(bestRecord.score)})`);
  parts.push(isString(raw.stop_reason) ? `stopped: ${label(raw.stop_reason)}` : "not finished");
  if (Number.isFinite(raw.duration_s) && raw.duration_s > 0) {
    parts.push(formatDuration(raw.duration_s));
  }
  const tokens = tokenCount(raw.token_usage);
  if (tokens > 0) parts.push(`${tokens.toLocaleString("en-US")} tokens`);
  if (Number.isFinite(raw.spend_usd)) parts.push(`$${raw.spend_usd.toFixed(2)}`);
  page.runMeta.textContent = parts.join(" · ");
}

function renderTimeline() {
  const { versions, best } = state.summary;
  const items = versions.map((record) => {
    const outcome = outcomeOf(record);
    const button = el(
      "button",
      {
        className: `version outcome-${outcome}`,
        attrs: { type: "button", "aria-pressed": "false" },
      },
      el("span", { className: "version-label", text: `v${record.version}` }),
      el("span", { className: "version-score", text: percent(record.score) }),
      el("span", { className: "version-outcome", text: label(outcome) }),
    );
    if (record.version === best) {
      button.classList.add("best");
      button.append(el("span", { className: "version-best", text: "★ best" }));
    }
    button.dataset.version = String(record.version);
    button.title = versionDescription(record);
    button.addEventListener("click", () => selectVersion(record.version));
    return el("li", {}, button);
  });
  page.versions.replaceChildren(...items);
}

function versionDescription(record) {
  const parent = Number.isInteger(record.parent_version)
    ? `fixed from v${record.parent_version}`
    : "built from the design doc";
  const parts = [`v${record.version}: ${percent(record.score)}`, label(outcomeOf(record)), parent];
  if (Number.isFinite(record.duration_s)) parts.push(`took ${formatDuration(record.duration_s)}`);
  return parts.join(", ");
}

function renderSparkline() {
  const { versions, best } = state.summary;
  const target = scoreTarget();
  const [left, top, bottom, labelSpace] = [12, 12, 12, 76];
  const width = left + Math.max(160, 56 * (versions.length - 1)) + labelSpace;
  const height = 84;
  const plotRight = width - labelSpace;
  const x = (index) =>
    versions.length === 1
      ? (left + plotRight) / 2
      : left + (index * (plotRight - left)) / (versions.length - 1);
  const y = (score) => top + (1 - Math.min(Math.max(score, 0), 1)) * (height - top - bottom);

  const scored = versions.flatMap((record, index) =>
    Number.isFinite(record.score) ? [{ record, cx: x(index), cy: y(record.score) }] : [],
  );
  const description =
    `Score per version: ${versions.map((r) => `v${r.version} ${percent(r.score)}`).join(", ")}.` +
    (target === null ? "" : ` Target ${percent(target)}.`);
  const svg = svgEl("svg", {
    class: "sparkline",
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: "img",
    "aria-label": description,
  });
  svg.append(
    svgEl("title", {}, description),
    svgEl("line", { class: "guide", x1: left, x2: plotRight, y1: y(0), y2: y(0) }),
    svgEl("line", { class: "guide", x1: left, x2: plotRight, y1: y(1), y2: y(1) }),
  );
  if (target !== null) {
    svg.append(
      svgEl("line", { class: "target", x1: left, x2: plotRight, y1: y(target), y2: y(target) }),
      svgEl(
        "text",
        { class: "target-label", x: plotRight + 12, y: y(target), "dominant-baseline": "middle" },
        `target ${percent(target)}`,
      ),
    );
  }
  svg.append(
    svgEl("polyline", {
      class: "scores",
      points: scored.map(({ cx, cy }) => `${cx},${cy}`).join(" "),
    }),
  );
  for (const { record, cx, cy } of scored) {
    const classes = ["point", `outcome-${outcomeOf(record)}`];
    if (record.version === best) classes.push("best");
    if (record.version === state.version) classes.push("selected");
    const point = svgEl(
      "circle",
      { class: classes.join(" "), cx, cy, r: record.version === state.version ? 6 : 4 },
      svgEl("title", {}, `v${record.version}: ${percent(record.score)}`),
    );
    point.dataset.version = String(record.version);
    point.addEventListener("click", () => selectVersion(record.version));
    svg.append(point);
  }
  page.sparkline.replaceChildren(svg);
}

/** The rubric's target, from the latest score.json that has loaded. */
function scoreTarget() {
  for (const record of [...state.summary.versions].reverse()) {
    const card = state.scores.get(record.version)?.card;
    if (isObject(card) && Number.isFinite(card.target)) return card.target;
  }
  return null;
}

// Playtest videos

function videoSlot(id) {
  const figure = byId(id);
  const slot = {
    figure,
    caption: figure.querySelector("figcaption"),
    video: figure.querySelector("video"),
    placeholder: figure.querySelector(".placeholder"),
    url: null,
    name: "",
    token: 0,
    pendingSeek: null,
  };
  slot.video.addEventListener("loadedmetadata", () => applyPendingSeek(slot));
  slot.video.addEventListener("error", () => {
    if (slot.video.getAttribute("src") !== null) {
      showPlaceholder(slot, `The ${slot.name} video could not be played.`);
    }
  });
  for (const type of ["play", "pause", "ended", "emptied"]) {
    slot.video.addEventListener(type, updatePlayBoth);
  }
  return slot;
}

function renderVideos() {
  showVideo(slots.first, state.summary.versions[0].version);
  showVideo(slots.current, state.version);
}

/** Show ``version``'s recording of the selected bot, or say why there is none. */
function showVideo(slot, version) {
  const bot = state.bot;
  const url = versionPath(state.runId, version, "playtest", `${bot}.webm`);
  const best = version === state.summary.best ? " · best" : "";
  slot.caption.textContent = `v${version} · ${botLabel(bot)}${best}`;
  if (url === slot.url) return;
  clearSlot(slot);
  slot.url = url;
  slot.name = `v${version} ${botLabel(bot)}`;
  const token = slot.token;
  showPlaceholder(slot, "Loading video…");
  exists(url).then(
    (found) => {
      if (token !== slot.token) return;
      if (!found) {
        showPlaceholder(slot, `No ${botLabel(bot)} video for v${version}.`);
        return;
      }
      slot.placeholder.hidden = true;
      slot.placeholder.textContent = "";
      slot.video.hidden = false;
      slot.video.src = url;
    },
    (error) => {
      if (token === slot.token) showPlaceholder(slot, error.message);
    },
  );
}

function clearSlot(slot) {
  slot.token += 1;
  slot.url = null;
  slot.pendingSeek = null;
  slot.video.pause();
  if (slot.video.getAttribute("src") !== null) {
    slot.video.removeAttribute("src");
    slot.video.load();
  }
}

function showPlaceholder(slot, text) {
  slot.video.hidden = true;
  slot.placeholder.hidden = false;
  slot.placeholder.textContent = text;
}

/** Seek the selected version's video to ``seconds``, switching to ``bot``'s run first. */
function seekCurrent(bot, seconds) {
  if (bot !== null && bot !== state.bot) setBot(bot);
  const slot = slots.current;
  slot.pendingSeek = seconds;
  applyPendingSeek(slot);
  slot.figure.scrollIntoView({ block: "nearest" });
}

function applyPendingSeek(slot) {
  const { video } = slot;
  if (slot.pendingSeek === null || video.getAttribute("src") === null) return;
  if (video.readyState < HTMLMediaElement.HAVE_METADATA) return;
  video.currentTime = slot.pendingSeek;
  slot.pendingSeek = null;
}

function playableVideos() {
  return Object.values(slots)
    .map((slot) => slot.video)
    .filter((video) => !video.hidden && video.getAttribute("src") !== null);
}

function togglePlayBoth() {
  const videos = playableVideos();
  if (videos.some((video) => !video.paused)) {
    for (const video of videos) video.pause();
    return;
  }
  for (const video of videos) {
    video.currentTime = 0;
    video.play().catch(updatePlayBoth);
  }
}

function updatePlayBoth() {
  const playing = playableVideos().some((video) => !video.paused);
  page.playBoth.textContent = playing ? "Pause both" : "Play both";
}

// Score breakdown

function renderScore() {
  const version = state.version;
  const entry = state.scores.get(version);
  if (entry === undefined) {
    page.score.replaceChildren(message("Loading score…"));
  } else if (entry.error !== undefined) {
    page.score.replaceChildren(message(entry.error.message, true));
  } else if (entry.card === null) {
    page.score.replaceChildren(message(`No score breakdown: v${version} has no score.json.`));
  } else if (!isObject(entry.card)) {
    page.score.replaceChildren(message(`v${version}'s score.json is not a score card.`, true));
  } else {
    page.score.replaceChildren(...scoreView(entry.card));
  }
}

function scoreView(card) {
  const verdict = card.meets_target === true ? "meets the target" : "is below the target";
  const total = el(
    "p",
    { className: "score-total" },
    `Total ${percent(card.total)} against a target of ${percent(card.target)}: it `,
    el("strong", { text: verdict }),
    ".",
  );
  const header = el(
    "tr",
    {},
    ...["Item", "Source", "Weight", "Score", "Detail"].map((name) =>
      el("th", { text: name, attrs: { scope: "col" } }),
    ),
  );
  const rows = asArray(card.items).filter(isObject).map(scoreRow);
  return [total, el("table", {}, el("thead", {}, header), el("tbody", {}, ...rows))];
}

function scoreRow(item) {
  const score = Number.isFinite(item.score) ? item.score : null;
  let verdict = "partial";
  if (score === null) verdict = "unmeasured";
  else if (score >= 1) verdict = "pass";
  else if (score <= 0) verdict = "fail";
  return el(
    "tr",
    { className: `item-${verdict}` },
    el("td", { text: stringOr(item.item_id, "?") }),
    el("td", { text: stringOr(item.source, "") }),
    el("td", { text: Number.isFinite(item.weight) ? String(item.weight) : "" }),
    el("td", { text: score === null ? "not measured" : percent(score) }),
    el("td", { text: stringOr(item.detail, "") }),
  );
}

// Verified issues

function issuesView(record, settled) {
  if (settled.status === "rejected") return [message(settled.reason.message, true)];
  const verified = settled.value;
  if (verified === null) {
    const why =
      outcomeOf(record) === "build_failed"
        ? `v${record.version} never booted, so it was not critiqued.`
        : `No verified issues: v${record.version} has no critique.verified.json.`;
    return [message(why)];
  }
  if (!isObject(verified)) {
    return [message(`v${record.version}'s critique.verified.json is not a critique.`, true)];
  }
  const nodes = issueGroups(asArray(verified.issues).filter(isObject)).map(issueGroupView);
  const notes = asArray(verified.feel_notes).filter(isString);
  if (notes.length > 0) {
    nodes.push(
      el(
        "section",
        { className: "feel-notes" },
        el("h3", { text: "Feel notes" }),
        el("ul", {}, ...notes.map((note) => el("li", { text: note }))),
      ),
    );
  }
  return nodes;
}

/** Issues by verification status, in ISSUE_GROUPS order, each sorted by severity. */
function issueGroups(issues) {
  const groups = ISSUE_GROUPS.map((group) => ({ ...group, issues: [] }));
  for (const entry of issues) {
    const status = stringOr(entry.status, "unknown");
    let group = groups.find((candidate) => candidate.status === status);
    if (group === undefined) {
      group = { status, title: `Status: ${label(status)}`, hint: "", issues: [] };
      groups.push(group);
    }
    group.issues.push(entry);
  }
  for (const group of groups) {
    group.issues.sort((a, b) => severityRank(a.issue) - severityRank(b.issue));
  }
  return groups;
}

function issueGroupView(group) {
  const section = el(
    "section",
    { className: "issue-group" },
    el(
      "h3",
      {},
      `${group.title} `,
      el("span", { className: "count", text: `(${group.issues.length})` }),
    ),
  );
  section.dataset.status = group.status;
  if (group.hint) section.append(el("p", { className: "hint", text: group.hint }));
  section.append(
    group.issues.length > 0
      ? el("ul", { className: "issues" }, ...group.issues.map(issueView))
      : message("None."),
  );
  return section;
}

function issueView(entry) {
  const issue = isObject(entry.issue) ? entry.issue : {};
  const severity = SEVERITIES.includes(issue.severity) ? issue.severity : "unknown";
  const bot = BOTS.includes(issue.bot) ? issue.bot : null;
  const head = el(
    "div",
    { className: "issue-head" },
    el("span", { className: "issue-id", text: stringOr(issue.id, "?") }),
    el("span", { className: `badge issue-severity severity-${severity}`, text: severity }),
    el("span", {
      className: "badge issue-rubric",
      text: stringOr(issue.rubric_item, "no rubric item"),
      attrs: { title: "Rubric item" },
    }),
  );
  if (bot !== null) head.append(el("span", { className: "badge issue-bot", text: botLabel(bot) }));
  const seconds = timestampSeconds(issue.timestamp);
  if (seconds !== null) head.append(timestampButton(issue.timestamp, bot, seconds));
  head.append(el("span", { className: "issue-origin", text: label(stringOr(entry.origin, "")) }));

  const item = el(
    "li",
    { className: `issue severity-${severity}` },
    head,
    el("p", { className: "issue-observation", text: stringOr(issue.observation, "") }),
    el(
      "p",
      { className: "issue-rationale" },
      el("strong", { text: "Verifier: " }),
      stringOr(entry.rationale, "no rationale given"),
    ),
  );
  for (const [name, value] of [
    ["Suspected cause", issue.suspected_cause],
    ["Fix hint", issue.fix_hint],
  ]) {
    if (isString(value) && value.trim()) {
      item.append(
        el("p", { className: "issue-diagnosis" }, el("strong", { text: `${name}: ` }), value),
      );
    }
  }
  return item;
}

function timestampButton(timestamp, bot, seconds) {
  const button = el("button", {
    className: "issue-timestamp",
    text: timestamp,
    attrs: { type: "button" },
  });
  const whose = bot === null ? "" : `${botLabel(bot)} `;
  button.title = `Seek the v${state.version} ${whose}video to ${timestamp}`;
  button.dataset.seconds = String(seconds);
  button.addEventListener("click", () => seekCurrent(bot, seconds));
  return button;
}

function severityRank(issue) {
  const rank = SEVERITIES.indexOf(issue?.severity);
  return rank === -1 ? SEVERITIES.length : rank;
}

// Patch notes

function patchNotesView(record, settled) {
  if (settled.status === "rejected") return [message(settled.reason.message, true)];
  const markdown = settled.value;
  if (markdown !== null && markdown.trim()) return [renderMarkdown(markdown)];
  if (record.version === 1) {
    return [message("v1 was built from the design doc, so it has no patch notes.")];
  }
  return [
    message(
      markdown === null
        ? `No patch notes: v${record.version} has no changelog.md.`
        : `v${record.version}'s changelog.md is empty.`,
    ),
  ];
}

/**
 * Minimal Markdown as DOM nodes: ATX headings, bullet and numbered lists (nested by
 * indentation), fenced code blocks, horizontal rules, paragraphs, and inline `code` and
 * **bold**. Every piece of text becomes a text node, so markup in the source shows as text.
 */
function renderMarkdown(markdown) {
  const root = el("div", { className: "markdown" });
  let paragraph = null;
  let lists = [];
  let fence = null;

  for (const line of markdown.split(/\r\n|\r|\n/)) {
    if (fence !== null) {
      if (closesFence(line, fence.marker)) fence = null;
      else fence.code.append(`${line}\n`);
      continue;
    }
    const opening = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    const heading = /^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/.exec(line);
    const bullet = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/.exec(line);
    if (opening !== null) {
      [paragraph, lists] = [null, []];
      const code = el("code");
      root.append(el("pre", {}, code));
      fence = { marker: opening[1], code };
    } else if (!line.trim()) {
      paragraph = null;
    } else if (heading !== null) {
      [paragraph, lists] = [null, []];
      const level = Math.min(heading[1].length + 2, 6);
      root.append(el(`h${level}`, {}, ...inline(heading[2])));
    } else if (/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      [paragraph, lists] = [null, []];
      root.append(el("hr"));
    } else if (bullet !== null) {
      paragraph = null;
      addListItem(root, lists, bullet[1].length, bullet[2], bullet[3]);
    } else if (paragraph === null && lists.length > 0 && /^\s/.test(line)) {
      lists.at(-1).node.lastElementChild.append(" ", ...inline(line.trim()));
    } else {
      if (paragraph === null) {
        lists = [];
        paragraph = el("p");
        root.append(paragraph);
      } else {
        paragraph.append(" ");
      }
      paragraph.append(...inline(line.trim()));
    }
  }
  return root;
}

function closesFence(line, marker) {
  const trimmed = line.trim();
  return trimmed.length >= marker.length && trimmed === marker[0].repeat(trimmed.length);
}

/** Append a list item, opening or closing nested lists to match its indentation. */
function addListItem(root, lists, indent, marker, content) {
  const ordered = /\d/.test(marker);
  while (lists.length > 0 && indent < lists.at(-1).indent) lists.pop();
  let current = lists.at(-1);
  let parent = null;
  if (current === undefined) {
    parent = root;
  } else if (indent > current.indent) {
    parent = current.node.lastElementChild;
  } else if (current.ordered !== ordered) {
    lists.pop();
    parent = current.node.parentNode;
  }
  if (parent !== null) {
    const node = el(ordered ? "ol" : "ul");
    if (ordered) node.start = Number.parseInt(marker, 10);
    parent.append(node);
    current = { indent, ordered, node };
    lists.push(current);
  }
  current.node.append(el("li", {}, ...inline(content)));
}

/** Text with `code` spans and **bold** runs, as nodes; everything else stays plain text. */
function inline(text) {
  const nodes = [];
  let last = 0;
  for (const match of text.matchAll(/`([^`]+)`|\*\*([^*]+)\*\*/g)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    nodes.push(
      match[1] !== undefined ? el("code", { text: match[1] }) : el("strong", { text: match[2] }),
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// Fetching artifacts

function runPath(runId, ...segments) {
  return ["runs", runId, ...segments].map(encodeURIComponent).join("/");
}

function versionPath(runId, version, ...segments) {
  return runPath(runId, `v${version}`, ...segments);
}

/** The response for ``path``, or null when the server has no such file (404). */
async function fetchFile(path, init = {}) {
  let response;
  try {
    response = await fetch(path, { cache: "no-store", ...init });
  } catch (error) {
    throw new LoadError(`Could not fetch ${path} (${error.message}). Is iterplay view running?`);
  }
  if (response.status === 404) return null;
  if (!response.ok) throw new LoadError(`Could not fetch ${path}: HTTP ${response.status}.`);
  return response;
}

async function loadText(path) {
  const response = await fetchFile(path);
  return response === null ? null : response.text();
}

async function loadJson(path) {
  const text = await loadText(path);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new LoadError(`${path} is not valid JSON: ${error.message}`);
  }
}

async function exists(path) {
  return (await fetchFile(path, { method: "HEAD" })) !== null;
}

// DOM and formatting helpers

function byId(id) {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`viewer.html has no #${id}`);
  return node;
}

/** A new element. ``text`` and string children become text nodes, never markup. */
function el(tag, { className, text, attrs } = {}, ...children) {
  const node = document.createElement(tag);
  if (className !== undefined) node.className = className;
  if (text !== undefined) node.textContent = text;
  for (const [name, value] of Object.entries(attrs ?? {})) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  node.append(...children);
  return node;
}

function option(value, text) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = text;
  return node;
}

function message(text, isError = false) {
  return el("p", { className: isError ? "message error" : "message", text });
}

function showStatus(text, isError = false) {
  page.status.textContent = text;
  page.status.classList.toggle("error", isError);
  page.status.hidden = false;
}

function report(promise) {
  promise.catch((error) => showStatus(error.message, true));
}

function outcomeOf(record) {
  return OUTCOMES.includes(record?.outcome) ? record.outcome : "unknown";
}

function percent(value) {
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "n/a";
}

function label(value) {
  return String(value).replaceAll("_", " ");
}

function botLabel(bot) {
  return bot.charAt(0).toUpperCase() + bot.slice(1);
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return minutes > 0 ? `${minutes}m ${total % 60}s` : `${total}s`;
}

function timestampSeconds(timestamp) {
  const match = isString(timestamp) ? TIMESTAMP.exec(timestamp) : null;
  return match === null ? null : Number(match[1]) * 60 + Number(match[2]);
}

function tokenCount(usage) {
  if (!isObject(usage)) return 0;
  return [usage.input_tokens, usage.output_tokens]
    .filter(Number.isFinite)
    .reduce((sum, count) => sum + count, 0);
}

function isRunId(value) {
  return isString(value) && RUN_ID.test(value);
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value) {
  return typeof value === "string";
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function stringOr(value, fallback) {
  if (isString(value)) return value;
  return value === null || value === undefined ? fallback : String(value);
}
