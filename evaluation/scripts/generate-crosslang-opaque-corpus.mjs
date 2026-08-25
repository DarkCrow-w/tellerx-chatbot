#!/usr/bin/env node

import crypto from "node:crypto";
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

const PROJECT = "Codex-CrossLang-Opaque-16-Test";
const FORMATS = ["md", "docx", "xlsx", "html", "pdf", "txt"];
const CHINESE_ALIASES = [
  "岚桥清算", "岚桥授信", "星港清算", "星港退款",
  "青岚对账", "青岚结算", "玄羽账务", "玄羽争议",
  "银杉收单", "银杉计费", "云舟付款", "云舟归档",
  "曦光票据", "曦光凭证", "翠湖税务", "翠湖授信",
];
const ENGLISH_ALIASES = [
  "Mistbridge Clearing", "Mistbridge Credit", "Starport Clearing", "Starport Refund",
  "Azuremist Reconciliation", "Azuremist Settlement", "Blackwing Ledger", "Blackwing Dispute",
  "Silverfir Acquiring", "Silverfir Billing", "Cloudark Payment", "Cloudark Archive",
  "Daybreak Invoice", "Daybreak Voucher", "Greenlake Tax", "Greenlake Credit",
];
const CHINESE_CONTROLS = [
  "海棠闸", "云杉锁", "青瓦门", "赤砂阀", "银杏栅", "玄石关", "苍鹭门", "白帆锁",
  "琥珀闸", "竹影阀", "月桂门", "松涛锁", "晨露栅", "远山关", "湖心阀", "雪松门",
];
const GOVERNANCE_OWNERS = [
  "区域风险治理负责人", "资金合规平台主管", "交易策略审批经理", "运营控制责任人",
  "跨境合规专员", "平台风险值班主管", "清算治理负责人", "业务控制委员会秘书",
];
const APPROVERS = [
  ["风险策略经理", "Risk Policy Manager"],
  ["区域业务主管", "Regional Business Lead"],
  ["资金运营负责人", "Treasury Operations Owner"],
  ["合规审批专员", "Compliance Approval Specialist"],
  ["平台值班经理", "Platform Duty Manager"],
];

function parseArgs(argv) {
  const args = {
    output: "evaluation/generated/crosslang-opaque-16",
    clusters: 16,
    seed: 20260824,
    force: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--output") args.output = argv[++index];
    else if (value === "--clusters") args.clusters = Number(argv[++index]);
    else if (value === "--seed") args.seed = Number(argv[++index]);
    else if (value === "--force") args.force = true;
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!Number.isInteger(args.clusters) || args.clusters < 1 || args.clusters > 16) {
    throw new Error("--clusters must be an integer between 1 and 16");
  }
  return args;
}

function opaqueFilename(seed, clusterIndex, role, extension) {
  const stem = crypto
    .createHash("sha256")
    .update(`${seed}|opaque-crosslang-v1|${clusterIndex}|${role}`)
    .digest("hex")
    .slice(0, 12);
  return `${stem}.${extension}`;
}

