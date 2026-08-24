#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import {
  AlignmentType,
  Document,
  HeadingLevel,
  Packer,
  Paragraph,
  Table,
  TableCell,
  TableRow,
  TextRun,
  WidthType,
} from "docx";
import ExcelJS from "exceljs";
import { PDFDocument, StandardFonts, degrees, rgb } from "pdf-lib";

const FORMATS = ["md", "txt", "html", "docx", "xlsx", "pdf"];
const ROLES = [
  ["业务平台主管", "Business Platform Lead"],
  ["风险控制经理", "Risk Control Manager"],
  ["项目交付负责人", "Project Delivery Owner"],
  ["财务审批专员", "Finance Approval Specialist"],
  ["系统责任人", "System Owner"],
];
const COLORS_ZH = ["雾蓝", "赤铜", "月白", "深绿", "砂金", "靛青", "灰紫", "海盐", "炭黑", "霜红"];
const COLORS_EN = ["Mist Blue", "Copper", "Moon White", "Deep Green", "Saffron", "Indigo", "Ash Violet", "Sea Salt", "Charcoal", "Frost Red"];
const OBJECTS_ZH = ["回声网关", "折纸航线", "迷雾账本", "旋转灯塔", "碎片罗盘", "延迟桥梁", "影子队列", "交错档案", "临时星图", "漂移信标"];
const OBJECTS_EN = ["Echo Gateway", "Origami Route", "Fog Ledger", "Rotating Beacon", "Fragment Compass", "Delay Bridge", "Shadow Queue", "Interleaved Archive", "Temporary Star Map", "Drifting Signal"];

