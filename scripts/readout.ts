/**
 * The 30-second status readout for the thin slice.
 *
 * Prints, per matter, which documents are received / in-review / missing, and --
 * prominently, as a call to action -- the count of documents awaiting triage
 * (ADR-005: operator association is a primary path, so this queue is where the
 * real work shows up, not a footnote).
 *
 * Dependency-free: shells out to the AWS CLI. Run from infra/:
 *
 *     cd infra && npx tsx ../scripts/readout.ts
 */

import { execFileSync } from 'node:child_process';

const REGION = 'us-east-1';
const TABLE = 'ida-dev-matters';
const GSI = 'GSI1';

function awsJson(args: string[]): any {
  const out = execFileSync('aws', [...args, '--region', REGION, '--output', 'json'], {
    encoding: 'utf-8',
  });
  return JSON.parse(out);
}

function s(v: any): string {
  return v && v.S ? v.S : '';
}

function queryTriage(): any[] {
  const res = awsJson([
    'dynamodb',
    'query',
    '--table-name',
    TABLE,
    '--index-name',
    GSI,
    '--key-condition-expression',
    'GSI1PK = :p',
    '--expression-attribute-values',
    JSON.stringify({ ':p': { S: 'STATUS#needs_triage' } }),
  ]);
  return res.Items ?? [];
}

function scanMatters(): Map<string, any[]> {
  // Thin slice: a scan is fine at this volume. The GSI exists for the scheduled
  // sweep (missing-docs-by-due-date), not for this readout.
  const res = awsJson(['dynamodb', 'scan', '--table-name', TABLE]);
  const byMatter = new Map<string, any[]>();
  for (const item of res.Items ?? []) {
    const pk = s(item.PK);
    if (!pk.startsWith('MATTER#')) continue;
    const id = pk.slice('MATTER#'.length);
    if (!byMatter.has(id)) byMatter.set(id, []);
    byMatter.get(id)!.push(item);
  }
  return byMatter;
}

const STATUS_MARK: Record<string, string> = {
  received: '[received]  ',
  'in-review': '[in-review] ',
  missing: '[missing]   ',
};

function main(): void {
  const byMatter = scanMatters();
  const triage = queryTriage();

  console.log('\n============================================================');
  console.log('  DOCUMENT-CHASE AGENT — MATTER READOUT (dev)');
  console.log('============================================================');

  if (triage.length > 0) {
    console.log(`\n  >>> ACTION NEEDED: ${triage.length} document(s) awaiting triage <<<`);
    console.log('  These arrived but could not be matched to a matter. A human');
    console.log('  must place them (ADR-005) — they are never auto-assigned.');
    for (const t of triage) {
      console.log(`      - ${s(t.sourceKey)}   (${s(t.reason)})`);
    }
  } else {
    console.log('\n  Triage queue: empty.');
  }

  const ids = [...byMatter.keys()].sort();
  for (const id of ids) {
    const rows = byMatter.get(id)!;
    const meta = rows.find((r) => s(r.SK) === 'META');
    const docs = rows.filter((r) => s(r.SK).startsWith('DOC#'));
    const actions = rows.filter((r) => s(r.SK).startsWith('ACTION#'));

    console.log(`\n  ${id}  ${meta ? s(meta.clientName) : '(no meta)'}  [${meta ? s(meta.status) : '?'}]`);
    if (meta) console.log(`     target close ${s(meta.targetCloseDate)} · chase ${s(meta.counterpartyName)}`);
    for (const d of docs) {
      const st = s(d.status);
      const mark = STATUS_MARK[st] ?? `[${st}] `;
      const conf = d.extractionConfidence ? `  conf=${d.extractionConfidence.N ?? ''}` : '';
      console.log(`     ${mark}${s(d.SK).slice('DOC#'.length)}  due ${s(d.dueDate)}${conf}`);
    }
    if (actions.length) console.log(`     ${actions.length} action(s) on record`);
  }

  console.log('\n============================================================\n');
}

main();
