"use strict";

const byId = id => document.getElementById(id);
const connection = byId("connection");
const signIn = byId("sign-in");
const workspace = byId("workspace");
const review = byId("review");
const form = byId("review-form");
const editMessage = byId("edit-message");
const slug = /^\/projects\/([a-z0-9][a-z0-9-]{0,62})$/.exec(location.pathname)?.[1];
const projectPath = slug ? "/projects/" + slug : "";
const labels = {audit: "Audit findings", preview: "Change preview", board: "Revit issue board", model: "3D model"};
const availability = {ready: "Current", stale: "Stale · Showing the last valid publication",
                      unavailable: "No valid publication available"};
let snapshot;
let selected;
let selectedRevision;
let rows = new Map();
let socket;
let closed = false;
let refreshing = false;
let refreshQueued = false;
let pending;
let saving = false;

function element(tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
}

async function request(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
        const error = new Error(await response.text());
        error.status = response.status;
        throw error;
    }
    return response;
}

function jsonRequest(method, value, headers = {}) {
    return {method, headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(value)};
}

function returnToSignIn() {
    closed = true;
    socket?.close();
    location.assign("/?project=" + encodeURIComponent(slug));
}

function renderSelection() {
    const row = rows.get(selected);
    review.hidden = !row;
    if (!row) return;
    byId("selected-title").textContent = row.title;
    const item = Object.hasOwn(snapshot.annotations, selected) ? snapshot.annotations[selected] : {};
    const status = item.status ?? "open";
    byId("review-state").textContent = "Review status · " + status + " · " + (item.assignee ?? "Unassigned");
    byId("comments").replaceChildren(...(item.comments ?? []).map(comment =>
        element("li", comment.actor + " · " + comment.recorded_at + " · " + comment.text)));
    form.hidden = snapshot.role !== "editor" || row.freshness !== "ready";
    if (!form.hidden && !saving && !pending &&
        (selectedRevision === undefined || form.elements.status.value === form.elements.status.dataset.saved &&
         form.elements.assignee.value === form.elements.assignee.dataset.saved)) {
        form.elements.status.value = status;
        form.elements.status.dataset.saved = status;
        const options = [new Option("Unassigned", ""), ...snapshot.members.map(actor => new Option(actor, actor))];
        form.elements.assignee.replaceChildren(...options);
        form.elements.assignee.value = item.assignee ?? "";
        form.elements.assignee.dataset.saved = form.elements.assignee.value;
        selectedRevision = snapshot.etag;
    }
}

function selectRow(target) {
    if (target === selected || saving) return;
    if ((pending || form.elements.comment.value ||
         form.elements.status.value !== (form.elements.status.dataset.saved ?? "open") ||
         form.elements.assignee.value !== (form.elements.assignee.dataset.saved ?? "")) &&
        !window.confirm("Leave this review item and discard its unsaved edits?")) return;
    pending = undefined;
    selectedRevision = undefined;
    selected = target;
    form.elements.comment.value = "";
    editMessage.textContent = "";
    for (const [id, row] of rows) row.button.setAttribute("aria-pressed", id === target);
    renderSelection();
    byId("selected-title").setAttribute("tabindex", "-1");
    byId("selected-title").focus();
}

function section(title, entries, freshness, summary, notices = []) {
    const wrapper = document.createElement("section");
    wrapper.append(element("h2", title));
    if (summary) wrapper.append(element("p", summary));
    for (const notice of notices) wrapper.append(element("p", notice));
    const list = document.createElement("ul");
    for (const row of entries) {
        const item = document.createElement("li");
        const button = element("button", row.title);
        button.type = "button";
        button.setAttribute("aria-pressed", row.target === selected);
        button.addEventListener("click", () => selectRow(row.target));
        rows.set(row.target, {...row, freshness, button});
        item.append(button);
        for (const detail of row.details) {
            if (detail != null && detail !== "") item.append(element("p", detail));
        }
        list.append(item);
    }
    if (entries.length) wrapper.append(list);
    return wrapper;
}

