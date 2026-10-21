// Server edition of the browser PDF editor. It starts from ../../PdfEditor.jsx (unchanged)
// and adds tools that run on the local backend: search with positions, find and replace,
// clean and optimize, document info, and undo. The editing tools are the same.
import { useEffect, useRef, useState } from "react";
import { PDFDocument, PDFCheckBox, PDFTextField, StandardFonts, degrees, rgb } from "pdf-lib";
import { IconFilePencil } from "@tabler/icons-react";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

const SCALE = 1.5;
const HISTORY_LIMIT = 30;
const EMPTY_SEARCH = { find: "", count: 0, hits: [], index: -1, truncated: false };
const CLEAN_OPTIONS = [
  { id: "strip_metadata", label: "Remove document info and XMP metadata" },
  { id: "strip_javascript", label: "Remove JavaScript actions" },
  { id: "strip_embedded_files", label: "Remove embedded files and attachments" },
  { id: "compress", label: "Compress (recompress streams, pack objects)" },
];
const DEFAULT_CLEAN = Object.fromEntries(CLEAN_OPTIONS.map((o) => [o.id, true]));

// Helvetica's descent: search boxes sit this far above the baseline (in font-size units).
const HELVETICA_DESCENT = 0.207;

// Cover only: hide the text under a rectangle and write the new text on top.
// The old characters stay in the file.
async function drawCover(doc, pageNumber, run, replacement) {
  const [, , , , x, y] = run.transform;
  const size = run.height;
  const page = doc.getPage(pageNumber);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  page.drawRectangle({
    x: x - 1,
    y: y - size * 0.25,
    width: run.width + 2,
    height: size * 1.1,
    color: rgb(1, 1, 1),
  });
  if (replacement) {
    page.drawText(replacement, { x, y, size, font, color: rgb(0, 0, 0) });
  }
}

// Cover one Find match. Its box comes from pdfminer in user space and spans the font size.
async function drawCoverHit(doc, hit, replacement) {
  const [x0, y0, x1, y1] = hit.box;
  const size = y1 - y0;
  const width = x1 - x0;
  const page = doc.getPage(hit.page - 1);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  page.drawRectangle({ x: x0 - 1, y: y0 - 1, width: width + 2, height: size + 2, color: rgb(1, 1, 1) });
  if (replacement) {
    // Shrink a longer replacement to the original width, down to 60% of the font size.
    const natural = font.widthOfTextAtSize(replacement, size);
    const fitted = natural > width ? Math.max(size * 0.6, (size * width) / natural) : size;
    page.drawText(replacement, {
      x: x0,
      y: y0 + size * HELVETICA_DESCENT,
      size: fitted,
      font,
      color: rgb(0, 0, 0),
    });
  }
}

// Apply as: Cover only, or Permanent (only when the local server is running).
function ModeSelector({ mode, onMode, serverOn, permanentNote = "" }) {
  return (
    <fieldset className="modes">
      <legend>Apply as</legend>
      <label className="mode">
        <input type="radio" name="apply-mode" checked={mode === "cover"} onChange={() => onMode("cover")} />
        <span>
          <strong>Cover only</strong>
          <small>Hides the old text on screen. It stays in the file, so copy and search still find it.</small>
        </span>
      </label>
      {serverOn ? (
        <label className={`mode${permanentNote ? " off" : ""}`}>
          <input
            type="radio"
            name="apply-mode"
            disabled={Boolean(permanentNote)}
            checked={mode === "permanent"}
            onChange={() => onMode("permanent")}
          />
          <span>
            <strong>Permanent</strong>
            <small>{permanentNote || "Removes the old text from the file."}</small>
          </span>
        </label>
      ) : (
        <p className="offline">Permanent changes need the local server. Start it to turn them on.</p>
      )}
    </fieldset>
  );
}

// Viewport box of a text run, plus its font size in screen pixels.
function runViewportBox(vp, run) {
  const [, , , , x, y] = run.transform;
  const [x1, y1, x2, y2] = vp.convertToViewportRectangle([
    x,
    y - run.height * 0.25,
    x + run.width,
    y + run.height * 0.85,
  ]);
  return {
    left: Math.min(x1, x2),
    top: Math.min(y1, y2),
    width: Math.abs(x2 - x1),
    height: Math.abs(y2 - y1),
    fontPx: run.height * vp.scale,
  };
}

// Viewport box of a Find match. Its box is in PDF points and spans the font size.
function hitViewportBox(vp, hit) {
  const [x0, y0, x1, y1] = hit.box;
  const [a, b, c, d] = vp.convertToViewportRectangle([x0, y0, x1, y1]);
  return {
    left: Math.min(a, c),
    top: Math.min(b, d),
    width: Math.abs(c - a),
    height: Math.abs(d - b),
    fontPx: (y1 - y0) * vp.scale,
  };
}

// Nearest match on the page to a point in PDF user space. Null when none is close enough.
function nearestMatch(list, page, cx, cy, size) {
  let best = null;
  for (const item of list) {
    if (item.page !== page) continue;
    const [x0, y0, x1, y1] = item.box;
    const distance = Math.hypot((x0 + x1) / 2 - cx, (y0 + y1) / 2 - cy);
    if (!best || distance < best.distance) best = { ...item, distance };
  }
  return best && best.distance <= Math.max(size * 2, 6) ? best : null;
}

