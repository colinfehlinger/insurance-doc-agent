# Submit Lambda -- the entry point of the deterministic pipeline.
#
# Trigger: an EventBridge rule on S3 "Object Created" for the raw bucket.
# Job:     work out which matter the object belongs to (ADR-005), then start a
#          Bedrock Data Automation job for it. It writes NO matter state -- the
#          mapper Lambda does that on completion. This function only submits.
#
# It deliberately does NOT poll. BDA emits an EventBridge completion event; the
# mapper is triggered by that. See docs/bda-orchestration-reference.md for why
# the accelerator's poll loop and its uuid4() client token were NOT copied.

import hashlib
import json
import logging
import os
import random
import re
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BDA_PROJECT_ARN = os.environ["BDA_PROJECT_ARN"]
BDA_PROFILE_ARN = os.environ["BDA_PROFILE_ARN"]
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "bda-output")
KMS_KEY_ARN = os.environ["KMS_KEY_ARN"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Ida/Understanding")

# --- Retry config: parameters copied from the accelerator (Extraction 1),
# --- but the total in-Lambda budget is capped far lower than theirs. They allow
# --- ~4 minutes of billed sleep; we cap at ~30s and let the event source
# --- redrive beyond that. See docs/bda-orchestration-reference.md.
MAX_RETRIES = 7
INITIAL_BACKOFF = 2.0  # seconds
MAX_BACKOFF = 30.0  # seconds -- NOT the accelerator's 300; keep the Lambda cheap
IN_LAMBDA_RETRY_BUDGET = 30.0  # seconds total across attempts, then give up and let redrive handle it

# --- Error classification: list copied VERBATIM from the accelerator
# --- (Extraction 2). Anything not in this list fails immediately.
RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ServiceQuotaExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "InternalServerException",
}

bda = boto3.client("bedrock-data-automation-runtime")
cloudwatch = boto3.client("cloudwatch")

# resolve_matter's key convention. Kept in sync with the mapper Lambda by hand
# (the two are tiny and self-contained to avoid a Lambda layer / Docker bundling).
_MATTER_KEY_RE = re.compile(r"^matters/(?P<matterId>[^/]+)/")


def resolve_matter(object_key: str) -> dict:
    """The ADR-005 seam: object key -> { matterId | None (triage), source, confidence }.

    Only the S3 key-prefix branch is implemented. Alias and operator branches are
    additive later; until then, anything that does not match the convention is
    NEEDS_TRIAGE -- it is never guessed at from content.
    """
    m = _MATTER_KEY_RE.match(object_key)
    if m:
        return {"matterId": m.group("matterId"), "source": "key-prefix", "confidence": 1.0}
    return {"matterId": None, "source": "unresolved", "confidence": 0.0}


def put_metric(name: str, value: float = 1.0, unit: str = "Count") -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": unit}],
        )
    except Exception:  # metrics must never break the pipeline
        logger.warning("failed to emit metric %s", name, exc_info=True)


def deterministic_client_token(bucket: str, key: str, version_id: str) -> str:
    """Idempotency (Extraction 3). The accelerator used uuid4() PER CALL, which
    deduplicates nothing and creates a duplicate BDA job on every retry. We key
    the token to the exact object version, so a retried or duplicated event for
    the same version produces the same token, while a genuine re-upload (new
    version) is legitimately reprocessed."""
    basis = f"{bucket}/{key}#{version_id}".encode("utf-8")
    return hashlib.sha256(basis).hexdigest()[:64]


def calculate_backoff(attempt: int) -> float:
    backoff = min(MAX_BACKOFF, INITIAL_BACKOFF * (2 ** attempt))
    jitter = random.uniform(0, 0.1 * backoff)  # 10% jitter, as the accelerator does
    return backoff + jitter


def invoke_bda(input_s3_uri: str, output_s3_uri: str, client_token: str) -> dict:
    put_metric("BDARequestsTotal")
    started = time.time()
    last_exc = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = bda.invoke_data_automation_async(
                clientToken=client_token,
                inputConfiguration={"s3Uri": input_s3_uri},
                outputConfiguration={"s3Uri": output_s3_uri},
                dataAutomationConfiguration={
                    "dataAutomationProjectArn": BDA_PROJECT_ARN,
                    "stage": "LIVE",
                },
                dataAutomationProfileArn=BDA_PROFILE_ARN,
                encryptionConfiguration={"kmsKeyId": KMS_KEY_ARN},
                notificationConfiguration={
                    "eventBridgeConfiguration": {"eventBridgeEnabled": True}
                },
            )
            put_metric("BDARequestsSucceeded")
            if attempt > 0:
                put_metric("BDARequestsRetrySuccess")
            return resp

        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in RETRYABLE_ERRORS:
                put_metric("BDARequestsThrottles")
                last_exc = e
                elapsed = time.time() - started
                if attempt == MAX_RETRIES - 1 or elapsed >= IN_LAMBDA_RETRY_BUDGET:
                    put_metric("BDARequestsFailed")
                    put_metric("BDARequestsMaxRetriesExceeded")
                    raise
                backoff = calculate_backoff(attempt)
                # Do not exceed the in-Lambda budget with the sleep either.
                backoff = min(backoff, IN_LAMBDA_RETRY_BUDGET - elapsed)
                logger.warning("BDA throttle (attempt %d), backing off %.1fs", attempt + 1, backoff)
                time.sleep(max(0.0, backoff))
            else:
                put_metric("BDARequestsFailed")
                put_metric("BDARequestsNonRetryableErrors")
                logger.error("non-retryable BDA error %s", code)
                raise
        except Exception:
            put_metric("BDARequestsFailed")
            put_metric("BDARequestsUnexpectedErrors")
            logger.error("unexpected error invoking BDA", exc_info=True)
            raise

    if last_exc:
        raise last_exc


def handler(event: dict, context) -> dict:
    logger.info("event: %s", json.dumps(event))

    detail = event["detail"]
    bucket = detail["bucket"]["name"]
    key = detail["object"]["key"]
    version_id = detail["object"].get("version-id", "null")

    resolution = resolve_matter(key)
    logger.info("resolved %s -> %s", key, resolution)
    if resolution["matterId"] is None:
        put_metric("DocumentsUnassociatedAtSubmit")
    else:
        put_metric("DocumentsAssociatedAtSubmit")

    # BDA runs regardless of association. The extracted fields are useful either
    # way: for an associated matter they update its state; for a triage document
    # they help a human place it. Correlation is still decided at ingestion (the
    # key), never created from the extracted content.
    input_s3_uri = f"s3://{bucket}/{key}"
    output_s3_uri = f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{key}/"
    client_token = deterministic_client_token(bucket, key, version_id)

    resp = invoke_bda(input_s3_uri, output_s3_uri, client_token)
    invocation_arn = resp.get("invocationArn")
    logger.info("BDA job started: %s", invocation_arn)

    return {"invocationArn": invocation_arn, "resolution": resolution}
