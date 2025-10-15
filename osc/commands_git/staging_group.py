import os
import re
import sys
import typing
from typing import List, Tuple

import osc.commandline_git

if typing.TYPE_CHECKING:
    from osc import gitea_api


# Assuming these are defined at the top of your file
BACKLOG_LABEL = "staging_backlog"
INPROGRESS_LABEL = "staging_inprogress"


class StagingGroupCommand(osc.commandline_git.GitObsCommand):
    """
    Group together staging pull requests
    """

    name = "group"
    aliases = []
    parent = "StagingCommand"

    def init_arguments(self):
        self.add_argument_owner_repo_pull(dest="pr_list", nargs="+").completer = osc.commandline_git.complete_pr
        self.add_argument('--title', required=True, help="The new title for the staging PR.")
        self.add_argument('--branch', required=False, help="The branch to use for the staging PR.")
        self.add_argument('--workdir', required=True, help="Working directory for git operations.")
        self.add_argument('--grouped-pr', dest="grouped_pr", required=False, help="An existing grouped PR to update (e.g., owner/repo#number).")
        self.add_argument('--fork', required=False, help="The fork to use for the pull request branch (e.g., 'owner/repo').")
        self.add_argument('--force', required=False, help="Force the operation.", action='store_true')

    def _initialize_pr_processing(self, args) -> dict:
        """Handles initial PR argument parsing for new or existing grouped PRs."""
        from osc import gitea_api
        from osc.output import tty

        context = {
            "all_pkg_prs": [],
            "prj_pkg_prs": [],
            "base_branch": None,
            "base_owner": None,
            "base_repo": None,
            "existing_pr_obj": None,
        }

        if args.grouped_pr:
            if args.branch:
                print(f"{tty.colorize('ERROR', 'red,bold')}: --branch cannot be used with --grouped-pr", file=sys.stderr)
                sys.exit(1)

            try:
                owner, repo, number = gitea_api.PullRequest.split_id(args.grouped_pr)
                existing_pr = gitea_api.PullRequest.get(self.gitea_conn, owner, repo, number)
                context["existing_pr_obj"] = existing_pr
                context["all_pkg_prs"].extend(existing_pr.parse_pr_references())
                context["prj_pkg_prs"].extend(re.findall(r"^Closes: *(.*)$", existing_pr.body, re.M))
                context["base_branch"] = existing_pr.base_branch
                context["base_owner"] = existing_pr.base_owner
                context["base_repo"] = existing_pr.base_repo

                args.branch = existing_pr.head_branch
                args.title = existing_pr.title
            except (gitea_api.GiteaException, ValueError) as e:
                print(f"{tty.colorize('ERROR', 'red,bold')}: Failed to process '{args.grouped_pr}': {e}", file=sys.stderr)
                sys.exit(1)
        else:
            if not args.branch:
                print(f"{tty.colorize('ERROR', 'red,bold')}: --branch is required for a new grouped PR", file=sys.stderr)
                sys.exit(1)

        return context

    def _process_forwarded_prs(self, args, context: dict) -> List[str]:
        """Processes the list of forwarded PRs, validates them, and updates the context."""
        from osc import gitea_api
        from osc.output import tty

        failed_entries = []
        for owner, repo, pull in args.pr_list:
            try:
                pr_obj = gitea_api.PullRequest.get(self.gitea_conn, owner, repo, int(pull))

                if context["base_branch"] is None:
                    context["base_branch"] = pr_obj.base_branch
                    context["base_owner"] = owner
                    context["base_repo"] = repo
                elif context["base_branch"] != pr_obj.base_branch or context["base_owner"] != owner or context["base_repo"] != repo:
                    print(f"{tty.colorize('ERROR', 'red,bold')}: All PRs must target the same base. Mismatch found in {owner}/{repo}#{pull}", file=sys.stderr)
                    sys.exit(1)

                if BACKLOG_LABEL not in pr_obj.labels and not args.force:
                    print(f"{tty.colorize('ERROR', 'red,bold')}: PR {owner}/{repo}#{pull} is missing the '{BACKLOG_LABEL}' label.", file=sys.stderr)
                    sys.exit(1)

                pkg_prs = pr_obj.parse_pr_references()
                if not pkg_prs:
                    print(f"{tty.colorize('ERROR', 'red,bold')}: No package references found in PR {owner}/{repo}#{pull}", file=sys.stderr)
                    sys.exit(1)

                context["all_pkg_prs"].extend(pkg_prs)
                for pkg_pr_owner, pkg_pr_repo, pkg_pr_num in pkg_prs:
                    context["prj_pkg_prs"].append(f"{owner}/{repo}!{pull} ({pkg_pr_owner}/{pkg_pr_repo}!{pkg_pr_num})")

            except gitea_api.GiteaException as e:
                if e.status == 404:
                    failed_entries.append(f"{owner}/{repo}#{pull}")
                    continue
                raise
        return failed_entries

    def _prepare_workspace(self, args, context: dict) -> Tuple["gitea_api.Git", str]:
        """Clones or updates the repository and returns the Git object and clone path."""
        from osc import gitea_api
        from osc.output import tty

        if not os.path.exists(args.workdir):
            print(f"{tty.colorize('ERROR', 'red,bold')}: Working directory '{args.workdir}' does not exist.", file=sys.stderr)
            sys.exit(1)

        clone_dir = os.path.join(args.workdir, f"{context['base_owner']}_{context['base_repo']}_{context['base_branch']}")
        print(f"Using working directory: {clone_dir}")

        # Assuming a utility function exists for this
        gitea_api.Repo.clone_or_update(
            self.gitea_conn,
            context["base_owner"],
            context["base_repo"],
            branch=context["base_branch"],
            directory=clone_dir,
            remote="origin",
        )

        return gitea_api.Git(clone_dir), clone_dir

    def _apply_submodule_changes(self, git: "gitea_api.Git", clone_dir: str, all_pkg_prs: list) -> List[str]:
        """Iterates through package PRs and applies changes to the submodules."""
        from osc import gitea_api
        from osc.output import tty

        failed_entries = []
        for owner, repo, pull in all_pkg_prs:
            try:
                pr_obj = gitea_api.PullRequest.get(self.gitea_conn, owner, repo, int(pull))
                print(f"Processing package PR: {owner}/{repo}#{pull}")

                submod_path = os.path.join(clone_dir, repo)
                if os.path.exists(submod_path):
                    gitsm = gitea_api.Git(submod_path)
                    if git.submodule_status(repo).startswith('-'):
                        git.submodule_update(repo, init=True)

                    gitsm.reset()
                    pr_branch = gitsm.fetch_pull_request(pull, commit=pr_obj.head_commit, force=True)
                    gitsm.switch(pr_branch)
                else:
                    print(f"{tty.colorize('ERROR', 'red,bold')}: Submodule path '{submod_path}' does not exist.", file=sys.stderr)
                    sys.exit(1)
            except gitea_api.GiteaException as e:
                if e.status == 404:
                    failed_entries.append(f"{owner}/{repo}#{pull}")
                    continue
                raise
        return failed_entries

    def _commit_and_manage_pr(self, args, git: "gitea_api.Git", context: dict):
        """Commits changes and creates a new PR or updates an existing one."""
        from osc import gitea_api
        from osc.output import tty

        repos_to_add = [repo for _, repo, _ in context["all_pkg_prs"]]
        git.add(repos_to_add)

        if not git.has_changes():
            print("No new changes to commit.")
            return

        source_owner = context['base_owner']
        remote = "origin"

        if args.fork:
            try:
                fork_owner, fork_repo_name = args.fork.split('/')
                # Check if the fork repo really exists
                fork_repo_obj = gitea_api.Repo.get(self.gitea_conn, fork_owner, fork_repo_name)
                # Add a remote "fork" in the cloned repository
                if not git.get_remote_url("fork"):
                    git.add_remote("fork", fork_repo_obj.ssh_url)
                remote = "fork"
                source_owner = fork_owner
            except (gitea_api.GiteaException, ValueError) as e:
                print(f"{tty.colorize('ERROR', 'red,bold')}: Invalid or inaccessible fork '{args.fork}': {e}", file=sys.stderr)
                sys.exit(1)

        commit_message = ""
        for owner,repo,pull in args.pr_list:
            for prj_pkg_pr in context["prj_pkg_prs"]:
                if f"{owner}/{repo}!{pull}" in prj_pkg_pr:
                    commit_message += f"- {prj_pkg_pr}\n"
        #commit_message = '\n'.join([f"- {pkg}" for pkg in context["prj_pkg_prs"]])

        pr_references = '\n'.join([f"PR: {org}/{repo}!{num}" for org, repo, num in context["all_pkg_prs"]])
        closes_references = '\n'.join([f"Closes: {pkg}" for pkg in context["prj_pkg_prs"]])
        description = f"{pr_references}\n\n{closes_references}"

        existing_pr = context["existing_pr_obj"]
        if existing_pr:
            if git.branch_exists(args.branch):
                git.branch(args.branch, remote + "/" + args.branch)
                git.checkout(args.branch)

            else:
                git.checkout(remote + "/" + args.branch, track=True)

            git.commit(f"Updated staging PR with following PRs\n\n{commit_message}")
            git.push(remote, branch=args.branch, force=args.force)
            print(f"Updating description for PR {existing_pr.id}")
            gitea_api.PullRequest.set(
                self.gitea_conn,
                existing_pr.base_owner,
                existing_pr.base_repo,
                existing_pr.number,
                description=description
            )
        else:
            if git.branch_exists(args.branch):
                if not args.force:
                    print(f"{tty.colorize('ERROR', 'red,bold')}: Branch '{args.branch}' already exists.", file=sys.stderr)
                    sys.exit(1)

                # delete branch and its remote branch tracking
                git.delete_branch(args.branch, remote, force=True)
                git.delete_branch(args.branch, force=True)

            git.checkout(args.branch, create_new=True)
            git.commit(f"Created staging PR {args.title}\n\n{commit_message}")
            git.push(remote, branch=args.branch, force=args.force)
            print(f"Creating pull request in {context['base_owner']}/{context['base_repo']}")
            pr_obj = gitea_api.PullRequest.create(
                self.gitea_conn,
                target_owner=context['base_owner'],
                target_repo=context['base_repo'],
                target_branch=context['base_branch'],
                source_owner=source_owner,
                source_branch=args.branch,
                title=args.title,
                description=description
            )
            gitea_api.PullRequest.add_labels(self.gitea_conn, context['base_owner'], context['base_repo'], pr_obj.number, [INPROGRESS_LABEL])
            print(pr_obj.to_human_readable_string())

    def _finalize_prs(self, args, failed_entries: list):
        """Closes original PRs and reports any failures."""
        from osc import gitea_api
        from osc.output import tty

        for owner, repo, pull in args.pr_list:
            try:
                gitea_api.PullRequest.close(self.gitea_conn, owner, repo, int(pull))
            except gitea_api.GiteaException as e:
                if e.status == 404:
                    failed_entries.append(f"{owner}/{repo}#{pull}")
                    continue
                raise

        if failed_entries:
            print(f"{tty.colorize('ERROR', 'red,bold')}: Could not retrieve: {', '.join(failed_entries)}", file=sys.stderr)
            sys.exit(1)

    def run(self, args):
        self.print_gitea_settings()

        # 1. Initialize and process PR arguments
        context = self._initialize_pr_processing(args)
        failed_entries = self._process_forwarded_prs(args, context)

        # Deduplicate and sort lists
        context["all_pkg_prs"] = sorted(list(set(context["all_pkg_prs"])))
        context["prj_pkg_prs"] = sorted(list(set(context["prj_pkg_prs"])))

        # 2. Prepare the workspace
        git, clone_dir = self._prepare_workspace(args, context)

        # 3. Apply submodule changes
        failed_entries.extend(self._apply_submodule_changes(git, clone_dir, context["all_pkg_prs"]))

        # 4. Commit and create/update the PR
        self._commit_and_manage_pr(args, git, context)

        # 5. Finalize by closing original PRs and reporting errors
        self._finalize_prs(args, failed_entries)

        print("\nStaging group process completed successfully.")