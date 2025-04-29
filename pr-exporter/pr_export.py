#!/usr/bin/env python3
import os
import sys
import json
import logging
import argparse
import mysql.connector
from mysql.connector import Error
from datetime import datetime
import time
import pathlib

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("pr_export.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("pr-export")

class DatabaseConnection:
    """Handles database connections and queries."""
    def __init__(self, config):
        self.config = config
        self.connection = None
    
    def connect(self):
        """Connect to the MySQL database."""
        try:
            self.connection = mysql.connector.connect(
                host=self.config["db_host"],
                port=self.config["db_port"],
                user=self.config["db_user"],
                password=self.config["db_password"],
                database=self.config["db_name"]
            )
            
            if self.connection.is_connected():
                logger.info(f"Connected to MySQL database: {self.config['db_name']}")
                return True
        except Error as e:
            logger.error(f"Error connecting to MySQL database: {e}")
        
        return False
    
    def disconnect(self):
        """Disconnect from the database."""
        if self.connection and self.connection.is_connected():
            self.connection.close()
            logger.info("Disconnected from MySQL database")
    
    def get_reviewed_prs(self):
        """Get a list of PRs that have AI reviews."""
        prs = []
        
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return prs
            
            cursor = self.connection.cursor(dictionary=True)
            
            # Fixed query: Include updated_at in SELECT clause
            query = """
                SELECT pr.id, pr.repo_owner, pr.repo_name, pr.number, pr.title, pr.updated_at
                FROM pull_requests pr
                JOIN ai_pr_reviews rev ON pr.id = rev.pr_id
                GROUP BY pr.id, pr.repo_owner, pr.repo_name, pr.number, pr.title, pr.updated_at
                ORDER BY pr.updated_at DESC
            """
            
            cursor.execute(query)
            prs = cursor.fetchall()
            
            logger.info(f"Found {len(prs)} PRs with AI reviews")
            
            cursor.close()
            
        except Error as e:
            logger.error(f"Error querying database: {e}")
        
        return prs
    
    def get_pr_details(self, pr_id):
        """Get all details for a specific PR."""
        pr_data = {}
        
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return pr_data
            
            cursor = self.connection.cursor(dictionary=True)
            
            # Get PR basic information
            query = """
                SELECT pr.*
                FROM pull_requests pr
                WHERE pr.id = %s
            """
            
            cursor.execute(query, (pr_id,))
            pr_data = cursor.fetchone()
            
            if pr_data:
                # Convert datetime objects to strings for JSON serialization
                for key, value in pr_data.items():
                    if isinstance(value, datetime):
                        pr_data[key] = value.isoformat()
                
                # Get PR comments
                query = """
                    SELECT *
                    FROM pr_comments
                    WHERE pr_id = %s
                    ORDER BY created_at ASC
                """
                
                cursor.execute(query, (pr_id,))
                comments = cursor.fetchall()
                
                for comment in comments:
                    for key, value in comment.items():
                        if isinstance(value, datetime):
                            comment[key] = value.isoformat()
                
                pr_data['comments'] = comments
                
                # Get PR reviews (original GitHub reviews)
                query = """
                    SELECT *
                    FROM pr_reviews
                    WHERE pr_id = %s
                    ORDER BY created_at ASC
                """
                
                cursor.execute(query, (pr_id,))
                reviews = cursor.fetchall()
                
                for review in reviews:
                    for key, value in review.items():
                        if isinstance(value, datetime):
                            review[key] = value.isoformat()
                
                pr_data['github_reviews'] = reviews
                
                # Get PR patches
                query = """
                    SELECT *
                    FROM pr_patches
                    WHERE pr_id = %s
                """
                
                cursor.execute(query, (pr_id,))
                patches = cursor.fetchall()
                pr_data['patches'] = patches
                
                # Get AI reviews
                query = """
                    SELECT *
                    FROM ai_pr_reviews
                    WHERE pr_id = %s
                    ORDER BY created_at DESC
                """
                
                cursor.execute(query, (pr_id,))
                ai_reviews = cursor.fetchall()
                
                for review in ai_reviews:
                    for key, value in review.items():
                        if isinstance(value, datetime):
                            review[key] = value.isoformat()
                
                # Get AI file reviews
                if ai_reviews:
                    for ai_review in ai_reviews:
                        query = """
                            SELECT *
                            FROM ai_file_reviews
                            WHERE review_id = %s
                        """
                        
                        cursor.execute(query, (ai_review['id'],))
                        file_reviews = cursor.fetchall()
                        
                        for file_review in file_reviews:
                            for key, value in file_review.items():
                                if isinstance(value, datetime):
                                    file_review[key] = value.isoformat()
                        
                        ai_review['file_reviews'] = file_reviews
                
                pr_data['ai_reviews'] = ai_reviews
            
            cursor.close()
            
        except Error as e:
            logger.error(f"Error querying PR details: {e}")
        
        return pr_data

def export_prs_to_json(db, output_dir):
    """Export all reviewed PRs to individual JSON files."""
    # Create output directory if it doesn't exist
    output_path = pathlib.Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Get all PRs that have been reviewed by AI
    prs = db.get_reviewed_prs()
    
    for pr in prs:
        try:
            pr_id = pr['id']
            pr_number = pr['number']
            repo_owner = pr['repo_owner']
            repo_name = pr['repo_name']
            
            # Get all details for this PR
            pr_details = db.get_pr_details(pr_id)
            
            if pr_details:
                # Create a filename with repo and PR number
                filename = f"{repo_owner}_{repo_name}_PR{pr_number}.json"
                filepath = output_path / filename
                
                # Write to JSON file
                with open(filepath, 'w') as f:
                    json.dump(pr_details, f, indent=2)
                
                logger.info(f"Exported PR #{pr_number} from {repo_owner}/{repo_name} to {filepath}")
            else:
                logger.warning(f"No details found for PR #{pr_number} from {repo_owner}/{repo_name}")
        
        except Exception as e:
            logger.error(f"Error exporting PR {pr.get('number', 'unknown')}: {e}")
    
    return len(prs)

def main():
    """Main entry point for the PR export script."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PR Export to JSON")
    parser.add_argument("--output-dir", default="exported_prs", help="Directory to export JSON files to")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"), help="Database host")
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3306")), help="Database port")
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "pruser"), help="Database user")
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", "prpassword"), help="Database password")
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "github_prs"), help="Database name")
    args = parser.parse_args()
    
    # Load configuration
    config = {
        "db_host": args.db_host,
        "db_port": args.db_port,
        "db_user": args.db_user,
        "db_password": args.db_password,
        "db_name": args.db_name,
        "output_dir": args.output_dir
    }
    
    # Initialize database connection
    db = DatabaseConnection(config)
    
    # Connect to the database
    if not db.connect():
        return 1
    
    try:
        # Export PRs to JSON
        exported_count = export_prs_to_json(db, args.output_dir)
        logger.info(f"Exported {exported_count} PRs to JSON files in {args.output_dir}")
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1
    
    finally:
        # Disconnect from the database
        db.disconnect()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())