function recordFor(index) {
  const number = index + 1;
  const seq = String(number).padStart(2, "0");
  const [apacRoleZh, apacRoleEn] = APPROVERS[number % APPROVERS.length];
  const [euRoleZh, euRoleEn] = APPROVERS[(number + 2) % APPROVERS.length];
  const apacThreshold = 420000 + number * 17300;
  const euThreshold = apacThreshold + 71000 + number * 1900;
  const timeoutMs = 340 + number * 17;
  return {
    number,
    seq,
    aliasZh: CHINESE_ALIASES[index],
    aliasEn: ENGLISH_ALIASES[index],
    controlZh: CHINESE_CONTROLS[index],
    ownerZh: GOVERNANCE_OWNERS[index % GOVERNANCE_OWNERS.length],
    linkId: `LNK-${7300 + number}`,
    controlId: `CTL-${4600 + number}`,
    runtimeId: `CORE-${6200 + number}`,
    routeId: `GATE-${8400 + number}`,
    releaseId: `REL-${5700 + number}`,
    queueId: `QFB-${3100 + number}`,
    legacyQueueId: `QOLD-${9100 + number}`,
    errorCode: `XR-${6800 + number}`,
    endpoint: `/platform/v3/authorization/${crypto.createHash("sha1").update(`route-${number}`).digest("hex").slice(0, 8)}/evaluate`,
    legacyEndpoint: `/legacy/v1/check/${crypto.createHash("sha1").update(`legacy-${number}`).digest("hex").slice(0, 7)}`,
    effectiveDate: `2026-${String(2 + (number % 7)).padStart(2, "0")}-${String(8 + (number % 18)).padStart(2, "0")}`,
    retentionDays: 365 + number * 11,
    apacThreshold,
    euThreshold,
    usThreshold: apacThreshold - 39000,
    oldThreshold: apacThreshold - 68000,
    draftThreshold: apacThreshold + 127000,
    timeoutMs,
    oldTimeoutMs: timeoutMs + 180,
    draftTimeoutMs: Math.max(120, timeoutMs - 93),
    apacRoleZh,
    apacRoleEn,
    euRoleZh,
    euRoleEn,
    usRoleZh: APPROVERS[(number + 3) % APPROVERS.length][0],
    usRoleEn: APPROVERS[(number + 3) % APPROVERS.length][1],
  };
}

function filenames(record, seed) {
  return {
    requirement: opaqueFilename(seed, record.number, "requirement", "md"),
    registry: opaqueFilename(seed, record.number, "registry", "txt"),
    architecture: opaqueFilename(seed, record.number, "architecture", "docx"),
    matrix: opaqueFilename(seed, record.number, "matrix", "xlsx"),
    api: opaqueFilename(seed, record.number, "api", "html"),
    release: opaqueFilename(seed, record.number, "release", "pdf"),
    draft: opaqueFilename(seed, record.number, "draft", "md"),
  };
}

async function writeChineseRequirement(filePath, record) {
  const text = `# 内部业务规则摘录（正式版）\n\n` +
    `文档语言：中文。状态：已批准、当前有效。\n\n` +
    `## 业务称谓\n\n` +
    `本规则覆盖“${record.aliasZh}”。其中文控制规则名为“${record.controlZh}”，治理责任人为“${record.ownerZh}”。\n\n` +
    `## 适用动作\n\n` +
    `当该业务在亚太北区执行高金额授权时，必须采用正式参数表中的当前门槛、审批角色和超时时间。` +
    `接口路径、英文系统代号以及发布编号不在本页重复维护，须先通过受控术语登记表完成中英文称谓映射。\n\n` +
    `## 证据优先级\n\n` +
    `已批准的发布记录和当前参数表优先于时间更新但未经批准的讨论材料。旧参数、候选值和临时接口不得作为生产结论。\n`;
  await fs.writeFile(filePath, text, "utf8");
}

async function writeRegistry(filePath, record) {
  const text = `CONTROLLED VOCABULARY / 受控术语登记表\n` +
    `Status / 状态: APPROVED CURRENT / 已批准当前有效\n\n` +
    `Chinese business name / 中文业务称谓: ${record.aliasZh}\n` +
    `English operational name / 英文运行称谓: ${record.aliasEn}\n` +
    `Chinese control name / 中文控制规则: ${record.controlZh}\n` +
    `Cross-language bridge key / 跨语言桥接键: ${record.linkId}\n\n` +
    `Approved references / 已批准引用:\n` +
    `- control reference / 控制引用: ${record.controlId}\n` +
    `- runtime reference / 运行时引用: ${record.runtimeId}\n` +
    `- routing reference / 路由引用: ${record.routeId}\n` +
    `- release reference / 发布引用: ${record.releaseId}\n\n` +
    `Names in different languages are equivalent only through this registry record. ` +
    `不得仅凭相似翻译或相近文件名推断关联。\n`;
  await fs.writeFile(filePath, text, "utf8");
}

