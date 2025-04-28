import os
import sys
import argparse
import logging
import json
import time
import mysql.connector
from mysql.connector import Error
import openai
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ai-pr-reviewer.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ai-pr-reviewer")

class Config:
    """Configuration settings for the AI PR reviewer."""
    def __init__(self):
        # Database connection settings (from environment variables)
        self.db_host = os.getenv("DB_HOST", "localhost")
        self.db_port = int(os.getenv("DB_PORT", "3306"))
        self.db_user = os.getenv("DB_USER", "pruser")
        self.db_password = os.getenv("DB_PASSWORD", "prpassword")
        self.db_name = os.getenv("DB_NAME", "github_prs")
        
        # GitHub API settings
        self.github_token = os.getenv("GITHUB_TOKEN", "")
        
        # OpenAI API settings
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
        self.openai_temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.0"))
        
        # Review settings
        self.max_prs_to_review = int(os.getenv("MAX_PRS_TO_REVIEW", "10"))
        self.review_only_open_prs = os.getenv("REVIEW_ONLY_OPEN_PRS", "true").lower() == "true"
        self.skip_already_reviewed = os.getenv("SKIP_ALREADY_REVIEWED", "true").lower() == "true"
        self.days_since_update = int(os.getenv("DAYS_SINCE_UPDATE", "7"))

    def validate(self):
        """Validate the configuration."""
        if not self.openai_api_key:
            logger.error("OPENAI_API_KEY environment variable is not set")
            return False
        
        return True

class DatabaseConnection:
    """Handles database connections and queries."""
    def __init__(self, config):
        self.config = config
        self.connection = None
    
    def connect(self):
        """Connect to the MySQL database."""
        try:
            self.connection = mysql.connector.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                user=self.config.db_user,
                password=self.config.db_password,
                database=self.config.db_name
            )
            
            if self.connection.is_connected():
                logger.info(f"Connected to MySQL database: {self.config.db_name}")
                return True
        except Error as e:
            logger.error(f"Error connecting to MySQL database: {e}")
        
        return False
    
    def disconnect(self):
        """Disconnect from the database."""
        if self.connection and self.connection.is_connected():
            self.connection.close()
            logger.info("Disconnected from MySQL database")
    
    def get_prs_for_review(self):
        """Get a list of PRs to review from the database."""
        prs = []
        
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return prs
            
            cursor = self.connection.cursor(dictionary=True)
            
            # Define the query based on configuration settings
            query = """
                SELECT pr.id, pr.repo_owner, pr.repo_name, pr.number, pr.title, 
                       pr.diffs, pr.state, pr.user_login, pr.base_commit_sha, 
                       pr.files_changed, pr.additions, pr.deletions, pr.updated_at
                FROM pull_requests pr
                LEFT JOIN pr_reviews rev ON pr.id = rev.pr_id AND rev.body LIKE '%AI REVIEW%'
                WHERE 1=1
            """
            
            params = []
            
            # Add filters based on configuration
            if self.config.review_only_open_prs:
                query += " AND pr.state = 'open'"
            
            if self.config.skip_already_reviewed:
                query += " AND rev.id IS NULL"
            
            if self.config.days_since_update > 0:
                cutoff_date = (datetime.now() - timedelta(days=self.config.days_since_update)).strftime('%Y-%m-%d')
                query += " AND pr.updated_at > %s"
                params.append(cutoff_date)
            
            query += " ORDER BY pr.updated_at DESC LIMIT %s"
            params.append(self.config.max_prs_to_review)
            
            cursor.execute(query, params)
            prs = cursor.fetchall()
            
            logger.info(f"Found {len(prs)} PRs for review")
            
            cursor.close()
            
        except Error as e:
            logger.error(f"Error querying database: {e}")
        
        return prs
    
    def get_pr_comments(self, pr_id):
        """Get all comments for a specific PR."""
        comments = []
        
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return comments
            
            cursor = self.connection.cursor(dictionary=True)
            
            query = """
                SELECT id, body, created_at, user_login, path, position
                FROM pr_comments
                WHERE pr_id = %s
                ORDER BY created_at ASC
            """
            
            cursor.execute(query, (pr_id,))
            comments = cursor.fetchall()
            
            cursor.close()
            
        except Error as e:
            logger.error(f"Error querying PR comments: {e}")
        
        return comments
    
    def get_pr_patches(self, pr_id):
        """Get all file patches for a specific PR."""
        patches = []
        
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return patches
            
            cursor = self.connection.cursor(dictionary=True)
            
            query = """
                SELECT path, patch, filename, status, changes, additions, deletions
                FROM pr_patches
                WHERE pr_id = %s
            """
            
            cursor.execute(query, (pr_id,))
            patches = cursor.fetchall()
            
            cursor.close()
            
        except Error as e:
            logger.error(f"Error querying PR patches: {e}")
        
        return patches
    
    def update_review_status(self, pr_id, review_id):
        """Mark a PR as reviewed in the database."""
        try:
            if not self.connection or not self.connection.is_connected():
                if not self.connect():
                    return False
            
            cursor = self.connection.cursor()
            
            # Update the PR's last_processed_time
            query = """
                UPDATE pull_requests
                SET last_processed_time = NOW()
                WHERE id = %s
            """
            
            cursor.execute(query, (pr_id,))
            self.connection.commit()
            
            cursor.close()
            return True
            
        except Error as e:
            logger.error(f"Error updating PR review status: {e}")
            return False

