from flask import Flask, render_template, jsonify, request
import mysql.connector
import os
from datetime import datetime
import json

app = Flask(__name__)

# Database configuration
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 3306)),
    'user': os.environ.get('DB_USER', 'pruser'),
    'password': os.environ.get('DB_PASSWORD', 'prpassword'),
    'database': os.environ.get('DB_NAME', 'github_prs')
}

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/repositories')
def get_repositories():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT id, owner, name, created_at, updated_at
        FROM repositories
        ORDER BY owner, name
    """)
    repos = cursor.fetchall()
    
    # Convert datetime objects to strings
    for repo in repos:
        repo['created_at'] = repo['created_at'].isoformat() if repo['created_at'] else None
        repo['updated_at'] = repo['updated_at'].isoformat() if repo['updated_at'] else None
    
    cursor.close()
    conn.close()
    
    return jsonify(repos)

@app.route('/api/summary')
def get_summary():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    
    # Build WHERE clause for repository filtering
    where_clause = ""
    params = []
    
    if repo_owner and repo_name:
        where_clause = "WHERE repo_owner = %s AND repo_name = %s"
        params = [repo_owner, repo_name]
    elif repo_owner:
        where_clause = "WHERE repo_owner = %s"
        params = [repo_owner]
    
    # Get PR summary stats
    query = f"""
        SELECT 
            COUNT(*) as total_prs,
            SUM(CASE WHEN state = 'open' THEN 1 ELSE 0 END) as open_prs,
            SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END) as closed_prs,
            COUNT(DISTINCT user_login) as unique_authors,
            SUM(files_changed) as total_files_changed,
            SUM(additions) as total_additions,
            SUM(deletions) as total_deletions,
            AVG(additions + deletions) as avg_pr_size
        FROM pull_requests
        {where_clause}
    """
    cursor.execute(query, params)
    summary = cursor.fetchone()
    
    # Get code churn by repository
    cursor.execute("""
        SELECT 
            repo_owner,
            repo_name,
            COUNT(*) as pr_count,
            SUM(additions) as total_additions,
            SUM(deletions) as total_deletions,
            SUM(files_changed) as total_files_changed
        FROM pull_requests
        GROUP BY repo_owner, repo_name
        ORDER BY pr_count DESC
    """)
    repo_stats = cursor.fetchall()
    
    # Get top contributors
    query = f"""
        SELECT 
            user_login, 
            COUNT(*) as pr_count,
            SUM(additions) as total_additions,
            SUM(deletions) as total_deletions
        FROM pull_requests
        {where_clause}
        GROUP BY user_login
        ORDER BY pr_count DESC
        LIMIT 10
    """
    cursor.execute(query, params)
    top_contributors = cursor.fetchall()
    
    # Get PRs with most discussions (comments + reviews)
    query = f"""
        SELECT 
            pr.repo_owner,
            pr.repo_name,
            pr.number,
            pr.title,
            (SELECT COUNT(*) FROM pr_comments WHERE pr_id = pr.id) +
            (SELECT COUNT(*) FROM pr_reviews WHERE pr_id = pr.id) as discussion_count,
            pr.files_changed,
            pr.additions,
            pr.deletions
        FROM 
            pull_requests pr
        {where_clause}
        HAVING discussion_count > 0
        ORDER BY 
            discussion_count DESC
        LIMIT 10
    """
    cursor.execute(query, params)
    most_discussed = cursor.fetchall()
    
    # Get review stats
    query = f"""
        SELECT 
            r.state,
            COUNT(*) as count
        FROM 
            pr_reviews r
        JOIN
            pull_requests pr ON r.pr_id = pr.id
        {where_clause}
        GROUP BY 
            r.state
    """
    cursor.execute(query, params)
    review_stats = cursor.fetchall()
    
    # Get review activity by reviewer
    query = f"""
        SELECT 
            r.user_login,
            COUNT(*) as review_count,
            SUM(CASE WHEN r.state = 'APPROVED' THEN 1 ELSE 0 END) as approvals,
            SUM(CASE WHEN r.state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) as change_requests
        FROM 
            pr_reviews r
        JOIN
            pull_requests pr ON r.pr_id = pr.id
        {where_clause}
        GROUP BY 
            r.user_login
        ORDER BY 
            review_count DESC
        LIMIT 10
    """
    cursor.execute(query, params)
    top_reviewers = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return jsonify({
        'summary': summary,
        'repo_stats': repo_stats,
        'top_contributors': top_contributors,
        'most_discussed': most_discussed,
        'review_stats': review_stats,
        'top_reviewers': top_reviewers
    })

@app.route('/api/prs')
def get_prs():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get search and filter parameters
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    state = request.args.get('state', 'all')
    author = request.args.get('author', '')
    reviewer = request.args.get('reviewer', '')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    
    # Build query with filters
    query = """
        SELECT 
            pr.id, 
            pr.repo_owner, 
            pr.repo_name, 
            pr.number, 
            pr.title, 
            pr.created_at, 
            pr.updated_at, 
            pr.state, 
            pr.user_login,
            pr.files_changed,
            pr.additions,
            pr.deletions,
            pr.commit_count,
            pr.mergeable_state,
            (SELECT COUNT(*) FROM pr_comments WHERE pr_id = pr.id) as comment_count,
            (SELECT COUNT(*) FROM pr_reviews WHERE pr_id = pr.id) as review_count,
            (SELECT COUNT(*) FROM pr_reviews WHERE pr_id = pr.id AND state = 'APPROVED') as approval_count,
            (SELECT COUNT(*) FROM pr_reviews WHERE pr_id = pr.id AND state = 'CHANGES_REQUESTED') as change_request_count
        FROM 
            pull_requests pr
        WHERE 1=1
    """
    
    params = []
    
    if repo_owner:
        query += " AND pr.repo_owner = %s"
        params.append(repo_owner)
    
    if repo_name:
        query += " AND pr.repo_name = %s"
        params.append(repo_name)
    
    if state != 'all':
        query += " AND pr.state = %s"
        params.append(state)
    
    if author:
        query += " AND pr.user_login = %s"
        params.append(author)
    
    if reviewer:
        query += " AND EXISTS (SELECT 1 FROM pr_reviews r WHERE r.pr_id = pr.id AND r.user_login = %s)"
        params.append(reviewer)
    
    query += " ORDER BY pr.updated_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    
    cursor.execute(query, params)
    prs = cursor.fetchall()
    
    # Convert datetime objects to strings for JSON serialization
    for pr in prs:
        pr['created_at'] = pr['created_at'].isoformat() if pr['created_at'] else None
        pr['updated_at'] = pr['updated_at'].isoformat() if pr['updated_at'] else None
    
    # Get total count for pagination
    count_query = """
        SELECT COUNT(*) as count 
        FROM pull_requests pr
        WHERE 1=1
    """
    
    count_params = []
    
    if repo_owner:
        count_query += " AND pr.repo_owner = %s"
        count_params.append(repo_owner)
    
    if repo_name:
        count_query += " AND pr.repo_name = %s"
        count_params.append(repo_name)
    
    if state != 'all':
        count_query += " AND pr.state = %s"
        count_params.append(state)
    
    if author:
        count_query += " AND pr.user_login = %s"
        count_params.append(author)
    
    if reviewer:
        count_query += " AND EXISTS (SELECT 1 FROM pr_reviews r WHERE r.pr_id = pr.id AND r.user_login = %s)"
        count_params.append(reviewer)
    
    cursor.execute(count_query, count_params)
    total = cursor.fetchone()['count']
    
    cursor.close()
    conn.close()
    
    return jsonify({
        'prs': prs,
        'total': total,
        'limit': limit,
        'offset': offset
    })

@app.route('/api/pr/<repo_owner>/<repo_name>/<int:pr_number>')
def get_pr_details(repo_owner, repo_name, pr_number):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get PR details
    cursor.execute("""
        SELECT 
            id, 
            repo_owner, 
            repo_name,
            number, 
            title, 
            created_at, 
            updated_at, 
            state, 
            user_login, 
            diffs,
            files_changed,
            additions,
            deletions,
            commit_count,
            mergeable_state
        FROM pull_requests
        WHERE repo_owner = %s AND repo_name = %s AND number = %s
    """, (repo_owner, repo_name, pr_number))
    pr = cursor.fetchone()
    
    if not pr:
        cursor.close()
        conn.close()
        return jsonify({'error': 'PR not found'}), 404
    
    # Convert datetime objects to strings
    pr['created_at'] = pr['created_at'].isoformat() if pr['created_at'] else None
    pr['updated_at'] = pr['updated_at'].isoformat() if pr['updated_at'] else None
    
    # Get PR comments
    cursor.execute("""
        SELECT id, body, created_at, user_login, path, position
        FROM pr_comments
        WHERE pr_id = %s
        ORDER BY created_at
    """, (pr['id'],))
    comments = cursor.fetchall()
    
    for comment in comments:
        comment['created_at'] = comment['created_at'].isoformat() if comment['created_at'] else None
    
    # Get PR reviews
    cursor.execute("""
        SELECT id, body, state, created_at, user_login
        FROM pr_reviews
        WHERE pr_id = %s
        ORDER BY created_at
    """, (pr['id'],))
    reviews = cursor.fetchall()
    
    for review in reviews:
        review['created_at'] = review['created_at'].isoformat() if review['created_at'] else None
    
    # Get PR patches
    cursor.execute("""
        SELECT id, path, patch, filename, status, changes, additions, deletions
        FROM pr_patches
        WHERE pr_id = %s
        ORDER BY path
    """, (pr['id'],))
    patches = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return jsonify({
        'pr': pr,
        'comments': comments,
        'reviews': reviews,
        'patches': patches
    })

@app.route('/api/authors')
def get_authors():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    
    query = "SELECT DISTINCT user_login FROM pull_requests WHERE 1=1"
    params = []
    
    if repo_owner:
        query += " AND repo_owner = %s"
        params.append(repo_owner)
    
    if repo_name:
        query += " AND repo_name = %s"
        params.append(repo_name)
    
    query += " ORDER BY user_login"
    
    cursor.execute(query, params)
    authors = [row['user_login'] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    
    return jsonify(authors)

@app.route('/api/reviewers')
def get_reviewers():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    
    query = """
        SELECT DISTINCT r.user_login
        FROM pr_reviews r
        JOIN pull_requests pr ON r.pr_id = pr.id
        WHERE 1=1
    """
    params = []
    
    if repo_owner:
        query += " AND pr.repo_owner = %s"
        params.append(repo_owner)
    
    if repo_name:
        query += " AND pr.repo_name = %s"
        params.append(repo_name)
    
    query += " ORDER BY r.user_login"
    
    cursor.execute(query, params)
    reviewers = [row['user_login'] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    
    return jsonify(reviewers)

@app.route('/api/code-stats')
def get_code_stats():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    
    # Build WHERE clause for repository filtering
    where_clause = ""
    params = []
    
    if repo_owner and repo_name:
        where_clause = "WHERE pr.repo_owner = %s AND pr.repo_name = %s"
        params = [repo_owner, repo_name]
    elif repo_owner:
        where_clause = "WHERE pr.repo_owner = %s"
        params = [repo_owner]
    
    # Get most modified files
    query = f"""
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
        {where_clause}
        GROUP BY 
            p.filename
        ORDER BY 
            total_changes DESC
        LIMIT 20
    """
    cursor.execute(query, params)
    file_stats = cursor.fetchall()
    
    # Get file extension statistics
    query = f"""
        SELECT 
            SUBSTRING_INDEX(p.filename, '.', -1) as extension,
            COUNT(DISTINCT p.id) as file_count,
            SUM(p.changes) as total_changes,
            SUM(p.additions) as total_additions,
            SUM(p.deletions) as total_deletions
        FROM 
            pr_patches p
        JOIN
            pull_requests pr ON p.pr_id = pr.id
        {where_clause}
        GROUP BY 
            extension
        HAVING
            extension NOT LIKE '%/%'
            AND extension != ''
            AND LENGTH(extension) < 10
        ORDER BY 
            file_count DESC
        LIMIT 15
    """
    cursor.execute(query, params)
    extension_stats = cursor.fetchall()
    
    # Get PR size distribution
    query = f"""
        SELECT 
            CASE
                WHEN additions + deletions < 50 THEN 'XS (< 50)'
                WHEN additions + deletions < 200 THEN 'S (50-199)'
                WHEN additions + deletions < 500 THEN 'M (200-499)'
                WHEN additions + deletions < 1000 THEN 'L (500-999)'
                WHEN additions + deletions < 2000 THEN 'XL (1000-1999)'
                ELSE 'XXL (2000+)'
            END as size_category,
            COUNT(*) as pr_count
        FROM 
            pull_requests pr
        {where_clause}
        GROUP BY 
            size_category
        ORDER BY 
            FIELD(size_category, 'XS (< 50)', 'S (50-199)', 'M (200-499)', 'L (500-999)', 'XL (1000-1999)', 'XXL (2000+)')
    """
    cursor.execute(query, params)
    pr_size_stats = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return jsonify({
        'file_stats': file_stats,
        'extension_stats': extension_stats,
        'pr_size_stats': pr_size_stats
    })

@app.route('/api/timeline')
def get_timeline():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    repo_owner = request.args.get('repo_owner', '')
    repo_name = request.args.get('repo_name', '')
    period = request.args.get('period', 'month')  # 'day', 'week', 'month'
    
    # Build WHERE clause for repository filtering
    where_clause = ""
    params = []
    
    if repo_owner and repo_name:
        where_clause = "WHERE repo_owner = %s AND repo_name = %s"
        params = [repo_owner, repo_name]
    elif repo_owner:
        where_clause = "WHERE repo_owner = %s"
        params = [repo_owner]
    
    # Build date format based on period
    date_format = "%Y-%m-%d"
    if period == 'week':
        date_format = "%Y-%u"  # ISO week
    elif period == 'month':
        date_format = "%Y-%m"
    
    # Get PR creation timeline
    query = f"""
        SELECT 
            DATE_FORMAT(created_at, '{date_format}') as time_period,
            COUNT(*) as pr_count,
            SUM(additions) as additions,
            SUM(deletions) as deletions
        FROM 
            pull_requests
        {where_clause}
        GROUP BY 
            time_period
        ORDER BY 
            MIN(created_at)
    """
    cursor.execute(query, params)
    timeline = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return jsonify(timeline)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)