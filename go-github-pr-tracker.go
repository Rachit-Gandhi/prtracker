package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/google/go-github/v55/github"
	_ "github.com/go-sql-driver/mysql"
	"golang.org/x/oauth2"
)

// Database configuration
type DBConfig struct {
	Host     string
	Port     int
	User     string
	Password string
	DBName   string
}

// Application configuration
type Config struct {
	DB             DBConfig
	GitHubToken    string
	GitHubOwner    string
	GitHubRepo     string
	FetchSinceDays int
}

// PullRequest represents a GitHub pull request with diffs and comments
type PullRequest struct {
	ID        int64
	Number    int
	Title     string
	CreatedAt time.Time
	UpdatedAt time.Time
	State     string
	UserLogin string
	Diffs     string
	Comments  []PRComment
}

// PRComment represents a comment on a pull request
type PRComment struct {
	ID        int64
	Body      string
	CreatedAt time.Time
	UserLogin string
}

func main() {
	// Load configuration
	config := loadConfig()

	// Connect to database
	db, err := connectToDatabase(config.DB)
	if err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}
	defer db.Close()

	// Create tables if they don't exist
	if err := createTables(db); err != nil {
		log.Fatalf("Failed to create tables: %v", err)
	}

	// Setup GitHub client
	githubClient := setupGitHubClient(config.GitHubToken)

	// Get pull requests from GitHub
	prs, err := fetchPullRequests(githubClient, config)
	if err != nil {
		log.Fatalf("Failed to fetch pull requests: %v", err)
	}

	// Store pull requests and their details in the database
	if err := storePullRequests(db, prs); err != nil {
		log.Fatalf("Failed to store pull requests: %v", err)
	}

	log.Printf("Successfully processed %d pull requests", len(prs))
}

// loadConfig loads the application configuration from environment variables
func loadConfig() Config {
	return Config{
		DB: DBConfig{
			Host:     getEnv("DB_HOST", "localhost"),
			Port:     getIntEnv("DB_PORT", 3306),
			User:     getEnv("DB_USER", "root"),
			Password: getEnv("DB_PASSWORD", ""),
			DBName:   getEnv("DB_NAME", "github_prs"),
		},
		GitHubToken:    getEnv("GITHUB_TOKEN", ""),
		GitHubOwner:    getEnv("GITHUB_OWNER", ""),
		GitHubRepo:     getEnv("GITHUB_REPO", ""),
		FetchSinceDays: getIntEnv("FETCH_SINCE_DAYS", 30),
	}
}

// getEnv gets an environment variable or returns a default value
func getEnv(key, defaultValue string) string {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	return value
}

// getIntEnv gets an environment variable as an integer or returns a default value
func getIntEnv(key string, defaultValue int) int {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	var result int
	fmt.Sscanf(value, "%d", &result)
	return result
}

// connectToDatabase connects to the MySQL database
func connectToDatabase(config DBConfig) (*sql.DB, error) {
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%d)/%s?parseTime=true", 
		config.User, config.Password, config.Host, config.Port, config.DBName)
	
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	
	// Test the connection
	if err = db.Ping(); err != nil {
		return nil, err
	}
	
	return db, nil
}

// createTables creates the necessary tables if they don't exist
func createTables(db *sql.DB) error {
	// Create pull_requests table
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS pull_requests (
			id BIGINT PRIMARY KEY,
			number INT NOT NULL,
			title VARCHAR(255) NOT NULL,
			created_at DATETIME NOT NULL,
			updated_at DATETIME NOT NULL,
			state VARCHAR(20) NOT NULL,
			user_login VARCHAR(100) NOT NULL,
			diffs LONGTEXT,
			UNIQUE INDEX idx_number (number)
		)
	`)
	if err != nil {
		return err
	}

	// Create pr_comments table
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS pr_comments (
			id BIGINT PRIMARY KEY,
			pr_id BIGINT NOT NULL,
			body TEXT NOT NULL,
			created_at DATETIME NOT NULL,
			user_login VARCHAR(100) NOT NULL,
			FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE
		)
	`)
	
	return err
}

// setupGitHubClient sets up a GitHub client with authentication
func setupGitHubClient(token string) *github.Client {
	ctx := context.Background()
	ts := oauth2.StaticTokenSource(
		&oauth2.Token{AccessToken: token},
	)
	tc := oauth2.NewClient(ctx, ts)
	
	return github.NewClient(tc)
}

// fetchPullRequests fetches pull requests from GitHub
func fetchPullRequests(client *github.Client, config Config) ([]PullRequest, error) {
	ctx := context.Background()
	
	// Calculate the time since when to fetch PRs
	sinceTime := time.Now().AddDate(0, 0, -config.FetchSinceDays)
	
	// Options for listing pull requests
	opts := &github.PullRequestListOptions{
		State: "all",
		Sort:  "updated",
		Direction: "desc",
		ListOptions: github.ListOptions{
			PerPage: 100,
		},
	}
	
	var allPRs []PullRequest
	
	// Fetch all pages of pull requests
	for {
		prs, resp, err := client.PullRequests.List(ctx, config.GitHubOwner, config.GitHubRepo, opts)
		if err != nil {
			return nil, err
		}
		
		// Process each PR
		for _, pr := range prs {
			// Skip PRs older than our cutoff
			if pr.UpdatedAt.Time.Before(sinceTime) {
				continue
			}
			
			prDetails := PullRequest{
				ID:        pr.GetID(),
				Number:    pr.GetNumber(),
				Title:     pr.GetTitle(),
				CreatedAt: pr.GetCreatedAt().Time,
				UpdatedAt: pr.GetUpdatedAt().Time,
				State:     pr.GetState(),
				UserLogin: pr.GetUser().GetLogin(),
			}
			
			// Get the PR diff
			diff, err := getPRDiff(client, config.GitHubOwner, config.GitHubRepo, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get diff for PR #%d: %v", pr.GetNumber(), err)
			}
			prDetails.Diffs = diff
			
			// Get PR comments
			comments, err := getPRComments(client, config.GitHubOwner, config.GitHubRepo, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get comments for PR #%d: %v", pr.GetNumber(), err)
			}
			prDetails.Comments = comments
			
			allPRs = append(allPRs, prDetails)
			log.Printf("Processed PR #%d: %s", pr.GetNumber(), pr.GetTitle())
		}
		
		if resp.NextPage == 0 {
			break
		}
		opts.Page = resp.NextPage
	}
	
	return allPRs, nil
}

