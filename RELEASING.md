# Releasing

This covers the one-time PyPI trusted publisher setup and the steps for
every release after that. Only the repository owner can do this; it needs
write access to both the GitHub repository and the PyPI project.

## One-time setup

1. Create the PyPI trusted publisher, before the first release, at
   <https://pypi.org/manage/account/publishing/>. Use these exact values:
   - PyPI project name: `shapa`
   - Owner: `Roukh`
   - Repository name: `shapa-llm`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. Create the `pypi` environment on GitHub, at
   `https://github.com/Roukh/shapa-llm/settings/environments`. No secrets
   go in it; trusted publishing uses a short-lived OIDC token instead of a
   stored API token.

## Every release

1. Update `CHANGELOG.md`: move the `[Unreleased]` entries under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading.
2. Bump the version in `pyproject.toml` and in `shapa/__init__.py`
   (`__version__`). Both must match the tag in step 3, exactly.
3. Commit, push, and tag the release commit:

   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. Publish the GitHub release from that tag, at
   `https://github.com/Roukh/shapa-llm/releases/new`. Publishing the
   release triggers `.github/workflows/publish.yml`, which builds the
   sdist and wheel, checks them, and uploads them to PyPI.
5. Confirm the new version at <https://pypi.org/project/shapa/>.

A `workflow_dispatch` run of the same workflow builds and checks the
distribution without publishing it, useful for testing a workflow change
before a real release.

## If the tag and the version disagree

The workflow fails before it uploads anything: a job compares the tag
(`vX.Y.Z`) against `project.version` in `pyproject.toml` and stops the run
on a mismatch. Fix the version or re-tag, then re-run the release.
