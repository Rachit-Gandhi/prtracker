#!/bin/bash

echo 'Starting PR exporter service...'

# Create the coordination file monitor
mkdir -p /coordination
touch /coordination/last_export_time

while true; do
  # Check if AI reviewer has completed a run
  if [ -f /coordination/ai_review_completed ]; then
    AI_COMPLETION_TIME=$(cat /coordination/ai_review_completed || echo 0)
    LAST_EXPORT_TIME=$(cat /coordination/last_export_time || echo 0)
    
    # If AI reviewer has run since our last export
    if [ "$AI_COMPLETION_TIME" -gt "$LAST_EXPORT_TIME" ]; then
      echo 'New AI reviews detected. Starting export...'
      python /app/pr_export.py --output-dir /app/exported_prs
      date +%s > /coordination/last_export_time
      echo 'Export completed.'
    else
      echo 'No new AI reviews since last export. Waiting...'
    fi
  else
    echo 'Waiting for AI reviewer to complete...'
  fi
  
  # Wait before next check
  sleep ${EXPORT_CHECK_INTERVAL:-30}
done