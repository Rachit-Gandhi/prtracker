import json
import os
import sys
import re
import argparse
from collections import defaultdict
import glob
from dotenv import load_dotenv
import openai
from tqdm import tqdm
from termcolor import colored

class PRReviewAnalyzer:
    def __init__(self, openai_api_key):
        self.openai_api_key = openai_api_key
        openai.api_key = openai_api_key
        self.metrics = {
            "total_prs_analyzed": 0,
            "human_reviewer_comments": 0,
            "ai_review_comments": 0,
            "file_overlap_scores": [],
            "sentiment_agreement_scores": [],
            "content_overlap_scores": [],
            "human_comments_without_ai_match": 0,
            "ai_comments_without_human_match": 0,
            "pr_results": []
        }
    
    def load_pr_data(self, json_file):
        """Load PR data from a JSON file"""
        try:
            with open(json_file, 'r') as f:
                pr_data = json.load(f)
            return pr_data
        except Exception as e:
            print(f"Error loading {json_file}: {e}")
            return None
    
    def extract_file_to_comments_map(self, pr_data):
        """Extract mapping from filenames to human comments"""
        file_to_comments = defaultdict(list)
        
        # Process PR comments
        for comment in pr_data.get("comments", []):
            if comment.get("path") and comment["path"].strip():
                file_to_comments[comment["path"]].append({
                    "user_login": comment.get("user_login", ""),
                    "body": comment.get("body", ""),
                    "line": comment.get("position", 0)
                })
        
        # Process review comments (these are more formal code reviews)
        for review in pr_data.get("github_reviews", []):
            if review.get("body"):
                # Sometimes reviews are not attached to specific files
                review_body = review.get("body", "")
                if review_body and not review_body.isspace():
                    file_to_comments["general"].append({
                        "user_login": review.get("user_login", ""),
                        "body": review_body,
                        "line": 0
                    })
        
        return file_to_comments
    
    def extract_file_to_ai_comments_map(self, pr_data):
        """Extract mapping from filenames to AI review comments"""
        file_to_ai_comments = defaultdict(list)
        
        # Process AI reviews
        for ai_review in pr_data.get("ai_reviews", []):
            # Process overall AI review summary
            file_to_ai_comments["general"].append({
                "user_login": "AI_reviewer",
                "body": ai_review.get("summary", ""),
                "line": 0,
                "type": "summary"
            })
            
            # Process file-specific AI reviews
            for file_review in ai_review.get("file_reviews", []):
                filename = file_review.get("filename", "")
                if filename:
                    file_to_ai_comments[filename].append({
                        "user_login": "AI_reviewer",
                        "body": file_review.get("content", ""),
                        "line": 0,
                        "type": "file_review"
                    })
        
        return file_to_ai_comments
    
    def extract_file_changes(self, pr_data):
        """Extract information about file changes in the PR"""
        file_changes = {}
        
        for patch in pr_data.get("patches", []):
            filename = patch.get("filename", "")
            if filename:
                file_changes[filename] = {
                    "patch": patch.get("patch", ""),
                    "additions": patch.get("additions", 0),
                    "deletions": patch.get("deletions", 0),
                    "status": patch.get("status", "modified")
                }
        
        return file_changes
    
    def compute_file_overlap_score(self, human_files, ai_files):
        """Compute score for overlap between files commented on by humans vs AI"""
        human_files_set = set(human_files)
        ai_files_set = set(ai_files)
        
        if not human_files_set and not ai_files_set:
            return 1.0  # Both empty means perfect agreement
        
        if not human_files_set or not ai_files_set:
            return 0.0  # One empty and other not means no agreement
        
        # Jaccard similarity: intersection over union
        intersection = len(human_files_set.intersection(ai_files_set))
        union = len(human_files_set.union(ai_files_set))
        
        return intersection / union if union > 0 else 0.0
    
    def analyze_comment_sentiment(self, comment):
        """Classify a comment as positive, neutral, or negative"""
        positive_patterns = [
            r'lgtm', r'looks good', r'great', r'nice', r'good job', r'well done',
            r'approve', r'make\s+sense', r'\+1', r'👍', r'approve', r'approved',
            r'excellent'
        ]
        
        negative_patterns = [
            r'issue', r'bug', r'error', r'problem', r'fix', r'incorrect', 
            r'wrong', r'concern', r'bad', r'fail', r'needs\s+work',
            r'not\s+work', r"doesn't\s+work", r'broken', r'reject', r'rejected',
            r'-1', r'👎', r'nit:', r'suggestion'
        ]
        
        text = comment.lower()
        
        # Check for positive patterns
        for pattern in positive_patterns:
            if re.search(pattern, text):
                return "positive"
        
        # Check for negative patterns
        for pattern in negative_patterns:
            if re.search(pattern, text):
                return "negative"
        
        # Default is neutral
        return "neutral"
    
    def compute_sentiment_agreement(self, human_comments, ai_comments):
        """Compute agreement between human and AI comment sentiments"""
        if not human_comments or not ai_comments:
            return 0.5  # Neutral if either is missing
        
        # Aggregate sentiments
        human_sentiments = [self.analyze_comment_sentiment(comment["body"]) for comment in human_comments]
        ai_sentiments = [self.analyze_comment_sentiment(comment["body"]) for comment in ai_comments]
        
        # Count sentiments
        human_sentiment_counts = {
            "positive": human_sentiments.count("positive"),
            "neutral": human_sentiments.count("neutral"),
            "negative": human_sentiments.count("negative")
        }
        
        ai_sentiment_counts = {
            "positive": ai_sentiments.count("positive"),
            "neutral": ai_sentiments.count("neutral"),
            "negative": ai_sentiments.count("negative")
        }
        
        # Determine predominant sentiment
        human_predominant = max(human_sentiment_counts, key=human_sentiment_counts.get)
        ai_predominant = max(ai_sentiment_counts, key=ai_sentiment_counts.get)
        
        # Score agreement
        if human_predominant == ai_predominant:
            return 1.0
        elif (human_predominant == "neutral" or ai_predominant == "neutral"):
            return 0.5
        else:
            return 0.0  # Complete disagreement (positive vs negative)
    
    def evaluate_content_overlap(self, human_comments, ai_comments, file_change=None):
        """Evaluate content overlap between human and AI comments using OpenAI API"""
        if not human_comments or not ai_comments:
            return {
                "score": 0.0,
                "reasoning": "No overlap - either human or AI comments missing",
                "human_only": "N/A",
                "ai_only": "N/A"
            }
        
        # Combine all human comments
        human_text = "\n".join([comment["body"] for comment in human_comments])
        
        # Combine all AI comments
        ai_text = "\n".join([comment["body"] for comment in ai_comments])
        
        # Prepare the context with file change if available
        context = ""
        if file_change:
            context = f"File changes summary:\n{file_change['patch'][:500]}...\n\n"
        
        # Create a prompt for OpenAI
        prompt = f"""
        I want to compare how similar the content is between human reviewer comments and AI reviewer comments on the same code.
        
        {context}
        
        Human reviewer comments:
        -------------------------
        {human_text[:1000]}
        
        AI reviewer comments:
        ---------------------
        {ai_text[:1000]}
        
        Please analyze the similarity between these comments on a scale of 0 to 1, where:
        - 0 means completely different topics/issues addressed
        - 0.5 means some overlap in topics but significant differences
        - 1 means very similar topics/issues addressed
        
        Also note if the AI identifies issues that humans missed or vice versa.
        
        Return your response in this format:
        Content Overlap Score: [0-1 value]
        Reasoning: [brief explanation]
        Human-only points: [issues only humans raised]
        AI-only points: [issues only AI raised]
        """
        
        try:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are an expert code reviewer analyzing the similarity between human and AI code reviews."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            
            analysis = response.choices[0].message.content
            
            # Extract score
            score_match = re.search(r'Content Overlap Score:\s*([0-9.]+)', analysis)
            if score_match:
                score = float(score_match.group(1))
                if score < 0:
                    score = 0
                elif score > 1:
                    score = 1
            else:
                score = 0.5  # Default if parsing fails
            
            # Extract other insights
            human_only = re.search(r'Human-only points:(.*?)(?:AI-only points:|$)', analysis, re.DOTALL)
            human_only = human_only.group(1).strip() if human_only else "None identified"
            
            ai_only = re.search(r'AI-only points:(.*?)(?:$)', analysis, re.DOTALL)
            ai_only = ai_only.group(1).strip() if ai_only else "None identified"
            
            return {
                "score": score,
                "reasoning": analysis,
                "human_only": human_only,
                "ai_only": ai_only
            }
            
        except Exception as e:
            print(f"Error evaluating content overlap: {e}")
            return {
                "score": 0.5,
                "reasoning": f"Error: {str(e)}",
                "human_only": "Error analyzing",
                "ai_only": "Error analyzing"
            }
    
    def analyze_pr(self, pr_data):
        """Analyze a single PR"""
        pr_number = pr_data.get("number", "unknown")
        print(f"\nAnalyzing PR #{pr_number}: {pr_data.get('title', '')}")
        
        # Extract data
        human_comments_map = self.extract_file_to_comments_map(pr_data)
        ai_comments_map = self.extract_file_to_ai_comments_map(pr_data)
        file_changes = self.extract_file_changes(pr_data)
        
        # Calculate metrics
        human_commented_files = set(human_comments_map.keys())
        ai_commented_files = set(ai_comments_map.keys())
        
        # File overlap score
        file_overlap_score = self.compute_file_overlap_score(
            human_commented_files, 
            ai_commented_files
        )
        self.metrics["file_overlap_scores"].append(file_overlap_score)
        
        # Calculate per-file metrics
        all_files = set(human_commented_files).union(ai_commented_files).union(set(file_changes.keys()))
        per_file_results = {}
        
        for filename in all_files:
            human_comments = human_comments_map.get(filename, [])
            ai_comments = ai_comments_map.get(filename, [])
            file_change = file_changes.get(filename)
            
            # Skip general comments for some metrics
            if filename == "general":
                continue
                
            # Count comments without matches
            if human_comments and not ai_comments:
                self.metrics["human_comments_without_ai_match"] += 1
            
            if ai_comments and not human_comments:
                self.metrics["ai_comments_without_human_match"] += 1
            
            # Calculate sentiment agreement
            sentiment_agreement = self.compute_sentiment_agreement(human_comments, ai_comments)
            
            # Calculate content overlap
            content_analysis = self.evaluate_content_overlap(human_comments, ai_comments, file_change)
            
            # Make sure content_analysis is a dictionary
            if isinstance(content_analysis, dict):
                content_overlap_score = content_analysis.get("score", 0.5)
            else:
                # If content_analysis is not a dictionary (perhaps a float was returned), create proper structure
                content_overlap_score = float(content_analysis) if content_analysis is not None else 0.5
                content_analysis = {
                    "score": content_overlap_score,
                    "reasoning": "No detailed analysis available",
                    "human_only": "N/A",
                    "ai_only": "N/A"
                }
            
            if human_comments and ai_comments:  # Only count if both commented
                self.metrics["sentiment_agreement_scores"].append(sentiment_agreement)
                self.metrics["content_overlap_scores"].append(content_overlap_score)
            
            per_file_results[filename] = {
                "human_comments_count": len(human_comments),
                "ai_comments_count": len(ai_comments),
                "sentiment_agreement": sentiment_agreement,
                "content_overlap": content_overlap_score,
                "content_analysis": content_analysis
            }
        
        # General PR level assessment using OpenAI
        pr_assessment = self.overall_pr_assessment(pr_data, human_comments_map, ai_comments_map)
        
        # Update metrics
        self.metrics["total_prs_analyzed"] += 1
        self.metrics["human_reviewer_comments"] += sum(len(comments) for comments in human_comments_map.values())
        self.metrics["ai_review_comments"] += sum(len(comments) for comments in ai_comments_map.values())
        
        # Add PR results
        pr_result = {
            "pr_number": pr_number,
            "title": pr_data.get("title", ""),
            "file_overlap_score": file_overlap_score,
            "per_file_results": per_file_results,
            "overall_assessment": pr_assessment
        }
        self.metrics["pr_results"].append(pr_result)
        
        return pr_result
    
    def overall_pr_assessment(self, pr_data, human_comments_map, ai_comments_map):
        """Generate an overall assessment of the PR review quality"""
        # Combine all human comments
        all_human_comments = []
        for filename, comments in human_comments_map.items():
            all_human_comments.extend(comments)
        
        human_text = "\n".join([f"[{comment['user_login']}]: {comment['body']}" 
                               for comment in all_human_comments])
        
        # Combine all AI comments
        ai_summary = ""
        ai_file_reviews = []
        
        for ai_review in pr_data.get("ai_reviews", []):
            ai_summary += ai_review.get("summary", "")
            for file_review in ai_review.get("file_reviews", []):
                ai_file_reviews.append(f"[{file_review.get('filename', '')}]: {file_review.get('content', '')[:500]}...")
        
        ai_text = ai_summary + "\n" + "\n".join(ai_file_reviews[:3])  # Limit to first 3 files for brevity
        
        # Create a prompt for OpenAI
        prompt = f"""
        I want to compare the overall quality and completeness of human reviewer comments versus AI reviewer comments on PR #{pr_data.get('number')}: "{pr_data.get('title')}".
        
        Human reviewer comments (sample):
        --------------------------------
        {human_text[:1500]}
        
        AI reviewer comments (sample):
        ----------------------------
        {ai_text[:1500]}
        
        Please provide:
        1. A subjective score from 0-10 for the AI review quality compared to human reviews
        2. A binary assessment (Yes/No) of whether the AI review could substitute for human review
        3. A brief explanation of your assessment
        4. What the AI review missed that humans caught (if applicable)
        5. What the AI review caught that humans missed (if applicable)
        
        Return your response in this format:
        Quality Score: [0-10]
        Could Substitute: [Yes/No]
        Explanation: [explanation]
        Humans caught but AI missed: [issues]
        AI caught but humans missed: [issues]
        """
        
        try:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are an expert in code review, evaluating the quality of code reviews."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            
            assessment = response.choices[0].message.content
            
            # Extract data
            quality_score_match = re.search(r'Quality Score:\s*(\d+)', assessment)
            quality_score = int(quality_score_match.group(1)) if quality_score_match else 5
            
            substitute_match = re.search(r'Could Substitute:\s*(Yes|No)', assessment, re.IGNORECASE)
            could_substitute = substitute_match.group(1).lower() == "yes" if substitute_match else False
            
            return {
                "quality_score": quality_score,
                "could_substitute": could_substitute,
                "full_assessment": assessment
            }
            
        except Exception as e:
            print(f"Error generating overall assessment: {e}")
            return {
                "quality_score": 5,
                "could_substitute": False,
                "full_assessment": f"Error generating assessment: {str(e)}"
            }
    
    def compute_aggregate_metrics(self):
        """Compute aggregate metrics across all PRs"""
        return {
            "total_prs_analyzed": self.metrics["total_prs_analyzed"],
            "total_human_comments": self.metrics["human_reviewer_comments"],
            "total_ai_comments": self.metrics["ai_review_comments"],
            "avg_file_overlap_score": sum(self.metrics["file_overlap_scores"]) / len(self.metrics["file_overlap_scores"]) 
                if self.metrics["file_overlap_scores"] else 0,
            "avg_sentiment_agreement": sum(self.metrics["sentiment_agreement_scores"]) / len(self.metrics["sentiment_agreement_scores"])
                if self.metrics["sentiment_agreement_scores"] else 0,
            "avg_content_overlap": sum(self.metrics["content_overlap_scores"]) / len(self.metrics["content_overlap_scores"])
                if self.metrics["content_overlap_scores"] else 0,
            "human_comments_without_ai_match": self.metrics["human_comments_without_ai_match"],
            "ai_comments_without_human_match": self.metrics["ai_comments_without_human_match"],
            "pr_quality_scores": [pr["overall_assessment"]["quality_score"] for pr in self.metrics["pr_results"]],
            "pr_substitute_counts": [int(pr["overall_assessment"]["could_substitute"]) for pr in self.metrics["pr_results"]]
        }
    
    def print_results(self):
        """Print analysis results"""
        agg_metrics = self.compute_aggregate_metrics()
        
        print("\n" + "=" * 80)
        print(f"ANALYSIS RESULTS FOR {agg_metrics['total_prs_analyzed']} PULL REQUESTS")
        print("=" * 80)
        
        print(f"\nComment Counts:")
        print(f"  Human Reviewer Comments: {agg_metrics['total_human_comments']}")
        print(f"  AI Reviewer Comments: {agg_metrics['total_ai_comments']}")
        
        print(f"\nPR by PR Quality Assessments:")
        for i, pr in enumerate(self.metrics["pr_results"]):
            print(f"\n  PR #{pr['pr_number']}: {pr['title']}")
            print(f"    AI Review Quality Score: {pr['overall_assessment']['quality_score']}/10")
            print(f"    Could Substitute for Human Review: {pr['overall_assessment']['could_substitute']}")
            print(f"    File Overlap Score: {pr['file_overlap_score']:.2f}")
        
        print(f"\nAggregate Metrics:")
        print(f"  Average File Overlap Score: {agg_metrics['avg_file_overlap_score']:.2f}")
        print(f"  Average Sentiment Agreement: {agg_metrics['avg_sentiment_agreement']:.2f}")
        print(f"  Average Content Overlap: {agg_metrics['avg_content_overlap']:.2f}")
        print(f"  Human Comments Without AI Match: {agg_metrics['human_comments_without_ai_match']}")
        print(f"  AI Comments Without Human Match: {agg_metrics['ai_comments_without_human_match']}")
        
        # Calculate averages for quality scores and substitution percentages
        avg_quality = sum(agg_metrics['pr_quality_scores']) / len(agg_metrics['pr_quality_scores']) if agg_metrics['pr_quality_scores'] else 0
        substitute_percentage = 100 * sum(agg_metrics['pr_substitute_counts']) / len(agg_metrics['pr_substitute_counts']) if agg_metrics['pr_substitute_counts'] else 0
        
        print(f"\nOverall AI Review Assessment:")
        print(f"  Average Quality Score: {avg_quality:.1f}/10")
        print(f"  Could Substitute for Human Review: {substitute_percentage:.1f}% of PRs")
        
        # Save detailed results to file
        results_dir = "results"
        os.makedirs(results_dir, exist_ok=True)
        results_file = os.path.join(results_dir, 'pr_review_analysis_results.json')
        
        with open(results_file, 'w') as f:
            json.dump(self.metrics, f, indent=2)
        
        print(f"\nDetailed results saved to {results_file}")
    
