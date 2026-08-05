"""Scheduled-sweep Lambda handler.

Deliberately thin: every decision, guard, and cap lives in agent/core/sweep.py,
which the CLI wrappers import identically. The handler's only jobs are to
assemble config from the environment, call sweep_once, and return a summary.

WHAT THIS HANDLER DOES NOT DO, and why:

  * No assert_account(). That guard exists because a SCRIPT runs wherever the
    operator's shell happens to point; a Lambda's account is fixed by where it
    is deployed. Keeping it would add a sys.exit() path that protects against
    nothing and kills the invocation when the env var is absent.

  * No gateway URL lookup. GATEWAY_URL arrives as a synth-time environment
    variable from the CfnGateway attribute, which removes a cold-start API call,
    a failure mode, and the bedrock-agentcore-control IAM permission the runtime
    lookup would otherwise require.

  * No imports beyond boto3/botocore and the stdlib, so no Lambda layer is
    needed -- both ship in the Python runtime.

DRY_RUN defaults to "true" and only the exact string "false" disables it, so a
missing or malformed value dry-runs. Combined with the dispatch gate in
decide.py (which requires `dispatch is True`), a misconfigured invocation cannot
send email.
"""

import json
import os
from datetime import date

import boto3

from agent.core import decide, sweep

# Module scope: reused across warm invocations.
_TABLE = boto3.resource("dynamodb", region_name=decide.REGION).Table(decide.TABLE)
_BRT = boto3.client("bedrock-runtime", region_name=decide.REGION)
_SYSTEM_TEXT = decide.load_prompt()


def handler(event, context):
    # Per-invocation so an event can override the schedule's default, e.g. a
    # hand-invoked Day-1 run that wants to be explicit rather than rely on env.
    dry_run = sweep.DRY_RUN
    if isinstance(event, dict) and "dryRun" in event:
        dry_run = bool(event["dryRun"])

    gateway_url = os.environ["GATEWAY_URL"]
    cfg = sweep.build_cfg(_SYSTEM_TEXT, gateway_url, dry_run=dry_run)
    clients = {
        "table": _TABLE,
        "brt": _BRT,
        # Fresh per invocation: the execution role's credentials rotate, and
        # SigV4-signing the Gateway call with stale creds fails opaquely.
        "creds": boto3.Session().get_credentials().get_frozen_credentials(),
    }

    as_of = date.today()
    print(f"sweep start | as_of={as_of} | dryRun={dry_run} | model={cfg['model_id']} "
          f"| promptVersion={cfg['prompt_version']} | caps: {sweep.MAX_MATTERS_PER_RUN} matters / "
          f"{sweep.MAX_ESCALATIONS_PER_RUN} escalations")

    result = sweep.sweep_once(_TABLE, clients, cfg, as_of)
    result["promptVersion"] = cfg["prompt_version"]
    result["model"] = cfg["model_id"]
    result["asOf"] = as_of.isoformat()

    print("sweep summary " + json.dumps(result, default=str))
    return result
