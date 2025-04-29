import os
import sys
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
        
        # OpenAI API settings
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
        self.openai_temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.0"))
        
        # Review settings
        self.max_prs_to_review = int(os.getenv("MAX_PRS_TO_REVIEW", "10"))

    def validate(self):
        """Validate the configuration."""
        if not self.openai_api_key:
            logger.error("OPENAI_API_KEY environment variable is not set")
            return False
        
        return True

def connect_to_database(config):
    """Connect to the MySQL database."""
    try:
        connection = mysql.connector.connect(
            host=config.db_host,
            port=config.db_port,
            user=config.db_user,
            password=config.db_password,
            database=config.db_name
        )
        
        if connection.is_connected():
            logger.info(f"Connected to MySQL database: {config.db_name}")
            return connection
    except Error as e:
        logger.error(f"Error connecting to MySQL database: {e}")
    
    return None

def export_pr_diagnostics(connection):
    """Export PR diagnostics to a text file."""
    diagnostics = []
    
    try:
        cursor = connection.cursor(dictionary=True)
        
        # Get total PR count
        cursor.execute("SELECT COUNT(*) as count FROM pull_requests")
        total_count = cursor.fetchone()['count']
        diagnostics.append(f"Total PRs in database: {total_count}")
        
        # Get counts by state
        cursor.execute("SELECT state, COUNT(*) as count FROM pull_requests GROUP BY state")
        states = cursor.fetchall()
        for state in states:
            diagnostics.append(f"PRs with state '{state['state']}': {state['count']}")
        
        # Get counts by repository
        cursor.execute("SELECT repo_owner, repo_name, COUNT(*) as count FROM pull_requests GROUP BY repo_owner, repo_name")
        repos = cursor.fetchall()
        diagnostics.append("\nPRs by repository:")
        for repo in repos:
            diagnostics.append(f"- {repo['repo_owner']}/{repo['repo_name']}: {repo['count']}")
        
        # Get recently updated PRs
        cursor.execute("SELECT COUNT(*) as count FROM pull_requests WHERE updated_at > DATE_SUB(NOW(), INTERVAL 30 DAY)")
        recent = cursor.fetchone()['count']
        diagnostics.append(f"\nPRs updated in the last 30 days: {recent}")
        
        # Sample of most recent PRs
        cursor.execute("""
            SELECT id, repo_owner, repo_name, number, title, state, updated_at 
            FROM pull_requests 
            ORDER BY updated_at DESC
            LIMIT 10
        """)
        recent_prs = cursor.fetchall()
        
        diagnostics.append("\nMost recently updated PRs:")
        for pr in recent_prs:
            formatted_date = pr['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            diagnostics.append(f"- #{pr['number']} ({pr['state']}): {pr['title']} [{pr['repo_owner']}/{pr['repo_name']}] - {formatted_date}")
        
        cursor.close()
        
        # Write to file
        with open("pr_diagnostics.txt", "w") as f:
            f.write("\n".join(diagnostics))
        
        logger.info(f"Exported PR diagnostics to pr_diagnostics.txt")
        
    except Error as e:
        logger.error(f"Error exporting PR diagnostics: {e}")

def ensure_tables_exist(connection):
    """Ensure the necessary tables exist for storing reviews."""
    try:
        cursor = connection.cursor()
        
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
        
        connection.commit()
        cursor.close()
        logger.info("Review tables created or verified")
        return True
        
    except Error as e:
        logger.error(f"Error creating review tables: {e}")
        return False

def get_all_prs_for_review(connection, limit):
    """Get all PRs for review, ignoring previous filters."""
    prs = []
    
    try:
        cursor = connection.cursor(dictionary=True)
        
        # Simple query to get PRs in order of newest first, with a limit
        query = """
            SELECT pr.id, pr.repo_owner, pr.repo_name, pr.number, pr.title, 
                   pr.diffs, pr.state, pr.user_login, pr.base_commit_sha, 
                   pr.files_changed, pr.additions, pr.deletions, pr.updated_at
            FROM pull_requests pr
            ORDER BY pr.updated_at DESC
            LIMIT %s
        """
        
        cursor.execute(query, (limit,))
        prs = cursor.fetchall()
        
        logger.info(f"Found {len(prs)} PRs for review (ignoring previous filters)")
        
        cursor.close()
        
    except Error as e:
        logger.error(f"Error querying database: {e}")
    
    return prs

def get_pr_patches(connection, pr_id):
    """Get all file patches for a specific PR."""
    patches = []
    
    try:
        cursor = connection.cursor(dictionary=True)
        
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

def get_pr_comments(connection, pr_id):
    """Get all comments for a specific PR."""
    comments = []
    
    try:
        cursor = connection.cursor(dictionary=True)
        
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

def summarize_pr(openai_api, model, title, patches):
    """Generate a summary of the PR using OpenAI."""
    try:
        # Prepare the prompt
        prompt = f"""
        ## GitHub PR Title
        `{title}`
        
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
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful AI code reviewer."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            max_tokens=500
        )
        
        summary = response.choices[0].message['content'].strip()
        logger.info("Generated PR summary")
        return summary
        
    except Exception as e:
        logger.error(f"Error generating PR summary: {e}")
        return "Failed to generate summary due to an error."

def review_file(openai_api, model, filename, patch_content, file_status):
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
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful AI code reviewer with expertise in multiple programming languages."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
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

def store_review(connection, pr_id, repo_owner, repo_name, pr_number, summary, full_review, file_reviews=None):
    """Store a PR review in the database."""
    try:
        cursor = connection.cursor()
        
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
        
        connection.commit()
        
        logger.info(f"Stored review for PR #{pr_number} in {repo_owner}/{repo_name}, review ID: {review_id}")
        return review_id
        
    except Error as e:
        logger.error(f"Error storing review in database: {e}")
        return None

def update_review_status(connection, pr_id):
    """Mark a PR as reviewed in the database."""
    try:
        cursor = connection.cursor()
        
        # Update the PR's last_processed_time
        query = """
            UPDATE pull_requests
            SET last_processed_time = NOW()
            WHERE id = %s
        """
        
        cursor.execute(query, (pr_id,))
        connection.commit()
        
        cursor.close()
        return True
        
    except Error as e:
        logger.error(f"Error updating PR review status: {e}")
        return False

def main():
    """Main entry point for the AI PR reviewer."""
    # Load configuration
    config = Config()
    if not config.validate():
        logger.error("Invalid configuration. Exiting.")
        return 1
    
    # Set up OpenAI API
    openai.api_key = config.openai_api_key
    
    # Connect to the database
    connection = connect_to_database(config)
    if not connection:
        return 1
    
    try:
        # Export diagnostics about available PRs
        export_pr_diagnostics(connection)
        
        # Ensure tables exist
        if not ensure_tables_exist(connection):
            logger.error("Failed to create or verify review tables. Exiting.")
            return 1
        
        # Get PRs to review (using simplified approach without constraints)
        prs = get_all_prs_for_review(connection, config.max_prs_to_review)
        logger.info(f"Found {len(prs)} PRs to review")
        
        if len(prs) == 0:
            logger.warning("No PRs found for review. Check if PRs have been fetched.")
            return 0
        
        # Create a txt file with PRs to be reviewed
        with open("prs_to_review.txt", "w") as f:
            f.write(f"PRs to be reviewed ({len(prs)}):\n")
            for pr in prs:
                f.write(f"- #{pr['number']} ({pr['state']}): {pr['title']} [{pr['repo_owner']}/{pr['repo_name']}]\n")
        
        logger.info("Exported list of PRs to be reviewed to prs_to_review.txt")
        
        # Process each PR
        for pr in prs:
            logger.info(f"Reviewing PR #{pr['number']} in {pr['repo_owner']}/{pr['repo_name']}: {pr['title']}")
            
            # Get PR details
            patches = get_pr_patches(connection, pr['id'])
            comments = get_pr_comments(connection, pr['id'])
            
            # Skip if no patches
            if not patches:
                logger.warning(f"No patches found for PR #{pr['number']}. Skipping review.")
                continue
            
            # Generate PR summary
            pr_summary = summarize_pr(openai, config.openai_model, pr['title'], patches)
            
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
                review_content = review_file(openai, config.openai_model, patch['filename'], patch['patch'], patch['status'])
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
            review_id = store_review(
                connection,
                pr['id'],
                pr['repo_owner'],
                pr['repo_name'],
                pr['number'],
                pr_summary,
                review_text,
                structured_file_reviews
            )
            
            if review_id:
                update_review_status(connection, pr['id'])
                logger.info(f"Stored review {review_id} for PR #{pr['number']}")
                
                # Write review file to disk for export
                review_dir = os.path.join("reviews", f"{pr['repo_owner']}-{pr['repo_name']}")
                os.makedirs(review_dir, exist_ok=True)
                
                review_file_path = os.path.join(review_dir, f"PR-{pr['number']}.md")
                with open(review_file_path, "w") as f:
                    f.write(review_text)
                
                logger.info(f"Exported review to {review_file_path}")
            
            # Small pause between reviews
            time.sleep(1)
        
        logger.info(f"Reviewed {len(prs)} PRs")
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1
    
    finally:
        # Disconnect from the database
        if connection.is_connected():
            connection.close()
            logger.info("Disconnected from MySQL database")
    
    return 0

if __name__ == "__main__":
    # Create output directories
    os.makedirs("reviews", exist_ok=True)
    
    # Run the main function
    sys.exit(main())