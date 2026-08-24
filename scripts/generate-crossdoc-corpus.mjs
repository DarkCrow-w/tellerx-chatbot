#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import {
  AlignmentType,
  BorderStyle,
  Document,
  Footer,
  Header,
  HeadingLevel,
  Packer,
  PageNumber,
  Paragraph,
  Table,
  TableCell,
  TableRow,
  TextRun,
  WidthType,
} from "docx";
import { PDFDocument, StandardFonts, rgb } from "pdf-lib";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const PROJECT = "Codex-CrossDoc-20-Test";
const FORMATS = ["md", "docx", "xlsx", "html", "pdf", "txt"];
const ENTITY_ZH = [
  "雾桥结算引擎", "星轨授信中台", "青铜退款网关", "晨曦对账枢纽", "翡翠批量付款", "霜白额度服务",
  "金穗票据路由", "靛蓝清分平台", "象牙商户门户", "翠影风控通道", "赤铜账户编排", "月白资金桥梁",
  "海盐争议中心", "炭黑归档总线", "砂金收单核心", "灰紫计费平台", "雾蓝凭证中心", "深绿授信队列",
  "霜红通知枢纽", "琥珀税务适配器",
];
const ENTITY_EN = [
  "Mist Bridge Settlement Engine", "Star Rail Credit Hub", "Bronze Refund Gateway", "Dawn Reconciliation Hub",
  "Jade Bulk Payment", "Frost Limit Service", "Golden Bill Router", "Indigo Clearing Platform",
  "Ivory Merchant Portal", "Emerald Risk Channel", "Copper Account Orchestrator", "Moonlight Fund Bridge",
  "Sea Salt Dispute Center", "Charcoal Archive Bus", "Saffron Acquiring Core", "Violet Billing Platform",
  "Mist Blue Voucher Center", "Deep Green Credit Queue", "Frost Red Notification Hub", "Amber Tax Adapter",
];
const ROLES = [
  ["区域业务主管", "Regional Business Lead"],
  ["风险策略经理", "Risk Policy Manager"],
  ["资金运营负责人", "Treasury Operations Owner"],
  ["平台值班经理", "Platform Duty Manager"],
  ["合规审批专员", "Compliance Approval Specialist"],
];

function parseArgs(argv) {
  const args = { output: "evaluation/generated/crossdoc-20", clusters: 20, seed: 20260824, force: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--output") args.output = argv[++index];
    else if (value === "--clusters") args.clusters = Number(argv[++index]);
    else if (value === "--seed") args.seed = Number(argv[++index]);
    else if (value === "--force") args.force = true;
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!Number.isInteger(args.clusters) || args.clusters < 1 || args.clusters > 20) {
    throw new Error("--clusters must be an integer between 1 and 20");
  }
  return args;
}

function recordFor(index) {
  const number = index + 1;
  const seq = String(number).padStart(2, "0");
  const [apacRoleZh, apacRoleEn] = ROLES[number % ROLES.length];
  const [euRoleZh, euRoleEn] = ROLES[(number + 2) % ROLES.length];
  const apacThreshold = 500000 + number * 13700;
  const euThreshold = apacThreshold + 83000 + number * 1100;
  const apacTimeout = 420 + number * 11;
  const euTimeout = apacTimeout + 90;
  return {
    number,
    seq,
    entityZh: ENTITY_ZH[index],
    entityEn: ENTITY_EN[index],
    businessId: `BIZ-${1200 + number}`,
    policyId: `POL-${4100 + number}`,
    routeId: `RTE-${6100 + number}`,
    changeId: `CHG-${8100 + number}`,
    serviceId: `SVC-${String(9100 + number)}`,
    errorCode: `E-${7100 + number}`,
    fallbackQueue: `FBQ-${String(5100 + number)}`,
    endpoint: `/api/v2/settlement/${String(number).padStart(2, "0")}/authorize`,
    effectiveDate: `2026-${String(3 + (number % 6)).padStart(2, "0")}-${String(10 + (number % 17)).padStart(2, "0")}`,
    retentionDays: 180 + number * 7,
    apacThreshold,
    euThreshold,
    usThreshold: apacThreshold - 47000,
    apacTimeout,
    euTimeout,
    usTimeout: apacTimeout + 35,
    oldTimeout: apacTimeout + 160,
    draftTimeout: apacTimeout - 75,
    oldThreshold: apacThreshold - 56000,
    draftThreshold: apacThreshold + 99000,
    apacRoleZh,
    apacRoleEn,
    euRoleZh,
    euRoleEn,
    usRoleZh: ROLES[(number + 3) % ROLES.length][0],
    usRoleEn: ROLES[(number + 3) % ROLES.length][1],
  };
}