// getPRDiff gets the diff for a specific pull request
func getPRDiff(client *github.Client, owner, repo string, number int) (string, error) {
	ctx := context.Background()
	
	// Get the PR in raw diff format
	opts := github.RawOptions{Type: github.Diff}
	diff, _, err := client.PullRequests.GetRaw(ctx, owner, repo, number, opts)
	if err != nil {
		return "", err
	}
	
	return diff, nil
}

// getPRComments gets all comments for a specific pull request
func getPRComments(client *github.Client, owner, repo string, number int) ([]PRComment, error) {
	ctx := context.Background()
	
	// Options for listing comments
	opts := &github.PullRequestListCommentsOptions{
		ListOptions: github.ListOptions{
			PerPage: 100,
		},
	}
	
	var allComments []PRComment
	
	// Fetch review comments (comments on specific lines)
	for {
		comments, resp, err := client.PullRequests.ListComments(ctx, owner, repo, number, opts)
		if err != nil {
			return nil, err
		}
		
		for _, comment := range comments {
			prComment := PRComment{
				ID:        comment.GetID(),
				Body:      comment.GetBody(),
				CreatedAt: comment.GetCreatedAt().Time,
				UserLogin: comment.GetUser().GetLogin(),
			}
			allComments = append(allComments, prComment)
		}
		
		if resp.NextPage == 0 {
			break
		}
		opts.Page = resp.NextPage
	}
	
	// Also fetch issue comments (general PR comments)
	issueOpts := &github.IssueListCommentsOptions{
		ListOptions: github.ListOptions{
			PerPage: 100,
		},
	}
	
	for {
		comments, resp, err := client.Issues.ListComments(ctx, owner, repo, number, issueOpts)
		if err != nil {
			return nil, err
		}
		
		for _, comment := range comments {
			prComment := PRComment{
				ID:        comment.GetID(),
				Body:      comment.GetBody(),
				CreatedAt: comment.GetCreatedAt().Time,
				UserLogin: comment.GetUser().GetLogin(),
			}
			allComments = append(allComments, prComment)
		}
		
		if resp.NextPage == 0 {
			break
		}
		issueOpts.Page = resp.NextPage
	}
	
	return allComments, nil
}

// storePullRequests stores the pull requests and their comments in the database
func storePullRequests(db *sql.DB, prs []PullRequest) error {
	// Start a transaction
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			tx.Rollback()
			return
		}
		err = tx.Commit()
	}()
	
	// Prepare statements
	insertPRStmt, err := tx.Prepare(`
		INSERT INTO pull_requests (id, number, title, created_at, updated_at, state, user_login, diffs)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			title = VALUES(title),
			updated_at = VALUES(updated_at),
			state = VALUES(state),
			diffs = VALUES(diffs)
	`)
	if err != nil {
		return err
	}
	defer insertPRStmt.Close()
	
	insertCommentStmt, err := tx.Prepare(`
		INSERT INTO pr_comments (id, pr_id, body, created_at, user_login)
		VALUES (?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			body = VALUES(body)
	`)
	if err != nil {
		return err
	}
	defer insertCommentStmt.Close()
	
	// Delete existing comments statement
	deleteCommentsStmt, err := tx.Prepare(`
		DELETE FROM pr_comments WHERE pr_id = ?
	`)
	if err != nil {
		return err
	}
	defer deleteCommentsStmt.Close()
	
	// Insert each PR and its comments
	for _, pr := range prs {
		// Insert or update the PR
		_, err = insertPRStmt.Exec(
			pr.ID, pr.Number, pr.Title, pr.CreatedAt, pr.UpdatedAt, pr.State, pr.UserLogin, pr.Diffs,
		)
		if err != nil {
			return fmt.Errorf("failed to insert PR #%d: %v", pr.Number, err)
		}
		
		// Delete existing comments for this PR to avoid duplicates
		_, err = deleteCommentsStmt.Exec(pr.ID)
		if err != nil {
			return fmt.Errorf("failed to delete existing comments for PR #%d: %v", pr.Number, err)
		}
		
		// Insert each comment
		for _, comment := range pr.Comments {
			_, err = insertCommentStmt.Exec(
				comment.ID, pr.ID, comment.Body, comment.CreatedAt, comment.UserLogin,
			)
			if err != nil {
				return fmt.Errorf("failed to insert comment for PR #%d: %v", pr.Number, err)
			}
		}
	}
	
	return nil
}