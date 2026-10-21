# PDF editor and find & replace (local backend)

A local web app that combines the browser PDF editor with tools that run on a local Python service. The root `PdfEditor.jsx` app is unchanged; this folder has its own copy with the server tools added.

Files are sent to the server running on this machine. Keep the server bound to `127.0.0.1` so nothing leaves your computer.

## Run it

Requirements: Python 3.10+ and Node.js 18+ (only needed the first time, to build the editor).

```bash
cd backend
PORT=8000 ./run_local.sh
```

The first run creates `.venv`, installs the Python packages, and builds the editor into `static/app/`. Later runs start in a few seconds. If port 8000 is taken, use another, for example `PORT=8080`.

| URL | What it is |
|---|---|
| `http://127.0.0.1:PORT/` | Redirects to the PDF editor |
| `http://127.0.0.1:PORT/app/` | PDF editor with server tools |
| `http://127.0.0.1:PORT/docs` | API reference |

To rebuild the editor after changing `frontend/src`:

```bash
cd backend/frontend && npm install && npm run build
```

## The editor

Everything the browser version does is here: open, add pages, edit text (click a word, replace it), add text, highlight, whiteout, rotate, move and delete pages, form fields, previous/next, download.

Added in this version:

- **Undo**: every change (editing or server) can be undone, up to 30 steps. Opening a new file clears the history.
- **Find**: searches the whole document on the server and highlights every match on its page, with Previous and Next. Spaces and line breaks are ignored, so names on rotated pages and names broken across lines are found.
- **Replace**: replaces text in the whole document on the server and loads the result. The replacement is checked before it is returned, so a name that remains is reported as an error and the document is left as it was.
- **Clean**: removes document info and XMP metadata, JavaScript actions, embedded files and attachments, and optionally compresses the file. The result is checked before it is returned. Form fields are kept.
- **Info**: pages, size, PDF version, encryption, metadata fields, JavaScript count, attachments, form fields and annotations.

The server tools are disabled when the local server is not running; the editing tools keep working without it. The header shows whether the server is connected.

## Editing text in place

Click a word in the page to edit it in place. Enter applies, Escape cancels, and clicking away applies a changed text. **Highlight text areas** outlines every editable text on the page.

- **Permanent** (default when the server is running): removes that one match from the file and writes the new text in its place. The match is the one nearest the clicked text on that page. Other copies of the same text are left in the file.
- **Cover only**: hides the original on screen. The original text stays in the file, so copy and search still find it.
- **Text tool**: click the page, type, and press Enter to add text.

A click picks a whole text run (usually a phrase or a line), so Delete and Permanent act on that whole run.

## API

All endpoints are under `/api/v1`. An upload gets an `id` that the later calls use.

| Method and path | Purpose |
|---|---|
| `POST /uploads` | Multipart `file`. Returns `id`, `format` and `details`. |
| `GET /uploads/{id}/inspect` | PDF info (see the Info panel). |
| `POST /uploads/{id}/search` | JSON `{"find": "..."}`. Returns `count`, `hits` (page, box in PDF points, context) and `truncated`. |
| `POST /uploads/{id}/clean` | JSON `{"strip_metadata", "strip_javascript", "strip_embedded_files", "compress"}` (all optional, default true). Returns `removed`, sizes and `download_url`. |
| `POST /uploads/{id}/find` | JSON `{"find": "..."}`. Returns `count` and `excerpts`. |
| `POST /uploads/{id}/replace` | JSON `{"find", "replace"}`. Returns counts, excerpts and `download_url`. |
| `POST /uploads/{id}/occurrences` | JSON `{"find": "..."}`. Returns every match with `page`, `index` and `box` (PDF points). Changes nothing. |
| `POST /uploads/{id}/replace-occurrence` | JSON `{"find", "replace", "page", "index"}`. Replaces or removes one match (empty `replace` removes it). Returns `other_copies`, `download_url`. |
| `GET /uploads/{id}/download?version=original\|result` | The file. |
| `GET /uploads/{id}/render?version=original\|result&page=N` | PDF page as PNG. |

Errors use `{"detail": "..."}`:

- `400`: empty or unusable text, or a PDF tool called on a file that isn't a PDF.
- `404`: upload expired or unknown, text not found, or no result yet.
- `409`: the rewritten file failed its checks, so no file was returned.
- `413` / `415`: too large (over 25 MB), or an unsupported file type.
- `422`: a PDF feature the engine does not support.

The original endpoint `POST /api/replace-name` (PDF only, `file`, `old_name`, `new_name`) still works.

## MCP server

