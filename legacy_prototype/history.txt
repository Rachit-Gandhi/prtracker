package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"os"
	"strings"
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

// Repository represents a GitHub repository to track
type Repository struct {
	Owner string
	Name  string
}

// Application configuration
type Config struct {
	DB             DBConfig
	GitHubToken    string
	Repositories   []Repository
	FetchSinceDays int
}

// PullRequest represents a GitHub pull request with diffs and comments
type PullRequest struct {
	ID             int64
	Number         int
	Title          string
	CreatedAt      time.Time
	UpdatedAt      time.Time
	State          string
	UserLogin      string
	Diffs          string
	Comments       []PRComment
	Reviews        []PRReview
	Patches        []PRPatch
	RepoOwner      string
	RepoName       string
	FilesChanged   int
	Additions      int
	Deletions      int
	CommitCount    int
	MergeableState string
}

// PRComment represents a comment on a pull request
type PRComment struct {
	ID        int64
	Body      string
	CreatedAt time.Time
	UserLogin string
	Path      string
	Position  int
}

// PRReview represents a review on a pull request
type PRReview struct {
	ID        int64
	Body      string
	State     string // APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
	CreatedAt time.Time
	UserLogin string
}

// PRPatch represents a patch for a specific file in a pull request
type PRPatch struct {
	PRID     int64
	Path     string
	Patch    string
	Filename string
	Status   string // added, removed, modified, renamed, etc.
	Changes  int
	Additions int
	Deletions int
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

	for _, repo := range config.Repositories {
		log.Printf("Processing repository: %s/%s", repo.Owner, repo.Name)
		
		// Get pull requests from GitHub
		prs, err := fetchPullRequests(githubClient, repo, config.FetchSinceDays)
		if err != nil {
			log.Printf("Failed to fetch pull requests for %s/%s: %v", repo.Owner, repo.Name, err)
			continue
		}

		// Store pull requests and their details in the database
		if err := storePullRequests(db, prs); err != nil {
			log.Printf("Failed to store pull requests for %s/%s: %v", repo.Owner, repo.Name, err)
			continue
		}

		log.Printf("Successfully processed %d pull requests for %s/%s", len(prs), repo.Owner, repo.Name)
	}
}

