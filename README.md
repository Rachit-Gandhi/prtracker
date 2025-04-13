# GitHub PR Tracker

This Go application connects to a MySQL database and communicates with the GitHub API to track pull requests, their code diffs, and comments.

## Features

- Connects to a local MySQL database
- Retrieves pull requests from a specified GitHub repository
- Gets code diffs for each pull request
- Retrieves PR comments (both review comments and issue comments)
- Stores all information in a database for easy querying

## Prerequisites

- Docker and Docker Compose
- GitHub Personal Access Token with repo permissions

## Configuration

The application is configured using environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| DB_HOST | Database host | localhost |
| DB_PORT | Database port | 3306 |
| DB_USER | Database username | root |
| DB_PASSWORD | Database password | |
| DB_NAME | Database name | github_prs |
| GITHUB_TOKEN | GitHub personal access token | |
| GITHUB_OWNER | Repository owner (username or organization) | |
| GITHUB_REPO | Repository name | |
| FETCH_SINCE_DAYS | Number of days to look back for PRs | 30 |

## Setup and Run

1. Clone this repository
2. Create a `.env` file with your GitHub credentials:

```
GITHUB_TOKEN=your_github_token
GITHUB_OWNER=repository_owner
GITHUB_REPO=repository_name
```

3. Build go.sum and start the application using Docker Compose:

```bash
go mod tidy
docker-compose up -d
```

## Database Schema

The application creates two tables:

### pull_requests

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | GitHub PR ID (primary key) |
| number | INT | PR number |
| title | VARCHAR(255) | PR title |
| created_at | DATETIME | PR creation time |
| updated_at | DATETIME | PR last update time |
| state | VARCHAR(20) | PR state (open, closed) |
| user_login | VARCHAR(100) | GitHub username of PR creator |
| diffs | LONGTEXT | PR code diffs |

### pr_comments

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT | Comment ID (primary key) |
| pr_id | BIGINT | Associated PR ID (foreign key) |
| body | TEXT | Comment content |
| created_at | DATETIME | Comment creation time |
| user_login | VARCHAR(100) | GitHub username of commenter |

## Querying the Data

Once the application has run, you can connect to the MySQL database and run queries:

```sql
-- Get all PRs with their diff count and comment count
SELECT 
    pr.number, 
    pr.title, 
    pr.state, 
    pr.user_login,
    LENGTH(pr.diffs) as diff_size,
    COUNT(c.id) as comment_count
FROM 
    pull_requests pr
LEFT JOIN 
    pr_comments c ON pr.id = c.pr_id
GROUP BY 
    pr.id
ORDER BY 
    pr.updated_at DESC;

-- Find PRs with the most comments
SELECT 
    pr.number, 
    pr.title, 
    COUNT(c.id) as comment_count
FROM 
    pull_requests pr
JOIN 
    pr_comments c ON pr.id = c.pr_id
GROUP BY 
    pr.id
ORDER BY 
    comment_count DESC
LIMIT 10;

-- Find the most active commenters
SELECT 
    c.user_login, 
    COUNT(c.id) as comment_count
FROM 
    pr_comments c
GROUP BY 
    c.user_login
ORDER BY 
    comment_count DESC
LIMIT 10;
```

## Extending the Application

This application can be extended in several ways:

1. Add more metrics such as PR size, time to merge, etc.
2. Track PR review approvals and statuses
3. Add a web UI to visualize the data
4. Set up scheduled runs to keep the database updated
5. Generate reports or notifications based on the data