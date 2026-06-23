    # Always convert SSH→HTTPS and use token auth for GitLab
    if repo_url.startswith("git@"):
        https_url = ssh_to_https(repo_url)
        print(f"[git] Converting to HTTPS: {https_url}", flush=True)
        gitlab_user = os.environ.get("GITLAB_USER", "gitlab-ci-token")
        gitlab_token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN", "")
        if gitlab_token:
            https_url = https_url.replace("https://", f"https://{gitlab_user}:{gitlab_token}@")
            print(f"[git] Using token auth for GitLab", flush=True)
        else:
            print(f"[git] WARNING: No GITLAB_TOKEN set, clone may fail", flush=True)
            https_url = https_url.replace("https://", "https://git:@")
        # Set SSH command to avoid git trying SSH if HTTPS fails
        os.environ.setdefault("GIT_SSH_COMMAND", "/bin/false")
        repo_url = https_url