// loadConfig loads the application configuration from environment variables
func loadConfig() Config {
	// Parse repositories from environment variable - format: owner1/repo1,owner2/repo2
	reposStr := getEnv("GITHUB_REPOS", "")
	var repos []Repository
	
	if reposStr != "" {
		repoList := strings.Split(reposStr, ",")
		for _, repo := range repoList {
			parts := strings.Split(strings.TrimSpace(repo), "/")
			if len(parts) == 2 {
				repos = append(repos, Repository{
					Owner: parts[0],
					Name:  parts[1],
				})
			}
		}
	} else {
		// Fallback to single repo if GITHUB_REPOS is not provided
		owner := getEnv("GITHUB_OWNER", "")
		repo := getEnv("GITHUB_REPO", "")
		if owner != "" && repo != "" {
			repos = append(repos, Repository{
				Owner: owner,
				Name:  repo,
			})
		}
	}

	return Config{
		DB: DBConfig{
			Host:     getEnv("DB_HOST", "localhost"),
			Port:     getIntEnv("DB_PORT", 3306),
			User:     getEnv("DB_USER", "root"),
			Password: getEnv("DB_PASSWORD", ""),
			DBName:   getEnv("DB_NAME", "github_prs"),
		},
		GitHubToken:    getEnv("GITHUB_TOKEN", ""),
		Repositories:   repos,
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
	// Create repositories table
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS repositories (
			id VARCHAR(100) PRIMARY KEY,
			owner VARCHAR(100) NOT NULL,
			name VARCHAR(100) NOT NULL,
			created_at DATETIME NOT NULL,
			updated_at DATETIME NOT NULL,
			UNIQUE INDEX idx_owner_name (owner, name)
		)
	`)
	if err != nil {
		return err
	}

	// Create pull_requests table with expanded fields
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS pull_requests (
			id BIGINT PRIMARY KEY,
			repo_owner VARCHAR(100) NOT NULL,
			repo_name VARCHAR(100) NOT NULL,
			number INT NOT NULL,
			title VARCHAR(255) NOT NULL,
			created_at DATETIME NOT NULL,
			updated_at DATETIME NOT NULL,
			state VARCHAR(20) NOT NULL,
			user_login VARCHAR(100) NOT NULL,
			diffs LONGTEXT,
			files_changed INT NOT NULL DEFAULT 0,
			additions INT NOT NULL DEFAULT 0,
			deletions INT NOT NULL DEFAULT 0,
			commit_count INT NOT NULL DEFAULT 0,
			mergeable_state VARCHAR(50),
			UNIQUE INDEX idx_repo_number (repo_owner, repo_name, number),
			INDEX idx_user (user_login),
			INDEX idx_state (state),
			INDEX idx_updated (updated_at)
		)
	`)
	if err != nil {
		return err
	}

	// Create pr_comments table with expanded fields
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS pr_comments (
			id BIGINT PRIMARY KEY,
			pr_id BIGINT NOT NULL,
			body TEXT NOT NULL,
			created_at DATETIME NOT NULL,
			user_login VARCHAR(100) NOT NULL,
			path VARCHAR(255),
			position INT,
			FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
			INDEX idx_pr_id (pr_id),
			INDEX idx_user (user_login)
		)
	`)
	if err != nil {
		return err
	}

	// Create pr_reviews table
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS pr_reviews (
			id BIGINT PRIMARY KEY,
			pr_id BIGINT NOT NULL,
			body TEXT,
			state VARCHAR(50) NOT NULL,
			created_at DATETIME NOT NULL,
			user_login VARCHAR(100) NOT NULL,
			FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
			INDEX idx_pr_id (pr_id),
			INDEX idx_user (user_login),
			INDEX idx_state (state)
		)
	`)
	if err != nil {
		return err
	}

	// Create pr_patches table
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS pr_patches (
			id BIGINT AUTO_INCREMENT PRIMARY KEY,
			pr_id BIGINT NOT NULL,
			path VARCHAR(255) NOT NULL,
			patch LONGTEXT,
			filename VARCHAR(255) NOT NULL,
			status VARCHAR(50) NOT NULL,
			changes INT NOT NULL DEFAULT 0,
			additions INT NOT NULL DEFAULT 0,
			deletions INT NOT NULL DEFAULT 0,
			FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
			UNIQUE INDEX idx_pr_path (pr_id, path),
			INDEX idx_filename (filename),
			INDEX idx_status (status)
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
func fetchPullRequests(client *github.Client, repo Repository, fetchSinceDays int) ([]PullRequest, error) {
	ctx := context.Background()
	
	// Calculate the time since when to fetch PRs
	sinceTime := time.Now().AddDate(0, 0, -fetchSinceDays)
	
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
		prs, resp, err := client.PullRequests.List(ctx, repo.Owner, repo.Name, opts)
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
				RepoOwner: repo.Owner,
				RepoName:  repo.Name,
				MergeableState: pr.GetMergeableState(),
			}
			
			// Get PR stats (files changed, additions, deletions)
			prStats, err := getPRStats(client, repo.Owner, repo.Name, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get stats for PR #%d: %v", pr.GetNumber(), err)
			} else {
				prDetails.FilesChanged = prStats.FilesChanged
				prDetails.Additions = prStats.Additions
				prDetails.Deletions = prStats.Deletions
				prDetails.CommitCount = prStats.CommitCount
				prDetails.Patches = prStats.Patches
			}
			
			// Get the PR diff
			diff, err := getPRDiff(client, repo.Owner, repo.Name, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get diff for PR #%d: %v", pr.GetNumber(), err)
			}
			prDetails.Diffs = diff
			
			// Get PR comments
			comments, err := getPRComments(client, repo.Owner, repo.Name, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get comments for PR #%d: %v", pr.GetNumber(), err)
			}
			prDetails.Comments = comments
			
			// Get PR reviews
			reviews, err := getPRReviews(client, repo.Owner, repo.Name, pr.GetNumber())
			if err != nil {
				log.Printf("Warning: Failed to get reviews for PR #%d: %v", pr.GetNumber(), err)
			}
			prDetails.Reviews = reviews
			
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

// PRStats represents statistics for a pull request
type PRStats struct {
	FilesChanged int
	Additions    int
	Deletions    int
	CommitCount  int
	Patches      []PRPatch
}

// getPRStats gets statistics for a pull request
func getPRStats(client *github.Client, owner, repo string, number int) (*PRStats, error) {
	ctx := context.Background()
	
	// Get PR details
	pr, _, err := client.PullRequests.Get(ctx, owner, repo, number)
	if err != nil {
		return nil, err
	}
	
	// Get files changed in PR
	opts := &github.ListOptions{
		PerPage: 100,
	}
	
	stats := &PRStats{
		FilesChanged: 0,
		Additions:    int(pr.GetAdditions()),
		Deletions:    int(pr.GetDeletions()),
		CommitCount:  pr.GetCommits(),
		Patches:      []PRPatch{},
	}
	
	// Fetch all pages of files
	for {
		files, resp, err := client.PullRequests.ListFiles(ctx, owner, repo, number, opts)
		if err != nil {
			return stats, err
		}
		
		stats.FilesChanged += len(files)
		
		// Process each file
		for _, file := range files {
			patch := PRPatch{
				PRID:      pr.GetID(),
				Path:      file.GetFilename(),
				Patch:     file.GetPatch(),
				Filename:  file.GetFilename(),
				Status:    file.GetStatus(),
				Changes:   file.GetChanges(),
				Additions: file.GetAdditions(),
				Deletions: file.GetDeletions(),
			}
			stats.Patches = append(stats.Patches, patch)
		}
		
		if resp.NextPage == 0 {
			break
		}
		opts.Page = resp.NextPage
	}
	
	return stats, nil
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
				Path:      comment.GetPath(),
				Position:  comment.GetPosition(),
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
				Path:      "", // Issue comments don't have path
				Position:  0,  // Issue comments don't have position
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

// getPRReviews gets all reviews for a specific pull request
func getPRReviews(client *github.Client, owner, repo string, number int) ([]PRReview, error) {
	ctx := context.Background()
	
	// Options for listing reviews
	opts := &github.ListOptions{
		PerPage: 100,
	}
	
	var allReviews []PRReview
	
	// Fetch all pages of reviews
	for {
		reviews, resp, err := client.PullRequests.ListReviews(ctx, owner, repo, number, opts)
		if err != nil {
			return nil, err
		}
		
		for _, review := range reviews {
			prReview := PRReview{
				ID:        review.GetID(),
				Body:      review.GetBody(),
				State:     review.GetState(),
				CreatedAt: review.GetSubmittedAt().Time,
				UserLogin: review.GetUser().GetLogin(),
			}
			allReviews = append(allReviews, prReview)
		}
		
		if resp.NextPage == 0 {
			break
		}
		opts.Page = resp.NextPage
	}
	
	return allReviews, nil
}

// storePullRequests stores the pull requests and their details in the database
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
	insertRepoStmt, err := tx.Prepare(`
		INSERT INTO repositories (id, owner, name, created_at, updated_at)
		VALUES (?, ?, ?, NOW(), NOW())
		ON DUPLICATE KEY UPDATE updated_at = NOW()
	`)
	if err != nil {
		return err
	}
	defer insertRepoStmt.Close()
	
	insertPRStmt, err := tx.Prepare(`
		INSERT INTO pull_requests (id, repo_owner, repo_name, number, title, created_at, updated_at, state, user_login, diffs, files_changed, additions, deletions, commit_count, mergeable_state)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			title = VALUES(title),
			updated_at = VALUES(updated_at),
			state = VALUES(state),
			diffs = VALUES(diffs),
			files_changed = VALUES(files_changed),
			additions = VALUES(additions),
			deletions = VALUES(deletions),
			commit_count = VALUES(commit_count),
			mergeable_state = VALUES(mergeable_state)
	`)
	if err != nil {
		return err
	}
	defer insertPRStmt.Close()
	
	insertCommentStmt, err := tx.Prepare(`
		INSERT INTO pr_comments (id, pr_id, body, created_at, user_login, path, position)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			body = VALUES(body),
			path = VALUES(path),
			position = VALUES(position)
	`)
	if err != nil {
		return err
	}
	defer insertCommentStmt.Close()
	
	insertReviewStmt, err := tx.Prepare(`
		INSERT INTO pr_reviews (id, pr_id, body, state, created_at, user_login)
		VALUES (?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			body = VALUES(body),
			state = VALUES(state)
	`)
	if err != nil {
		return err
	}
	defer insertReviewStmt.Close()
	
	insertPatchStmt, err := tx.Prepare(`
		INSERT INTO pr_patches (pr_id, path, patch, filename, status, changes, additions, deletions)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE
			patch = VALUES(patch),
			status = VALUES(status),
			changes = VALUES(changes),
			additions = VALUES(additions),
			deletions = VALUES(deletions)
	`)
	if err != nil {
		return err
	}
	defer insertPatchStmt.Close()
	
	// Delete existing data statements
	deleteCommentsStmt, err := tx.Prepare(`
		DELETE FROM pr_comments WHERE pr_id = ?
	`)
	if err != nil {
		return err
	}
	defer deleteCommentsStmt.Close()
	
	deleteReviewsStmt, err := tx.Prepare(`
		DELETE FROM pr_reviews WHERE pr_id = ?
	`)
	if err != nil {
		return err
	}
	defer deleteReviewsStmt.Close()
	
	deletePatchesStmt, err := tx.Prepare(`
		DELETE FROM pr_patches WHERE pr_id = ?
	`)
	if err != nil {
		return err
	}
	defer deletePatchesStmt.Close()
	
	// Process each repository
	repoMap := make(map[string]bool)
	
	// Insert each PR and its details
	for _, pr := range prs {
		// Insert repository if not already done
		repoKey := fmt.Sprintf("%s/%s", pr.RepoOwner, pr.RepoName)
		if _, exists := repoMap[repoKey]; !exists {
			_, err = insertRepoStmt.Exec(
				repoKey, pr.RepoOwner, pr.RepoName,
			)
			if err != nil {
				return fmt.Errorf("failed to insert repository %s: %v", repoKey, err)
			}
			repoMap[repoKey] = true
		}
		
		// Insert or update the PR
		_, err = insertPRStmt.Exec(
			pr.ID, pr.RepoOwner, pr.RepoName, pr.Number, pr.Title, pr.CreatedAt, pr.UpdatedAt, 
			pr.State, pr.UserLogin, pr.Diffs, pr.FilesChanged, pr.Additions, pr.Deletions, 
			pr.CommitCount, pr.MergeableState,
		)
		if err != nil {
			return fmt.Errorf("failed to insert PR #%d: %v", pr.Number, err)
		}
		
		// Delete existing data for this PR to avoid duplicates
		_, err = deleteCommentsStmt.Exec(pr.ID)
		if err != nil {
			return fmt.Errorf("failed to delete existing comments for PR #%d: %v", pr.Number, err)
		}
		
		_, err = deleteReviewsStmt.Exec(pr.ID)
		if err != nil {
			return fmt.Errorf("failed to delete existing reviews for PR #%d: %v", pr.Number, err)
		}
		
		_, err = deletePatchesStmt.Exec(pr.ID)
		if err != nil {
			return fmt.Errorf("failed to delete existing patches for PR #%d: %v", pr.Number, err)
		}
		
		// Insert each comment
		for _, comment := range pr.Comments {
			_, err = insertCommentStmt.Exec(
				comment.ID, pr.ID, comment.Body, comment.CreatedAt, comment.UserLogin, comment.Path, comment.Position,
			)
			if err != nil {
				return fmt.Errorf("failed to insert comment for PR #%d: %v", pr.Number, err)
			}
		}
		
		// Insert each review
		for _, review := range pr.Reviews {
			_, err = insertReviewStmt.Exec(
				review.ID, pr.ID, review.Body, review.State, review.CreatedAt, review.UserLogin,
			)
			if err != nil {
				return fmt.Errorf("failed to insert review for PR #%d: %v", pr.Number, err)
			}
		}
		
		// Insert each patch
		for _, patch := range pr.Patches {
			_, err = insertPatchStmt.Exec(
				pr.ID, patch.Path, patch.Patch, patch.Filename, patch.Status, patch.Changes, patch.Additions, patch.Deletions,
			)
			if err != nil {
				return fmt.Errorf("failed to insert patch for PR #%d: %v", pr.Number, err)
			}
		}
	}
	
	return nil
}