def main():
    parser = argparse.ArgumentParser(description='Analyze GitHub PR reviews')
    parser.add_argument('--folder', '-f', default='exported_prs', help='Folder containing JSON files with PR data')
    parser.add_argument('--api-key', '-k', help='OpenAI API key (overrides .env file)')
    parser.add_argument('--env-file', '-e', default='.env', help='Path to .env file containing OPENAI_API_KEY')
    
    args = parser.parse_args()
    
    # Load API key from .env file if not provided via command line
    if not args.api_key:
        load_dotenv(args.env_file)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Error: OpenAI API key not found. Please provide it with --api-key or set OPENAI_API_KEY in .env file")
            sys.exit(1)
    else:
        api_key = args.api_key
    
    # Find all JSON files in the specified folder
    json_files = glob.glob(os.path.join(args.folder, "*.json"))
    
    if not json_files:
        print(f"No JSON files found in folder: {args.folder}")
        sys.exit(1)
    
    print(f"Found {len(json_files)} JSON files in {args.folder}")
    analyzer = PRReviewAnalyzer(api_key)
    
    for json_file in json_files:
        print(f"Processing {os.path.basename(json_file)}...")
        pr_data = analyzer.load_pr_data(json_file)
        if pr_data:
            analyzer.analyze_pr(pr_data)
    
    analyzer.print_results()

if __name__ == "__main__":
    main()