function wordRun(text, options = {}) {
  return new TextRun({
    text,
    font: "Arial",
    size: options.size ?? 22,
    bold: options.bold ?? false,
    color: options.color,
  });
}

function wordParagraph(text, options = {}) {
  return new Paragraph({
    children: [wordRun(text, options)],
    heading: options.heading,
    spacing: {
      before: options.before ?? 0,
      after: options.after ?? 120,
      line: options.line ?? 264,
    },
    alignment: options.alignment ?? AlignmentType.LEFT,
  });
}

async function writeEnglishArchitecture(filePath, record) {
  const borders = Object.fromEntries(
    ["top", "bottom", "left", "right", "insideHorizontal", "insideVertical"].map((edge) => [
      edge,
      { style: BorderStyle.SINGLE, size: 4, color: "D9DEE5" },
    ]),
  );
  const tableRows = [
    ["Control reference", record.controlId],
    ["Runtime reference", record.runtimeId],
    ["Fallback queue", record.queueId],
    ["Audit retention", `${record.retentionDays} days`],
    ["Release authority", record.releaseId],
  ].map((values, rowIndex) => new TableRow({
    children: values.map((value, columnIndex) => new TableCell({
      width: { size: columnIndex === 0 ? 2700 : 6660, type: WidthType.DXA },
      shading: rowIndex === 0 ? { fill: "F2F4F7" } : undefined,
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [wordParagraph(value, { bold: columnIndex === 0, after: 0 })],
    })),
  }));
  const document = new Document({
    creator: "TellerX multilingual benchmark",
    description: "English runtime control reference with opaque filename",
    styles: {
      default: {
        document: {
          run: { font: "Arial", size: 22 },
          paragraph: { spacing: { after: 120, line: 264 } },
        },
      },
      paragraphStyles: [
        {
          id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: "Arial", size: 32, bold: true, color: "2E74B5" },
          paragraph: { spacing: { before: 320, after: 160, line: 264 } },
        },
        {
          id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: "Arial", size: 26, bold: true, color: "2E74B5" },
          paragraph: { spacing: { before: 240, after: 120, line: 264 } },
        },
      ],
    },
    sections: [{
      properties: {
        page: {
          size: { width: 12240, height: 15840 },
          margin: { top: 1440, right: 1440, bottom: 1440, left: 1440, header: 708, footer: 708 },
        },
      },
      headers: {
        default: new Header({
          children: [wordParagraph("APPROVED RUNTIME CONTROL NOTE", { size: 18, color: "667085", after: 0 })],
        }),
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            alignment: AlignmentType.RIGHT,
            children: [
              wordRun("Page ", { size: 18, color: "667085" }),
              new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 18, color: "667085" }),
            ],
          })],
        }),
      },
      children: [
        wordParagraph("RUNTIME CONTROL NOTE", { size: 46, bold: true, color: "0B2545", after: 80 }),
        wordParagraph(`${record.controlId} / production governance`, { size: 28, color: "475467", after: 260 }),
        wordParagraph("Authority", { heading: HeadingLevel.HEADING_1 }),
        wordParagraph(`APPROVED AND CURRENT. The runtime identified by ${record.runtimeId} implements control ${record.controlId}. The business name is intentionally not repeated here.`),
        new Table({
          width: { size: 9360, type: WidthType.DXA },
          indent: { size: 120, type: WidthType.DXA },
          columnWidths: [2700, 6660],
          borders,
          rows: tableRows,
        }),
        wordParagraph("Failure handling", { heading: HeadingLevel.HEADING_1 }),
        wordParagraph(`If synchronous processing cannot complete, ${record.runtimeId} sends the request to ${record.queueId}. Audit evidence is retained for ${record.retentionDays} days.`),
        wordParagraph("Rejected historical note", { heading: HeadingLevel.HEADING_2 }),
        wordParagraph(`${record.legacyQueueId} was used during a prototype. It is retired and must not be selected for current operations.`),
      ],
    }],
  });
  await fs.writeFile(filePath, await Packer.toBuffer(document));
}

