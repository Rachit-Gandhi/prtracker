#!/bin/bash

echo 'Starting PR exporter service...'

# Create the coordination directory and files if they don't exist
mkdir -p /coordination
touch /coordination/last_export_time
echo "0" > /coordination/last_export_time

# Function to safely get numeric values from files
get_safe_number() {
    local file="$1"
    local default="$2"
    
    if [ ! -f "$file" ]; then
        echo "$default"
        return
    fi
    
    local value=$(cat "$file" 2>/dev/null | grep -o '[0-9]*' || echo "$default")
    if [ -z "$value" ]; then
        echo "$default"
    else
        echo "$value"
    fi
}

# Main loop
while true; do
    echo "Checking for new AI reviews..."
    
    # Always do an initial export to catch any reviews we might have missed
    if [ ! -f "/coordination/initial_export_done" ]; then
        echo "Performing initial export..."
        python /app/pr_export.py --output-dir /app/exported_prs
        echo "1" > /coordination/initial_export_done
        date +%s > /coordination/last_export_time
        echo "Initial export completed."
    fi
    
    # Normal operation - check for new reviews
    LAST_EXPORT_TIME=$(get_safe_number "/coordination/last_export_time" "0")
    echo "Last export time: $LAST_EXPORT_TIME"
    
    # Force an export every 5 minutes regardless of AI reviewer activity
    CURRENT_TIME=$(date +%s)
    TIME_DIFF=$((CURRENT_TIME - LAST_EXPORT_TIME))
    FORCE_INTERVAL=$((5 * 60))  # 5 minutes in seconds
    
    if [ $TIME_DIFF -gt $FORCE_INTERVAL ]; then
        echo "Forcing export after $TIME_DIFF seconds since last export..."
        python /app/pr_export.py --output-dir /app/exported_prs
        date +%s > /coordination/last_export_time
        echo "Forced export completed."
    elif [ -f "/coordination/ai_review_completed" ]; then
        AI_COMPLETION_TIME=$(get_safe_number "/coordination/ai_review_completed" "0")
        echo "AI completion time: $AI_COMPLETION_TIME"
        
        if [ "$AI_COMPLETION_TIME" -gt "$LAST_EXPORT_TIME" ]; then
            echo "New AI reviews detected. Starting export..."
            python /app/pr_export.py --output-dir /app/exported_prs
            date +%s > /coordination/last_export_time
            echo "Export completed."
        else
            echo "No new AI reviews since last export."
        fi
    else
        echo "No AI review completion marker found."
    fi
    
    # Wait before next check
    SLEEP_TIME=${EXPORT_CHECK_INTERVAL:-30}
    echo "Waiting $SLEEP_TIME seconds before next check..."
    sleep $SLEEP_TIME
done