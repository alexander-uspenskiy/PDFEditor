import { useEffect, useRef, useState } from "react";
import { PDFDocument, PDFCheckBox, PDFTextField, StandardFonts, degrees, rgb } from "pdf-lib";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

const SCALE = 1.5;
const TOOLS = [
  { id: "edit", label: "Edit text" },
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

  const canvasRef = useRef(null);
  const viewportRef = useRef(null);
  const dragStart = useRef(null);
  const openInput = useRef(null);
  const mergeInput = useRef(null);

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
  async function commit(doc, { keepValues = true } = {}) {
    const next = await doc.save();
    const count = doc.getPageCount();
    const nextFields = readFields(doc);
    setPageCount(count);
    setPageIndex((i) => Math.min(i, count - 1));
    setFields((prev) =>
      nextFields.map(
        (f) => (keepValues && prev.find((p) => p.name === f.name && p.kind === f.kind)) || f
      )
    );
    setBytes(next);
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
      await commit(doc, { keepValues: false });
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
    if (!tool) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStart.current = pointerPosition(event);
  }

  async function onPointerUp(event) {
    const start = dragStart.current;
    dragStart.current = null;
    if (!tool || !start) return;

    const end = pointerPosition(event);
    const vp = viewportRef.current;
    const [x1, y1] = vp.convertToPdfPoint(start.x, start.y);
    const [x2, y2] = vp.convertToPdfPoint(end.x, end.y);

    if (tool === "edit") {
      const run = textRuns.page === pageIndex ? findTextAt(textRuns.runs, x1, y1) : null;
      if (run) {
        await replaceText(run);
      } else {
        setError("No text under that spot. Click directly on a word.");
      }
      return;
    }

    if (tool === "text") {
      const text = window.prompt("Text to add:");
      if (!text) return;
      await edit(async (doc) => {
        const font = await doc.embedFont(StandardFonts.Helvetica);
        doc.getPage(pageIndex).drawText(text, {
          x: x1,
          y: y1,
          size: 12,
          font,
          color: rgb(0.12, 0.3, 0.6),
        });
      });
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

  // Covers a text run with a white box and writes the replacement on top.
  // An empty replacement just removes the visible text.
  function replaceText(run) {
    const replacement = window.prompt("Replace text with:", run.str);
    if (replacement === null || replacement === run.str) return;
    const [, , , , x, y] = run.transform;
    const size = run.height;
    return edit(async (doc) => {
      const page = doc.getPage(pageIndex);
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

  const hasDoc = Boolean(bytes);

  return (
    <div className="desk">
      <style>{STYLE}</style>

      <header className="top">
        <div className="title">
          <h1>PDF Desk</h1>
          <span className="filename">{name || "No file open"}</span>
          {busy && <span className="busy">Working…</span>}
        </div>
        <div className="actions">
          <button onClick={() => openInput.current.click()}>Open</button>
          <button disabled={!hasDoc || busy} onClick={() => mergeInput.current.click()}>
            Add pages
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
        </div>
      )}

      <main className="workspace">
        <section className="pageArea">
          {hasDoc ? (
            <>
              <div className="canvasWrap">
                <canvas
                  ref={canvasRef}
                  className={tool ? `tool ${tool}` : ""}
                  onPointerDown={onPointerDown}
                  onPointerUp={onPointerUp}
                />
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
      </main>
    </div>
  );
}
