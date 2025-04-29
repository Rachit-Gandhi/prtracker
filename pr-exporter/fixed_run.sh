#!/bin/bash

echo 'Starting PR exporter service...'

# Create the coordination file monitor
mkdir -p /coordination
touch /coordination/last_export_time
echo "0" > /coordination/last_export_time

while true; do
  # Check if AI reviewer has completed a run
  if [ -f /coordination/ai_review_completed ]; then
    # Ensure we get valid integers, default to 0 if not valid
    AI_COMPLETION_TIME=$(cat /coordination/ai_review_completed 2>/dev/null | tr -cd '0-9' || echo 0)
    LAST_EXPORT_TIME=$(cat /coordination/last_export_time 2>/dev/null | tr -cd '0-9' || echo 0)
    
    # Default to 0 if empty
    AI_COMPLETION_TIME=${AI_COMPLETION_TIME:-0}
    LAST_EXPORT_TIME=${LAST_EXPORT_TIME:-0}
    
    echo "AI completion time: $AI_COMPLETION_TIME, Last export time: $LAST_EXPORT_TIME"
    
    # If AI reviewer has run since our last export or if we're forcing an export
    if [ -z "$AI_COMPLETION_TIME" ] || [ -z "$LAST_EXPORT_TIME" ] || [ "$AI_COMPLETION_TIME" -gt "$LAST_EXPORT_TIME" ]; then
      echo 'New AI reviews detected or forcing export. Starting export...'
      python /app/pr_export.py --output-dir /app/exported_prs
      # Update the last export time
      date +%s > /coordination/last_export_time
      echo 'Export completed.'
    else
      echo 'No new AI reviews since last export. Waiting...'
    fi
  else
    echo 'Waiting for AI reviewer to complete first run...'
    # Force an export attempt if AI reviewer hasn't run yet but we might have reviews
    python /app/pr_export.py --output-dir /app/exported_prs
    date +%s > /coordination/last_export_time
  fi
  
  # Wait before next check
  sleep ${EXPORT_CHECK_INTERVAL:-5}
done