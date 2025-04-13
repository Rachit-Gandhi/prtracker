# GitHub PR Tracker

This Go application connects to a MySQL database and communicates with the GitHub API to track pull requests, their code diffs, patches, reviews, and comments across multiple repositories.

## Features

- Multi-repository tracking support
- Detailed PR tracking including:
  - Code diffs and patches per file
  - PR comments (both review comments and issue comments)
  - PR reviews and their state (approved, changes requested, etc.)
  - File changes statistics (files changed, additions, deletions)
- Web UI for visualizing PR data:
  - Dashboard with repository statistics
  - Pull request listings with filtering
  - Code change analytics
  - Contributor insights
  - Review analytics

## Prerequisites

- Docker and Docker Compose
- GitHub Personal Access Token with repo permissions

## Configuration

The application is configured using environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| DB_HOST | Database host | localhost |
| DB_PORT | Database port | 3306 |
| DB_USER | Database username | pruser |
| DB_PASSWORD | Database password | prpassword |
| DB_NAME | Database name | github_prs |
| GITHUB_TOKEN | GitHub personal access token | |
| GITHUB_REPOS | Comma-separated list of repositories (owner/repo format) | |
| FETCH_SINCE_DAYS | Number of days to look back for PRs | 30 |
| UPDATE_INTERVAL | Seconds between update runs for scheduler (in seconds) | 3600 |

## Setup and Run

1. Clone this repository
2. Create a `.env` file with your GitHub credentials:

```
GITHUB_TOKEN=your_github_token
GITHUB_REPOS=owner1/repo1,owner2/repo2,owner3/repo3
FETCH_SINCE_DAYS=30
UPDATE_INTERVAL=3600
```

3. Build and start the application using Docker Compose:

```bash
docker-compose up -d
```

4. Access the web UI at http://localhost:5000

## Troubleshooting

### Database Schema Issues

If you see errors like `Unknown column 'files_changed' in 'field list'` or `Unknown column 'repo_owner' in 'field list'`, your database schema needs to be updated to match the application's expectations. You can fix this by:

1. Stopping all containers:
   ```bash
   docker-compose down
   ```

2. Removing the MySQL volume to start fresh:
   ```bash
   docker volume rm github-pr-tracker_mysql-data
   ```

3. Restarting everything:
   ```bash
   docker-compose up -d
   ```

### Viewing Logs

To view logs for a specific service:

```bash
# For the Go application
docker logs github-pr-app

# For the web UI
docker logs github-pr-web-ui

# For the scheduler
docker logs github-pr-scheduler

# For the MySQL database
docker logs github-pr-mysql
```

To follow logs in real-time:

```bash
docker logs -f github-pr-app
```

## Database Schema

The application creates the following tables:

### repositories

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR(100) | Repository ID (owner/name) |
| owner | VARCHAR(100) | Repository owner (username or organization) |
| name | VARCHAR(100) | Repository name |
| created_at | DATETIME | Record creation time |
| updated_at | DATETIME | Record update time |

### pull_requests

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | GitHub PR ID (primary key) |
| repo_owner | VARCHAR(100) | Repository owner |
| repo_name | VARCHAR(100) | Repository name |
| number | INT | PR number |
| title | VARCHAR(255) | PR title |
| created_at | DATETIME | PR creation time |
| updated_at | DATETIME | PR last update time |
| state | VARCHAR(20) | PR state (open, closed) |
| user_login | VARCHAR(100) | GitHub username of PR creator |
| diffs | LONGTEXT | PR code diffs |
| files_changed | INT | Number of files changed |
| additions | INT | Lines added |
| deletions | INT | Lines deleted |
| commit_count | INT | Number of commits |
| mergeable_state | VARCHAR(50) | PR mergeable state |

### pr_comments

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Comment ID (primary key) |
| pr_id | BIGINT | Associated PR ID (foreign key) |
| body | TEXT | Comment content |
| created_at | DATETIME | Comment creation time |
| user_login | VARCHAR(100) | GitHub username of commenter |
| path | VARCHAR(255) | File path (for review comments) |
| position | INT | Line position (for review comments) |

### pr_reviews

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Review ID (primary key) |
| pr_id | BIGINT | Associated PR ID (foreign key) |
| body | TEXT | Review content |
| state | VARCHAR(50) | Review state (APPROVED, CHANGES_REQUESTED, etc.) |
| created_at | DATETIME | Review creation time |
| user_login | VARCHAR(100) | GitHub username of reviewer |

### pr_patches

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Patch ID (primary key) |
| pr_id | BIGINT | Associated PR ID (foreign key) |
| path | VARCHAR(255) | File path |
| patch | LONGTEXT | File patch content |
| filename | VARCHAR(255) | File name |
| status | VARCHAR(50) | File status (added, modified, removed, etc.) |
| changes | INT | Total line changes |
| additions | INT | Lines added |
| deletions | INT | Lines deleted |

## Querying the Data

Once the application has run, you can connect to the MySQL database and run queries:

```sql
-- Get top repositories by PR count
SELECT 
    repo_owner, 
    repo_name, 
    COUNT(*) as pr_count,
    SUM(additions) as total_additions,
    SUM(deletions) as total_deletions
FROM 
    pull_requests
GROUP BY 
    repo_owner, repo_name
ORDER BY 
    pr_count DESC;

-- Get top contributors across all repositories
SELECT 
    user_login, 
    COUNT(*) as pr_count,
    SUM(additions) as total_additions,
    SUM(deletions) as total_deletions
FROM 
    pull_requests
GROUP BY 
    user_login
ORDER BY 
    pr_count DESC
LIMIT 10;

-- Get PRs with the most review activity
SELECT 
    pr.repo_owner,
    pr.repo_name,
    pr.number, 
    pr.title, 
    COUNT(r.id) as review_count,
    SUM(CASE WHEN r.state = 'APPROVED' THEN 1 ELSE 0 END) as approvals,
    SUM(CASE WHEN r.state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) as change_requests
FROM 
    pull_requests pr
JOIN 
    pr_reviews r ON pr.id = r.pr_id
GROUP BY 
    pr.id
ORDER BY 
    review_count DESC
LIMIT 10;

-- Get most modified files
SELECT 
    p.filename,
    COUNT(DISTINCT pr.id) as pr_count,
    SUM(p.changes) as total_changes,
    SUM(p.additions) as total_additions,
    SUM(p.deletions) as total_deletions
FROM 
    pr_patches p
JOIN
    pull_requests pr ON p.pr_id = pr.id
GROUP BY 
    p.filename
ORDER BY 
    total_changes DESC
LIMIT 20;
```

## Extending the Application

This application can be extended in several ways:

1. Add more metrics and analytics:
   - PR merge time analysis
   - Review turnaround time
   - Code quality metrics
   - Team collaboration patterns

2. Enhanced visualizations:
   - Team-based statistics
   - Code hotspots heat maps
   - Developer network graphs

3. Integration with other systems:
   - CI/CD status tracking
   - Issue tracker correlation
   - Slack/Discord notifications

4. Additional GitHub features:
   - Workflow runs tracking
   - Issue tracking
   - Release management