function filenames(record) {
  const prefix = `${record.seq}-${record.businessId}`;
  return {
    requirement: `${prefix}-business-requirement.md`,
    architecture: `${prefix}-policy-architecture.docx`,
    matrix: `${prefix}-policy-matrix.xlsx`,
    api: `${prefix}-routing-api.html`,
    change: `${prefix}-approved-change.pdf`,
    notes: `${prefix}-meeting-scratch.txt`,
  };
}

async function writeRequirement(filePath, record) {
  const text = `# ${record.entityZh} / ${record.entityEn} - 业务需求索引\n\n` +
    `状态：APPROVED / CURRENT。业务对象编号：${record.businessId}。\n\n` +
    `## 适用范围\n` +
    `${record.entityZh} 的高金额授权规则不在本文重复维护。本文只定义跨文档关联：` +
    `治理策略引用 ${record.policyId}，路由配置引用 ${record.routeId}，正式变更授权引用 ${record.changeId}。\n\n` +
    `## 证据优先级\n` +
    `区域门槛、审批角色和超时必须从 Policy Matrix v3 获取；API 路径与拒绝码从 ${record.routeId} 接口规范获取；` +
    `降级队列与审计保留期从 ${record.policyId} 架构说明获取；生效日期从 ${record.changeId} 变更单获取。\n\n` +
    `## 边界\n` +
    `会议草稿、聊天粘贴、旧矩阵和时间较新的未批准记录均不能覆盖上述正式来源。`;
  await fs.writeFile(filePath, text, "utf8");
}

function run(text, options = {}) {
  return new TextRun({ text, font: "Arial", size: options.size ?? 22, bold: options.bold ?? false, color: options.color });
}

function docParagraph(text, options = {}) {
  return new Paragraph({
    children: [run(text, options)],
    heading: options.heading,
    spacing: { before: options.before ?? 0, after: options.after ?? 120, line: 264 },
    alignment: options.alignment ?? AlignmentType.LEFT,
  });
}

async function writeArchitecture(filePath, record) {
  const borders = {
    top: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    bottom: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    left: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    right: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    insideHorizontal: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    insideVertical: { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
  };
  const rows = [
    ["Policy reference", record.policyId],
    ["Bound routing profile", record.routeId],
    ["Runtime service", record.serviceId],
    ["Fallback queue", record.fallbackQueue],
    ["Audit retention", `${record.retentionDays} days`],
    ["Authority", `Approved architecture; activated only by ${record.changeId}`],
  ].map((values, rowIndex) => new TableRow({
    children: values.map((value, columnIndex) => new TableCell({
      width: { size: columnIndex === 0 ? 2700 : 6660, type: WidthType.DXA },
      shading: rowIndex === 0 ? { fill: "F2F4F7" } : undefined,
      margins: { top: 100, bottom: 100, left: 120, right: 120 },
      children: [docParagraph(value, { bold: columnIndex === 0, after: 0 })],
    })),
  }));
  const document = new Document({
    creator: "TellerX cross-document benchmark",
    description: "Approved policy architecture fixture",
    styles: {
      default: { document: { run: { font: "Arial", size: 22 }, paragraph: { spacing: { after: 120, line: 264 } } } },
      paragraphStyles: [
        { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", run: { font: "Arial", size: 46, bold: true, color: "0B2545" }, paragraph: { spacing: { after: 80 } } },
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: "Arial", size: 32, bold: true, color: "2E74B5" }, paragraph: { spacing: { before: 320, after: 160 } } },
      ],
    },
    sections: [{
      properties: {
        page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440, header: 708, footer: 708 } },
      },
      headers: { default: new Header({ children: [docParagraph("APPROVED POLICY ARCHITECTURE", { size: 18, color: "667085", after: 0 })] }) },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [run("Page ", { size: 18, color: "667085" }), new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 18, color: "667085" })] })] }) },
      children: [
        docParagraph("POLICY ARCHITECTURE", { size: 46, bold: true, color: "0B2545", after: 80 }),
        docParagraph(`${record.policyId} / Runtime and governance reference`, { size: 28, color: "475467", after: 260 }),
        docParagraph("Decision status", { heading: HeadingLevel.HEADING_1 }),
        docParagraph(`APPROVED / CURRENT. This document intentionally uses ${record.policyId} as its identity and does not repeat the business-object name.`),
        new Table({ width: { size: 9360, type: WidthType.DXA }, columnWidths: [2700, 6660], borders, rows }),
        docParagraph("Operational interpretation", { heading: HeadingLevel.HEADING_1 }),
        docParagraph(`Requests governed by ${record.policyId} execute through ${record.serviceId}. If synchronous authorization cannot complete, route to ${record.fallbackQueue}; retain its audit evidence for ${record.retentionDays} days.`),
        docParagraph(`Do not infer regional thresholds or timeouts from this architecture. Those values are owned by Policy Matrix v3 and may change independently.`),
      ],
    }],
  });
  await fs.writeFile(filePath, await Packer.toBuffer(document));
}