function parseArgs(argv) {
  const args = { output: "evaluation/generated/chaos-100", count: 100, seed: 20260818, force: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--output") args.output = argv[++index];
    else if (value === "--count") args.count = Number(argv[++index]);
    else if (value === "--seed") args.seed = Number(argv[++index]);
    else if (value === "--force") args.force = true;
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!Number.isInteger(args.count) || args.count < 1) throw new Error("--count must be a positive integer");
  return args;
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(items, random) {
  const result = [...items];
  for (let index = result.length - 1; index > 0; index -= 1) {
    const other = Math.floor(random() * (index + 1));
    [result[index], result[other]] = [result[other], result[index]];
  }
  return result;
}

function htmlEscape(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function makeRecord(number, format) {
  const code = `CHAOS-${String(number).padStart(4, "0")}`;
  const cluster = (number - 1) % COLORS_ZH.length;
  const object = Math.floor((number - 1) / COLORS_ZH.length) % OBJECTS_ZH.length;
  const suffix = String.fromCharCode(65 + ((number - 1) % 5));
  const entityZh = `${COLORS_ZH[cluster]}${OBJECTS_ZH[object]}-${suffix}`;
  const entityEn = `${COLORS_EN[cluster]} ${OBJECTS_EN[object]} ${suffix}`;
  const finalThreshold = 21000 + number * 43;
  const oldThreshold = finalThreshold - 917;
  const draftThreshold = finalThreshold + 263;
  const timeoutMs = 320 + ((number * 29) % 271);
  const oldTimeoutMs = timeoutMs + 80;
  const retentionDays = 45 + ((number * 7) % 91);
  const [approverZh, approverEn] = ROLES[number % ROLES.length];
  const lifecycleCode = `CX-${String(number).padStart(4, "0")}-LIVE`;
  const neighborCode = `CHAOS-${String(((number + 8) % 100) + 1).padStart(4, "0")}`;
  const question = number % 2 === 0
    ? `${code} 的最终生效审批阈值、审批角色和服务超时分别是什么？请忽略草稿与作废值。`
    : `${entityZh} 当前正式生效的审批阈值、审批角色和服务超时分别是什么？`;
  return {
    number,
    format,
    code,
    entityZh,
    entityEn,
    finalThreshold,
    oldThreshold,
    draftThreshold,
    timeoutMs,
    oldTimeoutMs,
    retentionDays,
    approverZh,
    approverEn,
    lifecycleCode,
    neighborCode,
    question,
  };
}

function sections(record, random) {
  const authoritative = [
    "[FINAL / APPROVED / CURRENT - this block overrides every draft, chat note, example and appendix]",
    `Record identity: ${record.code}; business object: ${record.entityZh} / ${record.entityEn}.`,
    `Effective approval threshold CNY: ${record.finalThreshold}.`,
    `Required approver: ${record.approverZh} / ${record.approverEn}.`,
    `Canonical service timeout: ${record.timeoutMs} ms.`,
    `Audit retention: ${record.retentionDays} days.`,
    `Lifecycle code: ${record.lifecycleCode}.`,
    "Precedence: when another value appears anywhere in this file, use this FINAL block.",
  ];
  const obsolete = [
    "[OBSOLETE - copied from a retired 2024 ticket; MUST NOT be used]",
    `${record.code} threshold was once written as CNY ${record.oldThreshold}.`,
    `Old timeout ${record.oldTimeoutMs} ms; owner shown as Temporary Operator.`,
    "Status: withdrawn after review. This paragraph remains only for audit history.",
  ];
  const draft = [
    "[UNAPPROVED WORKING NOTE / TODO / not effective]",
    `Someone proposed CNY ${record.draftThreshold} in chat, then added “maybe?”.`,
    `Spreadsheet paste: threshold=${record.draftThreshold}; timeout=${record.timeoutMs + 17}; state=DRAFT_ONLY.`,
    "Do not promote this note without a signed decision record.",
  ];
  const noise = [
    "Meeting fragments: retry? owner TBD; coffee break at 15:30; screenshot missing.",
    `Cross-reference only: ${record.neighborCode} is a different object with unrelated numbers.`,
    `Example calculation (not policy): ${record.oldThreshold} + 917 = ${record.finalThreshold}.`,
    "中文/English mixed note: ‘current’ means 生效, not the newest timestamp in a copied email.",
    "Formatting damage from export: col-A?? | value | ??? | merged heading lost.",
  ];
  const pieces = [
    { title: "scratch / 临时记录", lines: draft },
    { title: "retired history / 作废历史", lines: obsolete },
    { title: "FINAL decision record / 最终批准记录", lines: authoritative },
    { title: "misc appendix / 杂项附录", lines: noise },
  ];
  return shuffle(pieces, random);
}

function flattenText(record, pieces) {
  const header = [
    `EXPORT COPY // ${record.entityZh} / ${record.entityEn}`,
    `File key: ${record.code} (spacing and section order may be unreliable)`,
    "NOTICE: this file intentionally preserves drafts and contradictory historical values.",
  ];
  const body = pieces.flatMap((piece, index) => [
    "",
    `${index + 1}. ${piece.title}`,
    ...piece.lines.map((line, lineIndex) => `${lineIndex % 2 ? "    " : ""}${line}`),
  ]);
  return [...header, ...body, "", `END-OF-EXPORT ${record.code}`];
}

async function writeMarkdown(filePath, record, pieces) {
  const body = flattenText(record, pieces);
  const lines = body.map((line) => {
    if (/^\d+\. /.test(line)) return `## ${line}`;
    if (line.startsWith("[FINAL")) return `> **${line}**`;
    if (line.startsWith("[OBSOLETE") || line.startsWith("[UNAPPROVED")) return `> ~~${line}~~`;
    return line;
  });
  lines.splice(4, 0, "<!-- export note: headings may be out of chronological order -->");
  await fs.writeFile(filePath, `${lines.join("\n")}\n`, "utf8");
}

async function writeText(filePath, record, pieces) {
  const lines = flattenText(record, pieces);
  lines.splice(7, 0, "\t\tbroken-column-copy :: N/A || old || review-later");
  await fs.writeFile(filePath, `${lines.join("\n")}\n`, "utf8");
}

async function writeHtml(filePath, record, pieces) {
  const cards = pieces.map((piece, index) => {
    const cssClass = piece.title.includes("FINAL") ? "final" : piece.title.includes("retired") ? "obsolete" : "note";
    const items = piece.lines.map((line) => `<li>${htmlEscape(line)}</li>`).join("\n");
    return `<section class="${cssClass}"><h${index % 2 ? 3 : 2}>${htmlEscape(piece.title)}</h${index % 2 ? 3 : 2}><ul>${items}</ul></section>`;
  }).join("\n");
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${htmlEscape(record.code)} export</title>
<style>body{font-family:Arial,sans-serif;max-width:900px;margin:24px}.obsolete{text-decoration:line-through;color:#777}.final{border:3px double #153e75;padding:12px;background:#eef6ff}.note{margin-left:${record.number % 3}rem}li{margin:5px}</style></head>
<body><header><small>Confluence export / order not guaranteed / ${htmlEscape(record.code)}</small><h1>${htmlEscape(record.entityZh)} <span>${htmlEscape(record.entityEn)}</span></h1></header>
<aside>⚠ Historical and draft values are retained intentionally. “Latest-looking” does not mean effective.</aside>${cards}
<footer>END-OF-EXPORT ${htmlEscape(record.code)}</footer></body></html>`;
  await fs.writeFile(filePath, html, "utf8");
}

function docxParagraph(text, options = {}) {
  return new Paragraph({
    heading: options.heading,
    alignment: options.alignment,
    spacing: { after: options.after ?? 100 },
    children: [new TextRun({
      text,
      font: "Arial",
      bold: options.bold ?? false,
      strike: options.strike ?? false,
      color: options.color,
      size: options.size,
    })],
  });
}

async function writeDocx(filePath, record, pieces) {
  const asciiOnly = (value) => String(value)
    .replaceAll(`${record.entityZh} / ${record.entityEn}`, record.entityEn)
    .replaceAll(`${record.approverZh} / ${record.approverEn}`, record.approverEn)
    .replaceAll(/[\u3400-\u9fff]+/g, "")
    .replaceAll(/\s+/g, " ")
    .trim();
  const children = [
    docxParagraph(record.entityEn, { heading: HeadingLevel.TITLE, bold: true, size: 32 }),
    docxParagraph(`Export copy ${record.code} — section order intentionally unreliable`, { alignment: AlignmentType.RIGHT, color: "666666" }),
  ];
  for (const piece of pieces) {
    children.push(docxParagraph(asciiOnly(piece.title), { heading: piece.title.includes("FINAL") ? HeadingLevel.HEADING_1 : HeadingLevel.HEADING_2, bold: true }));
    for (const line of piece.lines) {
      const obsolete = piece.title.includes("retired") || piece.title.includes("scratch");
      children.push(docxParagraph(asciiOnly(line), { strike: obsolete && line.includes("threshold"), color: obsolete ? "777777" : undefined }));
    }
    if (piece.title.includes("FINAL")) {
      children.push(new Table({
        width: { size: 9360, type: WidthType.DXA },
        rows: [
          ["Key", "Approved value"],
          ["Threshold CNY", String(record.finalThreshold)],
          ["Approver", record.approverEn],
          ["Timeout ms", String(record.timeoutMs)],
        ].map((row) => new TableRow({
          children: row.map((value) => new TableCell({
            width: { size: 4680, type: WidthType.DXA },
            children: [docxParagraph(value, { bold: row[0] === "Key" })],
          })),
        })),
      }));
    }
  }
  const document = new Document({
    creator: "TellerX chaos benchmark",
    description: "Synthetic irregular knowledge-base fixture",
    sections: [{
      properties: { page: { margin: { top: 1000, right: 1100, bottom: 1000, left: 1100 } } },
      children,
    }],
  });
  await fs.writeFile(filePath, await Packer.toBuffer(document));
}

async function writeXlsx(filePath, record, pieces) {
  const workbook = new ExcelJS.Workbook();
  workbook.creator = "TellerX chaos benchmark";
  workbook.created = new Date("2026-08-18T00:00:00Z");
  workbook.modified = new Date("2026-08-18T00:00:00Z");
  const scratch = workbook.addWorksheet("scratch-old");
  const finalSheet = workbook.addWorksheet(record.number % 2 ? "FINAL decision" : "已批准 FINAL");
  const notes = workbook.addWorksheet("misc-notes");

  scratch.addRows([
    ["DRAFT EXPORT", record.code, "status", "NOT APPROVED"],
    ["threshold candidate", record.oldThreshold, "timeout candidate", record.oldTimeoutMs],
    ["chat proposal", record.draftThreshold, "owner", "Temporary Operator"],
    ["nearby code", record.neighborCode, "warning", "different record"],
    ["formula demo", null, "not policy", true],
    ["乱序", "旧值保留", "do not use", "archived"],
    ["blank-ish", null, null, "?"],
  ]);
  scratch.getCell("B5").value = { formula: "B2+917", result: record.finalThreshold };
  scratch.getRow(1).eachCell((cell) => {
    cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF7A1F1F" } };
    cell.font = { bold: true, color: { argb: "FFFFFFFF" } };
  });
  scratch.eachRow((row, rowNumber) => row.eachCell({ includeEmpty: true }, (cell) => {
    cell.alignment = { wrapText: true, vertical: "top" };
    cell.border = { bottom: { style: "dotted", color: { argb: "FFB7B7B7" } } };
    if (rowNumber > 1) cell.font = { color: { argb: "FF777777" } };
  }));
  [24, 18, 22, 18].forEach((width, index) => { scratch.getColumn(index + 1).width = width; });

  const startRow = record.number % 3 === 0 ? 7 : record.number % 3 === 1 ? 3 : 5;
  const startCol = record.number % 2 === 0 ? 2 : 1;
  const endRow = startRow + 8;
  const startColumnName = String.fromCharCode(65 + startCol - 1);
  const endColumnName = String.fromCharCode(65 + startCol);
  const rangeName = `${startColumnName}${startRow}:${endColumnName}${endRow}`;
  const finalRows = [
    ["FINAL / APPROVED / CURRENT", "This table overrides all other sheets"],
    ["Record Code", record.code],
    ["Business Object", `${record.entityZh} / ${record.entityEn}`],
    ["Approval Threshold CNY", record.finalThreshold],
    ["Required Approver", `${record.approverZh} / ${record.approverEn}`],
    ["Canonical Timeout ms", record.timeoutMs],
    ["Audit Retention days", record.retentionDays],
    ["Lifecycle Code", record.lifecycleCode],
    ["Precedence", "Use this table; ignore scratch-old and chat notes"],
  ];
  finalRows.forEach((values, rowOffset) => values.forEach((value, columnOffset) => {
    const cell = finalSheet.getCell(startRow + rowOffset, startCol + columnOffset);
    cell.value = value;
    cell.alignment = { wrapText: true, vertical: "top" };
    cell.border = {
      top: { style: "thin", color: { argb: "FFA6A6A6" } },
      left: { style: "thin", color: { argb: "FFA6A6A6" } },
      bottom: { style: "thin", color: { argb: "FFA6A6A6" } },
      right: { style: "thin", color: { argb: "FFA6A6A6" } },
    };
    if (rowOffset === 0) {
      cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF17365D" } };
      cell.font = { bold: true, color: { argb: "FFFFFFFF" } };
    }
  }));
  finalSheet.getCell(startRow + 3, startCol + 1).numFmt = "#,##0";
  finalSheet.getColumn(startCol).width = 30;
  finalSheet.getColumn(startCol + 1).width = 48;

  notes.addRows([
    ["meeting paste", "value", "interpretation"],
    ["maybe threshold", record.draftThreshold, "draft only"],
    ["example arithmetic", `${record.oldThreshold} + 917`, "not a rule"],
    ["record-like", record.neighborCode, "different object"],
    ["owner?", "TBD", "unresolved"],
    ["中文备注", "时间戳不能决定是否生效", "以 FINAL sheet 为准"],
    ["duplicate", record.oldThreshold, "obsolete"],
    ["end", "--", "--"],
  ]);
  notes.eachRow((row, rowNumber) => row.eachCell({ includeEmpty: true }, (cell) => {
    cell.alignment = { wrapText: true, vertical: "top" };
    if (rowNumber === 1) {
      cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FFD9EAD3" } };
      cell.font = { bold: true };
    }
  }));
  [24, 22, 30].forEach((width, index) => { notes.getColumn(index + 1).width = width; });

  await workbook.xlsx.writeFile(filePath);
  return { expectedSheet: finalSheet.name, expectedCellRange: rangeName };
}

function wrapPdfLine(text, maxChars = 88) {
  const words = text.split(/\s+/);
  const lines = [];
  let current = "";
  for (const word of words) {
    if (!current || `${current} ${word}`.length <= maxChars) current = current ? `${current} ${word}` : word;
    else { lines.push(current); current = word; }
  }
  if (current) lines.push(current);
  return lines;
}

async function writePdf(filePath, record, pieces) {
  const pdf = await PDFDocument.create();
  const regular = await pdf.embedFont(StandardFonts.Helvetica);
  const bold = await pdf.embedFont(StandardFonts.HelveticaBold);
  const page1 = pdf.addPage([612, 792]);
  page1.drawText(`MESSY EXPORT / ${record.code} / historical page`, { x: 48, y: 744, size: 16, font: bold, color: rgb(0.45, 0.1, 0.1) });
  let y = 710;
  const page1Lines = [
    "OBSOLETE - do not use. This page is retained for audit history.",
    `${record.code} old threshold CNY ${record.oldThreshold}; old timeout ${record.oldTimeoutMs} ms.`,
    `Unapproved chat proposal: threshold CNY ${record.draftThreshold}.`,
    `Nearby reference ${record.neighborCode} belongs to a different business object.`,
    "Timestamp order is unreliable after PDF export. Approval state controls precedence.",
  ];
  for (const line of page1Lines.flatMap((value) => wrapPdfLine(value))) {
    page1.drawText(line, { x: 52, y, size: 11, font: regular, color: rgb(0.35, 0.35, 0.35) });
    y -= 20;
  }
  page1.drawText("PAGE 1 IS NOT CURRENT", { x: 160, y: 120, size: 24, font: bold, color: rgb(0.75, 0.75, 0.75), rotate: degrees(18) });

  const page2 = pdf.addPage([612, 792]);
  page2.drawRectangle({ x: 38, y: 420, width: 536, height: 330, borderWidth: 2, borderColor: rgb(0.08, 0.25, 0.45), color: rgb(0.94, 0.97, 1) });
  page2.drawText("FINAL / APPROVED / CURRENT", { x: 55, y: 716, size: 18, font: bold, color: rgb(0.08, 0.25, 0.45) });
  const finalLines = [
    `Record identity: ${record.code}`,
    `Business object: ${record.entityEn}`,
    `Effective approval threshold CNY: ${record.finalThreshold}`,
    `Required approver: ${record.approverEn}`,
    `Canonical service timeout: ${record.timeoutMs} ms`,
    `Audit retention: ${record.retentionDays} days`,
    `Lifecycle code: ${record.lifecycleCode}`,
    "This FINAL block overrides every draft, example, copied email and appendix.",
  ];
  y = 680;
  for (const line of finalLines.flatMap((value) => wrapPdfLine(value))) {
    page2.drawText(line, { x: 58, y, size: 12, font: line.startsWith("Effective") || line.startsWith("Required") ? bold : regular, color: rgb(0.05, 0.05, 0.05) });
    y -= 28;
  }
  page2.drawText("Appendix: examples and chat fragments below are non-authoritative.", { x: 48, y: 360, size: 10, font: regular, color: rgb(0.4, 0.4, 0.4) });
  page2.drawText(`Example only: ${record.oldThreshold} + 917 = ${record.finalThreshold}`, { x: 48, y: 336, size: 10, font: regular, color: rgb(0.4, 0.4, 0.4) });
  await fs.writeFile(filePath, await pdf.save());
  return { expectedPage: 2 };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const outputDir = path.resolve(args.output);
  const documentsDir = path.join(outputDir, "documents");
  if (args.force) await fs.rm(outputDir, { recursive: true, force: true });
  await fs.mkdir(documentsDir, { recursive: true });
  const random = mulberry32(args.seed);
  const manifest = [];
  const questions = [];
  const counts = Object.fromEntries(FORMATS.map((format) => [format, 0]));

  for (let index = 0; index < args.count; index += 1) {
    const number = index + 1;
    const format = FORMATS[index % FORMATS.length];
    const record = makeRecord(number, format);
    const pieces = sections(record, random);
    const filename = `${String(number).padStart(4, "0")}-${record.code}-${format}-messy.${format}`;
    const filePath = path.join(documentsDir, filename);
    let location = {};
    if (format === "md") await writeMarkdown(filePath, record, pieces);
    else if (format === "txt") await writeText(filePath, record, pieces);
    else if (format === "html") await writeHtml(filePath, record, pieces);
    else if (format === "docx") await writeDocx(filePath, record, pieces);
    else if (format === "xlsx") location = await writeXlsx(filePath, record, pieces);
    else if (format === "pdf") location = await writePdf(filePath, record, pieces);
    counts[format] += 1;
    const relativePath = path.relative(outputDir, filePath);
    const entry = {
      source_key: `chaos-source-${String(number).padStart(4, "0")}`,
      path: relativePath,
      logical_filename: filename,
      project: "Codex-Chaos-100-Test",
      document_type: ["business-rule", "system-design", "meeting-export", "runbook"][number % 4],
      source_type: format === "html" ? "confluence_export" : "upload",
      lifecycle_status: "approved",
      version_label: "chaos-approved-v1",
      format,
      expected_filename: filename,
      record_code: record.code,
      entity_zh: record.entityZh,
      entity_en: record.entityEn,
      final_threshold: record.finalThreshold,
      old_threshold: record.oldThreshold,
      draft_threshold: record.draftThreshold,
      approval_role_zh: record.approverZh,
      approval_role_en: record.approverEn,
      timeout_ms: record.timeoutMs,
      old_timeout_ms: record.oldTimeoutMs,
      retention_days: record.retentionDays,
      lifecycle_code: record.lifecycleCode,
      expected_sheet: location.expectedSheet ?? null,
      expected_cell_range: location.expectedCellRange ?? null,
      expected_page: location.expectedPage ?? null,
      question: record.question,
      answer_contains: [
        String(record.finalThreshold),
        ["docx", "pdf"].includes(format) ? record.approverEn : record.approverZh,
        String(record.timeoutMs),
      ],
    };
    manifest.push(entry);
    questions.push({
      id: `chaos-q-${String(number).padStart(4, "0")}`,
      kind: number % 2 === 0 ? "chaos-code-query" : "chaos-entity-query",
      question: record.question,
      expected_filename: filename,
      expected_filenames: [filename],
      expected_status: "answered",
      expected_version_label: "chaos-approved-v1",
      expected_sheet: location.expectedSheet ?? null,
      expected_cell_range: location.expectedCellRange ?? null,
      answer_contains: entry.answer_contains,
      forbidden_answer_values: [String(record.oldThreshold), String(record.draftThreshold), String(record.oldTimeoutMs)],
    });
  }

  await fs.writeFile(path.join(outputDir, "manifest.jsonl"), `${manifest.map((row) => JSON.stringify(row)).join("\n")}\n`, "utf8");
  await fs.writeFile(path.join(outputDir, "questions.jsonl"), `${questions.map((row) => JSON.stringify(row)).join("\n")}\n`, "utf8");
  const summary = { seed: args.seed, document_count: args.count, format_counts: counts, generated_at: new Date().toISOString() };
  await fs.writeFile(path.join(outputDir, "generation-summary.json"), `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

await main();
