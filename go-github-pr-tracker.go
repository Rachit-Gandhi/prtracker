package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"github.com/google/go-github/v55/github"
	"golang.org/x/oauth2"
	"golang.org/x/time/rate"
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
	DB               DBConfig
	GitHubToken      string
	Repositories     []Repository
	FetchSinceDays   int
	Concurrency      int     // Number of concurrent workers
	RateLimitPerHour float64 // GitHub API rate limit per hour
	MaxPRsToProcess  int     // Maximum number of PRs to process per repository
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
	BaseCommitSHA  string    // Base commit SHA
	BaseCommitLink string    // Link to base commit in GitHub
	ProcessedTime  time.Time // When this PR was processed
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
	PRID      int64
	Path      string
	Patch     string
	Filename  string
	Status    string // added, removed, modified, renamed, etc.
	Changes   int
	Additions int
	Deletions int
}

// RateLimitedClient is a GitHub client with rate limiting
type RateLimitedClient struct {
	Client    *github.Client
	Limiter   *rate.Limiter
	RateState struct {
		Remaining int
		Reset     time.Time
	}
	Mutex sync.Mutex
}

// PRStats represents statistics for a pull request
type PRStats struct {
	FilesChanged int
	Additions    int
	Deletions    int
	CommitCount  int
	Patches      []PRPatch
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

	// Setup GitHub client with rate limiting
	client := setupRateLimitedClient(config.GitHubToken, config.RateLimitPerHour)

	// Process each repository
	log.Printf("Starting PR processing for %d repositories", len(config.Repositories))
	log.Printf("Maximum PRs per repository: %d", config.MaxPRsToProcess)

	// Prepare a channel for workers
	type WorkItem struct {
		Repo Repository
	}
	workChan := make(chan WorkItem, len(config.Repositories))

	// Start worker goroutines
	var wg sync.WaitGroup
	for i := 0; i < config.Concurrency; i++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			for work := range workChan {
				repo := work.Repo
				log.Printf("Worker %d: Processing repository: %s/%s",
					workerID, repo.Owner, repo.Name)

				// Get pull requests from GitHub
				prs, err := fetchPullRequests(client, repo, config.FetchSinceDays, config.MaxPRsToProcess)
				if err != nil {
					log.Printf("Failed to fetch pull requests for %s/%s: %v",
						repo.Owner, repo.Name, err)
					continue
				}

				if len(prs) == 0 {
					log.Printf("No pull requests found for %s/%s",
						repo.Owner, repo.Name)
					continue
				}

				// Store pull requests in the database
				if err := storePullRequests(db, prs); err != nil {
					log.Printf("Failed to store pull requests for %s/%s: %v",
						repo.Owner, repo.Name, err)
					continue
				}

				log.Printf("Successfully processed %d pull requests for %s/%s",
					len(prs), repo.Owner, repo.Name)
			}
		}(i)
	}

	// Queue up all repositories
	for _, repo := range config.Repositories {
		workChan <- WorkItem{Repo: repo}
	}
	close(workChan)

	// Wait for all workers to finish
	wg.Wait()
	log.Printf("Completed processing all repositories")
}

// setupRateLimitedClient sets up a GitHub client with rate limiting
func setupRateLimitedClient(token string, ratePerHour float64) *RateLimitedClient {
	ctx := context.Background()
	ts := oauth2.StaticTokenSource(
		&oauth2.Token{AccessToken: token},
	)
	tc := oauth2.NewClient(ctx, ts)

	// Create rate limiter: requests per second
	ratePerSecond := ratePerHour / 3600
	limiter := rate.NewLimiter(rate.Limit(ratePerSecond), 1)

	return &RateLimitedClient{
		Client:  github.NewClient(tc),
		Limiter: limiter,
	}
}

// DoRequest performs a rate-limited GitHub API request
func (c *RateLimitedClient) DoRequest(f func() (*github.Response, error)) (*github.Response, error) {
	// Wait for rate limit
	c.Limiter.Wait(context.Background())

	// Lock for rate state updates
	c.Mutex.Lock()
	defer c.Mutex.Unlock()

	// Check if we're close to hitting the rate limit
	if c.RateState.Remaining < 10 && time.Now().Before(c.RateState.Reset) {
		// Sleep until reset time plus a small buffer
		sleepTime := time.Until(c.RateState.Reset) + time.Second
		log.Printf("Rate limit almost exceeded! Sleeping for %v until reset", sleepTime)
		time.Sleep(sleepTime)
	}

	// Execute the actual request
	resp, err := f()

	// Update rate limit state if response is available
	if resp != nil && resp.Rate.Limit > 0 {
		c.RateState.Remaining = resp.Rate.Remaining
		c.RateState.Reset = resp.Rate.Reset.Time
	}

	return resp, err
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
			User:     getEnv("DB_USER", "pruser"),
			Password: getEnv("DB_PASSWORD", "prpassword"),
			DBName:   getEnv("DB_NAME", "github_prs"),
		},
		GitHubToken:      getEnv("GITHUB_TOKEN", ""),
		Repositories:     repos,
		FetchSinceDays:   getIntEnv("FETCH_SINCE_DAYS", 30),
		Concurrency:      getIntEnv("CONCURRENCY", 3),              // Default to 3 concurrent workers
		RateLimitPerHour: getFloatEnv("RATE_LIMIT_PER_HOUR", 5000), // Default to GitHub's standard 5000 req/hour
		MaxPRsToProcess:  getIntEnv("MAX_PRS_PER_REPO", 50),        // Default to 50 PRs per repo
	}
}