async function writeBilingualMatrix(filePath, record) {
  const workbook = Workbook.create();
  const retired = workbook.worksheets.add("历史参数 Retired");
  const current = workbook.worksheets.add("当前参数 Current");
  const audit = workbook.worksheets.add("审计线索 Audit");

  current.showGridLines = false;
  current.getRange("A1:G1").merge();
  current.getRange("A1:G1").values = [[`${record.controlId} - 当前生效参数 / APPROVED CURRENT PARAMETERS`]];
  current.getRange("A1:G1").format = {
    fill: "#17365D",
    font: { bold: true, color: "#FFFFFF", size: 15 },
    rowHeight: 30,
  };
  current.getRange("A3:G6").values = [
    ["控制引用 Control", "区域 Region", "当前门槛 CNY", "审批角色 Approver", "超时 ms", "状态 Status", "桥接键 Link"],
    [record.controlId, "亚太北区 / APAC North", record.apacThreshold, `${record.apacRoleZh} / ${record.apacRoleEn}`, record.timeoutMs, "已批准 / CURRENT", record.linkId],
    [record.controlId, "欧洲西区 / Europe West", record.euThreshold, `${record.euRoleZh} / ${record.euRoleEn}`, record.timeoutMs + 70, "已批准 / CURRENT", record.linkId],
    [record.controlId, "美国中区 / US Central", record.usThreshold, `${record.usRoleZh} / ${record.usRoleEn}`, record.timeoutMs + 35, "已批准 / CURRENT", record.linkId],
  ];
  current.getRange("A3:G3").format = {
    fill: "#D9EAF7",
    font: { bold: true, color: "#0B2545" },
    wrapText: true,
    rowHeight: 36,
  };
  current.getRange("A4:G6").format = {
    wrapText: true,
    borders: { preset: "inside", style: "thin", color: "#D9DEE5" },
  };
  current.getRange("C4:C6").format.numberFormat = "#,##0";
  current.getRange("E4:E6").format.numberFormat = "0";
  [17, 25, 17, 39, 12, 20, 16].forEach((width, index) => {
    current.getRangeByIndexes(0, index, 6, 1).format.columnWidth = width;
  });
  current.freezePanes.freezeRows(3);

  retired.showGridLines = false;
  retired.getRange("A1:G5").values = [
    ["历史参数 / RETIRED", "不得使用 / NOT CURRENT", "", "", "", "", record.linkId],
    ["Control", "Region", "旧门槛 Old threshold", "旧审批 Old approver", "旧超时", "Status", "Link"],
    [record.controlId, "APAC North", record.oldThreshold, "Temporary Operator", record.oldTimeoutMs, "RETIRED", record.linkId],
    [record.controlId, "Europe West", record.euThreshold - 52000, "Legacy Manager", record.oldTimeoutMs + 40, "RETIRED", record.linkId],
    [record.controlId, "US Central", record.usThreshold - 27000, "Legacy Manager", record.oldTimeoutMs + 20, "RETIRED", record.linkId],
  ];
  retired.getRange("A1:G1").format = { fill: "#7A1F1F", font: { bold: true, color: "#FFFFFF" } };
  retired.getRange("A2:G5").format = {
    font: { color: "#6B7280" },
    wrapText: true,
    borders: { preset: "inside", style: "thin", color: "#D9DEE5" },
  };
  [17, 20, 22, 25, 14, 16, 16].forEach((width, index) => {
    retired.getRangeByIndexes(0, index, 5, 1).format.columnWidth = width;
  });

  audit.showGridLines = false;
  audit.getRange("A1:C7").values = [
    ["审计线索 / AUDIT TRAIL", "值 / Value", "说明 / Meaning"],
    ["发布引用 / Release", record.releaseId, "See approved release record"],
    ["路由引用 / Route", record.routeId, "See English route contract"],
    ["亚太当前超时", null, "Formula points to current sheet"],
    ["历史超时", record.oldTimeoutMs, "Retired; do not use"],
    ["会议候选值", record.draftTimeoutMs, "Draft; never approved"],
    ["优先级 / Precedence", "Current > Retired > Draft", "Approval authority beats timestamp"],
  ];
  audit.getRange("B4").formulas = [["='当前参数 Current'!E4"]];
  audit.getRange("A1:C1").format = { fill: "#E8EEF5", font: { bold: true, color: "#0B2545" } };
  audit.getRange("A1:C7").format = {
    wrapText: true,
    borders: { preset: "inside", style: "thin", color: "#D9DEE5" },
  };
  audit.getRange("B2:B7").format.horizontalAlignment = "center";
  [26, 26, 44].forEach((width, index) => {
    audit.getRangeByIndexes(0, index, 7, 1).format.columnWidth = width;
  });

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(filePath);
  await fs.rm(`${filePath}.inspect.ndjson`, { force: true });
  return { expectedSheet: "当前参数 Current", expectedCellRange: "A1:G6" };
}

