#!/usr/bin/env bash
# Git askpass helper for HTTPS-to-GitLab auth used by code_reviewer.py.
#
# Git invokes this for each credentials prompt (e.g. "Username for 'https://...':"
# and "Password for 'https://...':"). It echoes the username / token from the
# environment. The token is never placed on the command line or in the repo URL,
# so it does not appear in `ps`, `git remote -v`, config, or logs.
#
# Expected env: CR_GITLAB_USER, CR_GITLAB_TOKEN (set by the parent git_cmd()).
case "$1" in
  *[Uu]sername*) echo "${CR_GITLAB_USER:-gitlab-ci-token}" ;;
  *[Pp]assword*) echo "${CR_GITLAB_TOKEN:-}" ;;
esac
exit 0