async function writeMatrix(filePath, record) {
  const workbook = Workbook.create();
  const oldSheet = workbook.worksheets.add("retired-v2");
  const matrix = workbook.worksheets.add("Policy Matrix v3");
  const control = workbook.worksheets.add("control-notes");
  matrix.showGridLines = false;
  matrix.getRange("A1:F1").merge();
  matrix.getRange("A1:F1").values = [[`${record.policyId} - APPROVED CURRENT REGIONAL MATRIX`]];
  matrix.getRange("A1:F1").format = { fill: "#17365D", font: { bold: true, color: "#FFFFFF", size: 15 }, rowHeight: 30 };
  matrix.getRange("A3:F6").values = [
    ["Policy ID", "Region", "Approval Threshold CNY", "Required Approver", "Timeout ms", "Decision state"],
    [record.policyId, "APAC-N", record.apacThreshold, `${record.apacRoleZh} / ${record.apacRoleEn}`, record.apacTimeout, "APPROVED CURRENT"],
    [record.policyId, "EU-W", record.euThreshold, `${record.euRoleZh} / ${record.euRoleEn}`, record.euTimeout, "APPROVED CURRENT"],
    [record.policyId, "US-C", record.usThreshold, `${record.usRoleZh} / ${record.usRoleEn}`, record.usTimeout, "APPROVED CURRENT"],
  ];
  matrix.getRange("A3:F3").format = { fill: "#D9EAF7", font: { bold: true, color: "#0B2545" }, wrapText: true, rowHeight: 34 };
  matrix.getRange("A4:F6").format = { wrapText: true, borders: { preset: "inside", style: "thin", color: "#D9DEE5" } };
  matrix.getRange("C4:C6").format.numberFormat = "#,##0";
  matrix.getRange("E4:E6").format.numberFormat = "0";
  [16, 13, 24, 39, 14, 20].forEach((width, index) => { matrix.getRangeByIndexes(0, index, 6, 1).format.columnWidth = width; });
  matrix.freezePanes.freezeRows(3);

  oldSheet.getRange("A1:F5").values = [
    ["RETIRED MATRIX v2", "NOT CURRENT", "Replaced by v3", record.policyId, "", ""],
    ["Policy ID", "Region", "Old threshold", "Old approver", "Old timeout", "Status"],
    [record.policyId, "APAC-N", record.oldThreshold, "Temporary Operator", record.oldTimeout, "RETIRED"],
    [record.policyId, "EU-W", record.euThreshold - 42000, "Legacy Manager", record.euTimeout + 130, "RETIRED"],
    [record.policyId, "US-C", record.usThreshold - 25000, "Legacy Manager", record.usTimeout + 120, "RETIRED"],
  ];
  oldSheet.getRange("A1:F1").format = { fill: "#7A1F1F", font: { bold: true, color: "#FFFFFF" } };
  oldSheet.getRange("A2:F5").format = { font: { color: "#777777" }, wrapText: true };
  oldSheet.getRange("A1:F5").format.autofitColumns();
  oldSheet.getRange("D1:F5").format.columnWidth = 18;

  control.getRange("A1:C7").values = [
    ["CONTROL NOTES", "Value", "Meaning"],
    ["Activated by", record.changeId, "See approved change notice"],
    ["Routing profile", record.routeId, "See routing API specification"],
    ["Current APAC timeout", null, "Formula links to authoritative matrix"],
    ["Retired APAC timeout", record.oldTimeout, "Historical only"],
    ["Meeting candidate", record.draftTimeout, "Never approved"],
    ["Precedence", "v3 > v2 > meeting notes", "Approval state beats timestamp"],
  ];
  control.getRange("B4").formulas = [["='Policy Matrix v3'!E4"]];
  control.getRange("A1:C1").format = { fill: "#E8EEF5", font: { bold: true, color: "#0B2545" } };
  control.getRange("A1:C7").format.wrapText = true;
  [24, 24, 42].forEach((width, index) => { control.getRangeByIndexes(0, index, 7, 1).format.columnWidth = width; });

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(filePath);
  await fs.rm(`${filePath}.inspect.ndjson`, { force: true });
  // The parser emits the complete used worksheet range, including the title row.
  return { expectedSheet: "Policy Matrix v3", expectedCellRange: "A1:F6" };
}

