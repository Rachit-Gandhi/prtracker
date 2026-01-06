#!/bin/bash

echo 'Waiting for PR fetch to complete...'
sleep ${INITIAL_WAIT_TIME:-120}
mkdir -p /coordination

while true; do
  if python /app/ai-pr-reviewer.py; then
    echo 'AI review completed. Next run in ${AI_REVIEW_INTERVAL:-7200} seconds.'
    # Update timestamp to trigger exporter
    date +%s > /coordination/ai_review_completed
  else
    echo 'AI review failed. Will retry later.'
  fi
  sleep ${AI_REVIEW_INTERVAL:-7200}
done