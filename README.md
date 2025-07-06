# GitHub Actions Summary

Analyze GitHub Actions workflow runs for a specified number of days and generate step execution statistics.

## Features

- Analyzes GitHub Actions workflow runs for the last N days (excluding current day, using UTC timezone)
- Filters workflows by name pattern ("Building on" prefix)
- Generates step execution success/failure statistics
- Supports custom step ordering via YAML configuration
- Provides detailed success rate reporting

## Installation

This project uses `uv` for package management. Install dependencies:

```bash
uv sync
```

## Configuration

1. Create a `.env` file with your GitHub credentials:
   ```
   GITHUB_TOKEN=your_github_token_here
   GITHUB_REPOSITORY=owner/repo-name
   ```

2. Create a `list_of_steps.yaml` file to define which steps to analyze and their order:
   ```yaml
   - "Step Name 1"
   - "Step Name 2"
   - "Another Step"
   ```

## Usage

Run the analysis for the last N days:

```bash
uv run github_actions_summary.py <days>
```

Example:
```bash
uv run github_actions_summary.py 7
```

This will analyze GitHub Actions workflow runs from the last 7 days (excluding today, using UTC timezone) and display:
- Total number of processed runs and jobs
- Success/failure statistics for each step
- Success rate percentages
- Results sorted by the order defined in `list_of_steps.yaml`

## Output

The script generates a summary report showing:
- Analysis period and scope
- Step execution statistics table
- Success rates for each monitored step

## Requirements

- Python 3.8+
- GitHub Personal Access Token with repository access
- Repository with GitHub Actions workflows
