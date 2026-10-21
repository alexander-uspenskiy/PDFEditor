# PDF Editor

A PDF editor with two ways to run it:

- **Browser app:** a single React component. Files are opened, edited, and downloaded entirely in the browser, and nothing is uploaded.
- **Local backend (optional):** a Python server and an [MCP server](backend/mcp_server.py) on your own computer. They add document-wide find and replace, cleaning, search with positions, per-match editing, a document info panel, and PDF tools for AI clients. Files are processed on this machine and never leave it.

## Features

- **Open** a PDF from your computer and **download** the edited result as `<name>-edited.pdf`. The original file is never modified.
- **Edit text:** click a word, enter the replacement, and the original is covered with a white box while the new text is drawn on top. Clearing the field removes the visible text.
- **Text:** click to place a short note in blue, 12 pt Helvetica.
- **Highlight** and **Whiteout:** drag a rectangle on the page.
- **Rotate** the current page by 90°.
- **Move** the current page left or right.
- **Delete** the current page (asks for confirmation).
- **Add pages:** append all pages from another PDF to the end of the current one.
- **Form fields:** text fields and checkboxes are listed in a side panel. Edit them and click **Apply values** to write them into the PDF.
- **Navigate** between pages with Previous and Next.

## Requirements

- React
- A Vite-based project. The pdf.js worker is imported with Vite's `?url` syntax. Other bundlers need a different worker setup.
- Node.js, only if you want to run the tests.

## Installation

```bash
npm install pdf-lib pdfjs-dist@4
```

pdfjs-dist must be version 4 or later. The tests use its `legacy` build, which runs in Node.

## Usage

```jsx
import PdfEditor from "./PdfEditor";

export default function App() {
  return <PdfEditor />;
}
```

The component takes no props. It manages its own state, and it works offline once the page has loaded. The only external request is the Google Fonts stylesheet at the top of the component's styles. If that request is blocked, the page falls back to system fonts.

## Local backend and MCP server

The `backend/` folder is a local Python service. It serves its own copy of the editor at `http://127.0.0.1:8000/app/`, with the server tools added, and it exposes an API under `/api/v1`. Keep it bound to `127.0.0.1`. Everything runs on this computer.

Run it with:

```bash
cd backend
PORT=8000 ./run_local.sh
```

The server adds these to the editor:

- **Undo:** up to 30 steps for every change, editing or server-side.
- **Find:** searches the whole document and highlights every match, with Previous and Next. Spaces and line breaks are ignored, so names split across lines are found.
- **Replace:** replaces text across the whole document. The result is checked before it is returned, and a name that remains is reported as an error.
- **Edit text, Permanent:** removes one matched text run from the file and writes the new text in its place. Other copies of the text are left alone.
- **Clean:** strips document info and XMP metadata, JavaScript, embedded files and attachments, and optionally compresses. Form fields are kept.
- **Info:** pages, size, PDF version, encryption, metadata, JavaScript count, attachments, form fields and annotations.

