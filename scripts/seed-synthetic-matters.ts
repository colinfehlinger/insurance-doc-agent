/**
 * Synthetic matter seeder for the Step 5 thin slice.
 *
 * Writes a handful of fabricated matters into the single-table DynamoDB design
 * and uploads a synthetic census PDF per matter under the ADR-005 key
 * convention (`matters/{matterId}/...`), plus one document under `unassociated/`
 * to exercise the triage path end to end.
 *
 * Everything here is fabricated. No real broker, employer, member, or claim data
 * belongs in this file or in any environment -- that is the whole reason a
 * seeder exists.
 *
 * Dependency-free on purpose: it shells out to the AWS CLI (already required and
 * configured) rather than importing an SDK, and builds a minimal valid PDF by
 * hand rather than pulling a PDF library. Run it from infra/ so tsx resolves:
 *
 *     cd infra && npx tsx ../scripts/seed-synthetic-matters.ts
 *
 * Refuses to run against any stage other than dev.
 */

import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const STAGE = 'dev';
const REGION = 'us-east-1';
const TABLE = `ida-${STAGE}-matters`;

// --- Types (the shape the agent reads) -------------------------------------

export interface RequiredDocument {
  readonly docType: string;
  readonly label: string;
  readonly status: 'missing' | 'received' | 'in-review';
  readonly dueDate: string; // ISO-8601 date
  readonly sourceKey?: string;
  readonly confidence?: number;
}

export interface MatterAction {
  readonly timestamp: string;
  readonly action: 'reminder_sent' | 'followup_scheduled' | 'escalated' | 'anomaly_flagged' | 'note';
  readonly actor: 'agent' | 'human';
  readonly reason: string;
  readonly docType?: string;
}

export interface Matter {
  readonly matterId: string;
  readonly matterType: 'group-renewal' | 'claim' | 'onboarding' | 'closing';
  readonly clientName: string;
  readonly counterpartyName: string;
  readonly counterpartyEmail: string;
  readonly openedAt: string;
  readonly targetCloseDate: string;
  readonly requiredDocuments: readonly RequiredDocument[];
  readonly actionHistory: readonly MatterAction[];
  readonly status: 'open' | 'blocked' | 'awaiting-review' | 'closed';
  /** Census content used to render this matter's synthetic document. */
  readonly census: CensusContent;
}

interface CensusContent {
  readonly employerName: string;
  readonly groupNumber: string;
  readonly planEffectiveDate: string;
  readonly employees: ReadonlyArray<{ name: string; tier: string }>;
}

// --- Synthetic matters ------------------------------------------------------
// Chosen to exercise the branches the pipeline must handle, not just the happy
// path. The census is the document that arrives; other required docs stay
// missing so the readout has something to show.

const MATTERS: Matter[] = [
  {
    matterId: 'MTR-2026-0142',
    matterType: 'group-renewal',
    clientName: 'Northwind Manufacturing',
    counterpartyName: 'Dana Whitfield',
    counterpartyEmail: 'dana.whitfield@example-brokerage.test',
    openedAt: '2026-06-15T14:02:00Z',
    targetCloseDate: '2026-08-01',
    status: 'blocked',
    requiredDocuments: [
      { docType: 'census', label: 'Current employee census', status: 'missing', dueDate: '2026-07-24' },
      { docType: 'signed-employer-application', label: 'Signed employer application', status: 'missing', dueDate: '2026-07-28' },
    ],
    actionHistory: [
      {
        timestamp: '2026-07-16T09:15:00Z',
        action: 'reminder_sent',
        actor: 'agent',
        reason: 'Census missing, due 2026-07-24. No prior contact on this document.',
        docType: 'census',
      },
    ],
    census: {
      employerName: 'Northwind Manufacturing',
      groupNumber: 'GRP-88213',
      planEffectiveDate: '2026-09-01',
      employees: [
        { name: 'Alice Reyes', tier: 'Employee Only' },
        { name: 'Marcus Webb', tier: 'Employee+Spouse' },
        { name: 'Priya Nair', tier: 'Family' },
      ],
    },
  },
  {
    matterId: 'MTR-2026-0157',
    matterType: 'group-renewal',
    clientName: 'Cedarline Logistics',
    counterpartyName: 'Sam Okafor',
    counterpartyEmail: 'sam.okafor@example-brokerage.test',
    openedAt: '2026-07-01T10:00:00Z',
    targetCloseDate: '2026-08-15',
    status: 'open',
    requiredDocuments: [
      { docType: 'census', label: 'Current employee census', status: 'missing', dueDate: '2026-08-05' },
    ],
    actionHistory: [],
    census: {
      employerName: 'Cedarline Logistics',
      groupNumber: 'GRP-90455',
      planEffectiveDate: '2026-10-01',
      employees: [
        { name: 'Jordan Blake', tier: 'Employee Only' },
        { name: 'Tara Lindqvist', tier: 'Employee+Children' },
      ],
    },
  },
];

