import { PDFDocument, StandardFonts, rgb } from 'pdf-lib';
import * as pdfjsLib from 'pdfjs-dist/legacy/build/pdf.mjs';

// Same hit-test as the component
function findTextAt(runs, x, y) {
  return [...runs].reverse().find((run) => {
    const [, , , , tx, ty] = run.transform;
    return x >= tx && x <= tx + run.width && y >= ty - run.height * 0.25 && y <= ty + run.height * 0.85;
  });
}

async function runsOf(bytes) {
  const pdf = await pdfjsLib.getDocument({ data: bytes.slice() }).promise;
  return (await (await pdf.getPage(1)).getTextContent()).items.filter((i) => i.str.trim());
}

// Source PDF with two lines of text
const src = await PDFDocument.create();
const page = src.addPage([400, 300]);
const font = await src.embedFont(StandardFonts.Helvetica);
page.drawText('Hello World', { x: 72, y: 200, size: 20, font });
page.drawText('Second line', { x: 72, y: 150, size: 12, font });
let bytes = await src.save();

// Hit-testing
let runs = await runsOf(bytes);
const hit = findTextAt(runs, 100, 210);
console.log('hit:', hit?.str, hit ? 'PASS' : 'FAIL');
console.log('miss on empty area:', findTextAt(runs, 380, 40) === undefined ? 'PASS' : 'FAIL');
const nearSecond = findTextAt(runs, 100, 160);
console.log('hit second line:', nearSecond?.str, nearSecond?.str === 'Second line' ? 'PASS' : 'FAIL');

// Replacement, same logic as replaceText()
const doc = await PDFDocument.load(bytes);
const [, , , , x, y] = hit.transform;
const size = hit.height;
const p = doc.getPage(0);
const f = await doc.embedFont(StandardFonts.Helvetica);
p.drawRectangle({ x: x - 1, y: y - size * 0.25, width: hit.width + 2, height: size * 1.1, color: rgb(1, 1, 1) });
p.drawText('Hi there', { x, y, size, font: f, color: rgb(0, 0, 0) });
bytes = await doc.save();

// Verify: the new text is at the same origin, and the other line is untouched
runs = await runsOf(bytes);
const newRun = runs.find((r) => r.str === 'Hi there');
console.log('new text at same origin:', newRun && newRun.transform[4] === 72 && newRun.transform[5] === 200 ? 'PASS' : 'FAIL');
console.log('second line untouched:', runs.some((r) => r.str === 'Second line') ? 'PASS' : 'FAIL');

// A second click on the edited word should hit the replacement, not the hidden original
const again = findTextAt(runs, 100, 210);
console.log('second click hits replacement:', again?.str, again?.str === 'Hi there' ? 'PASS' : 'FAIL');
