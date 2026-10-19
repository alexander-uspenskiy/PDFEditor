const { PDFDocument, PDFTextField, PDFCheckBox, StandardFonts, degrees, rgb } = require('pdf-lib');

async function readFields(doc) {
  return doc.getForm().getFields().flatMap((field) => {
    const name = field.getName();
    if (field instanceof PDFTextField) return [{ name, kind: 'text', value: field.getText() ?? '' }];
    if (field instanceof PDFCheckBox) return [{ name, kind: 'checkbox', value: field.isChecked() }];
    return [];
  });
}

(async () => {
  // Source: 3 pages with distinct widths so we can track order; a text field and a checkbox
  const src = await PDFDocument.create();
  for (let i = 0; i < 3; i++) src.addPage([300 + i * 10, 400]);
  const form = src.getForm();
  form.createTextField('name').addToPage(src.getPage(0), { x: 20, y: 200, width: 150, height: 20 });
  form.createCheckBox('agree').addToPage(src.getPage(1), { x: 20, y: 150, width: 12, height: 12 });
  let bytes = await src.save();

  // 1. Move page 0 to index 1 (order should be widths 310, 300, 320)
  let doc = await PDFDocument.load(bytes);
  const p0 = doc.getPage(0);
  doc.removePage(0);
  doc.insertPage(1, p0);
  bytes = await doc.save();
  doc = await PDFDocument.load(bytes);
  const widths = doc.getPages().map((p) => p.getSize().width);
  console.log('after move widths:', widths, widths.join() === '310,300,320' ? 'PASS' : 'FAIL');

  // 2. Rotate page 0 by 90
  const r = doc.getPage(0);
  r.setRotation(degrees((r.getRotation().angle + 90) % 360));
  console.log('rotation:', doc.getPage(0).getRotation().angle, doc.getPage(0).getRotation().angle === 90 ? 'PASS' : 'FAIL');
  bytes = await doc.save();

  // 3. Fields survive reload and read correctly
  doc = await PDFDocument.load(bytes);
  console.log('fields before fill:', await readFields(doc));

  // 4. Draw highlight, whiteout, text
  const font = await doc.embedFont(StandardFonts.Helvetica);
  doc.getPage(0).drawText('hello', { x: 30, y: 300, size: 12, font, color: rgb(0.1, 0.2, 0.6) });
  doc.getPage(0).drawRectangle({ x: 30, y: 250, width: 80, height: 10, color: rgb(1, 0.93, 0.3), opacity: 0.4 });
  doc.getPage(0).drawRectangle({ x: 30, y: 230, width: 80, height: 10, color: rgb(1, 1, 1) });
  bytes = await doc.save();

  // 5. Fill form: set text, check checkbox, update appearances
  doc = await PDFDocument.load(bytes);
  const f = doc.getForm();
  f.getTextField('name').setText('Alex Test');
  f.getCheckBox('agree').check();
  f.updateFieldAppearances();
  bytes = await doc.save();

  // 6. Delete a page, then reload and verify values persisted
  doc = await PDFDocument.load(bytes);
  doc.removePage(2);
  bytes = await doc.save();
  doc = await PDFDocument.load(bytes);
  const after = await readFields(doc);
  console.log('after delete pages:', doc.getPageCount(), 'fields:', after);
  const ok = doc.getPageCount() === 2 && after.find((x) => x.name === 'name').value === 'Alex Test' && after.find((x) => x.name === 'agree').value === true;
  console.log(ok ? 'PASS form values persist' : 'FAIL');
  console.log('bytes:', bytes.length);
})().catch((e) => { console.error('ERROR', e); process.exit(1); });
