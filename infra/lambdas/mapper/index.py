# Mapper Lambda -- turns a finished BDA job into matter state.
#
# Trigger: an EventBridge rule on source aws.bedrock, detail-types
#          "Bedrock Data Automation Job Succeeded",
#          "... Failed With Client Error", "... Failed With Service Error".
#          ("Job Created" is intentionally not subscribed.)
#
# Job:     for a succeeded job, read the extracted fields + per-field confidence,
#          re-run resolve_matter on the original input key, and write matter
#          state -- either onto the matter's DOC# row, or onto a TRIAGE row if
#          the document could not be associated (ADR-005). For a failed job,
#          record the failure and emit the right metric.
#
# ROBUSTNESS NOTE: the exact field names inside the BDA completion event `detail`
# are not reliably documented (AWS lists job_id / job_status; the accelerator saw
# input_s3_object.name). Rather than depend on them, this function pulls the job
# identifier defensively and calls GetDataAutomationStatus, which is the
# authoritative source for the output location and input. The full event is
# logged so the exact shape can be confirmed on the first real deploy and the
# parsing tightened. Until confirmed, treat the detail-parsing as provisional.

import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["MATTER_TABLE"]
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Ida/Understanding")
DOC_TYPE = os.environ.get("DOC_TYPE", "census")  # this slice handles one class

bda = boto3.client("bedrock-data-automation-runtime")
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")
table = boto3.resource("dynamodb").Table(TABLE_NAME)

_MATTER_KEY_RE = re.compile(r"^matters/(?P<matterId>[^/]+)/")


def resolve_matter(object_key: str) -> dict:
    """ADR-005 seam -- MUST stay identical to the submit Lambda's copy."""
    m = _MATTER_KEY_RE.match(object_key)
    if m:
        return {"matterId": m.group("matterId"), "source": "key-prefix", "confidence": 1.0}
    return {"matterId": None, "source": "unresolved", "confidence": 0.0}


def put_metric(name: str, value: float = 1.0) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
        )
    except Exception:
        logger.warning("failed to emit metric %s", name, exc_info=True)


def extract_job_id(detail: dict) -> str:
    """Defensive: try the documented and observed field names in order."""
    for k in ("invocationArn", "job_id", "jobId", "invocation_arn"):
        if detail.get(k):
            return detail[k]
    raise KeyError(f"no job identifier in event detail; keys={list(detail.keys())}")


