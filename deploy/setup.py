#!/usr/bin/env python3
"""
Deploy helper — validate config and generate .env from template.
"""
import argparse
import os
import re
import sys


def cmd_init():
    """Create .env from .env.example."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bot_dir = os.path.join(script_dir, "..", "feishu-bot")
    example_path = os.path.join(bot_dir, ".env.example")
    env_path = os.path.join(bot_dir, ".env")

    if os.path.exists(env_path):
        print(f"[!] .env already exists at {env_path}")
        overwrite = input("    Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("    Skipped.")
            return

    with open(example_path) as f:
        content = f.read()

    with open(env_path, "w") as f:
        f.write(content)

    print(f"[+] Created {env_path}")
    print("[*] Edit the file and fill in your credentials.")
    print("    Then run:  sudo systemctl enable deploy/feishu-bot.service")


def cmd_validate():
    """Validate current config.yaml is complete."""
    import yaml
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(script_dir, "..")
    config_path = os.path.join(repo_root, "config.yaml")

    if not os.path.exists(config_path):
        print("[!] config.yaml not found")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    errors = []
    for proj_id, proj_cfg in config.get("projects", {}).items():
        for key in ("jira_project_key", "engine_repo", "game_repo", "default_branch"):
            if key not in proj_cfg:
                errors.append(f"  {proj_id}: missing '{key}'")
        if not proj_cfg.get("engine_repo", "").endswith(".git"):
            errors.append(f"  {proj_id}: engine_repo should end with .git")
        if not proj_cfg.get("game_repo", "").endswith(".git"):
            errors.append(f"  {proj_id}: game_repo should end with .git")

    if errors:
        print("[!] Config errors:")
        for e in errors:
            print(e)
        sys.exit(1)

    print(f"[+] Config valid: {list(config['projects'].keys())}")


def cmd_check():
    """Check environment readiness."""
    checks = []

    # Python deps
    try:
        import flask  # noqa
        checks.append(("Flask", True))
    except ImportError:
        checks.append(("Flask", False))

    try:
        import yaml  # noqa
        checks.append(("PyYAML", True))
    except ImportError:
        checks.append(("PyYAML", False))

    try:
        import anthropic  # noqa
        checks.append(("Anthropic SDK", True))
    except ImportError:
        checks.append(("Anthropic SDK", False))

    # Config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.join(script_dir, "..")
    checks.append(("config.yaml", os.path.exists(os.path.join(repo_root, "config.yaml"))))
    checks.append(("Jenkinsfile", os.path.exists(os.path.join(repo_root, "jenkins", "Jenkinsfile"))))
    checks.append((".env", os.path.exists(os.path.join(repo_root, "feishu-bot", ".env"))))

    # Jenkins
    checks.append(("JENKINS_URL set", bool(os.environ.get("JENKINS_URL"))))
    checks.append(("ANTHROPIC_AUTH_TOKEN set", bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))))

    print("=== Environment Check ===")
    all_ok = True
    for name, ok in checks:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n✅ All checks passed!")
    else:
        print("\n⚠️  Some checks failed — see above.")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Code Review Bot — Deploy & Config Helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create .env from .env.example")
    sub.add_parser("validate", help="Validate config.yaml structure")
    sub.add_parser("check", help="Check environment readiness")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "validate":
        cmd_validate()
    elif args.command == "check":
        ok = cmd_check()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