// Edits text in place on the page, the way a text editor does. Enter applies, Escape cancels,
// and clicking away applies a changed text. Cover only hides the original; Permanent removes it.
function InlineEditor({ editor, serverOn, busy, onValue, onMode, onApply, onCancel }) {
  const field = useRef(null);
  useEffect(() => {
    field.current?.focus();
    field.current?.select();
  }, []);
  const { box } = editor;
  const isAdd = editor.kind === "add";
  const isCover = editor.mode === "cover";
  const applyDisabled = busy || (isCover && Boolean(editor.blocked));
  return (
    <div className="inline" style={{ left: box.left, top: box.top, width: Math.max(box.width, 160) }}>
      <input
        ref={field}
        type="text"
        aria-label={isAdd ? "New text" : "Edit text in place"}
        placeholder={isAdd ? "Type text" : undefined}
        className="inline-field"
        value={editor.value}
        style={{ fontSize: Math.max(box.fontPx, 9), height: Math.max(box.height, 18) }}
        onChange={(event) => onValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onApply();
          } else if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
        }}
        onBlur={() => (editor.value === editor.original ? onCancel() : onApply())}
      />
      <div className="inline-bar" onMouseDown={(event) => event.preventDefault()}>
        <button type="button" disabled={applyDisabled} onClick={() => onApply()}>Apply</button>
        {!isAdd && (
          <button type="button" disabled={applyDisabled} onClick={() => onApply("")}>Delete</button>
        )}
        <button type="button" onClick={onCancel}>Cancel</button>
        {!isAdd && serverOn && (
          <span className="seg" role="group" aria-label="How to apply">
            <button
              type="button"
              aria-pressed={isCover}
              className={isCover ? "on" : ""}
              title="Hides the original text. It stays in the file."
              onClick={() => onMode("cover")}
            >
              Cover only
            </button>
            <button
              type="button"
              aria-pressed={!isCover}
              className={!isCover ? "on" : ""}
              title="Removes the original text from the file."
              onClick={() => onMode("permanent")}
            >
              Permanent
            </button>
          </span>
        )}
        {!isAdd && !serverOn && (
          <span className="chip-cover" title="Start the local server to make permanent changes">Cover only</span>
        )}
        {isCover && editor.blocked && <span className="pop-warn">{editor.blocked}</span>}
      </div>
    </div>
  );
}

const TOOLS = [
  { id: "text", label: "Text" },
  { id: "highlight", label: "Highlight" },
  { id: "whiteout", label: "Whiteout" },
];

const STYLE = `
@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap');
.desk { min-height: 100vh; background: #D9DED6; color: #1E2420; font-family: 'Hanken Grotesk', system-ui, sans-serif; display: flex; flex-direction: column; box-sizing: border-box; }
.desk *, .desk *::before, .desk *::after { box-sizing: border-box; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 20px; background: #FBFBF8; border-bottom: 1px solid #C5CCC2; flex-wrap: wrap; }
.title { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
.title h1 { font-family: 'Newsreader', Georgia, serif; font-weight: 600; font-size: 22px; margin: 0; }
.credit { padding: 10px 20px; font-size: 13px; color: #5B6660; background: #FBFBF8; border-top: 1px solid #C5CCC2; }
.credit a { color: #2E5E4E; }
.filename { color: #5B6660; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 40ch; }
.actions, .group { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.desk button { font: inherit; font-size: 14px; padding: 7px 12px; border-radius: 4px; border: 1px solid #AEB7AC; background: #FBFBF8; color: inherit; cursor: pointer; }
.desk button:hover:not(:disabled) { border-color: #2E5E4E; }
.desk button:disabled { opacity: .45; cursor: default; }
.desk button.primary { background: #2E5E4E; border-color: #2E5E4E; color: #FBFBF8; }
.desk button.on { background: #1E2420; border-color: #1E2420; color: #FBFBF8; }
.desk button:focus-visible, .desk input:focus-visible { outline: 2px solid #2E5E4E; outline-offset: 2px; }
.toolbar { display: flex; justify-content: space-between; gap: 12px; padding: 10px 20px; flex-wrap: wrap; border-bottom: 1px solid #C5CCC2; background: #E8ECE5; }
.workspace { display: flex; flex: 1; gap: 20px; padding: 24px 20px; align-items: flex-start; }
.pageArea { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 14px; }
.canvasWrap { max-width: 100%; overflow: auto; background: #fff; box-shadow: 0 1px 0 #AEB7AC, 6px 6px 0 #AEB7AC; }
.canvasWrap canvas { display: block; }
.canvasWrap canvas.tool { cursor: crosshair; }
.canvasWrap canvas.edit { cursor: text; }
.pager { display: flex; align-items: center; gap: 12px; font-size: 14px; }
.empty { margin-top: 12vh; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 16px; }
.empty p { font-family: 'Newsreader', Georgia, serif; font-size: 24px; margin: 0; }
.error { color: #8A2A1E; font-size: 14px; margin: 0; }
.busy { font-size: 13px; color: #5B6660; }
.form { width: 260px; flex-shrink: 0; background: #FBFBF8; border: 1px solid #C5CCC2; padding: 16px; display: flex; flex-direction: column; gap: 12px; border-radius: 4px; }
.form h2 { font-family: 'Newsreader', Georgia, serif; font-weight: 600; font-size: 18px; margin: 0; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.form label.check { flex-direction: row; align-items: center; gap: 8px; }
.form input:not([type=checkbox]) { font: inherit; font-size: 14px; padding: 6px 8px; border: 1px solid #AEB7AC; border-radius: 4px; background: #fff; width: 100%; }
@media (max-width: 760px) { .workspace { flex-direction: column; } .form { width: 100%; } }
.server { font-size: 13px; color: #5B6660; display: flex; align-items: center; gap: 8px; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #AEB7AC; display: inline-block; }
.dot.on { background: #2E5E4E; }
.note { font-size: 13px; color: #2E5E4E; margin: 0; }
.searchbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px 20px; background: #FBFBF8; border-bottom: 1px solid #C5CCC2; font-size: 14px; }
.searchbar input, .panel input[type=text] { font: inherit; font-size: 14px; padding: 6px 8px; border: 1px solid #AEB7AC; border-radius: 4px; background: #fff; min-width: 180px; }
.stack { position: relative; display: inline-block; }
.hl { position: absolute; pointer-events: none; background: rgba(255, 210, 0, .35); border: 1px solid rgba(190, 140, 0, .8); }
.hl.current { background: rgba(255, 120, 0, .35); border-color: #B45309; }
.panel { width: 280px; flex-shrink: 0; background: #FBFBF8; border: 1px solid #C5CCC2; padding: 16px; display: flex; flex-direction: column; gap: 12px; border-radius: 4px; font-size: 14px; }
.panel h2 { font-family: 'Newsreader', Georgia, serif; font-weight: 600; font-size: 18px; margin: 0; }
.panel label { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.panel label.stack-label { flex-direction: column; align-items: stretch; }
.panel dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; margin: 0; font-size: 13px; }
.panel dt { color: #5B6660; }
.panel dd { margin: 0; overflow-wrap: anywhere; }
.panel .meta { max-height: 180px; overflow: auto; }
@media (max-width: 760px) { .panel { width: 100%; } }
.editPop { position: absolute; width: 300px; z-index: 10; background: #FBFBF8; border: 1px solid #AEB7AC; border-radius: 6px; box-shadow: 0 6px 20px rgba(0, 0, 0, .18); padding: 12px; display: flex; flex-direction: column; gap: 10px; font-size: 14px; }
.editPop input[type=text] { font: inherit; font-size: 14px; padding: 6px 8px; border: 1px solid #AEB7AC; border-radius: 4px; background: #fff; width: 100%; }
.pop-title { font-weight: 600; overflow-wrap: anywhere; }
.pop-actions { display: flex; gap: 8px; align-items: center; }
.pop-warn { margin: 0; color: #8A2A1E; font-size: 13px; }
.modes { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.modes legend { font-size: 13px; color: #5B6660; padding: 0; margin-bottom: 4px; }
.modes .mode { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.modes .mode span { display: flex; flex-direction: column; gap: 2px; }
.modes .mode small { color: #5B6660; }
.modes .mode.off { opacity: .5; }
.modes .offline { margin: 0; font-size: 13px; color: #5B6660; }
.hint { margin: 0; font-size: 12px; color: #5B6660; }
.title svg { flex-shrink: 0; align-self: center; }
.hl.hover { background: rgba(46, 94, 78, .08); border: 1px dashed #2E5E4E; }
.zone { position: absolute; pointer-events: none; border: 1px dashed rgba(47, 111, 199, .6); background: rgba(47, 111, 199, .06); border-radius: 2px; }
.inline { position: absolute; z-index: 10; display: flex; flex-direction: column; gap: 4px; }
.inline-field { font-family: Helvetica, Arial, sans-serif; color: #000; background: #fff; border: 1px solid #2E5E4E; border-radius: 2px; padding: 0 3px; box-sizing: border-box; width: 100%; outline: none; box-shadow: 0 0 0 2px rgba(46, 94, 78, .25); line-height: 1.1; }
.inline-bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; background: #FBFBF8; border: 1px solid #AEB7AC; border-radius: 6px; padding: 6px; box-shadow: 0 4px 14px rgba(0, 0, 0, .15); font-size: 13px; }
.inline-bar button { font-size: 13px; padding: 4px 8px; }
.chip-cover, .chip-off { font-size: 12px; padding: 2px 8px; border-radius: 999px; }
.chip-cover { background: #FAEEDA; color: #633806; border: 1px solid #EF9F27; }
.chip-off { color: #5B6660; border: 1px dashed #AEB7AC; }
.seg { display: inline-flex; border: 1px solid #AEB7AC; border-radius: 6px; overflow: hidden; }
.seg button { border: 0; border-radius: 0; font-size: 12px; padding: 4px 8px; background: #FFFFFF; color: #1E2420; }
.seg button.on { background: #1E2420; color: #FBFBF8; }
`;

