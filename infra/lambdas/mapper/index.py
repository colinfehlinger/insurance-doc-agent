# Mapper Lambda -- turns a finished BDA job into matter state.
#
# Trigger: an EventBridge rule on source aws.bedrock, detail-types
#          "Bedrock Data Automation Job Succeeded",
#          "... Failed With Client Error", "... Failed With Service Error".
#          ("Job Created" is intentionally not subscribed.)
#
# Job:     for a succeeded job, read the extracted fields + per-field confidence,
#          resolve the matter from the input key (ADR-005), and write matter
#          state -- either the matter's DOC# row, or a TRIAGE row if the document
#          could not be associated. For a failed job, record it and emit a metric.
#
# CONTRACTS CONFIRMED against the first real run (2026-07-25) -- previously these
# were defensively guessed; see docs/bda-orchestration-reference.md:
#   - The completion event carries detail.input_s3_object.name (the input key,
#     used for correlation) and detail.output_s3_location.{s3_bucket,name} (the
#     result location). It does NOT carry a full invocation ARN, so an earlier
#     GetDataAutomationStatus(invocationArn=job_id) call was WRONG -- job_id is a
#     bare UUID and that API wants the ARN. The call is removed entirely: the
#     event already contains the output location it was fetching.
#   - The custom-blueprint result lives at
#     <output_s3_location.name>/custom_output/<n>/result.json, with:
#         inference_result:    {field: value, ..., employees: [{...}, ...]}
#         explainability_info: [ {field: {confidence, value, ...},
#                                 employees: [{sub: {confidence, ...}}, ...]} ]

import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["MATTER_TABLE"]
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Ida/Understanding")
DOC_TYPE = os.environ.get("DOC_TYPE", "census")  # this slice handles one class

s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")
table = boto3.resource("dynamodb").Table(TABLE_NAME)

_MATTER_KEY_RE = re.compile(r"^matters/(?P<matterId>[^/]+)/")


def resolve_matter(object_key: str) -> dict:
    """ADR-005 seam -- MUST stay identical to the submit Lambda's copy.

    Keys off the input object key (detail.input_s3_object.name on the event):
      matters/MTR-2026-0142/census.pdf   -> MTR-2026-0142  (key-prefix)
      unassociated/orphan-census.pdf     -> None           (NEEDS_TRIAGE)
    """
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


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _decimalize(value):
    """DynamoDB rejects float; round-trip through Decimal via JSON string."""
    import decimal

    return json.loads(json.dumps(value), parse_float=decimal.Decimal)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_custom_result(bucket: str, seg_prefix: str) -> dict:
    """Load the custom-blueprint result.json for a completed job.

    The completion event's output_s3_location.name points at the segment dir
    (e.g. 'bda-output/.../<job>/0'); the custom output is at
    '<seg>/custom_output/<n>/result.json'. Lists rather than hard-coding <n> so a
    multi-segment result still resolves. Returns {} if nothing is readable, which
    routes the document to review rather than crashing the handler.
    """
    prefix = f"{seg_prefix.rstrip('/')}/custom_output/"
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    for obj in sorted(listing.get("Contents", []), key=lambda o: o["Key"]):
        if obj["Key"].endswith("result.json"):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue
    return {}


def parse_extraction(result: dict):
    """(fields, min_confidence) from a custom-blueprint result.

    min_confidence is the minimum over every leaf field carrying a confidence,
    including nested employee rows -- the conservative ADR-002 gate: any one weak
    field routes the document to human review. Returns ({}, 0.0) when the result
    has no confidence info, which also routes to review.
    """
    fields = result.get("inference_result", {}) or {}
    explain = result.get("explainability_info") or []
    confidences = []
    if isinstance(explain, list) and explain and isinstance(explain[0], dict):
        for v in explain[0].values():
            if isinstance(v, dict) and "confidence" in v:
                confidences.append(_as_float(v["confidence"]))
            elif isinstance(v, list):  # nested rows, e.g. employees
                for row in v:
                    if isinstance(row, dict):
                        for sub in row.values():
                            if isinstance(sub, dict) and "confidence" in sub:
                                confidences.append(_as_float(sub["confidence"]))
    confidences = [c for c in confidences if c is not None]
    return fields, (min(confidences) if confidences else 0.0)


def write_matter_document(matter_id: str, fields: dict, min_confidence: float, source_key: str) -> None:
    """Upsert the DOC#<docType> row on the matter, keeping the GSI in sync so the
    missing-docs-by-due-date query and the readout stay accurate."""
    accepted = min_confidence >= CONFIDENCE_THRESHOLD
    status = "received" if accepted else "in-review"
    put_metric("DocumentsAccepted" if accepted else "DocumentsRoutedToReview")

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


def handler(event: dict, context) -> dict:
    logger.info("event: %s", json.dumps(event))
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    if "Failed" in detail_type:
        # The client/service split IS our retryable/non-retryable classification,
        # delivered by event type. Only "Succeeded" fired on the first real run,
        # so these paths stay defensive and log-only until a real failure event
        # confirms their detail shape (docs/bda-orchestration-reference.md).
        if "Client Error" in detail_type:
            put_metric("BDAJobsFailedClientError")
        else:
            put_metric("BDAJobsFailedServiceError")
        put_metric("BDAJobsFailed")
        logger.error("BDA job failed: %s detail=%s", detail_type, json.dumps(detail))
        return {"status": "failed", "detailType": detail_type}

    put_metric("BDAJobsTotal")
    put_metric("BDAJobsSucceeded")

    input_key = detail.get("input_s3_object", {}).get("name")
    output_loc = detail.get("output_s3_location", {})
    out_bucket = output_loc.get("s3_bucket")
    seg_prefix = output_loc.get("name")

    if not input_key or not out_bucket or not seg_prefix:
        logger.error("event missing input/output location: %s", json.dumps(detail))
        return {"status": "bad-event"}

    result = load_custom_result(out_bucket, seg_prefix)
    fields, min_conf = parse_extraction(result)

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
