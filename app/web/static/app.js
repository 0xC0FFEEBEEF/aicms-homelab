document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }

  if (target.classList.contains("copy-button")) {
    const text = target.dataset.copyText;
    if (!text) {
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
      const original = target.textContent;
      target.textContent = "Copied";
      setTimeout(() => {
        target.textContent = original || "Copy";
      }, 1200);
    } catch (_error) {
      target.textContent = "Copy failed";
    }
  }
});

function formatDateTime(value) {
  if (!value) {
    return "Never";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function renderTagPills(container, tags) {
  if (!(container instanceof HTMLElement)) {
    return;
  }
  container.replaceChildren(
    ...(tags || []).map((tag) => {
      const pill = document.createElement("span");
      pill.className = "pill pill-neutral";
      pill.textContent = tag;
      return pill;
    }),
  );
}

function renderRiskItems(container, items) {
  if (!(container instanceof HTMLElement)) {
    return;
  }
  container.replaceChildren(
    ...((items || []).map((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      return li;
    })),
  );
}

function renderDetailRows(container, rows) {
  if (!(container instanceof HTMLElement)) {
    return;
  }
  container.replaceChildren(
    ...((rows || []).map((row) => {
      const wrapper = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = row.label;
      const dd = document.createElement("dd");
      dd.textContent = row.value ?? "";
      wrapper.appendChild(dt);
      wrapper.appendChild(dd);
      return wrapper;
    })),
  );
}

function renderProfileItems(container, items, formatter) {
  if (!(container instanceof HTMLElement)) {
    return;
  }
  container.replaceChildren(
    ...((items || []).map((item) => {
      const li = document.createElement("li");
      li.textContent = formatter(item);
      return li;
    })),
  );
}

function renderEvent(event) {
  const item = document.createElement("li");
  item.dataset.eventId = String(event.id);

  const pill = document.createElement("span");
  pill.className = `pill pill-${event.level}`;
  pill.textContent = event.level;
  item.appendChild(pill);

  const strong = document.createElement("strong");
  strong.textContent = ` ${event.event_type}`;
  item.appendChild(strong);

  const message = document.createElement("p");
  message.textContent = event.message;
  item.appendChild(message);

  if (event.details && Object.keys(event.details).length > 0) {
    const details = document.createElement("pre");
    details.className = "code-block compact-block";
    details.textContent = JSON.stringify(event.details, null, 2);
    item.appendChild(details);
  }

  const small = document.createElement("small");
  small.textContent = formatDateTime(event.created_at);
  item.appendChild(small);

  return item;
}

function initRangeOutputs() {
  const rangeInputs = document.querySelectorAll("[data-range-output]");
  rangeInputs.forEach((input) => {
    if (!(input instanceof HTMLInputElement)) {
      return;
    }
    const outputId = input.dataset.rangeOutput;
    if (!outputId) {
      return;
    }
    const outputNode = document.getElementById(outputId);
    if (!(outputNode instanceof HTMLElement)) {
      return;
    }
    const update = () => {
      outputNode.textContent = input.value;
    };
    input.addEventListener("input", update);
    update();
  });
}

async function pollScanDetail() {
  const container = document.querySelector("[data-scan-detail]");
  if (!(container instanceof HTMLElement)) {
    return;
  }

  const scanId = container.dataset.scanId;
  if (!scanId) {
    return;
  }

  const statusPill = container.querySelector("[data-scan-status-pill]");
  const hostsNode = container.querySelector("[data-scan-hosts]");
  const projectsNode = container.querySelector("[data-scan-projects]");
  const errorsNode = container.querySelector("[data-scan-errors]");
  const summaryNode = container.querySelector("[data-scan-summary]");
  const finishedNode = container.querySelector("[data-scan-finished]");
  const liveNote = container.querySelector("[data-scan-live-note]");
  const stopForm = container.querySelector("[data-scan-stop-form]");
  let eventsList = container.querySelector("[data-scan-events]");
  let emptyState = container.querySelector("[data-scan-empty]");

  const refresh = async () => {
    const response = await fetch(`/api/scans/${scanId}`, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      return false;
    }
    const payload = await response.json();
    const scan = payload.scan;
    const events = payload.events || [];

    if (statusPill instanceof HTMLElement) {
      statusPill.textContent = scan.status;
      statusPill.className = `pill pill-${scan.status}`;
    }
    if (hostsNode instanceof HTMLElement) {
      hostsNode.textContent = String(scan.hosts_scanned ?? 0);
    }
    if (projectsNode instanceof HTMLElement) {
      projectsNode.textContent = String(scan.projects_found ?? 0);
    }
    if (errorsNode instanceof HTMLElement) {
      errorsNode.textContent = String(scan.errors_count ?? 0);
    }
    if (summaryNode instanceof HTMLElement) {
      summaryNode.textContent = scan.summary || "No summary recorded.";
    }
    if (finishedNode instanceof HTMLElement) {
      finishedNode.textContent = formatDateTime(scan.finished_at);
    }

    if (!(eventsList instanceof HTMLElement)) {
      eventsList = document.createElement("ul");
      eventsList.className = "timeline";
      eventsList.dataset.scanEvents = "true";
      if (emptyState instanceof HTMLElement) {
        emptyState.replaceWith(eventsList);
        emptyState = null;
      } else {
        container.appendChild(eventsList);
      }
    }
    eventsList.replaceChildren(...events.map(renderEvent));

    const isLive = scan.status === "queued" || scan.status === "running" || scan.status === "cancelling";
    if (liveNote instanceof HTMLElement) {
      liveNote.hidden = !isLive;
    }
    if (stopForm instanceof HTMLElement) {
      stopForm.hidden = !isLive;
    }
    return isLive;
  };

  let shouldContinue = await refresh();
  while (shouldContinue) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    shouldContinue = await refresh();
  }
}

pollScanDetail();

async function pollProjectInterpretation() {
  const container = document.querySelector("[data-project-profile]");
  if (!(container instanceof HTMLElement)) {
    return;
  }

  const projectId = container.dataset.projectId;
  if (!projectId) {
    return;
  }

  const statusPill = container.querySelector("[data-profile-status-pill]");
  const outputNode = container.querySelector("[data-profile-live-output]");
  const updatedNode = container.querySelector("[data-profile-updated]");
  const reportNode = container.querySelector("[data-profile-report]");
  const titleNode = container.querySelector("[data-profile-title]");
  const leadNode = container.querySelector("[data-profile-lead]");
  const kindNode = container.querySelector("[data-profile-kind]");
  const platformNode = container.querySelector("[data-profile-platform]");
  const roleNode = container.querySelector("[data-profile-role]");
  const needsReviewNode = container.querySelector("[data-profile-needs-review]");
  const identityPrimaryNode = document.querySelector("[data-identity-primary]");
  const identityConfidenceNode = document.querySelector("[data-identity-confidence]");
  const identityRoleNode = document.querySelector("[data-identity-role]");
  const identityPlatformNode = document.querySelector("[data-identity-platform]");
  const identityTagsNode = document.querySelector("[data-identity-tags]");
  const identityEvidenceNode = document.querySelector("[data-identity-evidence]");
  const identityLocalFactsNode = document.querySelector("[data-identity-local-facts]");
  const identityAlternatesNode = document.querySelector("[data-identity-alternates]");
  const mappingCardTitleNode = document.querySelector("[data-mapping-card-title]");
  const mappingCardSummaryNode = document.querySelector("[data-mapping-card-summary]");
  const mappingCardTagsNode = document.querySelector("[data-mapping-card-tags]");
  const mappingPublishTitleNode = document.querySelector("[data-mapping-publish-title]");
  const mappingPublishSummaryNode = document.querySelector("[data-mapping-publish-summary]");
  const mappingPublishLongNode = document.querySelector("[data-mapping-publish-long]");
  const mappingTagsNode = document.querySelector("[data-mapping-tags]");
  const mappingKindNode = document.querySelector("[data-mapping-kind]");
  const mappingStatusNode = document.querySelector("[data-mapping-status]");
  const mappingReasonsNode = document.querySelector("[data-mapping-reasons]");
  const roleSummaryNode = container.querySelector("[data-profile-role-summary]");
  const modelNode = container.querySelector("[data-profile-model]");
  const kindTextNode = container.querySelector("[data-profile-kind-text]");
  const platformTextNode = container.querySelector("[data-profile-platform-text]");
  const roleTextNode = container.querySelector("[data-profile-role-text]");
  const confidenceNode = container.querySelector("[data-profile-confidence]");
  const generatedAtNode = container.querySelector("[data-profile-generated-at]");
  const hostResourcesNode = container.querySelector("[data-profile-host-resources]");
  const storageNode = container.querySelector("[data-profile-storage]");
  const lxcsNode = container.querySelector("[data-profile-lxcs]");
  const vmsNode = container.querySelector("[data-profile-vms]");
  const roleDetailsNode = container.querySelector("[data-profile-role-details]");
  const setupNode = container.querySelector("[data-profile-setup]");
  const whyNode = container.querySelector("[data-profile-why]");
  const componentsNode = container.querySelector("[data-profile-components]");
  const evidenceNode = container.querySelector("[data-profile-evidence]");
  const topologyNode = container.querySelector("[data-profile-topology]");
  const enrichmentNode = container.querySelector("[data-profile-enrichment]");
  const unknownsNode = container.querySelector("[data-profile-unknowns]");
  const notesNode = container.querySelector("[data-profile-notes]");
  const jsonNode = container.querySelector("[data-profile-json]");
  const jsonWrapNode = container.querySelector("[data-profile-json-wrap]");
  const emptyNode = container.querySelector("[data-profile-empty]");
  const thinkingNode = container.querySelector("[data-profile-thinking]");
  const startForm = document.querySelector("[data-profile-start-form]");
  const stopForm = document.querySelector("[data-profile-stop-form]");

  const refresh = async () => {
    const response = await fetch(`/api/projects/${projectId}/profile/status`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      return false;
    }
    const payload = await response.json();
    const status = payload.status || "idle";
    const latest = payload.latest_profile;
    const identity = payload.identity_result;
    const mappingPreview = payload.mapping_preview;
    const topology = payload.topology || {};
    const enrichmentEvidence = payload.enrichment_evidence || [];

    if (statusPill instanceof HTMLElement) {
      statusPill.textContent = status;
      statusPill.className = `pill pill-${status}`;
    }
    if (outputNode instanceof HTMLElement) {
      outputNode.textContent = payload.output || "Profile generation output will appear here while the refresh is running.";
    }
    if (updatedNode instanceof HTMLElement) {
      updatedNode.textContent = payload.updated_at ? formatDateTime(payload.updated_at) : "";
    }
    const isLive = status === "queued" || status === "running" || status === "cancelling";
    if (thinkingNode instanceof HTMLDetailsElement) {
      thinkingNode.open = isLive;
    }
    if (startForm instanceof HTMLElement) {
      startForm.hidden = isLive;
    }
    if (stopForm instanceof HTMLElement) {
      stopForm.hidden = !isLive;
    }

    if (latest) {
      if (reportNode instanceof HTMLElement) {
        reportNode.hidden = false;
      }
      if (emptyNode instanceof HTMLElement) {
        emptyNode.hidden = true;
      }
      if (jsonWrapNode instanceof HTMLElement) {
        jsonWrapNode.hidden = false;
      }
      if (titleNode instanceof HTMLElement) {
        titleNode.textContent = titleNode.textContent || "Machine profile";
      }
      if (leadNode instanceof HTMLElement) {
        leadNode.textContent = latest.lead_paragraph || "";
      }
      if (kindNode instanceof HTMLElement) {
        kindNode.textContent = latest.machine_kind || "";
      }
      if (platformNode instanceof HTMLElement) {
        platformNode.textContent = latest.detected_platform || "";
      }
      if (roleNode instanceof HTMLElement) {
        roleNode.textContent = latest.detected_role || "";
      }
      if (needsReviewNode instanceof HTMLElement) {
        needsReviewNode.hidden = !((latest.confidence ?? 0) < 0.7);
      }
      if (roleSummaryNode instanceof HTMLElement) {
        roleSummaryNode.textContent = latest.project_role_summary || "";
      }
      if (modelNode instanceof HTMLElement) {
        modelNode.textContent = latest.llm_model_used || "deterministic only";
      }
      if (kindTextNode instanceof HTMLElement) {
        kindTextNode.textContent = latest.machine_kind || "";
      }
      if (platformTextNode instanceof HTMLElement) {
        platformTextNode.textContent = latest.detected_platform || "";
      }
      if (roleTextNode instanceof HTMLElement) {
        roleTextNode.textContent = latest.detected_role || "";
      }
      if (confidenceNode instanceof HTMLElement) {
        confidenceNode.textContent = latest.confidence ?? "";
      }
      if (generatedAtNode instanceof HTMLElement) {
        generatedAtNode.textContent = formatDateTime(latest.last_generated_at);
      }
      renderDetailRows(hostResourcesNode, [
        { label: "CPU", value: `${latest.host_resources_json?.cpu_cores ?? "?"} cores / ${latest.host_resources_json?.cpu_threads ?? "?"} threads` },
        { label: "RAM", value: latest.host_resources_json?.memory_gib ? `${latest.host_resources_json.memory_gib} GiB` : "Unknown" },
        ...(latest.host_resources_json?.root_disk_gib ? [{ label: "Root disk", value: `${latest.host_resources_json.root_disk_gib} GiB total / ${latest.host_resources_json?.root_used_gib ?? "Unknown"} GiB used` }] : []),
        ...(latest.host_resources_json?.os ? [{ label: "OS", value: latest.host_resources_json.os }] : []),
        ...(latest.host_resources_json?.management_ip ? [{ label: "Management IP", value: latest.host_resources_json.management_ip }] : []),
      ]);
      renderProfileItems(storageNode, latest.host_resources_json?.storage || [], (item) => {
        return `${item.storage_name}${item.mount_point ? ` at ${item.mount_point}` : ""}${item.total_gib ? ` · ${item.total_gib} GiB total` : ""}${item.available_gib ? ` · ${item.available_gib} GiB free` : ""}`;
      });
      renderProfileItems(lxcsNode, latest.contained_workloads_json?.lxcs || [], (item) => {
        return `${item.name || `LXC ${item.guest_id}`} · ${item.cores ?? "?"} cores · ${item.ram_mb ?? "?"} MB RAM · ${item.disk_summary || "disk unknown"}${item.role_guess ? ` · ${item.role_guess}` : ""}`;
      });
      renderProfileItems(vmsNode, latest.contained_workloads_json?.vms || [], (item) => {
        return `${item.name || `VM ${item.guest_id}`} · ${item.cores ?? "?"} vCPU · ${item.ram_mb ?? "?"} MB RAM · ${item.disk_summary || "disk unknown"}${item.role_guess ? ` · ${item.role_guess}` : ""}`;
      });
      renderDetailRows(roleDetailsNode, [
        { label: "Primary purpose", value: latest.project_role_json?.primary_purpose || "Unknown" },
        { label: "Environment type", value: latest.project_role_json?.environment_type || "Unknown" },
        { label: "Detected stack", value: latest.project_role_json?.detected_stack || "Unknown" },
      ]);
      renderRiskItems(setupNode, latest.setup_details_json || []);
      renderRiskItems(whyNode, latest.why_it_matters_json || []);
      renderProfileItems(componentsNode, latest.detected_components_json || [], (item) => {
        return `${item.name || "component"}${item.role ? ` · ${item.role}` : ""}${item.path ? ` · ${item.path}` : ""}`;
      });
      renderRiskItems(evidenceNode, latest.evidence_json || []);
      renderRiskItems(topologyNode, [
        ...(topology.parent ? [
          `Hosted on ${topology.parent.name}${topology.parent.platform ? ` · ${topology.parent.platform}` : ""}`,
          ...(topology.parent.guest_type ? [`Guest type: ${String(topology.parent.guest_type).toUpperCase()}`] : []),
          ...(topology.parent.guest_id ? [`Guest ID: ${topology.parent.guest_id}`] : []),
        ] : []),
        ...(!topology.parent && topology.guest_counts ? [
          `Enrolled guests: ${topology.guest_counts.enrolled ?? 0}`,
          `Discovered-only guests: ${topology.guest_counts.discovered ?? 0}`,
          ...((topology.guest_counts.unmanaged ?? 0) ? [`Unmanaged guests: ${topology.guest_counts.unmanaged}`] : []),
        ] : []),
      ]);
      renderProfileItems(enrichmentNode, enrichmentEvidence, (item) => {
        return `${item.source_type ? item.source_type.replace("_", " ") : "enrichment"}${item.title ? ` · ${item.title}` : ""}${item.source_url ? ` · ${item.source_url}` : ""}${item.evidence_text ? ` · ${item.evidence_text}` : ""}`;
      });
      renderRiskItems(unknownsNode, latest.unknowns_json || []);
      renderRiskItems(notesNode, latest.notes_json || []);
      if (jsonNode instanceof HTMLElement) {
        jsonNode.textContent = JSON.stringify(latest, null, 2);
      }
    } else {
      if (reportNode instanceof HTMLElement) {
        reportNode.hidden = true;
      }
      if (emptyNode instanceof HTMLElement) {
        emptyNode.hidden = false;
      }
      if (jsonWrapNode instanceof HTMLElement) {
        jsonWrapNode.hidden = true;
      }
    }

    if (identity) {
      if (identityPrimaryNode instanceof HTMLElement) {
        identityPrimaryNode.textContent = identity.primary_signature_name || "Fallback unknown profile";
      }
      if (identityConfidenceNode instanceof HTMLElement) {
        identityConfidenceNode.textContent = identity.identity_confidence ?? "";
      }
      if (identityRoleNode instanceof HTMLElement) {
        identityRoleNode.textContent = identity.detected_role || "";
      }
      if (identityPlatformNode instanceof HTMLElement) {
        identityPlatformNode.textContent = identity.detected_platform || "";
      }
      if (identityTagsNode instanceof HTMLElement) {
        identityTagsNode.textContent = (identity.stack_tags_json || []).join(", ") || "None";
      }
      renderRiskItems(identityEvidenceNode, identity.identity_evidence_json || []);
      renderRiskItems(identityLocalFactsNode, identity.local_facts_evidence_json || []);
      renderProfileItems(identityAlternatesNode, identity.alternate_matches_json || [], (item) => {
        return `${item.display_name || item.signature_id} · ${item.confidence ?? ""} · ${item.detected_role || ""}`;
      });
    }

    if (mappingPreview) {
      if (mappingCardTitleNode instanceof HTMLElement) {
        mappingCardTitleNode.textContent = mappingPreview.card_title_candidate || mappingPreview.project_title_candidate || "";
      }
      if (mappingCardSummaryNode instanceof HTMLElement) {
        mappingCardSummaryNode.textContent = mappingPreview.card_summary_candidate || mappingPreview.project_summary_candidate || "";
      }
      if (mappingCardTagsNode instanceof HTMLElement) {
        mappingCardTagsNode.textContent = (mappingPreview.card_tech_tags_candidate_json || mappingPreview.tech_tags_candidate_json || []).join(", ") || "None";
      }
      if (mappingPublishTitleNode instanceof HTMLElement) {
        mappingPublishTitleNode.textContent = mappingPreview.publish_title_candidate || mappingPreview.project_title_candidate || "";
      }
      if (mappingPublishSummaryNode instanceof HTMLElement) {
        mappingPublishSummaryNode.textContent =
          mappingPreview.publish_long_description_candidate ||
          mappingPreview.publish_summary_candidate ||
          mappingPreview.project_summary_candidate ||
          "";
      }
      if (mappingPublishLongNode instanceof HTMLElement) {
        const sections = mappingPreview.publish_sections_json || [];
        mappingPublishLongNode.textContent = sections.length ? sections.join("\n") : "Not generated yet.";
      }
      if (mappingTagsNode instanceof HTMLElement) {
        mappingTagsNode.textContent = (mappingPreview.tech_tags_candidate_json || []).join(", ") || "None";
      }
      if (mappingKindNode instanceof HTMLElement) {
        mappingKindNode.textContent = mappingPreview.publishable_kind || "";
      }
      if (mappingStatusNode instanceof HTMLElement) {
        mappingStatusNode.textContent = mappingPreview.recommended_status || "";
      }
      renderRiskItems(mappingReasonsNode, [
        ...(mappingPreview.publishability_reason ? [mappingPreview.publishability_reason] : []),
        ...(mappingPreview.internal_only_reason ? [mappingPreview.internal_only_reason] : []),
      ]);
    }

    return isLive;
  };

  let shouldContinue = await refresh();
  while (shouldContinue) {
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
    shouldContinue = await refresh();
  }
}

pollProjectInterpretation();

async function pollInvestigationDetail() {
  const container = document.querySelector("[data-investigation-detail]");
  if (!(container instanceof HTMLElement)) {
    return;
  }

  const investigationId = container.dataset.investigationId;
  if (!investigationId) {
    return;
  }

  const statusNode = document.querySelector("[data-investigation-status]");
  const phaseNode = document.querySelector("[data-investigation-phase]");
  const updatedNode = container.querySelector("[data-investigation-updated]");
  const errorNode = document.querySelector("[data-investigation-error]");
  const draftStateNode = container.querySelector("[data-investigation-draft-state]");
  const findingsCountNode = document.querySelector("[data-investigation-findings-count]");
  const proposalsCountNode = document.querySelector("[data-investigation-proposals-count]");
  const messagesWrap = container.querySelector("[data-investigation-messages]");
  const findingsWrap = document.querySelector("[data-investigation-findings]");
  const proposalsWrap = document.querySelector("[data-investigation-proposals]");
  const previewWrap = container.querySelector("[data-investigation-working-preview]");
  const stopForm = document.querySelector("[data-investigation-stop-form]");
  const messageForm = container.querySelector("[data-investigation-message-form]");
  const messageInput = container.querySelector("[data-investigation-message-input]");
  const messageSubmit = container.querySelector("[data-investigation-message-submit]");
  const threadShell = container.querySelector("[data-investigation-thread-shell]");
  const statusPill = document.querySelector("[data-investigation-status-pill]");
  const phasePill = document.querySelector("[data-investigation-phase-pill]");
  const confirmationNode = document.querySelector("[data-investigation-confirmation]");
  const confirmationSideNode = document.querySelector("[data-investigation-confirmation-side]");
  const findingsCountSecondaryNode = document.querySelector("[data-investigation-findings-count-secondary]");
  let shouldStickThread = true;
  let fastPollingUntil = Date.now() + 15000;
  let pendingAssistantExpectation = null;
  let lastRenderedMessageSignature = "";
  let thinkingIndicator = null;
  let latestRefreshState = null;

  const renderMessage = (message) => {
    const article = document.createElement("article");
    article.className = `investigation-message investigation-message-${message.sender_type}`;
    article.dataset.messageId = String(message.id);

    const header = document.createElement("div");
    header.className = "investigation-message__meta";

    const strong = document.createElement("strong");
    strong.textContent = message.sender_type === "ai" ? "AI Assistant" : "You";
    const span = document.createElement("span");
    span.textContent = formatDateTime(message.created_at);
    header.appendChild(strong);
    header.appendChild(span);
    article.appendChild(header);

    const body = document.createElement("div");
    body.className = "investigation-message-content";
    body.textContent = message.content;
    article.appendChild(body);

    if (message.metadata?.ai_rationale) {
      const rationale = document.createElement("p");
      rationale.className = "subtle investigation-rationale";
      rationale.innerHTML = `<strong>Why this matters:</strong> ${message.metadata.ai_rationale}`;
      article.appendChild(rationale);
    }
    return article;
  };

  const buildDraftList = (items) => {
    const ul = document.createElement("ul");
    ul.className = "draft-preview-list";
    items.forEach((text) => {
      const li = document.createElement("li");
      if (typeof text === "string" && text.endsWith(":")) {
        const strong = document.createElement("strong");
        strong.textContent = text;
        li.appendChild(strong);
      } else {
        li.textContent = text;
      }
      ul.appendChild(li);
    });
    return ul;
  };

  const buildDraftField = (heading, contentFn) => {
    const article = document.createElement("article");
    article.className = "draft-preview-field";
    const h4 = document.createElement("h4");
    h4.textContent = heading;
    article.appendChild(h4);
    contentFn(article);
    return article;
  };

  const buildDraftSection = (heading, contentFn) => {
    const section = document.createElement("section");
    section.className = "draft-preview-section";
    const h4 = document.createElement("h4");
    h4.className = "draft-preview-section__heading";
    h4.textContent = heading;
    section.appendChild(h4);
    contentFn(section);
    return section;
  };

  const renderWorkingPreview = (preview) => {
    if (!(previewWrap instanceof HTMLElement)) {
      return;
    }
    if (!preview) {
      const empty = document.createElement("div");
      empty.className = "draft-preview-empty";
      const title = document.createElement("h4");
      title.textContent = "Draft pending";
      title.dataset.investigationDraftTitle = "true";
      const body = document.createElement("p");
      body.className = "subtle";
      body.dataset.investigationDraftText = "true";
      body.textContent = "The assistant will draft here after the first pass.";
      empty.appendChild(title);
      empty.appendChild(body);
      previewWrap.replaceChildren(empty);
      return;
    }

    const detail = preview.detail || {};
    const fragment = document.createDocumentFragment();

    const hero = document.createElement("section");
    hero.className = "draft-preview-hero";
    const heroTitle = document.createElement("h3");
    heroTitle.dataset.investigationDraftTitle = "true";
    heroTitle.textContent = detail.title || preview.title || "Draft pending";
    hero.appendChild(heroTitle);
    if (detail.short_summary) {
      const subtitle = document.createElement("p");
      subtitle.className = "subtle draft-preview-subtitle";
      subtitle.textContent = detail.short_summary;
      hero.appendChild(subtitle);
    }
    if (detail.tech?.length) {
      const tags = document.createElement("div");
      tags.className = "tag-row draft-preview-tags";
      detail.tech.forEach((t) => {
        const pill = document.createElement("span");
        pill.className = "pill pill-neutral";
        pill.textContent = t;
        tags.appendChild(pill);
      });
      hero.appendChild(tags);
    }
    fragment.appendChild(hero);

    const longDesc = detail.long_description || preview.publish?.long_description;
    if (longDesc) {
      fragment.appendChild(
        buildDraftSection("Overview", (sec) => {
          const p = document.createElement("p");
          p.className = "draft-preview-long-copy";
          p.dataset.investigationDraftText = "true";
          p.textContent = longDesc;
          sec.appendChild(p);
        }),
      );
    }

    if (detail.detail_sections?.length) {
      fragment.appendChild(
        buildDraftSection("Build details", (sec) => {
          sec.appendChild(buildDraftList(detail.detail_sections));
        }),
      );
    }

    const fieldGrid = document.createElement("section");
    fieldGrid.className = "draft-preview-field-grid";
    let hasFields = false;

    if (detail.role_in_lab) {
      hasFields = true;
      fieldGrid.appendChild(
        buildDraftField("Role in my lab", (el) => {
          const p = document.createElement("p");
          p.textContent = detail.role_in_lab;
          el.appendChild(p);
        }),
      );
    }
    if (detail.host_resources?.length) {
      hasFields = true;
      fieldGrid.appendChild(
        buildDraftField("Host resources", (el) => {
          el.appendChild(buildDraftList(detail.host_resources));
        }),
      );
    }
    if (detail.setup_summary?.length) {
      hasFields = true;
      fieldGrid.appendChild(
        buildDraftField("How it is set up", (el) => {
          el.appendChild(buildDraftList(detail.setup_summary));
        }),
      );
    }
    if (detail.related_hosts?.length) {
      hasFields = true;
      fieldGrid.appendChild(
        buildDraftField("Related hosts", (el) => {
          el.appendChild(
            buildDraftList(
              detail.related_hosts.map((h) => (h.address ? `${h.name} — ${h.address}` : h.name)),
            ),
          );
        }),
      );
    }
    if (detail.notes_or_highlights?.length) {
      hasFields = true;
      fieldGrid.appendChild(
        buildDraftField("Notes / highlights", (el) => {
          el.appendChild(buildDraftList(detail.notes_or_highlights));
        }),
      );
    }
    if (hasFields) {
      fragment.appendChild(fieldGrid);
    }

    if (detail.hosted_workload_summary?.length) {
      fragment.appendChild(
        buildDraftSection("Hosted workloads", (sec) => {
          sec.appendChild(buildDraftList(detail.hosted_workload_summary));
        }),
      );
    }

    const formatGuest = (g) => {
      let text = g.guest_name || "Unknown";
      if (g.guest_id) text += ` (#${g.guest_id})`;
      if (g.status) text += ` — ${g.status}`;
      if (g.role) text += ` — ${g.role}`;
      return text;
    };

    if (detail.hosted_lxcs?.length) {
      fragment.appendChild(
        buildDraftSection("LXC inventory", (sec) => {
          sec.appendChild(buildDraftList(detail.hosted_lxcs.map(formatGuest)));
        }),
      );
    }
    if (detail.hosted_vms?.length) {
      fragment.appendChild(
        buildDraftSection("VM inventory", (sec) => {
          sec.appendChild(buildDraftList(detail.hosted_vms.map(formatGuest)));
        }),
      );
    }

    if (detail.custom_sections?.length) {
      detail.custom_sections.forEach((sec) => {
        if (sec.heading && sec.items?.length) {
          fragment.appendChild(
            buildDraftSection(sec.heading, (el) => {
              el.appendChild(buildDraftList(sec.items));
            }),
          );
        }
      });
    }

    previewWrap.replaceChildren(fragment);
  };

  const renderFinding = (finding) => {
    const article = document.createElement("article");
    article.className = "notes-block compact";
    const header = document.createElement("div");
    header.className = "card-header";
    const title = document.createElement("h4");
    title.textContent = finding.title;
    const meta = document.createElement("span");
    meta.textContent = `${(finding.confidence ?? 0).toFixed(2)} · ${finding.source_type}`;
    header.appendChild(title);
    header.appendChild(meta);
    article.appendChild(header);
    const p = document.createElement("p");
    p.textContent = finding.summary;
    article.appendChild(p);
    if (finding.details && Object.keys(finding.details).length > 0) {
      const pre = document.createElement("pre");
      pre.className = "code-block compact-block";
      pre.textContent = JSON.stringify(finding.details, null, 2);
      article.appendChild(pre);
    }
    return article;
  };

  const renderProposals = (proposals) => {
    if (!(proposalsWrap instanceof HTMLElement)) {
      return;
    }
    if (!proposals.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state compact";
      empty.innerHTML = "<h3>No proposals yet</h3><p>The investigation will populate the proposed host and project changes after it collects and drafts.</p>";
      proposalsWrap.replaceChildren(empty);
      return;
    }
    const tableWrap = document.createElement("div");
    tableWrap.className = "table-wrap";
    const table = document.createElement("table");
    table.className = "data-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>Type</th>
          <th>Target</th>
          <th>Field</th>
          <th>Old value</th>
          <th>New value</th>
          <th>Rationale</th>
          <th>Confidence</th>
        </tr>
      </thead>
    `;
    const tbody = document.createElement("tbody");
    proposals.forEach((proposal) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${proposal.proposal_type}</td>
        <td>${proposal.target_kind}${proposal.target_id ? ` #${proposal.target_id}` : ""}</td>
        <td>${proposal.field_name}</td>
        <td><pre class="compact-inline">${JSON.stringify(proposal.old_value ?? "—", null, 2)}</pre></td>
        <td><pre class="compact-inline">${JSON.stringify(proposal.new_value ?? "—", null, 2)}</pre></td>
        <td>${proposal.rationale || "—"}</td>
        <td>${(proposal.confidence ?? 0).toFixed(2)}</td>
      `;
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    proposalsWrap.replaceChildren(tableWrap);
  };

  const refresh = async () => {
    const response = await fetch(`/api/investigations/${investigationId}/status?_=${Date.now()}`, { headers: { Accept: "application/json", "Cache-Control": "no-cache" } });
    if (!response.ok) {
      return { isLive: false };
    }
    const payload = await response.json();
    const investigation = payload.session || {};
    const messages = payload.messages || [];
    const findings = payload.findings || [];
    const proposals = payload.proposals || [];
    const draft = investigation.draft_report_json || {};
    const runtime = payload.runtime || {};
    const workingPreview = payload.working_preview || null;
    if (threadShell instanceof HTMLElement) {
      const distanceFromBottom = threadShell.scrollHeight - threadShell.scrollTop - threadShell.clientHeight;
      shouldStickThread = distanceFromBottom < 120;
    }

    if (statusNode instanceof HTMLElement) {
      statusNode.textContent = investigation.status || "";
    }
    if (statusPill instanceof HTMLElement) {
      statusPill.textContent = investigation.status || "";
      statusPill.className = `pill pill-${investigation.status || "neutral"}`;
    }
    if (phaseNode instanceof HTMLElement) {
      phaseNode.textContent = investigation.current_phase || "";
    }
    if (phasePill instanceof HTMLElement) {
      phasePill.textContent = investigation.current_phase || "";
    }
    if (confirmationNode instanceof HTMLElement) {
      confirmationNode.textContent = investigation.confirmation_state || "";
    }
    if (confirmationSideNode instanceof HTMLElement) {
      confirmationSideNode.textContent = investigation.confirmation_state || "";
    }
    if (updatedNode instanceof HTMLElement) {
      updatedNode.textContent = formatDateTime(investigation.updated_at);
    }
    if (errorNode instanceof HTMLElement) {
      errorNode.textContent = investigation.last_error || "None";
    }
    renderWorkingPreview(workingPreview);
    if (draftStateNode instanceof HTMLElement) {
      const visibleSource = draft.visible_draft_source;
      const isLive = ["queued", "running", "drafting", "applying", "cancelling"].includes(investigation.status || "");
      if (visibleSource === "working") {
        draftStateNode.textContent = "working";
      } else if (visibleSource === "assistant" && isLive) {
        draftStateNode.textContent = "last AI draft shown while updating";
      } else if (visibleSource === "assistant") {
        draftStateNode.textContent = "assistant draft";
      } else if (visibleSource === "base") {
        draftStateNode.textContent = "base draft";
      } else {
        draftStateNode.textContent = "no draft yet";
      }
    }
    if (findingsCountNode instanceof HTMLElement) {
      findingsCountNode.textContent = `${findings.length} items`;
    }
    if (findingsCountSecondaryNode instanceof HTMLElement) {
      findingsCountSecondaryNode.textContent = `${findings.length} items`;
    }
    if (proposalsCountNode instanceof HTMLElement) {
      proposalsCountNode.textContent = `${proposals.length} pending`;
    }
    if (messagesWrap instanceof HTMLElement) {
      const signature = messages.map((m) => `${m.id}:${m.content.length}`).join(",");
      if (signature !== lastRenderedMessageSignature) {
        lastRenderedMessageSignature = signature;
        const rendered = messages.map(renderMessage);
        if (pendingAssistantExpectation) {
          if (!thinkingIndicator || !thinkingIndicator.parentNode) {
            thinkingIndicator = createThinkingIndicator();
          }
          rendered.push(thinkingIndicator);
        } else if (thinkingIndicator) {
          thinkingIndicator = null;
        }
        messagesWrap.replaceChildren(...rendered);
        if (threadShell instanceof HTMLElement && shouldStickThread) {
          requestAnimationFrame(() => {
            threadShell.scrollTop = threadShell.scrollHeight;
          });
        }
      }
    }
    if (findingsWrap instanceof HTMLElement) {
      if (findings.length) {
        findingsWrap.replaceChildren(...findings.map(renderFinding));
      } else {
        const empty = document.createElement("div");
        empty.className = "empty-state compact";
        empty.innerHTML = "<h3>No findings yet</h3><p>The assistant will populate findings after the first collection pass.</p>";
        findingsWrap.replaceChildren(empty);
      }
    }
    renderProposals(proposals);

    const isLive = ["queued", "running", "drafting", "applying", "cancelling"].includes(investigation.status || "");
    if (stopForm instanceof HTMLElement) {
      stopForm.hidden = !isLive;
    }
    const assistantMessages = messages.filter((message) => message.sender_type === "ai");
    const lastAssistantMessageId = assistantMessages.length ? assistantMessages[assistantMessages.length - 1].id : null;
    const state = {
      isLive,
      status: investigation.status || "",
      updatedAt: investigation.updated_at || "",
      lastAssistantMessageId,
      assistantDraftVersion: Number(draft.assistant_draft_version || 0),
      assistantDraftUpdatedAt: draft.assistant_draft_updated_at || "",
      visibleDraftSource: draft.visible_draft_source || "",
      hasPendingFollowUp: Boolean(runtime.has_pending_follow_up),
    };
    latestRefreshState = state;
    return state;
  };

  const renderOptimisticMessage = (content) => {
    const article = document.createElement("article");
    article.className = "investigation-message investigation-message-user";
    article.dataset.optimistic = "true";
    const header = document.createElement("div");
    header.className = "investigation-message__meta";
    const strong = document.createElement("strong");
    strong.textContent = "You";
    const span = document.createElement("span");
    span.textContent = "sending\u2026";
    header.appendChild(strong);
    header.appendChild(span);
    article.appendChild(header);
    const body = document.createElement("div");
    body.className = "investigation-message-content";
    body.textContent = content;
    article.appendChild(body);
    return article;
  };

  const createThinkingIndicator = () => {
    const article = document.createElement("article");
    article.className = "investigation-message investigation-message-ai";
    article.dataset.thinking = "true";
    const header = document.createElement("div");
    header.className = "investigation-message__meta";
    const strong = document.createElement("strong");
    strong.textContent = "AI";
    const span = document.createElement("span");
    span.textContent = "thinking\u2026";
    header.appendChild(strong);
    header.appendChild(span);
    article.appendChild(header);
    const body = document.createElement("div");
    body.className = "investigation-message-content";
    body.textContent = "Working on a response\u2026";
    body.style.opacity = "0.55";
    article.appendChild(body);
    return article;
  };

  if (messageForm instanceof HTMLFormElement && !messageForm.dataset.asyncBound) {
    messageForm.dataset.asyncBound = "true";
    messageForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!(messageInput instanceof HTMLTextAreaElement)) {
        return;
      }
      const content = messageInput.value.trim();
      if (!content) {
        messageInput.focus();
        return;
      }

      const previousScrollY = window.scrollY;
      try {
        if (messageSubmit instanceof HTMLButtonElement) {
          messageSubmit.disabled = true;
          messageSubmit.textContent = "Sending\u2026";
        }
        messageInput.value = "";
        shouldStickThread = true;

        if (messagesWrap instanceof HTMLElement) {
          messagesWrap.appendChild(renderOptimisticMessage(content));
          thinkingIndicator = createThinkingIndicator();
          messagesWrap.appendChild(thinkingIndicator);
          if (threadShell instanceof HTMLElement) {
            requestAnimationFrame(() => { threadShell.scrollTop = threadShell.scrollHeight; });
          }
        }

        const snapshotState = latestRefreshState || {};
        const previousMessageCount = messagesWrap instanceof HTMLElement ? messagesWrap.children.length : 0;

        const formData = new URLSearchParams();
        formData.set("content", content);
        const response = await fetch(messageForm.action, {
          method: "POST",
          body: formData,
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
          },
          credentials: "same-origin",
        });
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }

        pendingAssistantExpectation = {
          lastAssistantMessageId: snapshotState.lastAssistantMessageId || null,
          assistantDraftVersion: snapshotState.assistantDraftVersion || 0,
          assistantDraftUpdatedAt: snapshotState.assistantDraftUpdatedAt || "",
          previousMessageCount,
        };
        fastPollingUntil = Date.now() + 30000;
        await refresh();
      } catch (_error) {
        messagesWrap?.querySelectorAll("[data-optimistic]").forEach((el) => el.remove());
        if (thinkingIndicator) { thinkingIndicator.remove(); thinkingIndicator = null; }
        messageInput.value = content;
        messageForm.submit();
        return;
      } finally {
        if (messageSubmit instanceof HTMLButtonElement) {
          messageSubmit.disabled = false;
          messageSubmit.textContent = "Send";
        }
        window.scrollTo({ top: previousScrollY, behavior: "auto" });
      }
    });
  }

  let currentState = await refresh();
  while (container.isConnected) {
    if (pendingAssistantExpectation) {
      const assistantMessageChanged =
        currentState.lastAssistantMessageId !== null
        && currentState.lastAssistantMessageId !== pendingAssistantExpectation.lastAssistantMessageId;
      const draftChanged =
        currentState.assistantDraftVersion > pendingAssistantExpectation.assistantDraftVersion
        || (
          currentState.assistantDraftUpdatedAt
          && currentState.assistantDraftUpdatedAt !== pendingAssistantExpectation.assistantDraftUpdatedAt
        );
      const stableStateReached = !currentState.isLive && !currentState.hasPendingFollowUp && currentState.status !== "queued";
      const currentMessageCount = messagesWrap instanceof HTMLElement ? messagesWrap.children.length : 0;
      if (
        assistantMessageChanged
        || draftChanged
        || (currentMessageCount > pendingAssistantExpectation.previousMessageCount && stableStateReached)
        || stableStateReached
      ) {
        pendingAssistantExpectation = null;
        if (thinkingIndicator && thinkingIndicator.parentNode) {
          thinkingIndicator.remove();
          thinkingIndicator = null;
        }
        lastRenderedMessageSignature = "";
      }
    }
    const fastPoll = currentState.isLive || pendingAssistantExpectation !== null || Date.now() < fastPollingUntil;
    await new Promise((resolve) => window.setTimeout(resolve, fastPoll ? 1200 : 5000));
    currentState = await refresh();
  }
}

pollInvestigationDetail();
initRangeOutputs();

(function initDebugPanel() {
  const btn = document.querySelector("[data-debug-btn]");
  const overlay = document.getElementById("debug-overlay");
  const content = document.getElementById("debug-content");
  const copyBtn = overlay?.querySelector("[data-debug-copy]");
  const closeBtn = overlay?.querySelector("[data-debug-close]");
  if (!btn || !overlay || !content) return;

  async function gatherDebugLog() {
    const lines = [];
    lines.push(`=== AICMS Debug Log ===`);
    lines.push(`Timestamp: ${new Date().toISOString()}`);
    lines.push(`URL: ${location.href}`);
    lines.push(`User-Agent: ${navigator.userAgent}`);
    lines.push(`Viewport: ${innerWidth}x${innerHeight}`);
    lines.push("");

    const investigationIdEl = document.querySelector("[data-investigation-id]");
    if (investigationIdEl) {
      const id = investigationIdEl.dataset.investigationId;
      lines.push(`--- Investigation #${id} ---`);
      try {
        const res = await fetch(`/api/investigations/${id}/debug?_=${Date.now()}`);
        if (res.ok) {
          const data = await res.json();
          for (const [key, value] of Object.entries(data)) {
            if (key === "recent_messages") {
              lines.push(`\nrecent_messages (${value.length}):`);
              for (const m of value) {
                lines.push(`  [${m.sender}/${m.type}] id=${m.id} hidden=${m.hidden} at=${m.created_at}`);
                if (m.content) lines.push(`    ${m.content.slice(0, 120)}${m.content.length > 120 ? "..." : ""}`);
              }
            } else if (key === "runtime" && typeof value === "object" && value !== null) {
              lines.push(`\nruntime:`);
              for (const [rk, rv] of Object.entries(value)) {
                lines.push(`  ${rk}: ${JSON.stringify(rv)}`);
              }
            } else {
              lines.push(`${key}: ${JSON.stringify(value)}`);
            }
          }
        } else {
          lines.push(`(debug API returned ${res.status})`);
        }
      } catch (e) {
        lines.push(`(debug API error: ${e.message})`);
      }
    } else {
      lines.push("(not on an investigation detail page)");
    }

    lines.push("");
    lines.push(`--- JS State ---`);
    lines.push(`document.readyState: ${document.readyState}`);
    lines.push(`performance.now: ${Math.round(performance.now())}ms`);
    const entries = performance.getEntriesByType("navigation");
    if (entries.length) {
      const nav = entries[0];
      lines.push(`page load: domContentLoaded=${Math.round(nav.domContentLoadedEventEnd)}ms total=${Math.round(nav.loadEventEnd)}ms`);
    }
    return lines.join("\n");
  }

  btn.addEventListener("click", async () => {
    content.textContent = "Loading...";
    overlay.hidden = false;
    content.textContent = await gatherDebugLog();
  });

  closeBtn?.addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });

  copyBtn?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(content.textContent || "");
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
    } catch {
      copyBtn.textContent = "Failed";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.hidden) overlay.hidden = true;
  });
})();
