# PDF Desk

A browser-based PDF editor built as a single React component. Files are opened, edited, and downloaded entirely in the browser. Nothing is uploaded to a server.

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

## Tests

The tests run in Node and check the PDF logic. They do not exercise the React interface.

```bash
node tests/pdf-ops.test.js
node tests/text-edit.test.mjs
```

Run them from the project root with the dependencies installed. Every check prints `PASS` or `FAIL`.

- `pdf-ops.test.js` covers moving pages, rotating, drawing, filling form fields, deleting a page, and checking that form values persist after a save and reload.
- `text-edit.test.mjs` covers finding the text under a click, rejecting clicks on empty space, replacing a word at the same position, leaving neighboring text untouched, and making sure a second click hits the replacement rather than the hidden original.

## Project structure

```
PDFEditor/
├── PdfEditor.jsx          # The component
├── README.md              # This file
└── tests/
    ├── pdf-ops.test.js    # PDF operations checks
    └── text-edit.test.mjs # Text hit-testing and replacement checks
```

## How it works

- **pdf-lib** makes every edit. Each change reloads the current bytes, applies the change, saves the document, and updates the component's state.
- **pdf.js** renders the current page to a canvas. For the Edit text tool it also reads the page's text runs.
- **Coordinates:** a click is converted to PDF coordinates with pdf.js's `viewport.convertToPdfPoint`. Each text run gives its baseline origin in `transform[4]` and `transform[5]`, its font size as `height`, and its width as `width`.
- **Hit-testing** searches text runs from last to first. Later runs are drawn on top, so after an edit the new text is found before the hidden original.

## Limitations

- **Text is hidden, not removed.** The original text stays in the file under the white box. Copying, searching, or extracting text can still reveal it. Don't rely on this tool to remove sensitive information. Permanent removal needs true redaction, which this tool does not provide.
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

- Source files are in place and tested in Node.
- Not yet a git repository.
- Not yet deployed. GitHub Pages deployment needs a Vite project scaffold and a GitHub Actions workflow, which have not been built.

## License

Not specified.
