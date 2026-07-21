/**
 * STUB -- synthetic matter seeder.
 *
 * Lands with the thin-slice step. Its job is to write a handful of realistic
 * matters into the DynamoDB table so the agent has something to reason about
 * before any real document ever enters the system.
 *
 * Everything here is fabricated. No real broker, employer, member, or claim
 * data belongs in this file or in any dev environment -- that is the whole
 * reason a seeder exists.
 */

/** A document the matter requires, and whether it has shown up yet. */
export interface RequiredDocument {
  /** Stable identifier, e.g. 'signed-employer-application'. */
  readonly docType: string;
  /** Human label used in reminder copy. */
  readonly label: string;
  readonly status: 'missing' | 'received' | 'in-review';
  /** ISO-8601 date the document is due. */
  readonly dueDate: string;
  /** S3 key of the received object, once there is one. */
  readonly sourceKey?: string;
  /** BDA extraction confidence, 0-1. Below threshold routes to human review. */
  readonly confidence?: number;
}

/** One thing the agent (or a human) did, appended in order. Never rewritten. */
export interface MatterAction {
  readonly timestamp: string;
  readonly action: 'reminder_sent' | 'followup_scheduled' | 'escalated' | 'anomaly_flagged' | 'note';
  readonly actor: 'agent' | 'human';
  /** Why. This is the audit trail, so it has to be specific. */
  readonly reason: string;
  readonly docType?: string;
}

/** The shape the agent reads. Partition key is `matterId`. */
export interface Matter {
  readonly matterId: string;
  readonly matterType: 'group-renewal' | 'claim' | 'onboarding' | 'closing';
  readonly clientName: string;
  /** Who gets chased -- a broker or employer contact, never an insured. */
  readonly counterpartyName: string;
  readonly counterpartyEmail: string;
  readonly openedAt: string;
  readonly targetCloseDate: string;
  readonly requiredDocuments: readonly RequiredDocument[];
  readonly actionHistory: readonly MatterAction[];
  readonly status: 'open' | 'blocked' | 'awaiting-review' | 'closed';
}

/** One worked example, to pin down the shape. */
export const EXAMPLE_MATTER: Matter = {
  matterId: 'MTR-2026-0142',
  matterType: 'group-renewal',
  clientName: 'Northwind Manufacturing',
  counterpartyName: 'Dana Whitfield',
  counterpartyEmail: 'dana.whitfield@example-brokerage.test',
  openedAt: '2026-06-15T14:02:00Z',
  targetCloseDate: '2026-08-01',
  requiredDocuments: [
    {
      docType: 'signed-employer-application',
      label: 'Signed employer application',
      status: 'missing',
      dueDate: '2026-07-23',
    },
    {
      docType: 'current-census',
      label: 'Current employee census',
      status: 'received',
      dueDate: '2026-07-10',
      sourceKey: 'inbound/2026/07/08/northwind-census.xlsx',
      confidence: 0.97,
    },
    {
      docType: 'prior-carrier-billing',
      label: 'Prior carrier billing statement',
      status: 'in-review',
      dueDate: '2026-07-18',
      sourceKey: 'inbound/2026/07/16/northwind-billing.pdf',
      confidence: 0.62,
    },
  ],
  actionHistory: [
    {
      timestamp: '2026-07-16T09:15:00Z',
      action: 'reminder_sent',
      actor: 'agent',
      reason:
        'Signed employer application missing, due 2026-07-23. No prior contact on this document.',
      docType: 'signed-employer-application',
    },
    {
      timestamp: '2026-07-16T11:40:00Z',
      action: 'anomaly_flagged',
      actor: 'agent',
      reason:
        'Prior carrier billing statement extracted at 0.62 confidence, below the 0.80 threshold. Routed to human review rather than accepted.',
      docType: 'prior-carrier-billing',
    },
  ],
  status: 'blocked',
};

// TODO(thin-slice step): implement the seeder.
//   1. Read the table name from SSM or an env var rather than hard-coding it.
//   2. Generate a spread of matters that exercise every branch the agent has to
//      handle -- nothing missing (should do nothing), one item missing and not
//      yet due, one due in 48 hours, one already overdue (should escalate, not
//      remind), one at the reminder cap, one with a low-confidence extraction.
//      The interesting test cases are the ones where the right answer is
//      "do nothing" or "escalate", not "send a reminder".
//   3. BatchWriteItem via @aws-sdk/lib-dynamodb.
//   4. Refuse to run against any stage other than dev.

export {};
