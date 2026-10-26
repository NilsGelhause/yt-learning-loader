/* tab-structure.js — interactive map of the app's own code & data */
"use strict";

(function () {
  let root = null;            // api tree
  let selected = null;        // selected node
  let filter = "";
  const expanded = new Set(); // node paths currently expanded

  /* icons (inline svg) */
  const ICON_DIR = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M1.5 3.5a1 1 0 0 1 1-1h3l1.5 2h6.5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-8z" fill="#f5b954" fill-opacity="0.9"/></svg>`;
  const ICON_FILE = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 1.5h5L12.5 5v9a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5z" stroke="#8b93a7" stroke-width="1.2"/><path d="M9 1.5V5h3.5" stroke="#8b93a7" stroke-width="1.2"/></svg>`;

  function render(screen) {
    filter = "";
    selected = null;
    const grid = el("div", { class: "structure-grid screen-scroll" });

    const filterInput = el("input", {
      placeholder: "Filter by name…",
      oninput: (e) => { filter = e.target.value.trim().toLowerCase(); renderTree(); },
    });
    const treePane = el("div", { class: "pane" },
      el("div", { class: "pane-header" },
        el("span", null, "Files & folders ", el("span", { class: "muted small", id: "struct-count" })),
        el("span", { class: "muted small" }, "Read-only map")),
      el("div", { style: "padding:10px;border-bottom:1px solid var(--border)" }, filterInput),
      el("div", { class: "pane-body scroll", id: "struct-tree", style: "padding:6px 4px" }, skeletonLines(10)),
    );
    const detailPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Detail"),
      el("div", { class: "pane-body scroll", id: "struct-detail", style: "flex:1" },
        el("div", { class: "empty-state" },
          el("span", { class: "icon" }, "🗺"),
          "This tab maps how the app itself is built — click any file or folder.")),
    );

    grid.append(treePane, detailPane);
    screen.appendChild(grid);
    load();
  }

  async function load() {
    const box = document.getElementById("struct-tree");
    const data = await api("/api/app-structure").catch(() => null);
    if (!box || !box.isConnected) return;
    if (!data) { clear(box).appendChild(emptyState("Could not load the app structure.", "✕")); return; }
    root = data;
    /* default expansion: root + first level; data/ stays collapsed */
    expanded.clear();
    expandToDepth(root, 1);
    renderTree();
  }

  function expandToDepth(node, depth) {
    if (depth < 0 || node.type !== "dir") return;
    expanded.add(node.path);
    (node.children || []).forEach(c => expandToDepth(c, depth - 1));
  }

  /* ---------- filtering ---------- */
  /* returns true if node matches or has a matching descendant; collects ancestors */
  function matchNode(node, f) {
    if (!f) return true;
    if (node.name.toLowerCase().includes(f)) return true;
    return (node.children || []).some(c => matchNode(c, f));
  }

  function visibleCount(node) {
    if (!filter) return countAll(node);
    if (!matchNode(node, filter)) return 0;
    if (node.type === "file") return node.name.toLowerCase().includes(filter) ? 1 : 0;
    return 1 + (node.children || []).filter(c => matchNode(c, filter))
      .reduce((n, c) => n + visibleCount(c), 0);
  }

  function countAll(node) {
    if (node.type === "file") return 1;
    return 1 + (node.children || []).reduce((n, c) => n + countAll(c), 0);
  }

  /* ---------- tree rendering ---------- */
  function renderTree() {
    const box = document.getElementById("struct-tree");
    if (!box || !root) return;
    clear(box);
    const rows = [];
    buildRows(root, 0, rows);
    rows.forEach(r => box.appendChild(r));
    const cnt = document.getElementById("struct-count");
    if (cnt) cnt.textContent = `(${visibleCount(root)} visible)`;
  }

  function buildRows(node, depth, rows) {
    if (filter && !matchNode(node, filter)) return;
    const isDir = node.type === "dir";
    const hasKids = isDir && ((node.children && node.children.length) || node.child_count);
    /* auto-expand to matches when filtering */
    const isOpen = filter ? hasKids : expanded.has(node.path);
    const row = el("div", {
      class: `tree-row${selected === node.path ? " selected" : ""}`,
      style: `--depth:${depth}`,
      dataset: { path: node.path },
      role: "treeitem",
      tabindex: "0",
      "aria-label": node.name,
      onclick: () => { selectNode(node); },
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectNode(node); } },
    },
      el("span", {
        class: `tree-chevron${hasKids ? "" : " leaf"}`,
        onclick: (e) => {
          if (!hasKids) return;
          e.stopPropagation();
          toggle(node);
        },
      }, hasKids ? (isOpen ? "▾" : "▸") : ""),
      el("span", { class: "tree-icon", html: isDir ? ICON_DIR : ICON_FILE }),
      el("span", { class: "tree-name" }, node.name),
      el("span", { class: "tree-meta" },
        isDir
          ? (node.child_count != null ? `${node.child_count}` : `${(node.children || []).length}`)
          : (node.lines != null ? `${node.lines}` : "")),
    );
    rows.push(row);
    if (isDir && isOpen) {
      (node.children || []).forEach(c => buildRows(c, depth + 1, rows));
      if (node.child_count != null) {
        rows.push(el("div", { class: "tree-row tree-more", style: `--depth:${depth + 1}` },
          el("span", { class: "tree-chevron leaf" }),
          el("span", { class: "muted small", style: "padding-left:20px" },
            `… ${node.child_count} items — filter to narrow`)));
      }
    }
  }

  function toggle(node) {
    expanded.has(node.path) ? expanded.delete(node.path) : expanded.add(node.path);
    renderTree();
  }

  /* find a node by path; expand its ancestors in the tree; select + scroll to it */
  function revealNode(path) {
    const node = findByPath(root, path);
    if (!node) return;
    selected = path;
    /* expand every ancestor (and root) so the row is visible */
    if (root && root.path != null) expanded.add(root.path);
    let p = path;
    while (p && p.includes("/")) {
      p = p.slice(0, p.lastIndexOf("/"));
      expanded.add(p);
    }
    renderTree();
    renderDetail(node);
    setTimeout(() => {
      const row = document.querySelector(`.tree-row[data-path="${path}"]`);
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }, 0);
  }

  function findByPath(node, path) {
    if (!node) return null;
    if (node.path === path) return node;
    for (const c of node.children || []) {
      const r = findByPath(c, path);
      if (r) return r;
    }
    return null;
  }

  /* ---------- detail pane ---------- */
  function selectNode(node) {
    selected = node.path;
    renderTree();
    renderDetail(node);
  }

  function renderDetail(node) {
    const box = document.getElementById("struct-detail");
    if (!box) return;
    clear(box);
    const isDir = node.type === "dir";
    const desc = (node.description || "").trim();

    box.appendChild(el("h2", { style: "margin:0 0 4px;font-size:19px" }, node.name));
    box.appendChild(el("div", { class: "mono small muted", style: "margin-bottom:14px;word-break:break-all" }, node.path));

    /* stats row */
    const stats = [];
    if (node.size != null) stats.push(el("span", { class: "chip dim" }, `${(node.size / 1024).toFixed(1)} KB`));
    if (node.lines != null) stats.push(el("span", { class: "chip dim" }, `${node.lines} lines`));
    if (isDir) stats.push(el("span", { class: "chip dim" },
      `${node.child_count != null ? node.child_count : (node.children || []).length} items`));
    if (stats.length) box.appendChild(el("div", { class: "row wrap", style: "margin-bottom:14px" }, stats));

    if (isDir) {
      renderDirDetail(box, node, desc);
    } else {
      renderFileDetail(box, node, desc);
    }
  }

  /* directory: Contents first, description below */
  function renderDirDetail(box, node, desc) {
    box.appendChild(el("div", { class: "section-title" }, "Contents"));
    const kids = node.children || [];
    if (!kids.length && node.child_count == null) {
      box.appendChild(el("p", { class: "muted small", style: "font-style:italic" }, "(empty folder)"));
    } else {
      const sorted = kids.slice().sort((a, b) =>
        (a.type === b.type ? 0 : a.type === "dir" ? -1 : 1) || a.name.localeCompare(b.name));
      const list = el("div", { class: "contents-list" });
      sorted.forEach(c => list.appendChild(contentsRow(c)));
      box.appendChild(list);
      if (node.child_count != null) {
        box.appendChild(el("p", { class: "muted small", style: "margin:6px 0 0" },
          `… ${node.child_count} items total — filter to narrow`));
      }
    }

    box.appendChild(el("div", { class: "section-title" }, "What this is"));
    if (desc) {
      box.appendChild(el("p", { class: "struct-desc" }, desc));
    } else {
      box.appendChild(el("p", { class: "muted small", style: "font-style:italic" }, "No description yet."));
    }
  }

  function contentsRow(c) {
    const isDir = c.type === "dir";
    const truncated = isDir && c.child_count != null;
    const count = isDir
      ? (truncated ? `… ${c.child_count} items` : `${(c.children || []).length} items`)
      : (c.lines != null ? `${c.lines}` : "");
    return el("div", { class: "contents-row", onclick: () => revealNode(c.path) },
      el("span", { class: "tree-icon", html: isDir ? ICON_DIR : ICON_FILE }),
      el("span", { class: "contents-name" }, c.name),
      el("span", { class: "tree-meta" }, count));
  }

  /* file: description, then a peek-inside preview, then key lines, then symbols */
  const PREVIEW_EXT = new Set(["py", "js", "css", "html", "json", "md", "bat", "txt", "log", "csv", "toml"]);

  function renderFileDetail(box, node, desc) {
    box.appendChild(el("div", { class: "section-title" }, "What this is"));
    if (desc) {
      box.appendChild(el("p", { class: "struct-desc" }, desc));
    } else {
      box.appendChild(el("p", { class: "muted small", style: "font-style:italic" }, "No description yet."));
    }

    const ext = node.name.includes(".") ? node.name.split(".").pop().toLowerCase() : "";
    if (PREVIEW_EXT.has(ext)) {
      box.appendChild(el("div", { class: "section-title" }, "Preview"));
      const holder = el("div", null, el("p", { class: "muted small" }, "Loading…"));
      box.appendChild(holder);
      loadFilePreview(node, holder, false);
    }

    const hs = (node.highlights || []).slice(0, 5);
    if (hs.length) {
      box.appendChild(el("div", { class: "section-title" }, `Key lines (${hs.length}/5)`));
      const pre = el("pre", { class: "key-lines" });
      hs.forEach((line, i) => pre.appendChild(el("div", { class: "key-line" },
        el("span", { class: "key-gutter" }, String(i + 1)),
        el("span", { class: "key-code" }, line))));
      box.appendChild(pre);
    }

    if (node.symbols && node.symbols.length) {
      box.appendChild(el("div", { class: "section-title" }, "Functions & classes"));
      box.appendChild(el("div", { class: "row wrap" },
        node.symbols.map(s => el("span", { class: "chip mono-chip" }, s))));
    }
  }

  /* peek-inside preview from /api/file — short version first, full on request */
  async function loadFilePreview(node, holder, full) {
    const q = `/api/file?path=${encodeURIComponent(node.path)}${full ? "&full=1" : ""}`;
    const data = await api(q).catch(e => ({ error: (e && e.message) || "preview failed" }));
    if (!holder.isConnected) return;
    clear(holder);
    if (data.error) {
      holder.appendChild(el("p", { class: "muted small", style: "font-style:italic" },
        /404|415|400/.test(data.error) ? "No preview for this file." : `Preview failed: ${data.error}`));
      return;
    }
    holder.appendChild(el("pre", { class: "key-lines file-preview" },
      el("div", { class: "key-line" }, el("span", { class: "key-code" }, data.content))));
    if (data.truncated) {
      const kb = (data.size / 1024).toFixed(1);
      if (!full) {
        holder.appendChild(el("div", { class: "row", style: "margin-top:6px" },
          el("span", { class: "muted small" }, `Showing the first ${Math.round(data.content.length / 1024)} KB of ${kb} KB.`),
          el("button", {
            class: "ghost",
            onclick: () => loadFilePreview(node, holder, true),
          }, "Show more (up to 200 KB)")));
      } else {
        holder.appendChild(el("p", { class: "muted small", style: "margin-top:6px;font-style:italic" },
          `Still truncated — the file is ${kb} KB total. Open it in an editor if you need everything: ${node.path}`));
      }
    }
  }

  App.tabs.structure = { render, built: false };
})();