function render(data) {
    snapshot = data;
    const visible = new Set();
    const focus = [...rows].find(([, row]) => row.button === document.activeElement)?.[0];
    rows = new Map();
    const sections = Object.entries(data.artifacts).map(([name, artifact]) => {
        artifact.rows.forEach(row => visible.add(row.target));
        const wrapper = section(artifact.kind ? labels[artifact.kind] + " · " + name : name, artifact.rows,
                                artifact.freshness, availability[artifact.freshness], artifact.notices);
        if (artifact.viewer_digest) {
            const link = element("a", "Open 3D model");
            link.href = projectPath + "/models/" + encodeURIComponent(name) + "/" + artifact.viewer_digest + ".html";
            wrapper.append(link);
        }
        return wrapper;
    });
    if (data.audit_report_slots.length) {
        const reports = document.createElement("section");
        reports.append(element("h2", "Audit reports"));
        const path = projectPath + "/audit-report";
        const printable = element("a", "Open printable report");
        printable.href = path;
        const download = element("a", "Download HTML");
        download.href = path;
        download.download = slug + "-audit.html";
        reports.append(printable, element("span", " · "), download);
        if (data.audit_report_slots.length > 1) {
            const list = document.createElement("ul");
            for (const slot of data.audit_report_slots) {
                const item = document.createElement("li");
                const link = element("a", slot);
                link.href = path + "?slot=" + encodeURIComponent(slot);
                item.append(link);
                list.append(item);
            }
            reports.append(list);
        }
        sections.push(reports);
    }
    const manual = data.targets.map(target => ({target, title: target, details: []}));
    if (manual.length) sections.push(section("Review items", manual, "ready"));
    data.targets.forEach(target => visible.add(target));
    const history = Object.keys(data.annotations).filter(target => !visible.has(target))
        .map(target => ({target, title: target, details: []}));
    if (history.length) sections.push(section("Review history", history, "retired"));
    byId("artifacts").replaceChildren(...sections);
    if (focus) rows.get(focus)?.button.focus();
    renderSelection();
}

function showConnection() {
    connection.textContent = socket?.readyState === WebSocket.OPEN ?
        "Live updates connected" : "Live updates disconnected. Retrying…";
}

async function refresh() {
    refreshQueued = true;
    if (refreshing || closed) return;
    refreshing = true;
    try {
        while (refreshQueued && !closed) {
            refreshQueued = false;
            const response = await request(projectPath + "/workspace");
            const data = await response.json();
            if (snapshot?.etag !== data.etag) render(data);
            showConnection();
        }
    } catch (error) {
        if (error.status === 401) returnToSignIn();
        else if (error.status === 404) {
            closed = true;
            socket?.close();
            workspace.hidden = true;
            connection.textContent = "Project unavailable.";
        } else {
            connection.textContent = "Could not refresh the project. Retrying…";
            setTimeout(refresh, 3000);
        }
    } finally {
        refreshing = false;
    }
}

function connect() {
    if (closed) return;
    const events = new URL(projectPath + "/events", location.href);
    events.protocol = "wss:";
    socket = new WebSocket(events);
    socket.addEventListener("open", refresh);
    socket.addEventListener("message", refresh);
    socket.addEventListener("close", () => {
        if (closed) return;
        showConnection();
        refresh();
        setTimeout(connect, 3000);
    });
}

signIn.addEventListener("submit", async event => {
    event.preventDefault();
    const token = signIn.elements.token.value;
    const project = signIn.elements.project.value;
    try {
        await request("/session", jsonRequest("POST", {token}));
        location.assign("/projects/" + project);
    } catch (error) {
        connection.textContent = error.status ? error.message : "Sign-in failed. Try again.";
    }
});

byId("sign-out").addEventListener("click", async () => {
    try {
        await request("/session", {method: "DELETE"});
        returnToSignIn();
    } catch {
        connection.textContent = "Sign-out failed. Try again.";
    }
});

form.addEventListener("submit", async event => {
    event.preventDefault();
    if (saving || form.hidden) return;
    const isComment = event.submitter?.value === "comment";
    const patch = isComment ? {comment: form.elements.comment.value} : {};
    for (const field of ["status", "assignee"]) {
        const input = form.elements[field];
        if (!isComment && input.value !== input.dataset.saved) patch[field] = input.value || null;
    }
    if (pending && pending.isComment !== isComment) {
        editMessage.textContent = "Retry the previous save before submitting a different change.";
        return;
    }
    if (!pending && (isComment ? !patch.comment.trim() : !Object.keys(patch).length)) return;
    pending ??= {isComment, etag: isComment ? snapshot.etag : selectedRevision,
                 body: {target: selected, operation_id: crypto.randomUUID(), patch}};
    saving = true;
    const controls = Array.from(form.elements);
    controls.forEach(control => { control.disabled = true; });
    try {
        await request(projectPath + "/review", jsonRequest("PATCH", pending.body, {"If-Match": pending.etag}));
        if (isComment) form.elements.comment.value = "";
        else selectedRevision = undefined;
        pending = undefined;
        editMessage.textContent = "Saved.";
    } catch (error) {
        if (error.status === 401) returnToSignIn();
        else if (error.status && error.status < 500) {
            pending = undefined;
            if (error.status === 412) selectedRevision = undefined;
            editMessage.textContent = error.status === 412 ?
                "The review changed. Check the latest state and submit again." : error.message;
        } else {
            editMessage.textContent = "The save could not be confirmed. Use the same button to retry.";
        }
    } finally {
        saving = false;
        controls.forEach(control => { control.disabled = false; });
        renderSelection();
        await refresh();
        renderSelection();
    }
});

if (slug) {
    byId("heading").textContent = slug;
    byId("sign-out").hidden = false;
    workspace.hidden = false;
    connection.textContent = "Loading project…";
    refresh();
    connect();
} else {
    signIn.hidden = false;
    signIn.elements.project.value = new URLSearchParams(location.search).get("project") ?? "";
}
