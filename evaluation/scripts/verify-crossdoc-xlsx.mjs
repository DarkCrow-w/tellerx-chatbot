#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [documentsDir, outputDir] = process.argv.slice(2);
if (!documentsDir || !outputDir) {
  throw new Error("Usage: verify-crossdoc-xlsx.mjs DOCUMENTS_DIR OUTPUT_DIR");
}

await fs.mkdir(outputDir, { recursive: true });
const filenames = (await fs.readdir(documentsDir)).filter((name) => name.endsWith(".xlsx")).sort();
const report = [];
for (const filename of filenames) {
  const inputPath = path.join(documentsDir, filename);
  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
  const inspection = await workbook.inspect({
    kind: "workbook,sheet,table,formula",
    maxChars: 5000,
    tableMaxRows: 10,
    tableMaxCols: 8,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: `${filename} formula error scan`,
  });
  const sheetInspection = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
  const sheetNames = sheetInspection.ndjson.split("\n").filter(Boolean).map((line) => JSON.parse(line).name).filter(Boolean);
  const workbookOutput = path.join(outputDir, path.basename(filename, ".xlsx"));
  await fs.mkdir(workbookOutput, { recursive: true });
  for (let index = 0; index < sheetNames.length; index += 1) {
    const sheetName = sheetNames[index];
    const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1.5, format: "png" });
    const safeName = sheetName.replaceAll(/[^a-zA-Z0-9_-]+/g, "-");
    await fs.writeFile(
      path.join(workbookOutput, `${String(index + 1).padStart(2, "0")}-${safeName}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
  report.push({ filename, sheets: sheetNames, inspect: inspection.ndjson, formula_errors: errors.ndjson });
}
await fs.writeFile(path.join(outputDir, "verification.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
process.stdout.write(`${JSON.stringify({ workbooks: report.length, rendered_sheets: report.reduce((sum, row) => sum + row.sheets.length, 0) })}\n`);
