# escalate_to_human — the agent's one tool for the Step 6 thin slice.
#
# Invoked through AgentCore Gateway (the agent never holds AWS credentials; the
# Gateway does, and logs every call). The tool hands a matter to a human with
# the agent's stated reason attached, by:
#   1. appending an ACTION#escalate#<docType> row to the matter (the audit
#      trail), conditionally so a retried invocation cannot double-escalate; then
#   2. publishing to the escalation SNS topic (email to the internal owner) --
#      but only if the conditional write won, so SNS never fires twice.
# Record-then-act, the same idempotency shape the mapper uses.
#
# CONTRACT NOTE (confirm on first deploy): the exact event shape AgentCore
# Gateway delivers to a Lambda tool target is not fully pinned in the docs. This
# handler is defensive -- it logs the full event and pulls the tool arguments
# from the shapes seen in AWS examples (event directly, or event['input'] /
# ['arguments'] / ['body']). Tighten once the first real invocation confirms it.

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["MATTER_TABLE"]
ESCALATION_TOPIC_ARN = os.environ["ESCALATION_TOPIC_ARN"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Ida/Agent")

sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def put_metric(name: str, value: float = 1.0) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
        )
    except Exception:
        logger.warning("failed to emit metric %s", name, exc_info=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_args(event: dict) -> dict:
    """Pull the tool arguments defensively from whatever envelope Gateway uses."""
    for key in ("input", "arguments", "body", "parameters"):
        v = event.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = None
        if isinstance(v, dict):
            return v
    # Fall back to the event itself carrying the fields directly.
    return event


def handler(event: dict, context) -> dict:
    logger.info("escalate_to_human event: %s", json.dumps(event, default=str))
    args = extract_args(event)

    matter_id = args.get("matterId") or args.get("matter_id")
    reason = args.get("reason") or ""
    doc_type = args.get("docType") or args.get("doc_type") or "matter"

    if not matter_id or not reason:
        put_metric("EscalateRejectedBadInput")
        # Narrow, typed contract: the agent must name the matter and give a
        # reason. Missing either is a caller error, surfaced to the agent.
        return {
            "status": "error",
            "error": "escalate_to_human requires matterId and reason",
        }

    escalated_at = now_iso()
    sk = f"ACTION#escalate#{doc_type}"

    try:
        table.put_item(
            Item={
                "PK": f"MATTER#{matter_id}",
                "SK": sk,
                "action": "escalated",
                "actor": "agent",
                "reason": reason,
                "docType": doc_type,
                "escalatedAt": escalated_at,
            },
            # Idempotency: one escalation per matter+doc. A retried invocation
            # finds the row already present and does not re-notify.
            ConditionExpression="attribute_not_exists(SK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("already escalated %s / %s -- no duplicate notification", matter_id, sk)
            put_metric("EscalateDeduplicated")
            return {"status": "already_escalated", "matterId": matter_id, "docType": doc_type}
        raise

    # Only reached when the conditional write won -> notify exactly once.
    sns.publish(
        TopicArn=ESCALATION_TOPIC_ARN,
        Subject=f"[Ida] Matter {matter_id} escalated for review",
        Message=(
            f"Matter {matter_id} has been escalated to a human by the "
            f"Document-Chase Agent.\n\nDocument: {doc_type}\nReason: {reason}\n"
            f"Escalated at: {escalated_at}\n"
        ),
    )
    put_metric("EscalateSucceeded")
    logger.info("escalated %s / %s and notified", matter_id, sk)
    return {"status": "escalated", "matterId": matter_id, "docType": doc_type, "escalatedAt": escalated_at}