async function writeApi(filePath, record) {
  const html = `<!doctype html><html lang="zh-CN" data-corpus-revision="4"><head><meta charset="utf-8"><title>${record.routeId} Routing API</title></head><body>` +
    `<nav>Confluence export / API catalog / current</nav><h1>${record.routeId} - APPROVED Routing API</h1>` +
    `<p>Policy binding: <code>${record.policyId}</code>. Runtime service: <code>${record.serviceId}</code>.</p>` +
    `<section><h2>Authorization operation</h2><dl><dt>Endpoint</dt><dd><code>POST ${record.endpoint}</code></dd>` +
    `<dt>Validation rejection</dt><dd><code>${record.errorCode}</code> - governed request failed policy validation</dd></dl></section>` +
    `<aside>Do not copy thresholds into API documentation. Resolve regional values from Policy Matrix v3.</aside>` +
    `<footer>Current route profile ${record.routeId}; approved for ${record.policyId}.</footer></body></html>`;
  await fs.writeFile(filePath, html, "utf8");
}

function drawWrapped(page, font, text, x, y, options = {}) {
  const max = options.max ?? 86;
  const words = text.split(/\s+/);
  const lines = [];
  let line = "";
  for (const word of words) {
    if (!line || `${line} ${word}`.length <= max) line = line ? `${line} ${word}` : word;
    else { lines.push(line); line = word; }
  }
  if (line) lines.push(line);
  for (const value of lines) {
    page.drawText(value, { x, y, size: options.size ?? 11, font, color: options.color ?? rgb(0.12, 0.12, 0.12) });
    y -= options.leading ?? 18;
  }
  return y;
}

async function writeChange(filePath, record) {
  const pdf = await PDFDocument.create();
  pdf.setTitle(`${record.changeId} approved change notice`);
  pdf.setAuthor("TellerX cross-document benchmark");
  const regular = await pdf.embedFont(StandardFonts.Helvetica);
  const bold = await pdf.embedFont(StandardFonts.HelveticaBold);
  const first = pdf.addPage([612, 792]);
  first.drawText("HISTORICAL REVIEW COPY - NOT THE APPROVAL PAGE", { x: 42, y: 742, size: 15, font: bold, color: rgb(0.55, 0.1, 0.1) });
  let y = 705;
  y = drawWrapped(first, regular, `Earlier discussion for ${record.policyId} proposed timeout ${record.oldTimeout} ms. The proposal was not the final decision.`, 48, y, { color: rgb(0.4, 0.4, 0.4) });
  y = drawWrapped(first, regular, `A later chat suggested ${record.draftTimeout} ms. Chat timestamps do not establish authority.`, 48, y - 12, { color: rgb(0.4, 0.4, 0.4) });
  first.drawText("Continue to page 2 for the signed change notice.", { x: 48, y: 120, size: 12, font: bold, color: rgb(0.45, 0.45, 0.45) });

  const second = pdf.addPage([612, 792]);
  second.drawRectangle({ x: 38, y: 345, width: 536, height: 400, borderColor: rgb(0.08, 0.25, 0.45), borderWidth: 2, color: rgb(0.95, 0.98, 1) });
  second.drawText("APPROVED CHANGE NOTICE", { x: 54, y: 710, size: 20, font: bold, color: rgb(0.08, 0.25, 0.45) });
  const lines = [
    `Change authorization: ${record.changeId}`,
    `Governing policy: ${record.policyId}`,
    `Bound routing profile: ${record.routeId}`,
    `Effective date: ${record.effectiveDate}`,
    `Approved artifact: Policy Matrix v3`,
    `Retired value: ${record.oldTimeout} ms is historical and MUST NOT be used as current.`,
    `Current regional thresholds, approvers and timeouts are read from Policy Matrix v3.`,
  ];
  y = 670;
  for (const line of lines) y = drawWrapped(second, regular, line, 56, y, { max: 76, leading: 24, size: 12 });
  second.drawText("SIGNED / APPROVED / CURRENT", { x: 56, y: 382, size: 14, font: bold, color: rgb(0.05, 0.35, 0.18) });
  await fs.writeFile(filePath, await pdf.save());
  return { expectedPage: 2 };
}