The API has endpoints for upload, inspect, search, find, replace, clean, occurrence listing, one-match replacement, download and page rendering. The interactive reference is at `/docs`. Full endpoint table, error codes, and limits are in [backend/README.md](backend/README.md#api).

The [MCP server](backend/mcp_server.py) gives AI clients the same options as tools: `pdf_info`, `pdf_find`, `pdf_occurrences`, `pdf_form_fields`, `pdf_fill_form`, `pdf_render_page`, `pdf_replace`, `pdf_replace_occurrence`, `pdf_cover_occurrence`, `pdf_add_text`, `pdf_highlight`, `pdf_whiteout`, `pdf_rotate_page`, `pdf_move_page`, `pdf_delete_page`, `pdf_append_pages`, and `pdf_clean`. It runs over stdio, opens no port, and needs no running web server. Tools that change a PDF write a new file and never overwrite the input.

Setup, the full tool list, and the file-handling rules are in [backend/README.md](backend/README.md).

## Tests

The tests run in Node and check the PDF logic. They do not exercise the React interface.

```bash
node tests/pdf-ops.test.js
node tests/text-edit.test.mjs
```

Run them from the project root with the dependencies installed. Every check prints `PASS` or `FAIL`.

- `pdf-ops.test.js` covers moving pages, rotating, drawing, filling form fields, deleting a page, and checking that form values persist after a save and reload.
- `text-edit.test.mjs` covers finding the text under a click, rejecting clicks on empty space, replacing a word at the same position, leaving neighboring text untouched, and making sure a second click hits the replacement rather than the hidden original.

The backend has its own pytest suite. See [backend/README.md](backend/README.md#tests).

## Project structure

```
PDFEditor/
├── PdfEditor.jsx          # The component
├── src/main.jsx           # Entry point for the GitHub Pages app
├── index.html             # Page the Vite build starts from
├── vite.config.mjs        # Build config; ships LICENSE.txt with the build
├── LICENSE                # MIT License, plus third-party component notices
├── README.md              # This file
├── package.json
├── tests/
│   ├── pdf-ops.test.js    # PDF operations checks
│   └── text-edit.test.mjs # Text hit-testing and replacement checks
├── backend/               # Optional local server and MCP server (see below)
│   ├── main.py            # API routes and the editor mount
│   ├── mcp_server.py      # MCP server exposing the editor's options as tools
│   ├── editor_ops.py      # Page, annotation and form operations
│   ├── replace_name.py    # PDF replacement engine
│   ├── pdf_tools.py       # Inspect, clean and search with positions
│   ├── formats.py         # Format detection and find/replace
│   ├── jobs.py            # Uploads and results kept for one hour
│   ├── run_local.sh       # Sets up and starts the server
│   ├── frontend/          # Editor copy with the server tools (React + Vite)
│   └── tests/             # pytest suite for the backend and MCP server
└── .github/workflows/
    └── pages.yml          # Builds and deploys to GitHub Pages
```

## How it works

- **pdf-lib** makes every edit. Each change reloads the current bytes, applies the change, saves the document, and updates the component's state.
- **pdf.js** renders the current page to a canvas. For the Edit text tool it also reads the page's text runs.
- **Coordinates:** a click is converted to PDF coordinates with pdf.js's `viewport.convertToPdfPoint`. Each text run gives its baseline origin in `transform[4]` and `transform[5]`, its font size as `height`, and its width as `width`.
- **Hit-testing** searches text runs from last to first. Later runs are drawn on top, so after an edit the new text is found before the hidden original.

## Limitations

- **Text is hidden, not removed, in Cover only mode.** The original text stays in the file under the white box, and copying, searching, or extracting text can still reveal it. The backend's Permanent mode removes the matched text run, but neither mode is true redaction. Don't rely on this tool to remove sensitive information.
- **Replacement text doesn't match the original style.** It uses Helvetica in black at the original size. The original font, weight, and color are not preserved.
- **Longer replacements can overlap neighboring text,** because the white box matches the original width.
- **Only Latin characters are supported.** The standard Helvetica font can't draw characters outside its encoding, and pdf-lib will raise an error for those.
- **The white box shows on tinted or image backgrounds.** It only blends in on white or near-white pages.
- **Rotated and skewed text is not handled and was not tested.** Boxes may land in the wrong place.
- **Phrases split across several text runs** must be edited one run at a time.
- **Encrypted and password-protected PDFs** can't be opened. The component shows an error.
- **Every edit re-saves the whole document,** which can be slow for very large files.
- **Text entry uses `window.prompt`,** and drag rectangles have no live preview while you drag.
- **There is no undo.** To start over, reopen the original file.
- **Signed PDFs:** any edit breaks the digital signature. The component does not check for signatures first.
- **Not yet tested in a browser.** The PDF logic has been tested in Node, but the click handling, rendering, and layout still need a check in a real browser.

## Legal note

Editing your own documents is generally fine. Editing someone else's copyrighted document and distributing the result can infringe their rights. Changing contracts, invoices, IDs, or certificates to misrepresent facts or to fake a signature is illegal. Removing password or permission restrictions can break a document's terms or, in some jurisdictions, anti-circumvention laws. This note is general information, not legal advice.

## Status

- Browser app: source files are in place and tested in Node.
- Local backend: tested with pytest, including the API routes and the MCP tools.
- Version 1.1.0, in a git repository on GitHub.
- Deployed to GitHub Pages from a Vite project with a GitHub Actions workflow.

## License

Released under the [MIT License](LICENSE). The same file lists the third-party components the app uses (pdf-lib, pdf.js, React and others) with their licenses. The built app includes a copy of it as `LICENSE.txt`.

## Author

Created by Alex. Source: [github.com/alexander-uspenskiy/PDFEditor](https://github.com/alexander-uspenskiy/PDFEditor)
