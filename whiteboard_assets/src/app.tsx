import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Excalidraw, MainMenu } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";

// pywebview injects window.pywebview.api.{load_scene,save_scene}
declare global {
  interface Window {
    pywebview?: {
      api: {
        load_scene: () => Promise<string>;
        save_scene: (txt: string) => Promise<boolean>;
      };
    };
  }
}

function debounce<T extends (...a: any[]) => void>(fn: T, ms: number) {
  let t: any;
  return (...a: Parameters<T>) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

function App() {
  const apiRef = useRef<any>(null);
  const [initialData, setInitialData] = useState<any>(null);
  const [loaded, setLoaded] = useState(false);

  // Wait for pywebview bridge, then load scene
  useEffect(() => {
    let cancel = false;
    const tryLoad = async () => {
      // Wait until pywebview.api exists
      for (let i = 0; i < 100 && !window.pywebview?.api; i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      if (cancel) return;
      try {
        const txt = await window.pywebview!.api.load_scene();
        if (txt) {
          const data = JSON.parse(txt);
          setInitialData({
            elements: data.elements ?? [],
            appState: {
              viewBackgroundColor: data.appState?.viewBackgroundColor ?? "#ffffff",
              gridSize: data.appState?.gridSize ?? null,
              theme: data.appState?.theme ?? "light",
              // Open with the pen (freedraw) tool active — Shift+F8 is for
              // sketching, the user should be able to draw immediately.
              activeTool: { type: "freedraw", customType: null, locked: false, lastActiveTool: null },
              ...(data.appState?.zoom    !== undefined && { zoom:    data.appState.zoom    }),
              ...(data.appState?.scrollX !== undefined && { scrollX: data.appState.scrollX }),
              ...(data.appState?.scrollY !== undefined && { scrollY: data.appState.scrollY }),
            },
            files: data.files ?? {},
          });
        } else {
          setInitialData({
            elements: [],
            appState: {
              viewBackgroundColor: "#ffffff",
              activeTool: { type: "freedraw", customType: null, locked: false, lastActiveTool: null },
            },
          });
        }
      } catch {
        setInitialData({ elements: [], appState: { viewBackgroundColor: "#ffffff" } });
      }
      setLoaded(true);
    };
    tryLoad();
    return () => {
      cancel = true;
    };
  }, []);

  const save = useRef(
    debounce(async (elements: any, appState: any, files: any) => {
      if (!window.pywebview?.api) return;
      const payload = {
        type: "excalidraw",
        version: 2,
        source: "hotkeys-embed",
        elements,
        appState: {
          viewBackgroundColor: appState?.viewBackgroundColor,
          gridSize: appState?.gridSize,
          theme: appState?.theme,
          // persist viewport so close+reopen restores the user's view
          zoom: appState?.zoom,
          scrollX: appState?.scrollX,
          scrollY: appState?.scrollY,
        },
        files: files ?? {},
      };
      try {
        await window.pywebview.api.save_scene(JSON.stringify(payload));
      } catch {}
    }, 400),
  ).current;

  // App hotkeys always supersede the bundled whiteboard component's own shortcuts. The host's
  // global keyboard hook (kernel-level WH_KEYBOARD_LL) fires regardless of
  // what we do here, so the app gets every key. To stop the bundled whiteboard component also
  // acting on those keys we install a capture-phase keydown handler that
  // calls preventDefault + stopImmediatePropagation for any key matching
  // a reserved combo. The reserved list is fetched from the host via the
  // pywebview bridge — which reads config.json + prompts.json +
  // chains.json + macros/*.json — and refreshed on a slow poll so
  // Settings edits propagate without restarting the whiteboard.
  useEffect(() => {
    const reserved = new Set<string>();
    const MODKEY = new Set([
      "control", "shift", "alt", "meta", "altgraph", "os", "win",
    ]);
    const KEY_ALIAS: Record<string, string> = {
      " ": "space", "esc": "escape", "return": "enter",
      "del": "delete", "ins": "insert",
      "pgup": "page up", "pgdn": "page down",
      "arrowup": "up", "arrowdown": "down",
      "arrowleft": "left", "arrowright": "right",
    };
    const normalize = (e: KeyboardEvent): string => {
      const raw = (e.key || "").toLowerCase();
      if (MODKEY.has(raw)) return "";  // ignore modifier-only presses
      const parts: string[] = [];
      if (e.ctrlKey || e.metaKey) parts.push("ctrl");
      if (e.altKey) parts.push("alt");
      if (e.shiftKey) parts.push("shift");
      parts.push(KEY_ALIAS[raw] ?? raw);
      return parts.join("+");
    };

    // Hard-blocked combos that aren't app hotkeys but open features
    // we've removed (Command palette via Ctrl+/ or Ctrl+Shift+P). Kept
    // separate from `reserved` so the host app's hotkey list doesn't
    // need to enumerate every "feature off" shortcut.
    const BLOCKED = new Set(["ctrl+/", "ctrl+shift+p"]);
    const onKey = (e: KeyboardEvent) => {
      const combo = normalize(e);
      if (combo && (reserved.has(combo) || BLOCKED.has(combo))) {
        (window as any).__hk_intercepts += 1;
        e.preventDefault();
        e.stopImmediatePropagation();
        e.stopPropagation();
      }
    };
    window.addEventListener("keydown", onKey, true);

    const refresh = async () => {
      try {
        const api: any = (window as any).pywebview?.api;
        if (!api?.get_reserved_keys) return;
        const txt = await api.get_reserved_keys();
        const list: string[] = JSON.parse(txt);
        reserved.clear();
        for (const k of list) reserved.add(k);
      } catch {}
    };
    // First load (pywebview.api may not be ready immediately)
    const t0 = setTimeout(refresh, 400);
    const t1 = setTimeout(refresh, 1200);
    // Slow re-poll so Settings edits are picked up
    const iv = setInterval(refresh, 8000);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      clearTimeout(t0); clearTimeout(t1); clearInterval(iv);
    };
  }, []);

  // Rebrand every "the bundled whiteboard component" → "Whiteboard" in the live DOM. Catches the
  // help dialog, command-palette items, tooltips, aria-labels, and any error
  // toasts. Runs once on mount over the existing DOM then watches for added
  // / changed nodes so dynamically rendered surfaces (modals, menus) get
  // rewritten the moment they appear.
  useEffect(() => {
    const PAT = /Excalidraw/g;
    const REPL = "Whiteboard";
    const ATTRS = ["aria-label", "title", "placeholder", "alt"];
    const rewriteText = (n: Node) => {
      if (n.nodeType === Node.TEXT_NODE && n.nodeValue && PAT.test(n.nodeValue)) {
        n.nodeValue = n.nodeValue.replace(PAT, REPL);
      }
    };
    const rewriteAttrs = (el: Element) => {
      for (const a of ATTRS) {
        const v = el.getAttribute(a);
        if (v && PAT.test(v)) el.setAttribute(a, v.replace(PAT, REPL));
      }
    };
    const walk = (root: Node) => {
      const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
      let n: Node | null = w.currentNode;
      while (n) {
        if (n.nodeType === Node.TEXT_NODE) rewriteText(n);
        else if (n.nodeType === Node.ELEMENT_NODE) rewriteAttrs(n as Element);
        n = w.nextNode();
      }
    };
    // Hide orphan section headers — the "Generate" label sits above the
    // Mermaid-to-Whiteboard item which we hide via CSS. The label has no
    // Mermaid attribute to target, so do it here. the bundled whiteboard component's dropdown
    // is a flat list (label, button, label, button…) so we check what's
    // AFTER the label within the same parent: if no visible actionable
    // items follow before the next label / end-of-parent, hide it.
    const isActionable = (el: Element) =>
      el.tagName === "BUTTON" ||
      el.getAttribute("role") === "menuitem" ||
      el.getAttribute("role") === "option" ||
      !!el.querySelector("button, [role='menuitem'], [role='option']");
    const isVisible = (el: Element) => {
      const cs = getComputedStyle(el as HTMLElement);
      return cs.display !== "none" && cs.visibility !== "hidden";
    };
    const hideOrphanGenerate = () => {
      document.querySelectorAll<HTMLElement>("*").forEach((el) => {
        const t = (el.textContent || "").trim();
        if (t !== "Generate") return;
        if (el.querySelector("button, [role='menuitem'], [role='option']")) return;
        // Count visible actionable siblings following this label.
        let sib: Element | null = el.nextElementSibling;
        let visibleAfter = 0;
        let labelHit = false;
        while (sib && !labelHit) {
          // If we hit another section label, stop scanning.
          const sibTxt = (sib.textContent || "").trim();
          if (/^[A-Z][a-z]+$/.test(sibTxt) && !isActionable(sib)) {
            labelHit = true; break;
          }
          if (isVisible(sib) && isActionable(sib)) visibleAfter++;
          sib = sib.nextElementSibling;
        }
        if (visibleAfter === 0) el.style.display = "none";
      });
    };
    // Hide any UI element mentioning the removed library — toasts,
    // context menu items, status messages. Anchored on "library" only
    // (avoids hiding unrelated "added to ..." messages from other
    // features).
    const hideLibraryUi = () => {
      const SELECTORS = [
        ".Toast", ".toast", "[class*='toast' i]",
        "[role='status']", "[role='alert']",
        "[role='menuitem']", ".context-menu-item", ".context-menu li",
      ].join(",");
      document.querySelectorAll<HTMLElement>(SELECTORS).forEach((el) => {
        const t = (el.textContent || "").toLowerCase();
        if (/library/.test(t)) el.style.display = "none";
      });
    };

    // Hide help-dialog rows for features we don't ship — the bundled
    // component's AI flowchart wizard, "Magic frame", AI-keyed features,
    // and Command palette (removed because it exposes hidden features
    // via free-text search).
    const hideUnshippedHelpRows = () => {
      const dlg = document.querySelector(".HelpDialog");
      if (!dlg) return;
      dlg.querySelectorAll<HTMLElement>(
        "dl > dt, dl > dd, li, .HelpDialog__shortcut, " +
        ".HelpDialog__shortcut-row, [class*='shortcut' i]"
      ).forEach((row) => {
        const t = (row.textContent || "").toLowerCase();
        if (/flowchart|magic frame|generate with ai|command palette/.test(t)) {
          row.style.display = "none";
        }
      });
    };
    walk(document.body);
    hideOrphanGenerate();
    hideLibraryUi();
    hideUnshippedHelpRows();
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "characterData" && m.target) rewriteText(m.target);
        else if (m.type === "childList") {
          m.addedNodes.forEach((n) => walk(n));
        } else if (m.type === "attributes" && m.target.nodeType === 1) {
          rewriteAttrs(m.target as Element);
        }
      }
      // Cheap to rerun — the visible-items count is bounded
      hideOrphanGenerate();
      hideLibraryUi();
      hideUnshippedHelpRows();
    });
    mo.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
    return () => mo.disconnect();
  }, []);

  // Promote Frame tool + Laser pointer from the "more shapes" dropdown to
  // the main toolbar — the bundled whiteboard component exposes no prop for this, so we clone
  // styled buttons inline next to the eraser. Web Embed / Mermaid are
  // already CSS-hidden, so once Frame and Laser are moved out the dropdown
  // trigger has nothing left worth showing and we hide it too.
  useEffect(() => {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const ICONS: Record<string, string> = {
      // Both icons are stylistically the bundled whiteboard component-ish — simple line drawings,
      // 1.25 stroke, currentColor. Picked to read at toolbar size (~20×20).
      frame:
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4v3M6 17v3M18 4v3M18 17v3M4 6h3M17 6h3M4 18h3M17 18h3"/></svg>',
      laser:
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M18.5 5.5L13 11M16.5 4l1.5 1.5M19.5 7l1.5 1.5M11 13l-7 7M14 8l-1.5-1.5M18 12l1.5 1.5"/></svg>',
    };

    const buildToolButton = (
      tool: "frame" | "laser",
      label: string,
      shortcut: string,
      tmpl: HTMLLabelElement,
    ): HTMLLabelElement => {
      // Clone the eraser's <label> WITHOUT children so we get the same
      // ToolIcon* classes for free, then rewrite the inner DOM. We
      // deliberately don't clone the radio input — adding another input
      // to the radio group would break the bundled whiteboard component's controlled tool state.
      const el = tmpl.cloneNode(false) as HTMLLabelElement;
      el.setAttribute("aria-label", label);
      el.setAttribute("data-hk-tool", tool);
      // data-tooltip drives our shared styled-pill tooltip (defined in
      // index.html) — same look as the Reset button so every custom-
      // injected control speaks the same visual language.
      el.setAttribute("data-tooltip", `${label} — ${shortcut}`);
      el.innerHTML =
        `<div class="ToolIcon__icon" aria-hidden="true">${ICONS[tool]}</div>` +
        `<span class="ToolIcon__keybinding">${shortcut}</span>`;
      el.addEventListener("click", (e) => {
        e.preventDefault();
        try { apiRef.current?.setActiveTool({ type: tool }); } catch {}
      });
      return el;
    };

    const inject = () => {
      // the bundled whiteboard component tool buttons are <label class="ToolIcon Shape ..."> wrapping
      // a radio <input data-testid="toolbar-XXX">. The `value` attribute is the
      // browser default "on", so we identify by data-testid.
      const eraserInput = document.querySelector<HTMLInputElement>(
        'input[type="radio"][data-testid="toolbar-eraser"]'
      );
      if (!eraserInput) return false;
      const eraserLabel = eraserInput.closest<HTMLLabelElement>("label.ToolIcon, label");
      if (!eraserLabel) return false;
      const parent = eraserLabel.parentElement;
      if (!parent) return false;
      if (parent.querySelector('[data-hk-tool="laser"]')) return true;

      const frameEl = buildToolButton("frame", "Frame tool", "F", eraserLabel);
      const laserEl = buildToolButton("laser", "Laser pointer", "K", eraserLabel);
      eraserLabel.after(frameEl);
      frameEl.after(laserEl);
      return true;
    };

    // Try once now, then keep retrying via the existing MutationObserver
    // (the bundled whiteboard component mounts the toolbar slightly after the API ref fires).
    let done = inject();
    const mo = new MutationObserver(() => {
      if (!done) done = inject();
      // If React re-renders the toolbar it can drop our nodes — re-inject.
      if (done && !document.querySelector('[data-hk-tool="laser"]')) {
        done = inject();
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, []);

  // ── Top-right "Reset the canvas" button ───────────────────────────────
  // Lives in the same screen slot the Library trigger used to occupy.
  // Triggers the bundled whiteboard component's NATIVE clearCanvas confirm dialog via
  // appState.openDialog — same modal you'd see if you used the hamburger
  // menu entry. That gives a consistent look (centred, styled, themed)
  // instead of WebView2's browser-native confirm popup.
  useEffect(() => {
    // Trash icon path lifted from the bundled component's own icon set
    // so the visual matches the hamburger menu version exactly.
    const SVG_RESET =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" role="img"><path d="M3.333 5.833h13.334M8.333 9.167v5M11.667 9.167v5M4.167 5.833l.833 10A1.667 1.667 0 0 0 6.667 17.5h6.666A1.667 1.667 0 0 0 15 15.833l.833-10M7.5 5.833V3.333A.833.833 0 0 1 8.333 2.5h3.334a.833.833 0 0 1 .833.833v2.5"/></svg>';

    const buildResetBtn = () => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.setAttribute("data-hk-reset", "1");
      btn.setAttribute("aria-label", "Reset the canvas");
      // CSS-rendered tooltip (matches the bundled whiteboard component's dark-pill look) via
      // data-tooltip + .hk-reset-trigger:hover::after in index.html.
      // We deliberately DON'T set title="" — it would render a second
      // (native, plain) browser tooltip on top of our styled one.
      // aria-label keeps the button accessible.
      btn.setAttribute("data-tooltip", "Reset the canvas — Ctrl+Delete");
      btn.className = "hk-reset-trigger";
      btn.innerHTML =
        `<div class="hk-reset-icon" aria-hidden="true">${SVG_RESET}</div>` +
        `<span class="hk-reset-label">Reset the canvas</span>`;
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        // Open the hamburger, find the bundled whiteboard component's "Reset the canvas" row,
        // and click it. the bundled whiteboard component's confirm dialog then appears — same
        // native modal, centred, themed. The hamburger briefly opens
        // (~50 ms) before closing again on the row click; the flicker
        // is barely perceptible because the bundled whiteboard component closes the menu the
        // moment a row is selected.
        const trigger = document.querySelector<HTMLElement>(
          'button.main-menu-trigger, button[data-testid="main-menu-trigger"]'
        );
        if (!trigger) return;
        trigger.click();
        setTimeout(() => {
          const items = document.querySelectorAll<HTMLElement>(
            '.dropdown-menu-item, [role="menuitem"]'
          );
          for (const it of Array.from(items)) {
            const t = (it.textContent || "").toLowerCase();
            if (/reset the canvas/.test(t)) {
              it.click();
              return;
            }
          }
          // Fallback: close the menu if we couldn't find the row.
          trigger.click();
        }, 50);
      });
      return btn;
    };

    const injectReset = () => {
      if (document.querySelector('[data-hk-reset]')) return true;
      // the bundled whiteboard component's `.layer-ui__wrapper__top-right` doesn't actually sit
      // at the visual top-right in 0.18 — append to the the bundled whiteboard component root
      // and pin with fixed positioning in CSS instead.
      const slot =
        document.querySelector(".excalidraw.excalidraw-container") ||
        document.querySelector(".excalidraw") ||
        document.body;
      if (!slot) return false;
      slot.appendChild(buildResetBtn());
      return true;
    };

    let ok = injectReset();
    const mo = new MutationObserver(() => {
      if (!ok || !document.querySelector('[data-hk-reset]')) ok = injectReset();
    });
    mo.observe(document.body, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, []);

  // Belt-and-suspenders zoom shortcuts. Edge WebView2 historically intercepts
  // Ctrl++ / Ctrl+- / Ctrl+0 for browser-level zoom even after disabling its
  // built-in zoom control — by adding a window-level capture-phase handler
  // we guarantee the canvas zooms regardless of how the embedder is set up.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const api = apiRef.current;
      if (!api) return;
      const k = e.key;
      let next: number | null = null;
      if (k === "=" || k === "+") {
        next = Math.min((api.getAppState().zoom?.value ?? 1) * 1.2, 30);
      } else if (k === "-") {
        next = Math.max((api.getAppState().zoom?.value ?? 1) / 1.2, 0.1);
      } else if (k === "0") {
        next = 1;
      }
      if (next !== null) {
        e.preventDefault();
        e.stopPropagation();
        api.updateScene({ appState: { ...api.getAppState(), zoom: { value: next } } });
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  if (!loaded) return null;

  return (
    <Excalidraw
      excalidrawAPI={(api) => {
        apiRef.current = api;
        // Belt-and-braces: also call setActiveTool after a tick in case
        // initialData.appState.activeTool gets overridden by the bundled whiteboard component's
        // own init. Whichever lands second wins — and that's what we want.
        setTimeout(() => {
          try { api.setActiveTool({ type: "freedraw" }); } catch {}
        }, 100);
      }}
      initialData={initialData}
      name="Whiteboard"
      onChange={(elements, appState, files) => save(elements, appState, files)}
      UIOptions={{
        canvasActions: {
          loadScene: false,           // we auto-load from whiteboard.json
          saveToActiveFile: false,    // no .excalidraw file workflow
          saveAsImage: true,          // PNG/SVG export is genuinely useful
          export: false,              // hide the export-to-link feature
          toggleTheme: true,
          changeViewBackgroundColor: true,
          clearCanvas: true,
        },
      }}
      autoFocus
    >
      <MainMenu>
        <MainMenu.DefaultItems.SaveAsImage />
        <MainMenu.DefaultItems.SearchMenu />
        <MainMenu.DefaultItems.Help />
        <MainMenu.DefaultItems.ClearCanvas />
        <MainMenu.Separator />
        <MainMenu.DefaultItems.ToggleTheme />
        <MainMenu.DefaultItems.ChangeCanvasBackground />
        {/* ClearCanvas re-added so its native confirm dialog mechanism
            stays available. CSS (.hk-hide-clear-in-menu) hides this row
            VISUALLY inside the hamburger — the user's only entry point
            is the top-right "Reset the canvas" button, which programmati-
            cally clicks this same row to inherit the bundled component's modal. */}
      </MainMenu>
    </Excalidraw>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