async function writeNotes(filePath, record) {
  const text = `MEETING SCRATCH / DRAFT / NOT APPROVED\n` +
    `Topic: ${record.entityZh} (${record.businessId})\n` +
    `Someone suggested threshold ${record.draftThreshold} and timeout ${record.draftTimeout} ms. No decision recorded.\n` +
    `Copied endpoint /api/v1/legacy/${record.seq}/approve may belong to an old prototype.\n` +
    `Do not treat this file as current policy. Approved references are ${record.policyId}, ${record.routeId}, and ${record.changeId}.\n` +
    `Unresolved: owner? maybe Temporary Operator. Meeting ended without approval.\n`;
  await fs.writeFile(filePath, text, "utf8");
}

function manifestRow(record, file, role, format, lifecycleStatus, extra = {}) {
  return {
    source_key: `crossdoc-${record.seq}-${role}`,
    path: `documents/${file}`,
    logical_filename: file,
    logical_key: `crossdoc/${record.businessId}/${role}`,
    project: PROJECT,
    document_type: role,
    source_type: format === "html" ? "confluence_export" : "upload",
    lifecycle_status: lifecycleStatus,
    version_label: lifecycleStatus === "approved" ? "crossdoc-approved-v3" : "crossdoc-draft-v9",
    owner: lifecycleStatus === "approved" ? "Knowledge Governance" : "Working Group",
    format,
    business_id: record.businessId,
    policy_id: record.policyId,
    route_id: record.routeId,
    change_id: record.changeId,
    ...extra,
  };
}