// Reads the AcroForm fields that pdf-lib can edit (text and checkboxes).
function readFields(doc) {
  return doc.getForm().getFields().flatMap((field) => {
    const name = field.getName();
    if (field instanceof PDFTextField) return [{ name, kind: "text", value: field.getText() ?? "" }];
    if (field instanceof PDFCheckBox) return [{ name, kind: "checkbox", value: field.isChecked() }];
    return [];
  });
}

// Finds the text run under a point in PDF user space (baseline origin, font size as height).
// Later runs are drawn on top, so search from the end.
function findTextAt(runs, x, y) {
  return [...runs].reverse().find((run) => {
    const [, , , , tx, ty] = run.transform;
    return (
      x >= tx &&
      x <= tx + run.width &&
      y >= ty - run.height * 0.25 &&
      y <= ty + run.height * 0.85
    );
  });
}

export default function PdfEditor() {
  const [bytes, setBytes] = useState(null);
  const [name, setName] = useState("");
  const [pageCount, setPageCount] = useState(0);
  const [pageIndex, setPageIndex] = useState(0);
  const [tool, setTool] = useState(null);
  const [fields, setFields] = useState([]);
  const [pdf, setPdf] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [textRuns, setTextRuns] = useState({ page: -1, runs: [] });

  // Server edition state.
  const [serverOn, setServerOn] = useState(false);
  const [search, setSearch] = useState(EMPTY_SEARCH);
  const [searchOpen, setSearchOpen] = useState(false);
  const [panel, setPanel] = useState(null); // null | "replace" | "clean" | "info"
  const [findText, setFindText] = useState("");
  const [replaceFind, setReplaceFind] = useState("");
  const [replaceWith, setReplaceWith] = useState("");
  const [cleanOptions, setCleanOptions] = useState(DEFAULT_CLEAN);
  const [info, setInfo] = useState(null);
  const [notice, setNotice] = useState("");
  const [renderTick, setRenderTick] = useState(0);
  const [editor, setEditor] = useState(null); // the open word or match editor, or null
  const [replaceMode, setReplaceMode] = useState("permanent");
  const [hover, setHover] = useState(null); // text run under the pointer when no tool is active
  const [showZones, setShowZones] = useState(false); // "Highlight text areas" is on
  const applying = useRef(false);
  const occCache = useRef(new Map()); // matches per upload id and text
  const openedFromFindReplace = useRef(false); // Replace panel: permanent or cover

  const canvasRef = useRef(null);
  const viewportRef = useRef(null);
  const dragStart = useRef(null);
  const openInput = useRef(null);
  const mergeInput = useRef(null);
  const history = useRef([]);
  const uploaded = useRef({ bytes: null, id: null });

  // Is the local backend running? If not, the server tools stay disabled.
  useEffect(() => {
    fetch("/api/health")
      .then((res) => setServerOn(res.ok))
      .catch(() => setServerOn(false));
  }, []);

  // Opened from Find & Replace with ?upload=<id>: load that uploaded PDF into the editor.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("upload");
    if (!id || openedFromFindReplace.current) return;
    openedFromFindReplace.current = true;
    fetch(`/api/v1/uploads/${encodeURIComponent(id)}/download?version=original`)
      .then(async (res) => {
        if (!res.ok) throw new Error("That upload has expired. Upload the PDF again.");
        setName(/filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "document.pdf");
        const doc = await PDFDocument.load(await res.arrayBuffer());
        history.current = [];
        await commit(doc, { keepValues: false, record: false });
      })
      .catch((err) => setError(err.message));
  }, []);

  // Load the current bytes into pdf.js for display.
  useEffect(() => {
    if (!bytes) return;
    let active = true;
    const loading = pdfjsLib.getDocument({ data: bytes.slice() });
    loading.promise
      .then((doc) => active && setPdf(doc))
      .catch((err) => active && setError(err.message));
    return () => {
      active = false;
      loading.destroy();
    };
  }, [bytes]);

  // Render the selected page to the canvas.
  useEffect(() => {
    if (!pdf || !canvasRef.current) return;
    let active = true;
    let task = null;
    pdf
      .getPage(pageIndex + 1)
      .then((page) => {
        if (!active) return;
        const viewport = page.getViewport({ scale: SCALE });
        const canvas = canvasRef.current;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        viewportRef.current = viewport;
        setRenderTick((tick) => tick + 1); // lets the search highlights position themselves
        setHover(null);
        task = page.render({ canvasContext: canvas.getContext("2d"), viewport });
        return task.promise;
      })
      .catch(() => {});
    return () => {
      active = false;
      task?.cancel();
    };
  }, [pdf, pageIndex]);

  // Load the text runs for the current page, used by the Edit text tool.
  useEffect(() => {
    if (!pdf) return;
    let active = true;
    pdf
      .getPage(pageIndex + 1)
      .then((page) => page.getTextContent())
      .then((content) => {
        if (!active) return;
        const runs = content.items.filter((item) => item.str.trim());
        setTextRuns({ page: pageIndex, runs });
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [pdf, pageIndex]);

  // Save the edited document and refresh state from it.
  async function commit(doc, { keepValues = true, record = true } = {}) {
    const next = await doc.save();
    const count = doc.getPageCount();
    const nextFields = readFields(doc);
    if (record && bytes) {
      history.current.push(bytes);
      if (history.current.length > HISTORY_LIMIT) history.current.shift();
    }
    setPageCount(count);
    setPageIndex((i) => Math.min(i, count - 1));
    setFields((prev) =>
      nextFields.map(
        (f) => (keepValues && prev.find((p) => p.name === f.name && p.kind === f.kind)) || f
      )
    );
    // Search hits point at the old bytes, so they are cleared with every change.
    setSearch(EMPTY_SEARCH);
    setInfo(null);
    setBytes(next);
  }

  // Replace the document with new bytes (a server result). It goes into undo history
  // unless it is an undo itself.
  async function loadBytes(next, { note = "", record = true } = {}) {
    const doc = await PDFDocument.load(next);
    await commit(doc, { keepValues: false, record });
    setEditor(null);
    setNotice(note);
  }

  function undo() {
    const previous = history.current.pop();
    if (!previous) return;
    setError("");
    setBusy(true);
    loadBytes(previous, { note: "Undid the last change.", record: false })
      .catch((err) => setError(err.message))
      .finally(() => setBusy(false));
  }

  // Apply a change to the current document. Returns true on success.
  async function edit(change, options) {
    setBusy(true);
    setError("");
    try {
      const doc = await PDFDocument.load(bytes);
      await change(doc);
      await commit(doc, options);
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function openFile(file) {
    setBusy(true);
    setError("");
    try {
      const doc = await PDFDocument.load(await file.arrayBuffer());
      setName(file.name);
      setPageIndex(0);
      setTool(null);
      history.current = [];
      setPanel(null);
      setEditor(null);
      setNotice("");
      await commit(doc, { keepValues: false, record: false });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function addPages(file) {
    return edit(async (doc) => {
      const other = await PDFDocument.load(await file.arrayBuffer());
      const pages = await doc.copyPages(other, other.getPageIndices());
      pages.forEach((page) => doc.addPage(page));
    });
  }

  function pick(event, handler) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) handler(file);
  }

  function rotatePage() {
    return edit((doc) => {
      const page = doc.getPage(pageIndex);
      page.setRotation(degrees((page.getRotation().angle + 90) % 360));
    });
  }

  async function movePage(step) {
    const target = pageIndex + step;
    const ok = await edit((doc) => {
      const page = doc.getPage(pageIndex);
      doc.removePage(pageIndex);
      doc.insertPage(target, page);
    });
    if (ok) setPageIndex(target);
  }

  function deletePage() {
    if (pageCount < 2 || !window.confirm("Delete this page?")) return;
    return edit((doc) => doc.removePage(pageIndex));
  }

  function setFieldValue(index, value) {
    setFields((prev) => prev.map((f, i) => (i === index ? { ...f, value } : f)));
  }

  function applyForm() {
    return edit((doc) => {
      const form = doc.getForm();
      for (const f of fields) {
        if (f.kind === "text") form.getTextField(f.name).setText(f.value);
        else if (f.value) form.getCheckBox(f.name).check();
        else form.getCheckBox(f.name).uncheck();
      }
      form.updateFieldAppearances();
    });
  }

  // Pointer handling: a click places text, a drag draws a highlight or whiteout box.
  function pointerPosition(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function onPointerDown(event) {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStart.current = pointerPosition(event);
  }

  // With no tool active, the text under the pointer is outlined and a click edits it.
  function onPointerMove(event) {
    if (tool || dragStart.current || textRuns.page !== pageIndex || !viewportRef.current) return;
    const point = pointerPosition(event);
    const [x, y] = viewportRef.current.convertToPdfPoint(point.x, point.y);
    setHover(findTextAt(textRuns.runs, x, y) ?? null);
  }

  // Outline box for the hovered text, positioned with the page viewport.
  function hoverBox() {
    const vp = viewportRef.current;
    if (!hover || tool || !vp || editor) return null;
    const { left, top, width, height } = runViewportBox(vp, hover);
    return { left, top, width, height };
  }

  async function onPointerUp(event) {
    const start = dragStart.current;
    dragStart.current = null;
    if (!start) return;

    const end = pointerPosition(event);
    const vp = viewportRef.current;
    const [x1, y1] = vp.convertToPdfPoint(start.x, start.y);
    const [x2, y2] = vp.convertToPdfPoint(end.x, end.y);

    // With no tool, a click (not a drag) on text opens the editor.
    if (!tool) {
      const clicked = Math.hypot(end.x - start.x, end.y - start.y) <= 4;
      const run = clicked && textRuns.page === pageIndex ? findTextAt(textRuns.runs, x1, y1) : null;
      if (run) openRunEditor(run, start);
      return;
    }

    if (tool === "text") {
      openAddEditor(x1, y1);
      return;
    }

    const box = {
      x: Math.min(x1, x2),
      y: Math.min(y1, y2),
      width: Math.abs(x2 - x1),
      height: Math.abs(y2 - y1),
    };
    if (box.width < 2 || box.height < 2) return;

    const highlight = tool === "highlight";
    await edit((doc) => {
      doc.getPage(pageIndex).drawRectangle({
        ...box,
        color: highlight ? rgb(1, 0.93, 0.3) : rgb(1, 1, 1),
        opacity: highlight ? 0.4 : 1,
      });
    });
  }

  // Opens the in-place editor on a text run.
  function openRunEditor(run) {
    const vp = viewportRef.current;
    if (!vp) return;
    setEditor({
      id: Math.random(),
      kind: "run",
      original: run.str,
      value: run.str,
      mode: serverOn ? "permanent" : "cover",
      page: pageIndex + 1,
      run,
      box: runViewportBox(vp, run),
      blocked: "",
    });
  }

  // Opens the in-place editor on the current Find match.
  async function openHitEditor(hit) {
    if (!pdf || !hit || !viewportRef.current) return;
    const page = await pdf.getPage(hit.page);
    setEditor({
      id: Math.random(),
      kind: "hit",
      original: search.find,
      value: replaceWith,
      mode: serverOn ? "permanent" : "cover",
      page: hit.page,
      hit,
      box: hitViewportBox(viewportRef.current, hit),
      blocked: page.rotate ? "Cover only doesn't work on rotated pages yet." : "",
    });
  }

  // Opens an empty in-place box for the Text tool, at a point in PDF user space.
  function openAddEditor(x, y) {
    const vp = viewportRef.current;
    if (!vp) return;
    const [left, baseline] = vp.convertToViewportPoint(x, y);
    setEditor({
      id: Math.random(),
      kind: "add",
      original: "",
      value: "",
      mode: "cover",
      page: pageIndex + 1,
      point: { x, y },
      box: { left, top: baseline - 12 * vp.scale, width: 200, height: 12 * vp.scale + 4, fontPx: 12 * vp.scale },
      blocked: "",
    });
  }

  // Runs a server step: busy state and error text. Returns whether it worked.
  async function attempt(job) {
    setBusy(true);
    setError("");
    try {
      await job();
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  // Matches of a text in the uploaded file, cached per upload.
  async function occurrencesOf(id, find) {
    const key = `${id}\u0000${find}`;
    if (!occCache.current.has(key)) {
      const data = await (await serverCall(`/api/v1/uploads/${id}/occurrences`, postJson({ find }))).json();
      occCache.current.set(key, data.occurrences);
    }
    return occCache.current.get(key);
  }

  // Permanent: removes one match from the file and writes the new text in its place.
  // The match is the one nearest the edited text on the same page.
  async function permanentEdit(current, text) {
    const find = current.original;
    const id = await uploadId();
    const occurrences = await occurrencesOf(id, find);
    const [cx, cy, size] =
      current.kind === "run"
        ? [
            current.run.transform[4] + current.run.width / 2,
            current.run.transform[5] + current.run.height * 0.3,
            current.run.height,
          ]
        : [
            (current.hit.box[0] + current.hit.box[2]) / 2,
            (current.hit.box[1] + current.hit.box[3]) / 2,
            current.hit.box[3] - current.hit.box[1],
          ];
    const pick = nearestMatch(occurrences, current.page, cx, cy, size);
    if (!pick) throw new Error("This text can't be changed permanently here. Use Cover only.");
    const result = await (
      await serverCall(
        `/api/v1/uploads/${id}/replace-occurrence`,
        postJson({ find, replace: text, page: pick.page, index: pick.index })
      )
    ).json();
    const file = await (await serverCall(result.download_url)).arrayBuffer();
    await loadBytes(new Uint8Array(file), {
      note: text ? `Permanently replaced "${find}" with "${text}".` : `Permanently removed "${find}".`,
    });
  }

  // Applies the in-place edit. Permanent removes the original from the file; Cover only hides it;
  // the Text tool adds new text.
  async function applyEditor(text = editor?.value ?? "") {
    if (!editor || applying.current) return;
    if (editor.mode === "cover" && editor.blocked) return;
    const current = editor;
    const close = () => setEditor((open) => (open?.id === current.id ? null : open));
    if (current.kind !== "add" && text === current.original) return close();
    if (current.kind === "add" && !text) return close();

    applying.current = true;
    try {
      let ok;
      if (current.kind === "add") {
        ok = await edit(async (doc) => {
          const font = await doc.embedFont(StandardFonts.Helvetica);
          doc.getPage(current.page - 1).drawText(text, {
            x: current.point.x,
            y: current.point.y,
            size: 12,
            font,
            color: rgb(0.12, 0.3, 0.6),
          });
        });
        if (ok) setNotice(`Added "${text}".`);
      } else if (current.mode === "permanent") {
        ok = await attempt(() => permanentEdit(current, text));
      } else {
        ok = await edit((doc) =>
          current.kind === "run"
            ? drawCover(doc, current.page - 1, current.run, text)
            : drawCoverHit(doc, current.hit, text)
        );
        if (ok) {
          setNotice(`Covered "${current.original}"${text ? ` with "${text}"` : ""}. The original text is still in the file.`);
        }
      }
      if (ok) close();
    } finally {
      applying.current = false;
    }
  }

  // Covers every Find match with the new text. Visual only: the original text stays in the file.
  function coverAllMatches(find, replacement) {
    return serverJob(async () => {
      const id = await uploadId();
      const found = await (await serverCall(`/api/v1/uploads/${id}/search`, postJson({ find }))).json();
      if (!found.count) {
        setError(`"${find}" was not found.`);
        return;
      }
      const usable = [];
      for (const hit of found.hits) {
        const page = await pdf.getPage(hit.page);
        if (!page.rotate) usable.push(hit);
      }
      if (!usable.length) {
        setError("All matches are on rotated pages. Cover only doesn't support those yet; use Permanent.");
        return;
      }
      const skipped = found.hits.length - usable.length;
      const doc = await PDFDocument.load(bytes);
      for (const hit of usable) await drawCoverHit(doc, hit, replacement);
      await loadBytes(await doc.save(), {
        note: `Covered ${usable.length} match(es) with "${replacement}". The original text is still in the file.${
          skipped ? ` ${skipped} on rotated pages were skipped; use Permanent for those.` : ""
        }`,
      });
      setPanel(null);
    });
  }

  function download() {
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name.replace(/\.pdf$/i, "")}-edited.pdf`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---- Server tools (local backend) ------------------------------------------

  async function serverCall(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let message = `Server error ${res.status}`;
      try {
        const data = await res.json();
        if (Array.isArray(data.detail)) message = data.detail.map((item) => item.msg).join("; ");
        else if (data.detail) message = data.detail;
      } catch (_) {
        /* keep the generic message */
      }
      throw new Error(message);
    }
    return res;
  }

  const postJson = (body) => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  // Upload the current bytes once. The id is reused until the document changes.
  async function uploadId() {
    if (uploaded.current.bytes === bytes && uploaded.current.id) return uploaded.current.id;
    const form = new FormData();
    form.append("file", new Blob([bytes], { type: "application/pdf" }), name || "document.pdf");
    const created = await (await serverCall("/api/v1/uploads", { method: "POST", body: form })).json();
    uploaded.current = { bytes, id: created.id };
    return created.id;
  }

  // Runs one server job with the same busy and error handling as the editing tools.
  async function serverJob(job) {
    setBusy(true);
    setError("");
    try {
      await job();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function runSearch(find) {
    if (!find) {
      setSearch(EMPTY_SEARCH);
      return;
    }
    return serverJob(async () => {
      const id = await uploadId();
      const result = await (
        await serverCall(`/api/v1/uploads/${id}/search`, postJson({ find }))
      ).json();
      setSearch({
        find,
        count: result.count,
        hits: result.hits,
        index: result.hits.length ? 0 : -1,
        truncated: result.truncated,
      });
      if (result.hits.length) setPageIndex(result.hits[0].page - 1);
      setNotice(result.count ? "" : `"${find}" was not found in the text layer.`);
    });
  }

  function stepSearch(step) {
    const total = search.hits.length;
    if (!total) return;
    const index = (search.index + step + total) % total;
    setSearch({ ...search, index });
    setPageIndex(search.hits[index].page - 1);
  }

  // Every text area on the current page, outlined while "Highlight text areas" is on.
  function zoneBoxes() {
    const vp = viewportRef.current;
    if (!vp || textRuns.page !== pageIndex) return [];
    return textRuns.runs.map((run, index) => {
      const { left, top, width, height } = runViewportBox(vp, run);
      return { key: index, left, top, width, height };
    });
  }

  // Highlight boxes for the hits on the current page, positioned with the page viewport.
  function highlightBoxes() {
    const vp = viewportRef.current;
    if (!vp || !search.hits.length) return [];
    const boxes = [];
    search.hits.forEach((hit, index) => {
      if (hit.page !== pageIndex + 1) return;
      const [x1, y1, x2, y2] = vp.convertToViewportRectangle(hit.box);
      boxes.push({
        key: index,
        current: index === search.index,
        left: Math.min(x1, x2),
        top: Math.min(y1, y2),
        width: Math.abs(x2 - x1),
        height: Math.abs(y2 - y1),
      });
    });
    return boxes;
  }

  function replaceAll() {
    const find = replaceFind;
    const replacement = replaceWith;
    if (!find) {
      setError("Enter the text to replace.");
      return;
    }
    return serverJob(async () => {
      const id = await uploadId();
      const result = await (
        await serverCall(`/api/v1/uploads/${id}/replace`, postJson({ find, replace: replacement }))
      ).json();
      const file = await (await serverCall(result.download_url)).arrayBuffer();
      await loadBytes(new Uint8Array(file), {
        note: `Permanently replaced ${result.replacements} occurrence(s). ${result.remaining} left in the document.`,
      });
      setPanel(null);
    });
  }

  function cleanDocument() {
    return serverJob(async () => {
      const id = await uploadId();
      const result = await (
        await serverCall(`/api/v1/uploads/${id}/clean`, postJson(cleanOptions))
      ).json();
      const file = await (await serverCall(result.download_url)).arrayBuffer();
      const kb = (bytesCount) => `${(bytesCount / 1024).toFixed(1)} KB`;
      const removed = result.removed.length ? `Removed: ${result.removed.join(", ")}.` : "Nothing to remove.";
      await loadBytes(new Uint8Array(file), {
        note: `Cleaned: ${kb(result.size_before)} → ${kb(result.size_after)}. ${removed}`,
      });
    });
  }

  function loadInfo() {
    return serverJob(async () => {
      const id = await uploadId();
      setInfo(await (await serverCall(`/api/v1/uploads/${id}/inspect`)).json());
    });
  }

  function togglePanel(which) {
    if (panel === which) {
      setPanel(null);
      return;
    }
    setPanel(which);
    if (which === "info") loadInfo();
  }

  const yesNo = (value) => (value ? "Yes" : "No");
  const hasDoc = Boolean(bytes);

  return (
    <div className="desk">
      <style>{STYLE}</style>

      <header className="top">
        <div className="title">
          <IconFilePencil size={22} stroke={2} color="#2E5E4E" aria-hidden="true" />
          <h1>Edit PDF</h1>
          <span className="filename">{name || "No file open"}</span>
          {busy && <span className="busy">Working…</span>}
        </div>
        <div className="server" title="Search, replace, clean and info run on the local backend">
          <span className={`dot${serverOn ? " on" : ""}`} aria-hidden="true" />
          {serverOn ? "Local server connected" : "Local server offline (editing still works)"}
        </div>
        <div className="actions">
          <button onClick={() => openInput.current.click()}>Open</button>
          <button disabled={!hasDoc || busy} onClick={() => mergeInput.current.click()}>
            Add pages
          </button>
          <button disabled={!hasDoc || busy || history.current.length === 0} onClick={undo}>
            Undo
          </button>
          <button className="primary" disabled={!hasDoc || busy} onClick={download}>
            Download
          </button>
        </div>
        <input
          ref={openInput}
          type="file"
          accept="application/pdf"
          hidden
          onChange={(e) => pick(e, openFile)}
        />
        <input
          ref={mergeInput}
          type="file"
          accept="application/pdf"
          hidden
          onChange={(e) => pick(e, addPages)}
        />
      </header>

      {hasDoc && (
        <div className="toolbar">
          <div className="group" role="group" aria-label="Annotate">
            <button
              aria-pressed={showZones}
              className={showZones ? "on" : ""}
              title="Outline every text area on this page"
              onClick={() => setShowZones(!showZones)}
            >
              Highlight text areas
            </button>
            {TOOLS.map((t) => (
              <button
                key={t.id}
                aria-pressed={tool === t.id}
                className={tool === t.id ? "on" : ""}
                onClick={() => setTool(tool === t.id ? null : t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="group" role="group" aria-label="Pages">
            <button disabled={busy} onClick={rotatePage}>Rotate</button>
            <button disabled={busy || pageIndex === 0} onClick={() => movePage(-1)}>
              Move left
            </button>
            <button disabled={busy || pageIndex >= pageCount - 1} onClick={() => movePage(1)}>
              Move right
            </button>
            <button disabled={busy || pageCount < 2} onClick={deletePage}>Delete</button>
          </div>
          <div className="group" role="group" aria-label="Server tools">
            <button
              disabled={!serverOn || busy}
              aria-pressed={searchOpen}
              className={searchOpen ? "on" : ""}
              title={serverOn ? "Find text on the server and highlight every match" : "Start the local backend first"}
              onClick={() => setSearchOpen(!searchOpen)}
            >
              Find
            </button>
            <button
              disabled={!serverOn || busy}
              className={panel === "replace" ? "on" : ""}
              title="Replace text in the whole document on the server"
              onClick={() => togglePanel("replace")}
            >
              Replace
            </button>
            <button
              disabled={!serverOn || busy}
              className={panel === "clean" ? "on" : ""}
              title="Remove metadata, JavaScript and attachments, and compress"
              onClick={() => togglePanel("clean")}
            >
              Clean
            </button>
            <button
              disabled={!serverOn || busy}
              className={panel === "info" ? "on" : ""}
              title="Metadata, JavaScript, attachments and form fields"
              onClick={() => togglePanel("info")}
            >
              Info
            </button>
          </div>
        </div>
      )}

      {hasDoc && searchOpen && (
        <form
          className="searchbar"
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            runSearch(findText);
          }}
        >
          <input
            type="text"
            aria-label="Text to find on the server"
            placeholder="Find text"
            value={findText}
            onChange={(event) => setFindText(event.target.value)}
          />
          <button type="submit" disabled={busy || !findText}>Search</button>
          <button type="button" disabled={!search.hits.length} onClick={() => stepSearch(-1)}>
            Previous
          </button>
          <button type="button" disabled={!search.hits.length} onClick={() => stepSearch(1)}>
            Next
          </button>
          <span>
            {search.hits.length
              ? `${search.index + 1} of ${search.count}${search.truncated ? ` (showing first ${search.hits.length})` : ""}`
              : search.find && !search.count
                ? "No matches"
                : ""}
          </span>
          <button
            type="button"
            disabled={!search.hits.length || busy}
            onClick={() => openHitEditor(search.hits[search.index])}
          >
            Replace this match
          </button>
          <button
            type="button"
            disabled={!search.find && !findText}
            onClick={() => {
              setFindText("");
              setSearch(EMPTY_SEARCH);
            }}
          >
            Clear
          </button>
        </form>
      )}

      <main className="workspace">
        <section className="pageArea">
          {hasDoc ? (
            <>
              <div className="canvasWrap">
                <div className="stack">
                  <canvas
                    ref={canvasRef}
                    className={tool ? `tool ${tool}` : ""}
                    onPointerDown={onPointerDown}
                    onPointerUp={onPointerUp}
                    onPointerMove={onPointerMove}
                    onPointerLeave={() => setHover(null)}
                    style={{ cursor: hover && !tool ? "text" : "default" }}
                  />
                  {highlightBoxes().map((box) => (
                    <div
                      key={box.key}
                      className={box.current ? "hl current" : "hl"}
                      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
                    />
                  ))}
                  {showZones &&
                    zoneBoxes().map((zone) => (
                      <div
                        key={zone.key}
                        className="zone"
                        style={{ left: zone.left, top: zone.top, width: zone.width, height: zone.height }}
                      />
                    ))}
                  {hoverBox() && <div className="hl hover" style={hoverBox()} />}
                  {editor && (
                    <InlineEditor
                      editor={editor}
                      serverOn={serverOn}
                      busy={busy}
                      onValue={(value) => setEditor((current) => ({ ...current, value }))}
                      onMode={(mode) => setEditor((current) => ({ ...current, mode }))}
                      onApply={(text) => applyEditor(text)}
                      onCancel={() => setEditor(null)}
                    />
                  )}
                </div>
              </div>
              <nav className="pager">
                <button disabled={pageIndex === 0} onClick={() => setPageIndex((i) => i - 1)}>
                  Previous
                </button>
                <span>
                  Page {pageIndex + 1} of {pageCount}
                </span>
                <button
                  disabled={pageIndex >= pageCount - 1}
                  onClick={() => setPageIndex((i) => i + 1)}
                >
                  Next
                </button>
              </nav>
            </>
          ) : (
            <div className="empty">
              <p>Open a PDF to start marking it up.</p>
              <button className="primary" onClick={() => openInput.current.click()}>
                Choose a PDF
              </button>
            </div>
          )}
          {error && <p role="alert" className="error">{error}</p>}
          {notice && <p role="status" className="note">{notice}</p>}
        </section>

        {fields.length > 0 && (
          <aside className="form">
            <h2>Form fields</h2>
            {fields.map((f, i) =>
              f.kind === "text" ? (
                <label key={f.name}>
                  <span>{f.name}</span>
                  <input value={f.value} onChange={(e) => setFieldValue(i, e.target.value)} />
                </label>
              ) : (
                <label key={f.name} className="check">
                  <input
                    type="checkbox"
                    checked={f.value}
                    onChange={(e) => setFieldValue(i, e.target.checked)}
                  />
                  <span>{f.name}</span>
                </label>
              )
            )}
            <button className="primary" disabled={busy} onClick={applyForm}>
              Apply values
            </button>
          </aside>
        )}

        {panel === "replace" && (
          <aside className="panel">
            <h2>Replace text</h2>
            <label className="stack-label">
              Find
              <input
                type="text"
                value={replaceFind}
                onChange={(event) => setReplaceFind(event.target.value)}
              />
            </label>
            <label className="stack-label">
              Replace with
              <input
                type="text"
                value={replaceWith}
                onChange={(event) => setReplaceWith(event.target.value)}
              />
            </label>
            <ModeSelector mode={replaceMode} onMode={setReplaceMode} serverOn={serverOn} />
            <button
              className="primary"
              disabled={busy || !replaceFind}
              onClick={() =>
                replaceMode === "permanent" ? replaceAll() : coverAllMatches(replaceFind, replaceWith)
              }
            >
              {replaceMode === "permanent" ? "Replace all permanently" : "Cover all matches"}
            </button>
            <p className="hint">Undo reverts either choice.</p>
          </aside>
        )}

        {panel === "clean" && (
          <aside className="panel">
            <h2>Clean on server</h2>
            {CLEAN_OPTIONS.map((option) => (
              <label key={option.id}>
                <input
                  type="checkbox"
                  checked={cleanOptions[option.id]}
                  onChange={(event) =>
                    setCleanOptions({ ...cleanOptions, [option.id]: event.target.checked })
                  }
                />
                {option.label}
              </label>
            ))}
            <button className="primary" disabled={busy} onClick={cleanDocument}>
              Clean document
            </button>
          </aside>
        )}

        {panel === "info" && (
          <aside className="panel">
            <h2>Document info</h2>
            {info ? (
              <>
                <dl>
                  <dt>Pages</dt>
                  <dd>{info.pages}</dd>
                  <dt>Size</dt>
                  <dd>{(info.size / 1024).toFixed(1)} KB</dd>
                  <dt>First page</dt>
                  <dd>
                    {info.first_page_size[0]} × {info.first_page_size[1]} pt
                  </dd>
                  <dt>PDF version</dt>
                  <dd>{info.pdf_version}</dd>
                  <dt>Encrypted</dt>
                  <dd>{yesNo(info.encrypted)}</dd>
                  <dt>XMP metadata</dt>
                  <dd>{yesNo(info.has_xmp)}</dd>
                  <dt>JavaScript</dt>
                  <dd>{info.javascript_actions} action(s)</dd>
                  <dt>Attachments</dt>
                  <dd>{info.embedded_files}</dd>
                  <dt>Form fields</dt>
                  <dd>{info.form_fields}</dd>
                  <dt>Annotations</dt>
                  <dd>{info.annotations}</dd>
                </dl>
                <h2 style={{ fontSize: 15 }}>Metadata</h2>
                <dl className="meta">
                  {Object.keys(info.metadata).length === 0 && <dd>None</dd>}
                  {Object.entries(info.metadata).map(([key, value]) => (
                    <div key={key} style={{ display: "contents" }}>
                      <dt>{key}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              </>
            ) : (
              <button disabled={busy} onClick={loadInfo}>Load info</button>
            )}
          </aside>
        )}
      </main>
      <footer className="credit">
        Created by Alex · MIT License · <a href="https://github.com/alexander-uspenskiy/PDFEditor" target="_blank" rel="noreferrer">GitHub</a>
      </footer>
    </div>
  );
}
