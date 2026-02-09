#!/usr/bin/env python3
"""
GitHub Actions Temporal Report Log Analyzer

Extracts logs from finished GitHub workflows and searches for Temporal report success messages.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Tuple
import requests

from github import Github
from github.GithubException import GithubException
from dotenv import load_dotenv


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract workflow logs and search for Temporal report messages"
    )
    parser.add_argument(
        "days",
        type=int,
        help="Number of days to analyze (excluding current day), or 0 for today only",
    )
    return parser.parse_args()


def load_environment() -> Tuple[str, str]:
    """Load GitHub token and repository from environment variables."""
    load_dotenv()

    github_token = os.getenv("GITHUB_TOKEN")
    repository_path = os.getenv("GITHUB_REPOSITORY")

    if not github_token:
        raise ValueError("GITHUB_TOKEN not found in environment variables")
    if not repository_path:
        raise ValueError("GITHUB_REPOSITORY not found in environment variables")

    return github_token, repository_path


def get_date_range(days: int) -> Tuple[datetime, datetime]:
    """Calculate the date range for analysis (previous N days in UTC, or today if days=0)."""
    now = datetime.now(timezone.utc)
    if days == 0:
        # For today: from start of today to current time
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now
    else:
        # For previous N days: from N days ago to start of today
        end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = end_date - timedelta(days=days)
    return start_date, end_date


def extract_temporal_reports(repo_path: str, run_id: int, job_id: int) -> dict:
    """Extract logs and search for Temporal report success and failure messages with timestamps.

    Returns dict with 'bytes_downloaded', 'found_reports', 'found_failures', and 'attempt_stats' keys.
    """
    try:
        # Get the GitHub token from environment
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print("Cannot download logs: GITHUB_TOKEN not found")
            return {"bytes_downloaded": 0, "found_reports": []}

        # Use the GitHub API to get job logs
        headers = {"Authorization": f"token {github_token}"}
        logs_url = (
            f"https://api.github.com/repos/{repo_path}/actions/jobs/{job_id}/logs"
        )

        response = requests.get(logs_url, headers=headers, stream=True)
        bytes_downloaded = 0
        found_reports = []
        found_failures = []
        attempt_stats = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 'failures': 0}

        if response.status_code == 200:
            # GitHub Actions log format: YYYY-MM-DDTHH:MM:SS.fffffffZ <message>
            timestamp_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(.*)$')
            
            for line in response.iter_lines(decode_unicode=True):
                if line is None:
                    continue

                # Track bytes (approximate - line length + newline)
                bytes_downloaded += len(line.encode('utf-8')) + 1

                # Look for success messages (excluding echo lines)
                if "Successfully reported to Temporal on attempt" in line and "echo" not in line:
                    # Try to extract timestamp from the line
                    match = timestamp_pattern.match(line)
                    if match:
                        timestamp = match.group(1)
                        message = match.group(2).strip()
                        # Remove leading non-ASCII characters and symbols
                        message = re.sub(r'^[^\x20-\x7E]+\s*', '', message)
                        
                        # Extract attempt number
                        attempt_match = re.search(r'attempt (\d+)', message)
                        if attempt_match:
                            attempt_num = int(attempt_match.group(1))
                            if attempt_num in attempt_stats:
                                attempt_stats[attempt_num] += 1
                        
                        found_reports.append({
                            "timestamp": timestamp,
                            "message": message
                        })
                    else:
                        # If no timestamp pattern found, just store the line
                        found_reports.append({
                            "timestamp": None,
                            "message": line.strip()
                        })
                
                # Look for failure messages (excluding echo lines)
                elif "Failed to report to Temporal after" in line and "echo" not in line:
                    attempt_stats['failures'] += 1
                    match = timestamp_pattern.match(line)
                    if match:
                        timestamp = match.group(1)
                        message = match.group(2).strip()
                        # Remove leading non-ASCII characters and symbols
                        message = re.sub(r'^[^\x20-\x7E]+\s*', '', message)
                        found_failures.append({
                            "timestamp": timestamp,
                            "message": message
                        })
                    else:
                        found_failures.append({
                            "timestamp": None,
                            "message": line.strip()
                        })

            return {
                "bytes_downloaded": bytes_downloaded,
                "found_reports": found_reports,
                "found_failures": found_failures,
                "attempt_stats": attempt_stats
            }
        else:
            print(
                f"Failed to download logs for run {run_id}: HTTP {response.status_code}"
            )
            return {
                "bytes_downloaded": 0,
                "found_reports": [],
                "found_failures": [],
                "attempt_stats": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 'failures': 0}
            }

    except Exception as e:
        print(f"Error processing logs for run {run_id}: {e}")
        return {
            "bytes_downloaded": 0,
            "found_reports": [],
            "found_failures": [],
            "attempt_stats": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 'failures': 0}
        }


def analyze_workflow_runs(github_client: Github, repo_path: str, days: int) -> dict:
    """Find workflow runs and extract Temporal report messages.

    Returns dict with bandwidth statistics.
    """
    try:
        repo = github_client.get_repo(repo_path)
        start_date, end_date = get_date_range(days)

        print(
            f"Analyzing workflow runs from {start_date.isoformat()} to {end_date.isoformat()}"
        )

        # Get workflow runs - filter by name starting with "Building on"
        all_workflow_runs = repo.get_workflow_runs(
            status="completed",
            created=f"{start_date.isoformat()}..{end_date.isoformat()}",
        )

        workflow_runs = [
            run
            for run in all_workflow_runs
            if run.name and run.name.startswith("Building on")
        ]

        total_runs = len(workflow_runs)
        print(f"{total_runs} workflow runs found.")
        print(
            "\nSearching for 'Successfully reported to Temporal' and 'Failed to report to Temporal' messages...\n"
        )

        processed_count = 0
        total_bytes = 0
        total_reports_found = 0
        total_failures_found = 0
        overall_stats = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 'failures': 0}

        for run_index, run in enumerate(workflow_runs, 1):
            print(f"\rProcessing run {run_index}/{total_runs}...", end="", flush=True)

            if run.status != "completed":
                continue

            jobs = run.jobs()

            # Process only first job per run
            try:
                job = jobs[0]
            except IndexError:
                continue

            if job.completed_at and start_date <= job.completed_at <= end_date and job.conclusion != "cancelled":
                processed_count += 1
                result = extract_temporal_reports(repo_path, run.id, job.id)
                total_bytes += result["bytes_downloaded"]
                
                # Accumulate statistics
                for attempt_num in [1, 2, 3, 4, 5]:
                    overall_stats[attempt_num] += result["attempt_stats"][attempt_num]
                overall_stats['failures'] += result["attempt_stats"]['failures']
                
                if result["found_reports"] or result["found_failures"]:
                    print(f"\n\nRun ID {run.id}, Job ID {job.id}:")
                    
                    if result["found_reports"]:
                        total_reports_found += len(result["found_reports"])
                        for report in result["found_reports"]:
                            if report["timestamp"]:
                                print(f"  [{report['timestamp']}] {report['message']}")
                            else:
                                print(f"  {report['message']}")
                    
                    if result["found_failures"]:
                        total_failures_found += len(result["found_failures"])
                        for failure in result["found_failures"]:
                            if failure["timestamp"]:
                                print(f"  ❌ FAILURE: [{failure['timestamp']}] {failure['message']}")
                            else:
                                print(f"  ❌ FAILURE: {failure['message']}")

        print(f"\n\nProcessed {processed_count} jobs.")
        print(f"\nTemporal Report Statistics:")
        print(f"{'='*50}")
        print(f"  Succeeded on attempt 1: {overall_stats[1]}")
        print(f"  Succeeded on attempt 2: {overall_stats[2]}")
        print(f"  Succeeded on attempt 3: {overall_stats[3]}")
        print(f"  Succeeded on attempt 4: {overall_stats[4]}")
        print(f"  Succeeded on attempt 5: {overall_stats[5]}")
        print(f"  Failed after all attempts: {overall_stats['failures']}")
        print(f"{'='*50}")
        total_all = sum(overall_stats[i] for i in [1, 2, 3, 4, 5]) + overall_stats['failures']
        print(f"  Total: {total_all}")
        print(f"\nTotal data downloaded: {total_bytes:,} bytes ({total_bytes / 1024 / 1024:.2f} MB)")

        return {"processed_count": processed_count, "total_bytes": total_bytes, "stats": overall_stats}

    except GithubException as e:
        if e.status == 401:
            raise ValueError("Authentication failed. Check your GitHub token.")
        elif e.status == 403:
            raise ValueError("API rate limit exceeded or insufficient permissions.")
        else:
            raise ValueError(f"GitHub API error: {e.data}")


def main():
    """Main function."""
    try:
        args = parse_arguments()
        github_token, repo_path = load_environment()

        github_client = Github(github_token)
        analyze_workflow_runs(github_client, repo_path, args.days)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
