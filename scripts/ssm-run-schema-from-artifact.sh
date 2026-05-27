#!/usr/bin/env bash
# =============================================================================
# ssm-run-schema-from-artifact.sh
# Bootstrap the Logistics-Prod schema on RDS via SSM Run Command.
#
# This script targets a running ASG instance (which already has psql installed
# and the S3 artifact downloaded to /opt/app/).  No bastion required.
#
# Prerequisites:
#   * cfn/template.yaml stack in CREATE_COMPLETE
#   * At least one healthy ASG instance
#   * Your terminal has AWS credentials with ssm:SendCommand permission
#
# Usage:
#   chmod +x ssm-run-schema-from-artifact.sh
#   BACKEND_STACK=logistics-prod-backend APP_REGION=us-east-1 ./ssm-run-schema-from-artifact.sh
#
# What it does:
#   1. Looks up the RDS endpoint and Secrets Manager secret from CloudFormation outputs.
#   2. Retrieves DB credentials from Secrets Manager.
#   3. Picks a healthy ASG instance via SSM.
#   4. Sends the psql command to run schema.sql on that instance.
# =============================================================================

set -euo pipefail

# ---- Configuration ----------------------------------------------------------
BACKEND_STACK="${BACKEND_STACK:-logistics-prod}"
APP_REGION="${APP_REGION:-us-east-1}"
SCHEMA_PATH="/opt/app/schema.sql"

echo "[INFO] Backend stack : $BACKEND_STACK"
echo "[INFO] Region        : $APP_REGION"

# ---- Look up CloudFormation outputs -----------------------------------------
echo "[INFO] Fetching stack outputs..."

DB_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$BACKEND_STACK" \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DbEndpoint'].OutputValue | [0]" \
  --output text)

echo "[INFO] RDS endpoint  : $DB_ENDPOINT"

# ---- Find a healthy SSM-managed instance ------------------------------------
echo "[INFO] Finding a registered SSM instance..."

INSTANCE_ID=$(aws ssm describe-instance-information \
  --region "$APP_REGION" \
  --filters "Key=tag:Name,Values=${BACKEND_STACK}-asg" \
  --query "InstanceInformationList[?PingStatus=='Online'].InstanceId | [0]" \
  --output text 2>/dev/null || true)

# Fall back to describe-instances if SSM filter doesn't match yet.
if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  echo "[WARN] SSM tag filter returned nothing; falling back to EC2 describe-instances..."
  INSTANCE_ID=$(aws ec2 describe-instances \
    --region "$APP_REGION" \
    --filters \
      "Name=tag:aws:autoscaling:groupName,Values=${BACKEND_STACK}-asg" \
      "Name=instance-state-name,Values=running" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text)
fi

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  echo "[ERROR] No running instance found for stack $BACKEND_STACK"
  echo "        Is the ASG fully up?  Check:"
  echo "        aws autoscaling describe-auto-scaling-groups \\"
  echo "          --auto-scaling-group-names ${BACKEND_STACK}-asg \\"
  echo "          --region $APP_REGION \\"
  echo "          --query 'AutoScalingGroups[0].Instances'"
  exit 1
fi

echo "[INFO] Target instance: $INSTANCE_ID"

# ---- Fetch DB credentials from Secrets Manager ------------------------------
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$BACKEND_STACK" \
  --region "$APP_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='SecretArn'].OutputValue | [0]" \
  --output text)

echo "[INFO] Secret ARN    : $SECRET_ARN"

SECRET_JSON=$(aws secretsmanager get-secret-value \
  --region "$APP_REGION" \
  --secret-id "$SECRET_ARN" \
  --query SecretString --output text)

DB_USER=$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['username'])")
DB_PASS=$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

echo "[INFO] DB user       : $DB_USER"

# ---- Send the SSM Run Command -----------------------------------------------
echo "[INFO] Sending SSM Run Command to run schema.sql..."

CMD="PGPASSWORD='$DB_PASS' psql -h $DB_ENDPOINT -U $DB_USER -d logistics -f $SCHEMA_PATH"
echo "[INFO] Command: psql -h $DB_ENDPOINT -U $DB_USER -d logistics -f $SCHEMA_PATH"

COMMAND_ID=$(aws ssm send-command \
  --region "$APP_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --comment "Logistics-Prod schema bootstrap" \
  --parameters "commands=[\"$CMD\"]" \
  --query "Command.CommandId" \
  --output text)

echo "[INFO] SSM Command ID: $COMMAND_ID"
echo "[INFO] Waiting for command to complete (up to 120 seconds)..."

# Poll for completion.
for i in $(seq 1 24); do
  sleep 5
  STATUS=$(aws ssm get-command-invocation \
    --region "$APP_REGION" \
    --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" \
    --query "Status" \
    --output text 2>/dev/null || echo "Pending")

  echo "[INFO] [$((i*5))s] Status: $STATUS"

  if [[ "$STATUS" == "Success" ]]; then
    echo ""
    echo "[OK] Schema bootstrap completed successfully."
    # Show the output (sanity check query at the end of schema.sql).
    OUTPUT=$(aws ssm get-command-invocation \
      --region "$APP_REGION" \
      --command-id "$COMMAND_ID" \
      --instance-id "$INSTANCE_ID" \
      --query "StandardOutputContent" \
      --output text)
    echo "[OUTPUT]"
    echo "$OUTPUT"
    exit 0
  fi

  if [[ "$STATUS" == "Failed" || "$STATUS" == "TimedOut" || "$STATUS" == "Cancelled" ]]; then
    echo ""
    echo "[ERROR] Command $STATUS"
    aws ssm get-command-invocation \
      --region "$APP_REGION" \
      --command-id "$COMMAND_ID" \
      --instance-id "$INSTANCE_ID" \
      --query "[StandardOutputContent,StandardErrorContent]" \
      --output text
    exit 1
  fi
done

echo "[WARN] Timed out waiting. Check the command manually:"
echo "  aws ssm get-command-invocation \\"
echo "    --command-id $COMMAND_ID \\"
echo "    --instance-id $INSTANCE_ID \\"
echo "    --region $APP_REGION"
