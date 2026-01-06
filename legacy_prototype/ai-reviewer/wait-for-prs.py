#!/usr/bin/env python
import os
import time
import mysql.connector
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wait-for-prs")

def wait_for_prs():
    db_config = {
        'host': os.getenv('DB_HOST', 'mysql'),
        'port': int(os.getenv('DB_PORT', '3306')),
        'user': os.getenv('DB_USER', 'pruser'),
        'password': os.getenv('DB_PASSWORD', 'prpassword'),
        'database': os.getenv('DB_NAME', 'github_prs')
    }
    
    max_wait_time = 600  # 5 minutes
    check_interval = 10  # 10 seconds
    start_time = time.time()
    
    logger.info("Waiting for PRs to be available in the database...")
    
    while (time.time() - start_time) < max_wait_time:
        try:
            conn = mysql.connector.connect(**db_config)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pull_requests")
            pr_count = cursor.fetchone()[0]
            cursor.close()
            conn.close()
            
            if pr_count > 0:
                logger.info(f"Found {pr_count} PRs in database. Ready to start reviewing.")
                return True
            
            logger.info(f"No PRs found yet. Waiting {check_interval} seconds...")
            time.sleep(check_interval)
            
        except Exception as e:
            logger.warning(f"Database check failed: {e}. Retrying in {check_interval} seconds...")
            time.sleep(check_interval)
    
    logger.warning(f"Timeout after waiting {max_wait_time} seconds. Starting anyway.")
    return False

if __name__ == "__main__":
    wait_for_prs()