// A census with NO group number, to exercise the low-confidence / review path:
// BDA should return the field empty or low-confidence, routing to in-review.
const AMBIGUOUS_MATTER: Matter = {
  matterId: 'MTR-2026-0163',
  matterType: 'group-renewal',
  clientName: 'Harbor Point Foods',
  counterpartyName: 'Lee Contreras',
  counterpartyEmail: 'lee.contreras@example-brokerage.test',
  openedAt: '2026-07-05T12:00:00Z',
  targetCloseDate: '2026-08-20',
  status: 'open',
  requiredDocuments: [
    { docType: 'census', label: 'Current employee census', status: 'missing', dueDate: '2026-08-08' },
  ],
  actionHistory: [],
  census: {
    employerName: 'Harbor Point Foods',
    groupNumber: '', // deliberately absent
    planEffectiveDate: '2026-10-15',
    employees: [{ name: 'Otis Pemberton', tier: 'Employee Only' }],
  },
};

// --- Minimal PDF writer (no dependencies) -----------------------------------
// BDA document processing expects a document format, not plain text. This builds
// a byte-correct single-page PDF with the census rendered as text, so BDA has a
// real document to OCR/extract. Offsets in the xref table are computed so the
// file is valid.

function buildCensusPdf(c: CensusContent): Buffer {
  const lines = [
    'EMPLOYEE CENSUS',
    `Employer: ${c.employerName}`,
    c.groupNumber ? `Group Number: ${c.groupNumber}` : 'Group Number: (not shown)',
    `Plan Effective Date: ${c.planEffectiveDate}`,
    `Enrolled Employees: ${c.employees.length}`,
    '',
    ...c.employees.map((e, i) => `${i + 1}. ${e.name} - ${e.tier}`),
  ];

  const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
  let text = 'BT /F1 12 Tf 50 740 Td 16 TL\n';
  lines.forEach((l, i) => {
    text += i === 0 ? `(${esc(l)}) Tj\n` : `T* (${esc(l)}) Tj\n`;
  });
  text += 'ET';

  const objs = [
    '<</Type/Catalog/Pages 2 0 R>>',
    '<</Type/Pages/Kids[3 0 R]/Count 1>>',
    '<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>',
    '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>',
    `<</Length ${Buffer.byteLength(text, 'latin1')}>>\nstream\n${text}\nendstream`,
  ];

  let pdf = '%PDF-1.4\n';
  const offsets: number[] = [];
  objs.forEach((body, i) => {
    offsets.push(Buffer.byteLength(pdf, 'latin1'));
    pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xrefStart = Buffer.byteLength(pdf, 'latin1');
  pdf += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n`;
  offsets.forEach((o) => {
    pdf += `${String(o).padStart(10, '0')} 00000 n \n`;
  });
  pdf += `trailer\n<</Size ${objs.length + 1}/Root 1 0 R>>\nstartxref\n${xrefStart}\n%%EOF`;
  return Buffer.from(pdf, 'latin1');
}

// --- DynamoDB item construction (single-table design) -----------------------

function ddbMetaItem(m: Matter): string {
  return JSON.stringify({
    PK: { S: `MATTER#${m.matterId}` },
    SK: { S: 'META' },
    matterType: { S: m.matterType },
    clientName: { S: m.clientName },
    counterpartyName: { S: m.counterpartyName },
    counterpartyEmail: { S: m.counterpartyEmail },
    openedAt: { S: m.openedAt },
    targetCloseDate: { S: m.targetCloseDate },
    status: { S: m.status },
  });
}

