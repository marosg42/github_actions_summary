#!/usr/bin/env python3
"""
GitHub Actions Setup Project Dir Log Analyzer

Extracts logs from Setup Project Dir step and searches for collect-versions success messages.
"""

import argparse
import os
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
        description="Extract Setup Project Dir logs and search for collect-versions messages"
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


def extract_setup_project_logs(repo_path: str, run_id: int, job_id: int) -> None:
    """Extract logs from Setup Project Dir step and search for collect-versions success messages."""
    try:
        # Get the GitHub token from environment
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print("Cannot download logs: GITHUB_TOKEN not found")
            return

        # Use the GitHub API to get job logs
        headers = {"Authorization": f"token {github_token}"}
        logs_url = (
            f"https://api.github.com/repos/{repo_path}/actions/jobs/{job_id}/logs"
        )

        response = requests.get(logs_url, headers=headers)
        if response.status_code == 200:
            full_logs = response.text
            lines = full_logs.split("\n")

            # Find the section between the markers
            start_idx = None
            end_idx = None

            for i, line in enumerate(lines):
                if "actions/setup/setup-project" in line and start_idx is None:
                    start_idx = i
                elif (
                    "actions/reports/report-to-weebl" in line and start_idx is not None
                ):
                    end_idx = i
                    break

            if start_idx is not None and end_idx is not None:
                relevant_lines = lines[start_idx:end_idx]

                # Search for lines containing the target strings (excluding echo lines)
                success_lines = [
                    line
                    for line in relevant_lines
                    if "collect-versions succeeded on attempt" in line
                    and "echo" not in line
                ]
                failed_lines = [
                    line
                    for line in relevant_lines
                    if "collect-versions failed" in line and "echo" not in line
                ]

                # Filter success lines to only show attempts > 1
                retry_success_lines = []
                for line in success_lines:
                    # Extract attempt number (last word in the line)
                    attempt_num = int(line.split()[-1])
                    if attempt_num > 1:
                        retry_success_lines.append(line)

                # Only show runs that had retry successes
                if retry_success_lines:
                    print(f"\nRun ID {run_id}, Job ID {job_id}:")
                    for line in retry_success_lines:
                        print(f"  {line.strip()}")
                    # Also show any failed attempts for context
                    for line in failed_lines:
                        print(f"  {line.strip()}")
            else:
                print(
                    f"Run ID {run_id}, Job ID {job_id}: Could not find log section between markers"
                )
        else:
            print(
                f"Failed to download logs for run {run_id}: HTTP {response.status_code}"
            )

    except Exception as e:
        print(f"Error processing logs for run {run_id}: {e}")


def analyze_workflow_runs(github_client: Github, repo_path: str, days: int) -> None:
    """Find Setup Project Dir steps and extract collect-versions messages."""
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
            "\nSearching for collect-versions retry successes (attempt > 1) in Setup Project Dir logs..."
        )

        processed_count = 0
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
                # Look for Setup Project Dir step that was actually executed (not skipped)
                has_executed_setup_project_dir = False
                for step in job.steps:
                    if (
                        step.name == "Setup Project Dir"
                        and step.conclusion is not None
                        and step.conclusion != "skipped"
                    ):
                        has_executed_setup_project_dir = True
                        break

                # Only process if Setup Project Dir step was executed
                if has_executed_setup_project_dir:
                    processed_count += 1
                    extract_setup_project_logs(repo_path, run.id, job.id)

        print(f"\nProcessed {processed_count} Setup Project Dir steps.")

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
