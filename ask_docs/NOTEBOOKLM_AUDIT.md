# NotebookLM behaviour audit — June 2026

Captured by driving NotebookLM via Chrome MCP. This is the reference for what
"works like NotebookLM" actually means functionally (not just visually). When
we close any gap here, the entry below becomes a checked item.

---

## Confirmed behaviours

### 1. Notebook lifecycle
- Click **+ Create new** → blank "Untitled notebook" opens immediately
  (no name prompt). Title is **inline-editable** by clicking it.
- After the **first source** is added, NotebookLM **auto-renames** the notebook
  to a content-derived title (e.g. "Large Language Models: Architectures,
  Training, and Evolution" from a Wikipedia LLM source).
- Recent notebooks list shows on the home page; the empty-state card has a
  `+` icon to create.

### 2. Adding sources
- Single **+ Add sources** button on the left panel opens a modal.
- Modal title is *context-aware*: changes between "Create Audio and Video
  Overviews from your documents / websites" depending on the active tab.
- Modal contains:
  - "Search the web for new sources" with Web / Fast Research dropdowns
    and a magnifier (functional in NotebookLM, **we explicitly skip**).
  - Big drop zone with "or drop your files" + "pdf, images, docs, audio,
    and more" — DnD active throughout the modal.
  - Pill-button row: **↑ Upload files** · **🔗 Websites** · **🅓 Drive** ·
    **📋 Copied text** (Drive we skip).
- **Websites** subpage:
  - Title: "Website and YouTube URLs"
  - Back arrow + X close
  - Big textbox, placeholder "Paste any links"
  - Constraint bullets: multi-URL via space/newline, visible text only,
    no paid articles, YouTube uses transcript only, no recent uploads.
  - "Insert" button (disabled until non-empty)
- Drag-and-drop appears to work anywhere on the drop zone.

### 3. First-source experience
- Adding the first source **auto-generates a full summary** as the first
  assistant message — without the user typing anything. The summary uses
  **inline citation chips** numbered (e.g. `[1]`).
- A robot avatar accompanies the auto-summary.
- Title above: matches the auto-rename ("Large Language Models...").
- Subtitle: "1 source · Jun 2, 2026" (date the summary was generated).
- Below the summary: **3 suggested follow-up questions** as clickable chips.
  Examples: "How do transformer architectures differ from earlier language
  models?", "What are the main societal and ethical concerns with LLMs?".
- Below the question chips: action row of icons under the assistant message:
  - 📌 Pin/save as note
  - 📋 Copy
  - 👍 Thumbs up
  - 👎 Thumbs down

### 4. Chat interaction
- Source count shown next to send arrow: "1 source", "5 sources".
- Send is the dark circular arrow button.
- Each assistant answer renders with **full markdown**:
  - **Bold** for key terms
  - Headings (e.g. "How RLHF is Used in Training")
  - Bullet points with bold lead-ins ("**Training a Reward Model:** ...")
  - Body text in regular weight, dark grey on white
- **Citations are inline numbered chips embedded in the text** — they
  appear right after the cited phrase (e.g. "after its initial
  pre-training [1] ."). Each chip is small, round, numbered.
- Clicking a citation:
  - Opens a **floating tooltip near the citation** showing the source
    section heading + the exact passage with hyperlinks preserved.
  - "View source" link at the bottom of the tooltip.
  - The **left sidebar also transitions** to show that source's full
    content (see #6 below).

### 5. Chat header
- "Chat" title on the left.
- Adjustments-slider icon (mid).
- 3-dot menu (right) opens:
  - "Customize notebook" — opens a persona/style editor modal.
  - "Delete chat history" with the helper "Chat history is private to you."

### 6. Source preview (left sidebar transformation)
- Clicking a source NAME in the left list (or a citation in the answer):
  - **Replaces the source list view** with a single-source content view.
  - Shows the **source name** at the top with an **external-link icon**
    (opens the original URL/file in a new tab).
  - A collapsible **"Source guide"** card at the top, marked with a sparkle
    icon and a loading spinner during initial generation — this is an
    auto-generated **per-source summary** distinct from the
    all-sources Summary artifact.
  - Below the guide, the **full extracted source content** rendered
    with markdown: bold key terms, hyperlinks (e.g. Wikipedia "edit"
    links), section headings.
- A **collapse-source-panel icon** at top right of the source panel
  collapses the entire left column.
- Returning from a source preview: click back to the source list or click
  X / collapse.

### 7. Sources panel (list view)
- Header: "Sources" with the collapse icon on the right.
- "+ Add sources" pill button (white, thin border).
- "Search the web for new sources" card with Web / Fast Research
  dropdowns (we **skip**).
- When sources exist:
  - Each source row: small icon (Wikipedia W, PDF icon, etc.) + truncated
    name + **per-source checkbox** on the right.
  - "Select all" master checkbox shown above the list when ≥2 sources.
  - Checking/unchecking scopes the chat to selected sources only.
- When no sources: empty-state placeholder (document icon + helper text).

### 8. Studio panel (we skip ENTIRELY)
For reference, NotebookLM's right panel contains these cards we will NOT
build: Audio Overview, Slide Deck (BETA), Video Overview, Mind Map,
Reports, Flashcards, Quiz, Infographic (BETA), Data Table.

Below the cards: a magic-wand icon + "Studio output will be saved here"
empty state, plus an "Add note" pill button at the very bottom.

### 9. Auto-renaming
- The notebook's title is automatically derived from source content the
  moment the first source is added. Likely an LLM call:
  "Given this source, suggest a title for the notebook."
- Subsequent sources do not seem to trigger renames.

---

## Gap list — what to build to match this

Tracked as tasks #33-#43 in the task list.

| # | Feature | Effort | Visible difference |
|---|---|---|---|
| 33 | Auto-rename notebook from first source | 30 min | Big — first impression |
| 34 | Auto-summary as first assistant message | 30 min | Big — sets the demo experience |
| 35 | Inline numbered citation chips | 2-3 hr | Major UX gap |
| 36 | Markdown rendering in chat | 3-4 hr | Major — currently shows raw `**asterisks**` |
| 37 | Source-click → preview panel | 2 hr | Major — currently nothing happens |
| 38 | Source-guide auto-summary per source | 1 hr | Polish (cached) |
| 39 | Suggested follow-up question chips | 1 hr | Polish, drives engagement |
| 40 | Per-message action buttons (copy/pin) | 1 hr | Quality of life |
| 41 | Source-selection checkboxes | 30 min | Real scoping behaviour |
| 42 | Customize dialog (persona) | 30 min | Already have data layer |
| 43 | Multi-URL paste with constraints text | 30 min | Polish |

**Total: ~14-16 hours of focused work to close the visible gap.**

---

## What we will NOT build (deliberate)

- Studio panel entirely (audio overview, video overview, mind map,
  flashcards, quiz, slide deck, infographic, data table, reports).
- Web search for sources (out of scope per user).
- Google Drive integration.
- Sharing / collaboration.
