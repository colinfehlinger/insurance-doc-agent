#!/usr/bin/env python3
"""Assert the sweep Lambda's role against the SYNTHESIZED template.

A least-privilege list written in a design doc is a claim; this makes it a test.
The absences matter more than the presences -- a permission quietly added later
is exactly the kind of drift nobody reads a diff carefully enough to catch.

Per the project's testing principle, this is also run against a deliberately
wrong expectation to prove it can fail (see --self-test).

Usage:
    cd infra && npx cdk synth Ida-Dev-Agent -c stage=dev > /tmp/t.json
    python scripts/verify-sweep-iam.py /tmp/t.json
"""

import json
import sys

# Every action the sweep role is allowed to have. Anything else is a failure.
ALLOWED = {
    "dynamodb:Query",
    "dynamodb:PutItem",
    "kms:Decrypt",
    "kms:GenerateDataKey",
    "bedrock:InvokeModel",
    "bedrock-agentcore:InvokeGateway",
    # Lambda's basic execution role, attached by CDK.
    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
    "xray:PutTraceSegments", "xray:PutTelemetryRecords",
}

# Named explicitly so the failure message says WHY, not just "unexpected".
FORBIDDEN = {
    "lambda:InvokeFunction": "the Gateway invokes the escalate Lambda, never the sweep",
    "sns:Publish": "the escalate Lambda notifies; the sweep must have no path to email",
    "dynamodb:UpdateItem": "the sweep never mutates matter state",
    "dynamodb:DeleteItem": "the sweep never deletes anything",
    "dynamodb:BatchWriteItem": "batch writes would bypass the single-row audit shape",
    "dynamodb:Scan": "candidate selection is Query-only by construction",
}


def sweep_role_actions(template: dict) -> set:
    res = template.get("Resources", {})
    # The sweep function's role logical id, via its DependsOn/Role ref.
    sweep_fns = [k for k, v in res.items()
                 if v.get("Type") == "AWS::Lambda::Function"
                 and "SweepFn" in k and "LogGroup" not in k]
    if not sweep_fns:
        sys.exit("FAIL: no SweepFn in the template")
    role_ref = res[sweep_fns[0]]["Properties"]["Role"]["Fn::GetAtt"][0]

    actions = set()
    for k, v in res.items():
        if v.get("Type") == "AWS::IAM::Policy":
            roles = json.dumps(v["Properties"].get("Roles", []))
            if role_ref not in roles:
                continue
            for st in v["Properties"]["PolicyDocument"]["Statement"]:
                a = st.get("Action", [])
                actions |= set(a if isinstance(a, list) else [a])
        if v.get("Type") == "AWS::IAM::Role" and k == role_ref:
            for arn in v["Properties"].get("ManagedPolicyArns", []):
                actions.add(f"<managed:{json.dumps(arn)[:60]}>")
    return actions


def main() -> None:
    if "--self-test" in sys.argv:
        # Prove the checker can fail: feed it a role carrying a forbidden action.
        fake = {"Resources": {
            "SweepFnABC": {"Type": "AWS::Lambda::Function",
                           "Properties": {"Role": {"Fn::GetAtt": ["SweepRoleXYZ", "Arn"]}}},
            "SweepRoleXYZ": {"Type": "AWS::IAM::Role", "Properties": {}},
            "P1": {"Type": "AWS::IAM::Policy", "Properties": {
                "Roles": [{"Ref": "SweepRoleXYZ"}],
                "PolicyDocument": {"Statement": [{"Action": ["dynamodb:Query", "sns:Publish"]}]}}},
        }}
        acts = sweep_role_actions(fake)
        bad = [a for a in acts if a in FORBIDDEN]
        print(f"self-test: injected sns:Publish -> detected forbidden {bad}")
        sys.exit(0 if bad == ["sns:Publish"] else "SELF-TEST FAILED: checker cannot detect a forbidden action")

    template = json.load(open(sys.argv[1], encoding="utf-8"))
    actions = sweep_role_actions(template)
    concrete = {a for a in actions if not a.startswith("<managed:")}

    print("sweep role actions in the synthesized template:")
    for a in sorted(actions):
        print(f"   {a}")

    violations = sorted(a for a in concrete if a in FORBIDDEN)
    unexpected = sorted(a for a in concrete if a not in ALLOWED and a not in FORBIDDEN)

    print()
    if violations:
        for a in violations:
            print(f"  FAIL forbidden action present: {a}  -- {FORBIDDEN[a]}")
    for a in sorted(FORBIDDEN):
        if a not in concrete:
            print(f"  ok   absent: {a:28s} ({FORBIDDEN[a]})")
    if unexpected:
        print(f"\n  FAIL not on the allow-list: {unexpected}")
    if violations or unexpected:
        sys.exit(1)
    print("\nPASS -- every allowed action expected, every forbidden action absent.")


if __name__ == "__main__":
    main()
