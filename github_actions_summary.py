#!/usr/bin/env python3
"""
GitHub Actions Workflow Analysis Script

Analyzes GitHub Actions workflow runs for a specified number of days
and generates step execution statistics.

Only a little of human brain and human eyes were involved in the creation of this script.
It is a product of LLM (Claude Code) with a meatbag giving instructions and checking script results.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple
import yaml

from github import Github
from github.GithubException import GithubException
from dotenv import load_dotenv


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Analyze GitHub Actions workflow runs for N days"
    )
    parser.add_argument(
        "days", type=int, help="Number of days to analyze (excluding current day)"
    )
    parser.add_argument(
        "--noprogress", action="store_true", help="Disable progress indicator"
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
    """Calculate the date range for analysis (previous N days in UTC)."""
    now = datetime.now(timezone.utc)
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=days)
    return start_date, end_date


def load_step_order_from_file() -> Dict[str, int]:
    """Load step order from local list_of_steps.yaml file."""
    try:
        with open("list_of_steps.yaml", "r") as file:
            steps_list = yaml.safe_load(file)

        # Create order mapping
        step_order = {}
        for i, step_name in enumerate(steps_list):
            step_order[step_name] = i

        print(f"Loaded {len(step_order)} steps from list_of_steps.yaml")
        return step_order

    except Exception as e:
        print(f"Warning: Could not load list_of_steps.yaml: {e}")
        return {}


def analyze_workflow_runs(github_client: Github, repo_path: str, days: int, show_progress: bool = True) -> Dict:
    """Analyze GitHub Actions workflow runs for the specified time period."""
    try:
        repo = github_client.get_repo(repo_path)
        start_date, end_date = get_date_range(days)

        print(
            f"Analyzing workflow runs from {start_date.isoformat()} to {end_date.isoformat()}"
        )

        # Get workflow runs with pagination limit - filter by name starting with "Building on"
        all_workflow_runs = repo.get_workflow_runs(
            status="completed",
            created=f"{start_date.isoformat()}..{end_date.isoformat()}",
        )

        # Filter for workflows starting with "Building on"
        workflow_runs = [
            run
            for run in all_workflow_runs
            if run.name and run.name.startswith("Building on")
        ]

        # Get step order from local YAML file
        print("Reading step order from list_of_steps.yaml...")
        workflow_step_order = load_step_order_from_file()

        step_stats = {}

        # Initialize step_stats with all steps from the YAML file
        for step_name in workflow_step_order:
            step_stats[step_name] = {"success": 0, "failure": 0, "total": 0}
        processed_jobs = 0

        total_runs = len(workflow_runs)
        print(f"{total_runs} workflow runs found.")
        
        for run_index, run in enumerate(workflow_runs, 1):
            if show_progress:
                print(f"\rAnalyzing run {run_index}/{total_runs}...", end="", flush=True)
            if run.status != "completed":
                continue

            jobs = run.jobs()

            # Process only first job per run
            try:
                job = jobs[0]
            except IndexError:
                continue
            if job.completed_at and start_date <= job.completed_at <= end_date:
                processed_jobs += 1

                # Analyze job steps
                for step in job.steps:
                    # Only include steps that are in our list of interest
                    if step.name not in step_stats:
                        continue
                    if step.conclusion is None or step.conclusion == "skipped":
                        continue
                    step_stats[step.name]["total"] += 1
                    if step.conclusion == "success":
                        step_stats[step.name]["success"] += 1
                    elif step.conclusion == "failure":
                        step_stats[step.name]["failure"] += 1
        
        if show_progress:
            print()  # New line after progress indicator
        return {
            "step_stats": step_stats,
            "workflow_step_order": workflow_step_order,
            "processed_jobs": processed_jobs,
            "date_range": (start_date, end_date),
        }

    except GithubException as e:
        if e.status == 401:
            raise ValueError("Authentication failed. Check your GitHub token.")
        elif e.status == 403:
            raise ValueError("API rate limit exceeded or insufficient permissions.")
        else:
            raise ValueError(f"GitHub API error: {e.data}")


def print_summary(analysis_result: Dict):
    """Print the summary of step execution statistics."""
    step_stats = analysis_result["step_stats"]
    workflow_step_order = analysis_result["workflow_step_order"]
    processed_jobs = analysis_result["processed_jobs"]
    start_date, end_date = analysis_result["date_range"]

    print("\n" + "=" * 60)
    print("GITHUB ACTIONS WORKFLOW ANALYSIS SUMMARY")
    print("=" * 60)
    print(
        f"Analysis Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
    )
    print(f"Processed Jobs: {processed_jobs}")
    print()

    if not step_stats:
        print("No steps found in the specified time period.")
        return

    print("STEP EXECUTION STATISTICS:")
    print("-" * 60)
    print(
        f"{'Step Name':<40} {'Total':<8} {'Success':<8} {'Failure':<8} {'Success %':<10}"
    )
    print("-" * 60)

    # Sort steps by the order defined in list_of_steps.yaml
    def get_sort_key(step_item):
        step_name = step_item[0]
        return workflow_step_order.get(step_name, 999999)  # Use order from YAML file

    sorted_steps = sorted(step_stats.items(), key=get_sort_key)

    for step_name, stats in sorted_steps:
        total = stats["total"]
        success = stats["success"]
        failure = stats["failure"]
        success_rate = (success / total * 100) if total > 0 else 0

        print(
            f"{step_name[:39]:<40} {total:<8} {success:<8} {failure:<8} {success_rate:<10.1f}"
        )


def main():
    """Main function."""
    try:
        args = parse_arguments()
        github_token, repo_path = load_environment()

        github_client = Github(github_token)
        analysis_result = analyze_workflow_runs(github_client, repo_path, args.days, show_progress=not args.noprogress)
        print_summary(analysis_result)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