function questionRows(record, files) {
  const evidence = (roles) => roles.map((role) => files[role]);
  return [
    {
      id: `crossdoc-${record.seq}-operation`, kind: "three-document-operation",
      question: `业务对象 ${record.businessId} 在 APAC-N 发起金额超过当前门槛的交易时，应调用哪个 API、由什么角色审批，服务超时是多少？`,
      expected_status: "answered", expected_filename: files.requirement,
      expected_filenames: evidence(["requirement", "matrix", "api"]),
      answer_contains: [record.endpoint, record.apacRoleZh, String(record.apacTimeout)],
      forbidden_answer_values: [String(record.oldTimeout), String(record.draftTimeout), String(record.draftThreshold)], project: PROJECT,
    },
    {
      id: `crossdoc-${record.seq}-governance`, kind: "three-document-governance",
      question: `${record.entityZh} 当前受哪个策略管控？审计证据保留多少天，由哪份变更单在什么日期正式启用？`,
      expected_status: "answered", expected_filename: files.requirement,
      expected_filenames: evidence(["requirement", "architecture", "change"]),
      answer_contains: [record.policyId, String(record.retentionDays), record.changeId, record.effectiveDate],
      forbidden_answer_values: [], project: PROJECT,
    },
    {
      id: `crossdoc-${record.seq}-failure`, kind: "three-document-failure-path",
      question: `${record.businessId} 的高金额授权如果被策略校验拒绝，应返回什么错误码，并转入哪个降级队列？`,
      expected_status: "answered", expected_filename: files.requirement,
      expected_filenames: evidence(["requirement", "api", "architecture"]),
      answer_contains: [record.errorCode, record.fallbackQueue],
      forbidden_answer_values: [], project: PROJECT,
    },
    {
      id: `crossdoc-${record.seq}-comparison`, kind: "linked-region-comparison",
      question: `比较 ${record.businessId} 在 APAC-N 与 EU-W 的当前审批门槛和审批角色：哪个区域门槛更高？`,
      expected_status: "answered", expected_filename: files.requirement,
      expected_filenames: evidence(["requirement", "matrix"]),
      answer_contains: [String(record.apacThreshold), String(record.euThreshold), record.apacRoleZh, record.euRoleZh],
      forbidden_answer_values: [String(record.oldThreshold), String(record.draftThreshold)],
      expected_sheet: "Policy Matrix v3", expected_cell_range: "A1:F6", project: PROJECT,
    },
    {
      id: `crossdoc-${record.seq}-precedence`, kind: "linked-version-precedence",
      question: `${record.businessId} 在最新已批准变更后，APAC-N 当前超时是多少、由哪个矩阵版本生效，生效日期是什么？不要把退役值或会议候选当成当前值。`,
      expected_status: "answered", expected_filename: files.requirement,
      expected_filenames: evidence(["requirement", "matrix", "change"]),
      answer_contains: [String(record.apacTimeout), "Policy Matrix v3", record.effectiveDate],
      forbidden_answer_values: [String(record.oldTimeout), String(record.draftTimeout)], project: PROJECT,
    },
  ];
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const outputDir = path.resolve(args.output);
  const documentsDir = path.join(outputDir, "documents");
  if (args.force) await fs.rm(outputDir, { recursive: true, force: true });
  await fs.mkdir(documentsDir, { recursive: true });
  const manifest = [];
  const questions = [];
  const relationGraph = [];
  const formatCounts = Object.fromEntries(FORMATS.map((format) => [format, 0]));

  for (let index = 0; index < args.clusters; index += 1) {
    const record = recordFor(index);
    const files = filenames(record);
    await writeRequirement(path.join(documentsDir, files.requirement), record);
    await writeArchitecture(path.join(documentsDir, files.architecture), record);
    const matrixLocation = await writeMatrix(path.join(documentsDir, files.matrix), record);
    await writeApi(path.join(documentsDir, files.api), record);
    const changeLocation = await writeChange(path.join(documentsDir, files.change), record);
    await writeNotes(path.join(documentsDir, files.notes), record);

    manifest.push(
      manifestRow(record, files.requirement, "business-requirement", "md", "approved"),
      manifestRow(record, files.architecture, "system-design", "docx", "approved"),
      manifestRow(record, files.matrix, "parameter-matrix", "xlsx", "approved", matrixLocation),
      manifestRow(record, files.api, "api-specification", "html", "approved"),
      manifestRow(record, files.change, "change-notice", "pdf", "approved", { ...changeLocation, effective_at: `${record.effectiveDate}T00:00:00Z` }),
      manifestRow(record, files.notes, "meeting-notes", "txt", "draft"),
    );
    for (const format of FORMATS) formatCounts[format] += 1;
    questions.push(...questionRows(record, files));
    relationGraph.push({
      business_id: record.businessId, entity_zh: record.entityZh, entity_en: record.entityEn,
      edges: [
        [record.businessId, "governed_by", record.policyId],
        [record.businessId, "routed_by", record.routeId],
        [record.businessId, "activated_by", record.changeId],
        [record.policyId, "implemented_by", record.serviceId],
        [record.policyId, "fallback_to", record.fallbackQueue],
        [record.routeId, "rejects_with", record.errorCode],
      ],
    });
  }

  for (let index = 1; index <= 10; index += 1) {
    questions.push({
      id: `crossdoc-missing-${String(index).padStart(2, "0")}`,
      kind: "unanswerable-linked-entity",
      question: `不存在的业务对象 BIZ-${9900 + index} 当前使用哪个策略、接口和审批门槛？`,
      expected_status: "insufficient_evidence", expected_filename: null, expected_filenames: [],
      answer_contains: [], forbidden_answer_values: [], project: PROJECT,
    });
  }

  await fs.writeFile(path.join(outputDir, "manifest.jsonl"), `${manifest.map((row) => JSON.stringify(row)).join("\n")}\n`, "utf8");
  await fs.writeFile(path.join(outputDir, "questions.jsonl"), `${questions.map((row) => JSON.stringify(row)).join("\n")}\n`, "utf8");
  await fs.writeFile(path.join(outputDir, "relation-graph.json"), `${JSON.stringify(relationGraph, null, 2)}\n`, "utf8");
  const summary = {
    seed: args.seed, project: PROJECT, clusters: args.clusters,
    document_count: manifest.length, question_count: questions.length,
    answerable_questions: questions.filter((row) => row.expected_status === "answered").length,
    format_counts: formatCounts, generated_at: new Date().toISOString(),
  };
  await fs.writeFile(path.join(outputDir, "generation-summary.json"), `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

await main();