// getFloatEnv gets an environment variable as a float or returns a default value
func getFloatEnv(key string, defaultValue float64) float64 {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	var result float64
	fmt.Sscanf(value, "%f", &result)
	return result
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

	// Add retry logic for database connection
	var db *sql.DB
	var err error
	maxRetries := 5
	retryDelay := 5 * time.Second

	for i := 0; i < maxRetries; i++ {
		db, err = sql.Open("mysql", dsn)
		if err != nil {
			log.Printf("Failed to open database connection (attempt %d/%d): %v",
				i+1, maxRetries, err)
			time.Sleep(retryDelay)
			continue
		}

		// Test the connection
		if err = db.Ping(); err != nil {
			log.Printf("Failed to ping database (attempt %d/%d): %v",
				i+1, maxRetries, err)
			db.Close()
			time.Sleep(retryDelay)
			continue
		}

		// Success
		log.Printf("Successfully connected to database %s at %s:%d",
			config.DBName, config.Host, config.Port)
		return db, nil
	}

	return nil, fmt.Errorf("failed to connect to database after %d attempts: %v",
		maxRetries, err)
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
			base_commit_sha VARCHAR(40),
			base_commit_link VARCHAR(255),
			last_processed_time DATETIME,
			UNIQUE INDEX idx_repo_number (repo_owner, repo_name, number),
			INDEX idx_user (user_login),
			INDEX idx_state (state),
			INDEX idx_updated (updated_at)
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
			path VARCHAR(255),
			position INT,
			FOREIGN KEY (pr_id) REFERENCES pull_requests(id) ON DELETE CASCADE,
			INDEX idx_pr_id (pr_id)
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
			INDEX idx_pr_id (pr_id)
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
			UNIQUE INDEX idx_pr_path (pr_id, path)
		)
	`)
	if err != nil {
		return err
	}

	log.Println("Database tables created or verified successfully")
	return nil
}

// fetchPullRequests fetches pull requests from GitHub
func fetchPullRequests(client *RateLimitedClient, repo Repository, fetchSinceDays, maxPRs int) ([]PullRequest, error) {
	ctx := context.Background()

	// Calculate the time since when to fetch PRs
	sinceTime := time.Now().AddDate(0, 0, -fetchSinceDays)

	// Options for listing pull requests
	opts := &github.PullRequestListOptions{
		State:     "all",
		Sort:      "updated",
		Direction: "desc",
		ListOptions: github.ListOptions{
			PerPage: 50, // Fetch in batches of 50
		},
	}

	var allPRs []PullRequest
	prsProcessed := 0

	// Fetch pages of pull requests until we have enough or run out
	for {
		// Break if we've processed enough PRs
		if prsProcessed >= maxPRs {
			log.Printf("Reached max PRs limit (%d) for %s/%s",
				maxPRs, repo.Owner, repo.Name)
			break
		}

		var prs []*github.PullRequest
		var resp *github.Response
		var err error

		// Perform rate-limited GitHub API request
		resp, err = client.DoRequest(func() (*github.Response, error) {
			var innerResp *github.Response
			prs, innerResp, err = client.Client.PullRequests.List(ctx, repo.Owner, repo.Name, opts)
			return innerResp, err
		})

		if err != nil {
			return nil, fmt.Errorf("API error: %v", err)
		}

		if len(prs) == 0 {
			log.Printf("No more PRs to fetch for %s/%s", repo.Owner, repo.Name)
			break
		}

		// Process each PR
		for _, pr := range prs {
			// Skip PRs older than our cutoff
			if pr.UpdatedAt.Time.Before(sinceTime) {
				continue
			}

			// Create the base commit link
			baseCommitSHA := pr.GetBase().GetSHA()
			baseCommitLink := fmt.Sprintf("https://github.com/%s/%s/commit/%s",
				repo.Owner, repo.Name, baseCommitSHA)

			prDetails := PullRequest{
				ID:             pr.GetID(),
				Number:         pr.GetNumber(),
				Title:          pr.GetTitle(),
				CreatedAt:      pr.GetCreatedAt().Time,
				UpdatedAt:      pr.GetUpdatedAt().Time,
				State:          pr.GetState(),
				UserLogin:      pr.GetUser().GetLogin(),
				RepoOwner:      repo.Owner,
				RepoName:       repo.Name,
				MergeableState: pr.GetMergeableState(),
				BaseCommitSHA:  baseCommitSHA,
				BaseCommitLink: baseCommitLink,
				ProcessedTime:  time.Now(),
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
			prsProcessed++

			log.Printf("Processed PR #%d: %s (State: %s)",
				pr.GetNumber(), pr.GetTitle(), pr.GetState())

			// Break if we've processed enough PRs
			if prsProcessed >= maxPRs {
				break
			}
		}

		if resp.NextPage == 0 || prsProcessed >= maxPRs {
			break
		}
		opts.Page = resp.NextPage
	}

	log.Printf("Fetched %d PRs for %s/%s", len(allPRs), repo.Owner, repo.Name)
	return allPRs, nil
}

// getPRStats gets statistics for a pull request
func getPRStats(client *RateLimitedClient, owner, repo string, number int) (*PRStats, error) {
	ctx := context.Background()

	// Get PR details
	var pr *github.PullRequest
	var err error

	_, err = client.DoRequest(func() (*github.Response, error) {
		var resp *github.Response
		pr, resp, err = client.Client.PullRequests.Get(ctx, owner, repo, number)
		return resp, err
	})

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
		var files []*github.CommitFile
		var resp *github.Response

		resp, err = client.DoRequest(func() (*github.Response, error) {
			var innerResp *github.Response
			files, innerResp, err = client.Client.PullRequests.ListFiles(ctx, owner, repo, number, opts)
			return innerResp, err
		})

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
func getPRDiff(client *RateLimitedClient, owner, repo string, number int) (string, error) {
	ctx := context.Background()

	// Get the PR in raw diff format
	opts := github.RawOptions{Type: github.Diff}
	var diff string

	_, err := client.DoRequest(func() (*github.Response, error) {
		var resp *github.Response
		var err error
		diff, resp, err = client.Client.PullRequests.GetRaw(ctx, owner, repo, number, opts)
		return resp, err
	})

	if err != nil {
		return "", err
	}

	return diff, nil
}

// getPRComments gets all comments for a specific pull request
func getPRComments(client *RateLimitedClient, owner, repo string, number int) ([]PRComment, error) {
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
		var comments []*github.PullRequestComment
		var resp *github.Response

		resp, err := client.DoRequest(func() (*github.Response, error) {
			var innerResp *github.Response
			var err error
			comments, innerResp, err = client.Client.PullRequests.ListComments(ctx, owner, repo, number, opts)
			return innerResp, err
		})

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
		var comments []*github.IssueComment
		var resp *github.Response

		resp, err := client.DoRequest(func() (*github.Response, error) {
			var innerResp *github.Response
			var err error
			comments, innerResp, err = client.Client.Issues.ListComments(ctx, owner, repo, number, issueOpts)
			return innerResp, err
		})

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
func getPRReviews(client *RateLimitedClient, owner, repo string, number int) ([]PRReview, error) {
	ctx := context.Background()

	// Options for listing reviews
	opts := &github.ListOptions{
		PerPage: 100,
	}

	var allReviews []PRReview

	// Fetch all pages of reviews
	for {
		var reviews []*github.PullRequestReview
		var resp *github.Response

		resp, err := client.DoRequest(func() (*github.Response, error) {
			var innerResp *github.Response
			var err error
			reviews, innerResp, err = client.Client.PullRequests.ListReviews(ctx, owner, repo, number, opts)
			return innerResp, err
		})

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
	// Process each repository
	repoMap := make(map[string]bool)

	// Process each PR individually with its own transaction
	for _, pr := range prs {
		// Start a transaction for this PR
		tx, err := db.Begin()
		if err != nil {
			log.Printf("Failed to begin transaction for PR #%d: %v", pr.Number, err)
			continue
		}

		// Insert repository if not already done
		repoKey := fmt.Sprintf("%s/%s", pr.RepoOwner, pr.RepoName)
		if _, exists := repoMap[repoKey]; !exists {
			_, err = tx.Exec(`
				INSERT INTO repositories (id, owner, name, created_at, updated_at)
				VALUES (?, ?, ?, NOW(), NOW())
				ON DUPLICATE KEY UPDATE updated_at = NOW()
			`, repoKey, pr.RepoOwner, pr.RepoName)

			if err != nil {
				tx.Rollback()
				log.Printf("Failed to insert repository %s: %v", repoKey, err)
				continue
			}
			repoMap[repoKey] = true
		}

		// Insert or update the PR
		_, err = tx.Exec(`
			INSERT INTO pull_requests (
				id, repo_owner, repo_name, number, title, created_at, updated_at,
				state, user_login, diffs, files_changed, additions, deletions,
				commit_count, mergeable_state, base_commit_sha, base_commit_link, last_processed_time
			) VALUES (
				?, ?, ?, ?, ?, ?, ?,
				?, ?, ?, ?, ?, ?,
				?, ?, ?, ?, ?
			) ON DUPLICATE KEY UPDATE
				title = VALUES(title),
				updated_at = VALUES(updated_at),
				state = VALUES(state),
				diffs = VALUES(diffs),
				files_changed = VALUES(files_changed),
				additions = VALUES(additions),
				deletions = VALUES(deletions),
				commit_count = VALUES(commit_count),
				mergeable_state = VALUES(mergeable_state),
				base_commit_sha = VALUES(base_commit_sha),
				base_commit_link = VALUES(base_commit_link),
				last_processed_time = VALUES(last_processed_time)
		`,
			pr.ID, pr.RepoOwner, pr.RepoName, pr.Number, pr.Title, pr.CreatedAt, pr.UpdatedAt,
			pr.State, pr.UserLogin, pr.Diffs, pr.FilesChanged, pr.Additions, pr.Deletions,
			pr.CommitCount, pr.MergeableState, pr.BaseCommitSHA, pr.BaseCommitLink, pr.ProcessedTime)

		if err != nil {
			tx.Rollback()
			log.Printf("Failed to insert PR #%d: %v", pr.Number, err)
			continue
		}

		// Delete existing data for this PR to avoid duplicates
		_, err = tx.Exec("DELETE FROM pr_comments WHERE pr_id = ?", pr.ID)
		if err != nil {
			tx.Rollback()
			log.Printf("Failed to delete existing comments for PR #%d: %v", pr.Number, err)
			continue
		}

		_, err = tx.Exec("DELETE FROM pr_reviews WHERE pr_id = ?", pr.ID)
		if err != nil {
			tx.Rollback()
			log.Printf("Failed to delete existing reviews for PR #%d: %v", pr.Number, err)
			continue
		}

		_, err = tx.Exec("DELETE FROM pr_patches WHERE pr_id = ?", pr.ID)
		if err != nil {
			tx.Rollback()
			log.Printf("Failed to delete existing patches for PR #%d: %v", pr.Number, err)
			continue
		}

		// Insert each comment
		for _, comment := range pr.Comments {
			_, err = tx.Exec(`
				INSERT INTO pr_comments (id, pr_id, body, created_at, user_login, path, position)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				ON DUPLICATE KEY UPDATE
					body = VALUES(body),
					path = VALUES(path),
					position = VALUES(position)
			`, comment.ID, pr.ID, comment.Body, comment.CreatedAt, comment.UserLogin, comment.Path, comment.Position)

			if err != nil {
				log.Printf("Warning: Failed to insert comment for PR #%d: %v", pr.Number, err)
				// Continue despite error with comments
			}
		}

		// Insert each review
		for _, review := range pr.Reviews {
			_, err = tx.Exec(`
				INSERT INTO pr_reviews (id, pr_id, body, state, created_at, user_login)
				VALUES (?, ?, ?, ?, ?, ?)
				ON DUPLICATE KEY UPDATE
					body = VALUES(body),
					state = VALUES(state)
			`, review.ID, pr.ID, review.Body, review.State, review.CreatedAt, review.UserLogin)

			if err != nil {
				log.Printf("Warning: Failed to insert review for PR #%d: %v", pr.Number, err)
				// Continue despite error with reviews
			}
		}

		// Insert each patch
		for _, patch := range pr.Patches {
			_, err = tx.Exec(`
				INSERT INTO pr_patches (pr_id, path, patch, filename, status, changes, additions, deletions)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?)
				ON DUPLICATE KEY UPDATE
					patch = VALUES(patch),
					status = VALUES(status),
					changes = VALUES(changes),
					additions = VALUES(additions),
					deletions = VALUES(deletions)
			`, pr.ID, patch.Path, patch.Patch, patch.Filename, patch.Status, patch.Changes, patch.Additions, patch.Deletions)

			if err != nil {
				log.Printf("Warning: Failed to insert patch for PR #%d: %v", pr.Number, err)
				// Continue despite error with patches
			}
		}

		// Commit transaction for this PR
		err = tx.Commit()
		if err != nil {
			tx.Rollback()
			log.Printf("Failed to commit transaction for PR #%d: %v", pr.Number, err)
			continue
		}

		log.Printf("Successfully stored PR #%d in database", pr.Number)
	}

	return nil
}