function ddbDocItem(m: Matter, d: RequiredDocument): string {
  const item: Record<string, unknown> = {
    PK: { S: `MATTER#${m.matterId}` },
    SK: { S: `DOC#${d.docType}` },
    label: { S: d.label },
    status: { S: d.status },
    dueDate: { S: d.dueDate },
    // Missing docs live in the STATUS#missing GSI partition, ordered by due date,
    // so the scheduled sweep and readout can query rather than scan.
    GSI1PK: { S: `STATUS#${d.status}` },
    GSI1SK: { S: `DUE#${d.dueDate}` },
  };
  return JSON.stringify(item);
}

function ddbActionItem(m: Matter, a: MatterAction): string {
  return JSON.stringify({
    PK: { S: `MATTER#${m.matterId}` },
    SK: { S: `ACTION#${a.timestamp}` },
    action: { S: a.action },
    actor: { S: a.actor },
    reason: { S: a.reason },
    ...(a.docType ? { docType: { S: a.docType } } : {}),
  });
}

// --- AWS CLI helpers --------------------------------------------------------

function aws(args: string[]): void {
  execFileSync('aws', [...args, '--region', REGION], { stdio: 'inherit' });
}

function putItem(itemJson: string): void {
  aws(['dynamodb', 'put-item', '--table-name', TABLE, '--item', itemJson]);
}

// --- Main -------------------------------------------------------------------

function main(): void {
  if (STAGE !== 'dev') {
    throw new Error(`Refusing to seed stage '${STAGE}'. This script is dev-only.`);
  }
  const rawBucketEnv = process.env.RAW_BUCKET;
  if (!rawBucketEnv) {
    throw new Error(
      'Set RAW_BUCKET to the ida-dev-raw-<account> bucket name, e.g.\n' +
        '  RAW_BUCKET=ida-dev-raw-000000000000 npx tsx ../scripts/seed-synthetic-matters.ts',
    );
  }

  const work = mkdtempSync(join(tmpdir(), 'ida-seed-'));
  const all = [...MATTERS, AMBIGUOUS_MATTER];

  for (const m of all) {
    console.log(`\n=== ${m.matterId} (${m.clientName}) ===`);
    putItem(ddbMetaItem(m));
    for (const d of m.requiredDocuments) putItem(ddbDocItem(m, d));
    for (const a of m.actionHistory) putItem(ddbActionItem(m, a));

    // Upload the census PDF under the ADR-005 key convention.
    const pdf = buildCensusPdf(m.census);
    const local = join(work, `${m.matterId}-census.pdf`);
    writeFileSync(local, pdf);
    const key = `matters/${m.matterId}/census.pdf`;
    aws(['s3', 'cp', local, `s3://${rawBucketEnv}/${key}`]);
    console.log(`  uploaded ${key}`);
  }

  // One document that does NOT match the key convention, to exercise triage.
  const orphan = buildCensusPdf({
    employerName: 'Unknown Sender Corp',
    groupNumber: 'GRP-70011',
    planEffectiveDate: '2026-11-01',
    employees: [{ name: 'Casey Idris', tier: 'Employee Only' }],
  });
  const orphanLocal = join(work, 'orphan-census.pdf');
  writeFileSync(orphanLocal, orphan);
  aws(['s3', 'cp', orphanLocal, `s3://${rawBucketEnv}/unassociated/orphan-census.pdf`]);
  console.log('\n  uploaded unassociated/orphan-census.pdf (exercises triage)');

  console.log('\nSeed complete. BDA jobs will start via S3 -> EventBridge; run the readout in ~1-2 min.');
}

main();
