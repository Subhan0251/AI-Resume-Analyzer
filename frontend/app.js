"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { file: null, key: "", busy: false, report: null, sample: false };
  const limits = { file: 10 * 1024 * 1024, jd: 20000 };
  const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
  let chosenTheme = null;
  try { chosenTheme = localStorage.getItem("matchpoint-theme"); } catch { /* Storage is optional. */ }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const light = theme === "light";
    $("theme-label").textContent = light ? "Dark mode" : "Light mode";
    $("theme-icon").textContent = light ? "☾" : "☀";
    $("theme-toggle").setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
    $("theme-toggle").setAttribute("aria-pressed", String(light));
  }
  setTheme(["light", "dark"].includes(chosenTheme) ? chosenTheme : themeMedia.matches ? "dark" : "light");
  $("theme-toggle").addEventListener("click", () => {
    chosenTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(chosenTheme);
    try { localStorage.setItem("matchpoint-theme", chosenTheme); } catch { /* Continue without persistence. */ }
  });
  themeMedia.addEventListener("change", (event) => {
    if (!chosenTheme) setTheme(event.matches ? "dark" : "light");
  });

  function announce(message) { $("announcement").textContent = message; }
  function fieldError(kind, message) {
    const error = $(kind === "file" ? "file-error" : "jd-error");
    const input = $(kind === "file" ? "resume-file" : "job-description");
    error.textContent = message;
    error.hidden = !message;
    input.setAttribute("aria-invalid", String(Boolean(message)));
  }
  function hideReport() {
    state.report = null;
    $("results").hidden = true;
    $("request-error").hidden = true;
  }
  function clearFile() {
    state.file = null;
    $("resume-file").value = "";
    $("selected-file").hidden = true;
    $("file-name").textContent = "";
    $("file-size").textContent = "";
  }
  function selectFiles(files) {
    if (state.busy) return;
    hideReport();
    if (!files.length) return;
    clearFile();
    const file = files[0];
    let error = "";
    if (files.length !== 1) error = "Please select one CV at a time.";
    else if (!/\.(pdf|docx)$/i.test(file.name)) error = "Upload a text-based PDF or DOCX file.";
    else if (!file.size) error = "This file is empty. Please choose your CV again.";
    else if (file.size > limits.file) error = "Your CV exceeds 10 MB. Please choose a smaller file.";
    fieldError("file", error);
    if (error) { announce(error); return; }
    state.file = file;
    $("file-name").textContent = file.name;
    $("file-size").textContent = file.size < 1024 * 1024 ? `${Math.max(1, Math.round(file.size / 1024))} KB · Ready to compare` : `${(file.size / 1024 / 1024).toFixed(1)} MB · Ready to compare`;
    $("selected-file").hidden = false;
    announce(`${file.name} selected.`);
  }
  $("resume-file").addEventListener("change", (event) => selectFiles(Array.from(event.target.files)));
  $("remove-file").addEventListener("click", () => {
    if (state.busy) return;
    clearFile(); hideReport(); fieldError("file", ""); $("resume-file").focus();
  });
  ["dragenter", "dragover"].forEach((name) => $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    if (!state.busy) $("dropzone").classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((name) => $("dropzone").addEventListener(name, (event) => {
    event.preventDefault(); $("dropzone").classList.remove("dragging");
  }));
  $("dropzone").addEventListener("drop", (event) => selectFiles(Array.from(event.dataTransfer.files)));
  // Prevent an accidental drop outside the target from navigating away with private input.
  window.addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
  window.addEventListener("drop", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
  $("job-description").addEventListener("input", () => {
    $("jd-count").textContent = `${$("job-description").value.length.toLocaleString()} / 20,000`;
    fieldError("jd", ""); hideReport();
  });

  function openSettings() {
    $("access-key").value = "";
    $("access-status").textContent = state.key ? "An access key is set for this tab. Enter a new key to replace it." : "No access key is set yet.";
    $("access-dialog").showModal();
  }
  $("open-settings").addEventListener("click", openSettings);
  $("close-settings").addEventListener("click", () => $("access-dialog").close());
  $("access-dialog").addEventListener("close", () => { $("access-key").value = ""; });
  $("access-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const key = $("access-key").value.trim();
    if (!key) return;
    state.key = key;
    $("connection-dot").classList.add("connected");
    $("open-settings").setAttribute("aria-label", "Access settings: key set, verified on next analysis");
    $("access-dialog").close();
    announce("Access key set. It will be checked when you run an analysis.");
    $("analyze-button").focus();
  });
  $("clear-key").addEventListener("click", () => {
    state.key = "";
    $("access-key").value = "";
    $("connection-dot").classList.remove("connected");
    $("open-settings").setAttribute("aria-label", "Access settings: no key set");
    $("access-status").textContent = "Access key cleared from this tab.";
  });

  function setBusy(busy) {
    state.busy = busy;
    $("input-fields").disabled = busy;
    for (const id of ["analyze-button", "sample-button", "reset-button", "open-settings"]) $(id).disabled = busy;
    $("progress").hidden = !busy;
    $("analysis-form").setAttribute("aria-busy", String(busy));
    $("analyze-label").textContent = busy ? "Comparing your CV…" : "Analyze my fit";
  }
  function requestError(message) {
    $("request-error").textContent = message;
    $("request-error").hidden = false;
    $("request-error").focus();
  }
  function apiMessage(status, payload) {
    if (status === 401) return "Your access key wasn’t accepted. Open Access settings and enter a valid key.";
    if (status === 429) return "You’ve reached the request limit. Please wait a minute before trying again.";
    if (status >= 500 && status !== 503) return "We couldn’t complete this comparison. Please try again shortly. Your inputs are still here.";
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => {
      const field = item.loc?.includes("job_description") ? "Job description" : item.loc?.includes("file") ? "CV" : "Input";
      return `${field}: ${item.msg || "Please check this field."}`;
    }).join(" ");
    if (status === 413) return "The upload or job description is too large. Use a CV under 10 MB and a JD under 20,000 characters.";
    if (status === 503) return "Analysis is temporarily unavailable. Please try again shortly.";
    return "We couldn’t process this request. Check your CV and job description and try again.";
  }
  $("analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy) return;
    hideReport();
    const jd = $("job-description").value.trim();
    const missingFile = !state.file;
    const invalidJd = !jd || /^(?:string|test|testing|placeholder|asdf|qwerty)[.!\s]*$/i.test(jd) || !/\p{L}/u.test(jd);
    fieldError("file", missingFile ? "Choose your CV to start the comparison." : "");
    fieldError("jd", invalidJd ? "Paste an actual job description with responsibilities, skills, or qualifications." : jd.length > limits.jd ? "Keep the job description under 20,000 characters." : "");
    if (missingFile || invalidJd || jd.length > limits.jd) {
      $(missingFile ? "resume-file" : "job-description").focus();
      announce("Please check the required CV and job description fields."); return;
    }
    if (!state.key) { openSettings(); return; }
    const formData = new FormData();
    formData.append("file", state.file, state.file.name);
    formData.append("job_description", jd);
    const filename = state.file.name;
    setBusy(true);
    $("elapsed").textContent = "0 seconds elapsed";
    const started = Date.now();
    const timer = setInterval(() => { $("elapsed").textContent = `${Math.floor((Date.now() - started) / 1000)} seconds elapsed`; }, 1000);
    try {
      const response = await fetch("/api/v1/analyze", { method: "POST", headers: { "X-API-Key": state.key }, body: formData });
      let payload;
      try { payload = await response.json(); } catch { throw new Error("The server returned an unreadable response. Please try again shortly."); }
      if (!response.ok) throw new Error(apiMessage(response.status, payload));
      if (!payload || payload.status !== "success" || !payload.scorecard || !Array.isArray(payload.gaps)) throw new Error("The comparison response was incomplete. Please try again.");
      renderReport(payload, false, filename);
    } catch (error) {
      requestError(error instanceof TypeError ? "Couldn’t reach the server. Check your connection and try again. Your inputs are still here." : error.message || "Something went wrong. Please try again.");
    } finally {
      clearInterval(timer); setBusy(false);
    }
  });

  // All API-supplied text is rendered as text, never executable HTML.
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function scoreValue(value) { return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null; }
  function renderReport(report, sample, filename) {
    state.report = report; state.sample = sample;
    $("results").hidden = false;
    $("sample-banner").hidden = !sample;
    $("report-eyebrow").textContent = sample ? "SAMPLE COMPARISON" : "YOUR COMPARISON";
    $("report-context").textContent = `${sample ? "Example CV" : filename || report.filename || "Your CV"} · ${sample ? "Example operations coordinator role" : "Compared with your submitted job description"}`;
    const score = scoreValue(report.match_score);
    $("overall-score").textContent = score === null ? "—" : Math.round(score);
    $("score-ring").style.setProperty("--score", score ?? 0);
    $("score-heading").textContent = score === null ? "Score unavailable" : "Required criteria supported";
    $("subscores").replaceChildren();
    for (const importance of ["required", "preferred", "unspecified"]) {
      const summary = report.scorecard?.[importance] || {};
      const card = element("article", null, "card metric-card");
      const top = element("div", null, "metric-top");
      top.append(element("h3", `${importance[0].toUpperCase() + importance.slice(1)} criteria`), element("strong", `${summary.supported || 0}/${summary.total || 0}`));
      card.append(top, element("p", `${summary.partially_supported || 0} partially supported · ${summary.mentioned_only || 0} mentioned only · ${summary.not_evidenced || 0} not evidenced · ${summary.unclear || 0} unclear`));
      $("subscores").append(card);
    }
    renderInsights(report.scorecard);
    const gaps = Array.isArray(report.gaps) ? report.gaps : [];
    $("gap-count").textContent = `${gaps.length} SUGGESTION${gaps.length === 1 ? "" : "S"}`;
    $("gap-list").replaceChildren();
    if (!gaps.length) {
      const card = element("div", null, "card empty-gaps");
      card.append(element("h4", "No criterion gaps identified."), element("p", "Review the score explanations above for context. This does not guarantee a complete fit or a hiring outcome."));
      $("gap-list").append(card);
    }
    for (const [index, gap] of gaps.entries()) {
      // All gaps are present; reveal six at a time without changing the analysis.
      const card = element("article", null, "card gap-card");
      card.dataset.gapIndex = index;
      card.hidden = index >= 6;
      card.append(element("span", gap.importance === "required" ? "Required criterion" : gap.importance === "preferred" ? "Preferred criterion" : "Unspecified importance", "importance"), element("h4", gap.missing_skill || "Opportunity to improve"), element("p", gap.suggestion || "No suggestion was provided."));
      if (gap.evidence_quote) card.append(element("blockquote", `From your CV: “${gap.evidence_quote}”`));
      $("gap-list").append(card);
    }
    updateGapControls();
    renderParsed(report.data?.parsed_fields || {}, report.data?.raw_normalized_text);
    announce(sample ? "Sample report displayed. This is illustrative data." : "Your comparison is ready.");
    $("results-title").focus({ preventScroll: true });
    $("results").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
  }
  function updateGapControls() {
    const remaining = $("gap-list").querySelectorAll("[data-gap-index][hidden]").length;
    $("more-gaps").hidden = remaining === 0;
    $("all-gaps").hidden = remaining === 0;
    $("more-gaps").textContent = `Show ${Math.min(6, remaining)} more`;
    $("all-gaps").textContent = `Show all ${state.report?.gaps?.length || 0} gaps`;
  }
  for (const [id, count] of [["more-gaps", 6], ["all-gaps", Infinity]]) {
    $(id).addEventListener("click", () => {
      [...$("gap-list").querySelectorAll("[data-gap-index][hidden]")].slice(0, count).forEach(card => { card.hidden = false; });
      updateGapControls(); announce("More improvement suggestions are now visible.");
    });
  }
  $("insights-button").addEventListener("click", () => {
    const content = $("insights-content"); content.hidden = !content.hidden;
    $("insights-button").setAttribute("aria-expanded", String(!content.hidden));
    $("insights-button").textContent = content.hidden ? "View insights" : "Hide insights";
  });
  function renderInsights(scorecard) {
    const target = $("insights-content"); target.replaceChildren(); target.hidden = true;
    $("insights-button").setAttribute("aria-expanded", "false");
    $("insights-button").textContent = "View insights";
    target.append(element("p", scorecard?.method || "Criteria coverage"));
    target.append(element("p", "Supported means the CV documents the requirement, not that credentials or performance have been independently verified. Source lines refer to extracted document text, not PDF page numbers."));
    for (const [status, description] of Object.entries(scorecard?.rubric || {})) {
      target.append(element("p", `${status.replaceAll("_", " ")}: ${description}`, "rubric-line"));
    }
    for (const row of scorecard?.criteria || []) {
      const detail = element("details", null, "criterion-insight");
      detail.append(element("summary", `${row.requirement} — ${row.status.replaceAll("_", " ")}`));
      detail.append(element("p", `${row.importance} · ${row.category}`), element("p", row.reason));
      detail.append(element("h4", `JD evidence · line ${row.jd_evidence?.line || "?"}`), element("blockquote", row.jd_evidence?.quote));
      for (const evidence of row.cv_evidence || []) {
        detail.append(element("h4", `CV evidence · line ${evidence.line} · ${evidence.kind}`), element("blockquote", evidence.quote));
      }
      if (!row.cv_evidence?.length) detail.append(element("p", "No verified supporting passage was identified in your CV. This is not proof you lack the capability."));
      for (const clause of row.unresolved_parts || []) detail.append(element("p", `Not established: ${clause}`));
      target.append(detail);
    }
  }
  function renderParsed(fields, rawSource) {
    const target = $("parsed-content"); target.replaceChildren();
    if (typeof rawSource === "string" && rawSource.trim()) {
      target.append(element("h3", "Extracted CV source"), element("p", rawSource));
      return;
    }
    if (!Object.keys(fields).length) { target.append(element("p", "No extracted CV details were returned.")); return; }
    target.append(element("h3", fields.contact?.name || "Your CV"));
    const contact = [fields.contact?.email, fields.contact?.phone].filter(Boolean).join(" · ");
    if (contact) target.append(element("p", contact));
    if (fields.summary) target.append(element("p", fields.summary));
    if (typeof fields.total_years_experience === "number") target.append(element("p", `${fields.total_years_experience} years of estimated experience · ${fields.seniority_level || "Seniority unavailable"}`));
    const skills = element("div", null, "skill-list");
    for (const skill of fields.skills || []) skills.append(element("span", skill, "skill-chip"));
    target.append(skills);
    for (const experience of fields.experience || []) {
      target.append(element("h3", [experience.role, experience.company].filter(Boolean).join(" · ") || "Experience"));
      if (experience.dates) target.append(element("p", experience.dates));
      const bullets = element("ul");
      for (const bullet of experience.bullets || []) bullets.append(element("li", bullet));
      target.append(bullets);
    }
    for (const [key, label] of [["education", "Education"], ["certifications", "Certifications"]]) {
      if (fields[key]) target.append(element("h3", label), element("p", fields[key]));
    }
  }
  $("export-button").addEventListener("click", async () => {
    if (!state.report) return;
    if (!state.key) { openSettings(); return; }
    const button = $("export-button");
    button.disabled = true;
    try {
      const response = await fetch("/api/v1/report/pdf", {
        method: "POST", headers: { "X-API-Key": state.key, "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.report.session_id, sample: state.sample }),
      });
      if (!response.ok) {
        let payload = {}; try { payload = await response.json(); } catch {}
        throw new Error(apiMessage(response.status, payload));
      }
      if (!response.headers.get("content-type")?.includes("application/pdf")) throw new Error("The server did not return a PDF. Please try again.");
      const url = URL.createObjectURL(await response.blob());
      const link = element("a"); link.href = url; link.download = state.sample ? "matchpoint-sample.pdf" : "matchpoint-report.pdf";
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      announce("Your PDF report download has started. It includes all criteria, evidence and suggestions.");
    } catch (error) { requestError(error.message || "PDF download failed. Please try again."); }
    finally { button.disabled = false; }
  });
  $("reset-button").addEventListener("click", () => {
    if (state.busy) return;
    $("analysis-form").reset(); clearFile(); hideReport();
    $("jd-count").textContent = "0 / 20,000";
    fieldError("file", ""); fieldError("jd", "");
    $("resume-file").focus(); announce("Workspace cleared. Choose a CV and a new job description.");
  });
  $("sample-button").addEventListener("click", async () => {
    if (state.busy) return;
    try {
      const response = await fetch("/static/sample-report.json");
      if (!response.ok) throw new Error("Sample report unavailable.");
      renderReport(await response.json(), true);
    } catch { requestError("Sample report unavailable. Please try again."); }
  });
})();
