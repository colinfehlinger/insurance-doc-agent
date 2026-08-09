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
    # DRY_RUN IS A HARD FLOOR, not a default.
    #
    # env DRY_RUN=true forces a dry run and NO payload can lower it. env
    # DRY_RUN=false lets a payload still ASK for dry. So the env can always be
    # made safer by a caller, never less safe -- which is what makes
    # `DRY_RUN=true` a pause lever that cannot be bypassed by whatever happens
    # to invoke the function.
    #
    # It is also now the SINGLE lever for the unattended path: the schedule's
    # payload no longer carries dryRun at all. Two places stating the same
    # intent was useful while both said "dry"; going live it becomes two places
    # that can disagree, with the payload silently winning.
    if sweep.DRY_RUN:
        dry_run = True
    elif isinstance(event, dict) and "dryRun" in event:
        dry_run = bool(event["dryRun"])
    else:
        dry_run = False

    # WHO TRIGGERED THIS. The schedule sends invokedBy="eventbridge-scheduler"
    # in its payload; a hand-invoke does not, so it reports "manual".
    #
    # This exists because "did the schedule actually fire, or did I invoke it
    # and forget?" is not answerable from a RequestId, a timestamp, or an audit
    # row -- and that exact ambiguity is what made Day 1's diagnosis take a
    # detour. Recording the answer at the moment of invocation is cheaper than
    # reconstructing it afterwards.
    invoked_by = "manual"
    if isinstance(event, dict):
        invoked_by = event.get("invokedBy") or (
            "eventbridge-rule" if event.get("source") == "aws.events" else "manual"
        )

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
    print(f"sweep start | invokedBy={invoked_by} | as_of={as_of} | dryRun={dry_run} "
          f"| model={cfg['model_id']} | promptVersion={cfg['prompt_version']} "
          f"| caps: {sweep.MAX_MATTERS_PER_RUN} matters / {sweep.MAX_ESCALATIONS_PER_RUN} escalations")

    result = sweep.sweep_once(_TABLE, clients, cfg, as_of)
    result["promptVersion"] = cfg["prompt_version"]
    result["model"] = cfg["model_id"]
    result["asOf"] = as_of.isoformat()
    result["invokedBy"] = invoked_by

    print("sweep summary " + json.dumps(result, default=str))

    # A single, greppable token emitted ONLY when the run did something worth a
    # human's attention. A CloudWatch metric filter turns this into a metric and
    # an alarm, which is why it is a distinct line rather than a field inside the
    # summary JSON: filter patterns cannot parse JSON that has a text prefix.
    #
    # Deliberately a log line rather than put_metric_data -- emitting metrics
    # directly would require cloudwatch:PutMetricData on the sweep role, and that
    # role's verified-absent permission list is worth more than the convenience.
    if (result["escalations"] or result["errors"] or result["valveTripped"]
            or result.get("blockedCapability")):
        print("SWEEP_NOTABLE " + json.dumps({
            "escalations": result["escalations"],
            "errors": len(result["errors"]),
            "valveTripped": result["valveTripped"],
            "blockedCapability": result.get("blockedCapability", 0),
            "processed": result["processed"],
            "invokedBy": invoked_by,
        }, default=str))
    return result