def read_output(output_s3_uri: str) -> dict:
    """Read the BDA result manifest from its output location. The exact output
    layout is confirmed on first deploy; job_metadata.json is the standard entry
    point. Returns {} if nothing readable is found, so a malformed output routes
    to review rather than crashing."""
    parsed = urlparse(output_s3_uri)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    for obj in listing.get("Contents", []):
        if obj["Key"].endswith(".json"):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue
    return {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_matter_document(matter_id: str, fields: dict, min_confidence: float, source_key: str) -> None:
    """Upsert the DOC#<docType> row on the matter, and keep the GSI in sync so
    the missing-docs-by-due-date query and the readout stay accurate."""
    accepted = min_confidence >= CONFIDENCE_THRESHOLD
    status = "received" if accepted else "in-review"
    put_metric("DocumentsAccepted" if accepted else "DocumentsRoutedToReview")

    # Flip the required-document row from missing -> received/in-review. Remove it
    # from the STATUS#missing GSI partition by moving GSI1PK to the new status.
    table.update_item(
        Key={"PK": f"MATTER#{matter_id}", "SK": f"DOC#{DOC_TYPE}"},
        UpdateExpression=(
            "SET #st = :st, sourceKey = :sk, extractionConfidence = :conf, "
            "extractedFields = :fields, updatedAt = :now, "
            "GSI1PK = :gpk, GSI1SK = :gsk"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":st": status,
            ":sk": source_key,
            ":conf": _decimalize(min_confidence),
            ":fields": _decimalize(fields),
            ":now": now_iso(),
            ":gpk": f"STATUS#{status}",
            ":gsk": f"RECEIVED#{now_iso()}",
        },
    )


def write_triage_item(document_id: str, fields: dict, source_key: str, reason: str) -> None:
    """ADR-005: an unassociated document goes to the triage queue with its
    extracted fields (so a human can place it) -- never auto-assigned. Indexed
    under STATUS#needs_triage so the readout can count it with one query."""
    put_metric("DocumentsSentToTriage")
    table.put_item(
        Item={
            "PK": "TRIAGE",
            "SK": f"DOC#{document_id}",
            "status": "needs_triage",
            "sourceKey": source_key,
            "reason": reason,
            "extractedFields": _decimalize(fields),
            "receivedAt": now_iso(),
            "GSI1PK": "STATUS#needs_triage",
            "GSI1SK": f"RECEIVED#{now_iso()}",
        }
    )


def _decimalize(value):
    """DynamoDB rejects float; round-trip through Decimal via JSON string."""
    import decimal

    return json.loads(json.dumps(value), parse_float=decimal.Decimal)


def parse_extraction(output: dict):
    """Pull (fields, min_confidence, input_key) from the BDA result manifest.
    Defensive -- the exact manifest schema is confirmed on first deploy. Returns
    ({}, 0.0, None) when nothing is parseable, which routes to review/triage
    rather than crashing."""
    # BDA custom-output results carry inference_result + explainability with
    # per-field confidence. Shapes vary; try the common ones.
    fields = (
        output.get("inference_result")
        or output.get("inferenceResult")
        or output.get("custom_output", {}).get("inference_result")
        or {}
    )
    confidences = []
    explain = output.get("explainability_info") or output.get("explainabilityInfo") or []
    if isinstance(explain, list):
        for block in explain:
            for v in (block or {}).values():
                if isinstance(v, dict) and "confidence" in v:
                    try:
                        confidences.append(float(v["confidence"]))
                    except (TypeError, ValueError):
                        pass
    min_conf = min(confidences) if confidences else 0.0

    input_key = None
    meta = output.get("job_metadata") or output.get("metadata") or {}
    input_uri = meta.get("input_s3_object", {}).get("s3_uri") or meta.get("inputS3Uri")
    if input_uri:
        input_key = urlparse(input_uri).path.lstrip("/")

    return fields, min_conf, input_key


def handler(event: dict, context) -> dict:
    logger.info("event: %s", json.dumps(event))
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    if "Failed" in detail_type:
        # The client/service split IS our retryable/non-retryable classification,
        # delivered by event type (docs/bda-orchestration-reference.md).
        if "Client Error" in detail_type:
            put_metric("BDAJobsFailedClientError")
        else:
            put_metric("BDAJobsFailedServiceError")
        put_metric("BDAJobsFailed")
        logger.error("BDA job failed: %s detail=%s", detail_type, json.dumps(detail))
        return {"status": "failed", "detailType": detail_type}

    put_metric("BDAJobsTotal")
    put_metric("BDAJobsSucceeded")

    job_id = extract_job_id(detail)
    status = bda.get_data_automation_status(invocationArn=job_id)
    output_uri = status.get("outputConfiguration", {}).get("s3Uri")
    if not output_uri:
        logger.error("no output location for job %s: %s", job_id, json.dumps(status, default=str))
        return {"status": "no-output"}

    output = read_output(output_uri)
    fields, min_conf, input_key = parse_extraction(output)

    if not input_key:
        # Fall back to the defensively-parsed event field if the manifest did not
        # carry the input reference. Confirmed/tightened on first deploy.
        input_key = detail.get("input_s3_object", {}).get("name")
    if not input_key:
        logger.error("could not determine input key; routing to triage")
        write_triage_item(job_id, fields, "unknown", "no-input-key-on-completion")
        return {"status": "triage", "reason": "no-input-key"}

    resolution = resolve_matter(input_key)
    document_id = input_key.rsplit("/", 1)[-1]

    if resolution["matterId"] is None:
        write_triage_item(document_id, fields, input_key, "unresolved-at-ingestion")
        return {"status": "triage", "documentId": document_id}

    write_matter_document(resolution["matterId"], fields, min_conf, input_key)
    return {
        "status": "mapped",
        "matterId": resolution["matterId"],
        "docType": DOC_TYPE,
        "minConfidence": min_conf,
    }