class ReviewStore:
    """Handles storing reviews in the database."""
    def __init__(self, db_connection):
        self.db_connection = db_connection
    
    def ensure_tables_exist(self):
        """Ensure the necessary tables exist for storing reviews."""
        try:
            cursor = self.db_connection.connection.cursor()
            
            # Create ai_pr_reviews table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_pr_reviews (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    pr_id BIGINT NOT NULL,
                    summary TEXT NOT NULL,
                    full_review LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
                    INDEX idx_pr_id (pr_id),
                    INDEX idx_created_at (created_at)
                )
            """)
            
            # Create ai_file_reviews table for individual file reviews
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_file_reviews (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    review_id BIGINT NOT NULL,
                    pr_id BIGINT NOT NULL,
                    filename VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (review_id) REFERENCES ai_pr_reviews(id) ON DELETE CASCADE,
                    FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
                    INDEX idx_review_id (review_id),
                    INDEX idx_pr_id (pr_id),
                    INDEX idx_filename (filename)
                )
            """)
            
            self.db_connection.connection.commit()
            cursor.close()
            logger.info("Review tables created or verified")
            return True
            
        except Error as e:
            logger.error(f"Error creating review tables: {e}")
            return False
    
    def store_review(self, pr_id, repo_owner, repo_name, pr_number, summary, full_review, file_reviews=None):
        """Store a PR review in the database."""
        try:
            if not self.db_connection.connection or not self.db_connection.connection.is_connected():
                if not self.db_connection.connect():
                    return None
            
            cursor = self.db_connection.connection.cursor()
            
            # Insert the main PR review
            cursor.execute("""
                INSERT INTO ai_pr_reviews 
                (pr_id, summary, full_review, created_at) 
                VALUES (%s, %s, %s, NOW())
            """, (pr_id, summary, full_review))
            
            review_id = cursor.lastrowid
            
            # Insert individual file reviews if provided
            if file_reviews and isinstance(file_reviews, list):
                for file_review in file_reviews:
                    if file_review.get('content') and file_review.get('filename'):
                        cursor.execute("""
                            INSERT INTO ai_file_reviews
                            (review_id, pr_id, filename, content, created_at)
                            VALUES (%s, %s, %s, %s, NOW())
                        """, (review_id, pr_id, file_review['filename'], file_review['content']))
            
            self.db_connection.connection.commit()
            
            logger.info(f"Stored review for PR #{pr_number} in {repo_owner}/{repo_name}, review ID: {review_id}")
            return review_id
            
        except Error as e:
            logger.error(f"Error storing review in database: {e}")
            return None
    
    def get_latest_review(self, pr_id):
        """Get the latest review for a PR."""
        try:
            if not self.db_connection.connection or not self.db_connection.connection.is_connected():
                if not self.db_connection.connect():
                    return None
            
            cursor = self.db_connection.connection.cursor(dictionary=True)
            
            cursor.execute("""
                SELECT id, summary, full_review, created_at
                FROM ai_pr_reviews
                WHERE pr_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (pr_id,))
            
            review = cursor.fetchone()
            
            if review:
                # Get file reviews associated with this review
                cursor.execute("""
                    SELECT filename, content
                    FROM ai_file_reviews
                    WHERE review_id = %s
                """, (review['id'],))
                
                file_reviews = cursor.fetchall()
                review['file_reviews'] = file_reviews
            
            cursor.close()
            return review
            
        except Error as e:
            logger.error(f"Error getting latest review: {e}")
            return None

class AIReviewer:
    """Handles AI-powered code reviews using OpenAI's API."""
    def __init__(self, config):
        self.config = config
        openai.api_key = config.openai_api_key
    
    def summarize_pr(self, pr_title, pr_description, patches):
        """Generate a summary of the PR using OpenAI."""
        try:
            # Prepare the prompt
            prompt = f"""
            ## GitHub PR Title
            `{pr_title}`
            
            ## Description
            ```
            {pr_description if pr_description else 'No description provided'}
            ```
            
            ## Changes Overview
            The PR changes {len(patches)} files with the following modifications:
            
            """
            
            # Add a summary of each file's changes
            for patch in patches[:5]:  # Limit to first 5 files to avoid token limits
                prompt += f"- {patch['filename']} ({patch['status']}): {patch['additions']} additions, {patch['deletions']} deletions\n"
            
            if len(patches) > 5:
                prompt += f"- ... and {len(patches) - 5} more files\n"
            
            prompt += """
            ## Instructions
            Please provide a concise summary of this PR. Your summary should:
            1. Explain the purpose of the changes
            2. Note significant modifications
            3. Identify potential concerns or areas for improvement
            4. Keep the response under 250 words
            """
            
            # Make the API call
            response = openai.ChatCompletion.create(
                model=self.config.openai_model,
                messages=[
                    {"role": "system", "content": "You are a helpful AI code reviewer."},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.config.openai_temperature,
                max_tokens=500
            )
            
            summary = response.choices[0].message['content'].strip()
            logger.info("Generated PR summary")
            return summary
            
        except Exception as e:
            logger.error(f"Error generating PR summary: {e}")
            return "Failed to generate summary due to an error."
    
    def review_file(self, filename, patch_content, file_status):
        """Review a single file's changes using OpenAI."""
        try:
            if not patch_content or len(patch_content) < 10:
                return None
            
            # Limit patch content size to avoid token limits
            if len(patch_content) > 8000:
                patch_content = patch_content[:8000] + "\n... (content truncated for token limit)"
            
            # Prepare the prompt
            prompt = f"""
            ## File: {filename}
            ## Status: {file_status}
            
            ## Diff
            ```diff
            {patch_content}
            ```
            
            ## Instructions
            Please review this code diff and provide specific, actionable feedback. Focus on:
            
            1. Bugs or logical errors
            2. Performance issues
            3. Security vulnerabilities
            4. Code style and best practices
            5. Potential edge cases
            
            Format your response with these rules:
            - If you find issues, start with a brief overall assessment, then list specific issues with line numbers.
            - If the code looks good without issues, respond with "LGTM! (Looks Good To Me)"
            - Be concise and specific.
            """
            
            # Make the API call
            response = openai.ChatCompletion.create(
                model=self.config.openai_model,
                messages=[
                    {"role": "system", "content": "You are a helpful AI code reviewer with expertise in multiple programming languages."},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.config.openai_temperature,
                max_tokens=1000
            )
            
            review = response.choices[0].message['content'].strip()
            logger.info(f"Generated review for {filename}")
            
            if review.startswith("LGTM"):
                return None
                
            return review
            
        except Exception as e:
            logger.error(f"Error reviewing file {filename}: {e}")
            return None

def main():
    """Main entry point for the AI PR reviewer."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="AI PR Reviewer")
    parser.add_argument("--dry-run", action="store_true", help="Run without saving reviews to database")
    args = parser.parse_args()
    
    # Load configuration
    config = Config()
    if not config.validate():
        logger.error("Invalid configuration. Exiting.")
        return 1
    
    # Initialize components
    db = DatabaseConnection(config)
    ai = AIReviewer(config)
    
    # Connect to the database
    if not db.connect():
        return 1
    
    # Initialize review store and ensure tables exist
    review_store = ReviewStore(db)
    if not review_store.ensure_tables_exist():
        logger.error("Failed to create or verify review tables. Exiting.")
        db.disconnect()
        return 1
    
    try:
        # Get PRs to review
        prs = db.get_prs_for_review()
        logger.info(f"Found {len(prs)} PRs to review")
        
        # Process each PR
        for pr in prs:
            logger.info(f"Reviewing PR #{pr['number']} in {pr['repo_owner']}/{pr['repo_name']}: {pr['title']}")
            
            # Get PR details
            patches = db.get_pr_patches(pr['id'])
            comments = db.get_pr_comments(pr['id'])
            
            # Generate PR summary
            pr_summary = ai.summarize_pr(pr['title'], pr.get('description', ''), patches)
            
            # Generate the header for the review
            review_header = f"""# AI Review 🤖

## Summary
{pr_summary}

## Detailed Review
"""
            
            # Review each file
            file_reviews = []
            structured_file_reviews = []
            
            for patch in patches:
                review_content = ai.review_file(patch['filename'], patch['patch'], patch['status'])
                if review_content:
                    file_reviews.append(f"### {patch['filename']}\n{review_content}\n")
                    structured_file_reviews.append({
                        'filename': patch['filename'],
                        'content': review_content
                    })
            
            # Skip if no issues found
            if not file_reviews:
                review_text = f"{review_header}\nAll changes look good! 👍"
            else:
                review_text = f"{review_header}\n{''.join(file_reviews)}"
            
            review_text += "\n\n---\n*This review was automatically generated by an AI assistant.*"
            
            # Store the review in the database
            if not args.dry_run:
                review_id = review_store.store_review(
                    pr['id'],
                    pr['repo_owner'],
                    pr['repo_name'],
                    pr['number'],
                    pr_summary,
                    review_text,
                    structured_file_reviews
                )
                
                if review_id:
                    db.update_review_status(pr['id'], review_id)
                    logger.info(f"Stored review {review_id} for PR #{pr['number']}")
            else:
                logger.info("Dry run: Would have stored a review")
                logger.info(f"Review content: {review_text[:100]}...")
            
            # Small pause between reviews
            time.sleep(1)
        
        logger.info(f"Reviewed {len(prs)} PRs")
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1
    
    finally:
        # Disconnect from the database
        db.disconnect()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())