async function writeEnglishApi(filePath, record) {
  const html = `<!doctype html><html lang="en" data-corpus-revision="1"><head><meta charset="utf-8">` +
    `<title>Gateway contract</title></head><body><nav>Internal catalog / active contracts</nav>` +
    `<h1>Production authorization contract</h1>` +
    `<p>Status: <strong>APPROVED CURRENT</strong>. Routing reference: <code>${record.routeId}</code>. ` +
    `Control binding: <code>${record.controlId}</code>.</p>` +
    `<section><h2>Current operation</h2><dl>` +
    `<dt>HTTP operation</dt><dd><code>POST ${record.endpoint}</code></dd>` +
    `<dt>Policy validation rejection</dt><dd><code>${record.errorCode}</code></dd>` +
    `</dl></section><aside><h2>Deprecated appendix</h2>` +
    `<p><code>POST ${record.legacyEndpoint}</code> is retired and MUST NOT be used.</p></aside>` +
    `<footer>Current contract for ${record.routeId}; business aliases are maintained elsewhere.</footer></body></html>`;
  await fs.writeFile(filePath, html, "utf8");
}

function drawWrapped(page, font, text, x, y, options = {}) {
  const max = options.max ?? 82;
  const words = text.split(/\s+/);
  const lines = [];
  let line = "";
  for (const word of words) {
    if (!line || `${line} ${word}`.length <= max) line = line ? `${line} ${word}` : word;
    else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  for (const value of lines) {
    page.drawText(value, {
      x, y, size: options.size ?? 11, font,
      color: options.color ?? rgb(0.12, 0.12, 0.12),
    });
    y -= options.leading ?? 18;
  }
  return y;
}

async function writeEnglishRelease(filePath, record) {
  const pdf = await PDFDocument.create();
  pdf.setTitle("Approved multilingual control release");
  pdf.setAuthor("TellerX multilingual benchmark");
  const regular = await pdf.embedFont(StandardFonts.Helvetica);
  const bold = await pdf.embedFont(StandardFonts.HelveticaBold);

  const history = pdf.addPage([612, 792]);
  history.drawText("HISTORICAL REVIEW PAGE - NOT AUTHORITATIVE", {
    x: 42, y: 742, size: 15, font: bold, color: rgb(0.55, 0.1, 0.1),
  });
  let y = 700;
  y = drawWrapped(history, regular, `A workshop proposed threshold ${record.oldThreshold} CNY and timeout ${record.oldTimeoutMs} ms for ${record.controlId}.`, 48, y, { color: rgb(0.4, 0.4, 0.4) });
  y = drawWrapped(history, regular, `The proposal was superseded. Continue to page 2 for release ${record.releaseId}.`, 48, y - 14, { color: rgb(0.4, 0.4, 0.4) });
  history.drawText("Page 1 / historical material", { x: 48, y: 90, size: 10, font: regular, color: rgb(0.45, 0.45, 0.45) });

  const approved = pdf.addPage([612, 792]);
  approved.drawRectangle({
    x: 38, y: 330, width: 536, height: 415,
    borderColor: rgb(0.08, 0.25, 0.45), borderWidth: 2, color: rgb(0.95, 0.98, 1),
  });
  approved.drawText("APPROVED RELEASE RECORD", {
    x: 54, y: 708, size: 20, font: bold, color: rgb(0.08, 0.25, 0.45),
  });
  const lines = [
    `Release reference: ${record.releaseId}`,
    `Control activated: ${record.controlId}`,
    `Routing contract: ${record.routeId}`,
    `Effective date: ${record.effectiveDate}`,
    "Approved parameter source: Bilingual Matrix R4, current-parameter sheet.",
    `Historical threshold ${record.oldThreshold} CNY is explicitly retired.`,
    "Business names in Chinese and English must be resolved through the controlled vocabulary record.",
  ];
  y = 668;
  for (const line of lines) y = drawWrapped(approved, regular, line, 56, y, { max: 75, leading: 25, size: 12 });
  approved.drawText("SIGNED / APPROVED / CURRENT", {
    x: 56, y: 370, size: 14, font: bold, color: rgb(0.05, 0.35, 0.18),
  });
  approved.drawText("Page 2 / authority page", { x: 48, y: 90, size: 10, font: regular, color: rgb(0.45, 0.45, 0.45) });
  await fs.writeFile(filePath, await pdf.save());
  return { expectedPage: 2 };
}

async function writeDraft(filePath, record) {
  const text = `# 讨论记录 / WORKSHOP DRAFT\n\n` +
    `状态：草稿，未经批准。Status: DRAFT / NOT APPROVED.\n\n` +
    `有人把 ${record.aliasZh} 临时翻译为 ${record.aliasEn}，但本记录不是受控术语来源。` +
    `会议候选门槛为 ${record.draftThreshold}，候选超时为 ${record.draftTimeoutMs} ms。\n\n` +
    `Prototype endpoint: POST ${record.legacyEndpoint}\n` +
    `Prototype queue: ${record.legacyQueueId}\n\n` +
    `以上内容没有审批签名，不得覆盖正式术语登记表、当前参数表、运行时说明或发布记录。\n`;
  await fs.writeFile(filePath, text, "utf8");
}

function manifestRow(record, file, role, format, lifecycleStatus, extra = {}) {
  return {
    source_key: `crosslang-${record.seq}-${role}`,
    path: `documents/${file}`,
    logical_filename: file,
    logical_key: `crosslang/${record.linkId}/${role}`,
    project: PROJECT,
    document_type: role,
    source_type: format === "html" ? "confluence_export" : "upload",
    lifecycle_status: lifecycleStatus,
    version_label: lifecycleStatus === "approved" ? "multilingual-approved-r4" : "multilingual-draft-r9",
    owner: lifecycleStatus === "approved" ? "Knowledge Governance" : "Workshop Group",
    format,
    language: role === "business-rules-zh" || role === "workshop-draft" ? "zh" :
      role === "runtime-control-en" || role === "route-contract-en" || role === "release-record-en" ? "en" : "mixed",
    bridge_key: record.linkId,
    ...extra,
  };
}

function questionRows(record, files) {
  return [
    {
      id: `crosslang-${record.seq}-zh-to-en-failure`,
      kind: "zh-query-to-en-runtime",
      question: `${record.aliasZh}发生高金额授权时，正式调用哪个接口，策略拒绝码是什么，失败后进入哪个队列？`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.api, files.architecture],
      answer_contains: [record.endpoint, record.errorCode, record.queueId],
      forbidden_answer_values: [record.legacyEndpoint, record.legacyQueueId],
      project: PROJECT,
    },
    {
      id: `crosslang-${record.seq}-en-to-zh-governance`,
      kind: "en-query-to-zh-governance",
      question: `For ${record.aliasEn}, who is the Chinese governance owner, and what is the official Chinese control name?`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.requirement],
      answer_contains: [record.ownerZh, record.controlZh],
      forbidden_answer_values: [],
      project: PROJECT,
    },
    {
      id: `crosslang-${record.seq}-zh-to-mixed-parameters`,
      kind: "zh-query-to-bilingual-matrix",
      question: `${record.aliasZh}在亚太北区的当前门槛、审批角色和超时分别是多少？`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.matrix],
      answer_contains: [String(record.apacThreshold), record.apacRoleZh, String(record.timeoutMs)],
      forbidden_answer_values: [String(record.oldThreshold), String(record.draftThreshold), String(record.oldTimeoutMs), String(record.draftTimeoutMs)],
      expected_sheet: "当前参数 Current",
      expected_cell_range: "A1:G6",
      project: PROJECT,
    },
    {
      id: `crosslang-${record.seq}-en-to-mixed-release`,
      kind: "en-query-to-mixed-release-chain",
      question: `For ${record.aliasEn}, state the current APAC North threshold, the approved release reference, and its effective date.`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.matrix, files.release],
      answer_contains: [String(record.apacThreshold), record.releaseId, record.effectiveDate],
      forbidden_answer_values: [String(record.oldThreshold), String(record.draftThreshold)],
      expected_sheet: "当前参数 Current",
      expected_cell_range: "A1:G6",
      project: PROJECT,
    },
    {
      id: `crosslang-${record.seq}-mixed-comparison`,
      kind: "mixed-language-region-comparison",
      question: `For ${record.aliasEn}（${record.aliasZh}），比较亚太北区与 Europe West 的当前门槛和审批角色，哪个区域门槛更高？`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.matrix],
      answer_contains: [String(record.apacThreshold), String(record.euThreshold), record.apacRoleZh, record.euRoleEn],
      forbidden_answer_values: [String(record.oldThreshold), String(record.draftThreshold)],
      expected_sheet: "当前参数 Current",
      expected_cell_range: "A1:G6",
      project: PROJECT,
    },
    {
      id: `crosslang-${record.seq}-five-document-synthesis`,
      kind: "five-document-cross-language-synthesis",
      question: `For ${record.aliasEn}, 请基于已批准资料说明中文责任人、English API endpoint、亚太北区当前门槛和审计保留天数。`,
      expected_status: "answered",
      expected_filename: files.registry,
      expected_filenames: [files.registry, files.requirement, files.api, files.matrix, files.architecture],
      answer_contains: [record.ownerZh, record.endpoint, String(record.apacThreshold), String(record.retentionDays)],
      forbidden_answer_values: [record.legacyEndpoint, String(record.oldThreshold), String(record.draftThreshold)],
      project: PROJECT,
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
  const languageCounts = { zh: 0, en: 0, mixed: 0 };
  const formatCounts = Object.fromEntries(FORMATS.map((format) => [format, 0]));

  for (let index = 0; index < args.clusters; index += 1) {
    const record = recordFor(index);
    const files = filenames(record, args.seed);
    await writeChineseRequirement(path.join(documentsDir, files.requirement), record);
    await writeRegistry(path.join(documentsDir, files.registry), record);
    await writeEnglishArchitecture(path.join(documentsDir, files.architecture), record);
    const matrixLocation = await writeBilingualMatrix(path.join(documentsDir, files.matrix), record);
    await writeEnglishApi(path.join(documentsDir, files.api), record);
    const releaseLocation = await writeEnglishRelease(path.join(documentsDir, files.release), record);
    await writeDraft(path.join(documentsDir, files.draft), record);

    const rows = [
      manifestRow(record, files.requirement, "business-rules-zh", "md", "approved"),
      manifestRow(record, files.registry, "terminology-registry-mixed", "txt", "approved"),
      manifestRow(record, files.architecture, "runtime-control-en", "docx", "approved"),
      manifestRow(record, files.matrix, "parameter-matrix-mixed", "xlsx", "approved", matrixLocation),
      manifestRow(record, files.api, "route-contract-en", "html", "approved"),
      manifestRow(record, files.release, "release-record-en", "pdf", "approved", {
        ...releaseLocation,
        effective_at: `${record.effectiveDate}T00:00:00Z`,
      }),
      manifestRow(record, files.draft, "workshop-draft", "md", "draft"),
    ];
    manifest.push(...rows);
    for (const row of rows) {
      formatCounts[row.format] += 1;
      languageCounts[row.language] += 1;
    }
    questions.push(...questionRows(record, files));
    relationGraph.push({
      bridge_key: record.linkId,
      aliases: { zh: record.aliasZh, en: record.aliasEn },
      filenames: files,
      edges: [
        [record.aliasZh, "translated_as", record.aliasEn],
        [record.aliasEn, "registered_by", record.linkId],
        [record.linkId, "governed_by", record.controlId],
        [record.controlId, "implemented_by", record.runtimeId],
        [record.controlId, "routed_by", record.routeId],
        [record.controlId, "activated_by", record.releaseId],
        [record.runtimeId, "fallback_to", record.queueId],
        [record.routeId, "rejects_with", record.errorCode],
      ],
    });
  }

  const missingAliases = [
    ["岚桥归档", "Mistbridge Archive"],
    ["星港授信", "Starport Credit"],
    ["青岚退款", "Azuremist Refund"],
    ["玄羽收单", "Blackwing Acquiring"],
    ["银杉税务", "Silverfir Tax"],
    ["云舟计费", "Cloudark Billing"],
    ["曦光争议", "Daybreak Dispute"],
    ["翠湖清算", "Greenlake Clearing"],
    ["岚桥凭证", "Mistbridge Voucher"],
    ["星港付款", "Starport Payment"],
    ["青岚票据", "Azuremist Invoice"],
    ["玄羽税务", "Blackwing Tax"],
  ];
  missingAliases.forEach(([aliasZh, aliasEn], index) => {
    const mode = index % 3;
    questions.push({
      id: `crosslang-missing-${String(index + 1).padStart(2, "0")}`,
      kind: mode === 0 ? "unanswerable-zh-alias" : mode === 1 ? "unanswerable-en-alias" : "unanswerable-mixed-alias",
      question: mode === 0
        ? `${aliasZh}当前使用哪个正式接口、门槛和降级队列？`
        : mode === 1
          ? `For ${aliasEn}, what are the current endpoint, threshold, and fallback queue?`
          : `For ${aliasEn}, 中文称谓 ${aliasZh} 的 approved control 与当前参数是什么？`,
      expected_status: "insufficient_evidence",
      expected_filename: null,
      expected_filenames: [],
      answer_contains: [],
      forbidden_answer_values: [],
      project: PROJECT,
    });
  });

  const jsonLines = (rows) => `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`;
  await fs.writeFile(path.join(outputDir, "manifest.jsonl"), jsonLines(manifest), "utf8");
  await fs.writeFile(path.join(outputDir, "questions.jsonl"), jsonLines(questions), "utf8");
  await fs.writeFile(path.join(outputDir, "relation-graph.json"), `${JSON.stringify(relationGraph, null, 2)}\n`, "utf8");
  const summary = {
    seed: args.seed,
    project: PROJECT,
    clusters: args.clusters,
    document_count: manifest.length,
    approved_documents: manifest.filter((row) => row.lifecycle_status === "approved").length,
    draft_documents: manifest.filter((row) => row.lifecycle_status === "draft").length,
    question_count: questions.length,
    answerable_questions: questions.filter((row) => row.expected_status === "answered").length,
    unanswerable_questions: questions.filter((row) => row.expected_status === "insufficient_evidence").length,
    language_counts: languageCounts,
    format_counts: formatCounts,
    opaque_filename_rule: "12 lowercase hexadecimal characters plus extension; no business alias or relation ID",
    generated_at: new Date().toISOString(),
  };
  await fs.writeFile(path.join(outputDir, "generation-summary.json"), `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

await main();
