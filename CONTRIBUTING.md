# Contributing to idx-financial-scraper

Thank you for your interest in contributing. This document describes how to
report issues, propose changes, and submit pull requests.

## Reporting Issues

Before opening an issue, please search existing issues to avoid duplicates.
When reporting a bug, include:

- A clear description of the problem and the expected behavior
- Steps to reproduce it
- Python version and operating system
- Relevant logs or error messages

For feature requests, explain the use case and how it would fit the project.

## Development Setup

1. Fork the repository and clone your fork:

   git clone https://github.com/<your-username>idx-financial-scraper.git

   cd idx-financial-scraper

3. Create a virtual environment and install dependencies:

   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

4. Create a branch for your work:

   git checkout -b feature/short-description

## Making Changes

- Keep each pull request focused on a single change.
- Follow PEP 8 and keep functions small and documented.
- Do not commit credentials, API keys, or downloaded data files.
- Update the README if your change affects usage.

## Commit Messages

Use short, descriptive messages in the imperative mood, for example:

    fix: handle missing fiscal year in report parser
    feat: add support for quarterly statements
    docs: clarify installation steps

## Submitting a Pull Request

1. Push your branch to your fork.
2. Open a pull request against the `main` branch.
3. Describe what the change does and why it is needed.
4. Link any related issue, for example "Closes #12".

A maintainer will review your pull request and may request changes before
merging.

## Code of Conduct

By participating in this project, you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).