`mcp_server.py` gives an AI client the editor's options as [Model Context Protocol](https://modelcontextprotocol.io) tools, so it can edit PDF files on this computer. It runs over stdio, so it opens no port and doesn't need the web server.

Set it up once:

```bash
cd backend
python3 -m venv .venv                       # skip if .venv already exists
.venv/bin/pip install -r requirements-mcp.txt
```

Then add it to your MCP client's configuration, using absolute paths:

```json
{
  "mcpServers": {
    "pdf-editor": {
      "command": "/absolute/path/to/PDFEditor/backend/.venv/bin/python",
      "args": ["/absolute/path/to/PDFEditor/backend/mcp_server.py"]
    }
  }
}
```

There is one tool for each editor option:

| Editor option | Tool | What it does |
|---|---|---|
| Info | `pdf_info` | Pages, size, metadata, JavaScript, attachments, form fields, annotations. |
| Find | `pdf_find` | Counts text and returns each hit with its page and box. |
| (for the edit tools) | `pdf_occurrences` | Lists every match with the `page`, `index` and `box` that the edit tools take. |
| Form fields | `pdf_form_fields`, `pdf_fill_form` | Reads, then sets, text fields and checkboxes by name. |
| (check your work) | `pdf_render_page` | Renders a page as a PNG. |
| Replace | `pdf_replace` | Replaces text across the whole document. |
| Edit text: Permanent | `pdf_replace_occurrence` | Removes one match from the file and writes the new text. Empty `replace` removes it. |
| Edit text: Cover only | `pdf_cover_occurrence` | Hides one match under a white box. The original characters stay in the file. Not for rotated pages. |
| Text | `pdf_add_text` | Adds a one-line blue note, 12 pt Helvetica, at a point. |
| Highlight | `pdf_highlight` | Adds a translucent yellow rectangle. |
| Whiteout | `pdf_whiteout` | Adds a white rectangle. The content underneath stays in the file. |
| Rotate | `pdf_rotate_page` | Turns a page 90° clockwise per `turns`. |
| Move | `pdf_move_page` | Moves a page to a new position. |
| Delete | `pdf_delete_page` | Removes a page. A PDF keeps at least one page. |
| Add pages | `pdf_append_pages` | Appends every page of another PDF. |
| Clean | `pdf_clean` | Strips metadata, JavaScript and embedded files, and optionally compresses. Form fields are kept. |

How the tools handle files:

- A tool that changes a PDF writes a new file beside the input, named `<name>-edited.pdf` (then `-edited-2.pdf`, and so on). The input is never changed, and an existing file is never overwritten. Pass `output_path` to choose the name.
- Pass a tool's `output_path` to the next call to chain edits. There is no undo, so keep the original file.
- Coordinates are PDF points with the origin at the bottom left. Boxes are `[x0, y0, x1, y1]`, as `pdf_find` and `pdf_occurrences` return them.
- Use absolute paths. Files are limited to 25 MB, the same as the web app.
- A refused edit returns its reason, for example a missing page, or text that the standard fonts can't write.

## Limits

- **Replace (PDF)**: the text must sit inside one text run. A name split across runs is not matched. Composite fonts, custom `/Differences` encodings, encrypted files, and names in form fields or annotations are not supported. The replacement is always non-empty.
- **Find and Search**: matching is case-sensitive. Text is found through the text layer, so text inside images is not found. Matches that span lines are highlighted as one box covering the whole match.
- **Clean**: does not remove links, form fields, or tagged-PDF structure. Compression rewrites the file, so the output is not byte-identical to the input.
- **Single process**: uploads and results are kept in memory and a temp folder for one hour. Run one worker.

## Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

The tests cover PDF replace (including rotated pages), search positions, cleaning (JavaScript, attachments, metadata, form fields kept), and the API routes and error codes. `tests/test_editor_ops.py` covers the page, annotation and form options, and `tests/test_mcp_server.py` covers the MCP tools.

## Files

- `main.py`: API routes and the editor mount.
- `mcp_server.py`: MCP server that exposes the editor's options as tools.
- `editor_ops.py`: page, annotation and form operations for the MCP server (pikepdf).
- `replace_name.py`: PDF name replacement engine (content-stream based, verified before output).
- `formats.py`: format detection and find/replace.
- `pdf_tools.py`: inspect, clean and search with positions.
- `jobs.py`: uploads and results kept for one hour.
- `frontend/`: the editor (React + pdf.js + pdf-lib). `frontend/src/PdfEditor.jsx` starts from the root `PdfEditor.jsx` and adds the server tools.
- `run_local.sh`: sets up and starts everything.
