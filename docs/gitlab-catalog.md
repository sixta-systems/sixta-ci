# Publishing sixta-review to the GitLab CI/CD Catalog

`templates/sixta-review.yml` is written as a **GitLab CI/CD Catalog component**.
The catalog is GitLab's discoverable, versioned registry of reusable CI/CD
building blocks (it is the modern replacement for pointing people at a bare
`include: remote:` URL). This doc records what it takes to actually list it.

The template stays functionally identical to what GitLab users run today. Nothing
here changes the job it produces or breaks the existing `include: remote:` usage;
it only adds the metadata and the publish path a catalog listing needs.

## Component conventions this file already follows

- **Single-file component.** A catalog component is either
  `templates/<name>.yml` or `templates/<name>/template.yml`. Ours is the
  single-file form, `templates/sixta-review.yml`, so the component name is
  `sixta-review`.
- **`spec:inputs:` header.** The `spec:` block declares every input and its
  default (and now a component `description:` that GitLab renders on the catalog
  page). The job body sits below the `---` document separator and reads inputs
  via `$[[ inputs.* ]]`.
- **Documented include syntax.** A catalog component is consumed with
  `include: component: $CI_SERVER_FQDN/<project-path>/<component-name>@<version>`.
  The top-of-file comment and the README document both that form and the legacy
  remote-include form.

## Prerequisites to publish (the real blockers)

1. **A GitLab-hosted home for the project.** The catalog only indexes components
   that live in a project on a GitLab instance (gitlab.com or self-managed).
   `sixta-systems/sixta-ci` currently lives on GitHub, so a catalog listing needs
   a GitLab home or mirror, e.g. `gitlab.com/sixta-systems/sixta-ci`. Until that
   exists, GitLab users keep using the remote-include form, which works from the
   GitHub raw URL. **This is a decision for the maintainer, not a code change.**

   A pull mirror (GitLab pulls from the GitHub repo) keeps one source of truth on
   GitHub while giving the catalog a project to index. Note the component's own
   `curl` still fetches `sixta_review.py` from `raw.githubusercontent.com`, so the
   mirror does not have to host the script for the component to work; hosting it on
   GitLab too is a later hardening step, not a blocker.

2. **Flag the project as a CI/CD Catalog resource.** In the GitLab project:
   Settings -> General -> Visibility -> turn on **CI/CD Catalog resource** (the
   project must be public for a public listing). This marks the project as
   catalog-eligible; without it, tagged releases are not indexed.

3. **A project description and a README.** The catalog page shows the project
   description and README. The repo README already documents usage; keep a short
   "GitLab CI/CD Catalog" section pointing at the `include: component:` form.

4. **A semantic-version release, created by a pipeline.** The catalog indexes a
   component **version** only when the project publishes a release for a semver
   git tag (e.g. `1.0.0`). The release is created from a pipeline job using the
   `release` keyword (or `glab`/the Releases API) on that tag. Pushing the tag
   alone is not enough; the release object is what the catalog picks up.

   Example release job (runs on a semver tag pipeline):

   ```yaml
   create-release:
     stage: release
     image: registry.gitlab.com/gitlab-org/release-cli:latest
     rules:
       - if: '$CI_COMMIT_TAG =~ /^\d+\.\d+\.\d+$/'
     script: echo "publishing $CI_COMMIT_TAG to the CI/CD Catalog"
     release:
       tag_name: '$CI_COMMIT_TAG'
       description: 'sixta-review $CI_COMMIT_TAG'
   ```

   After the release publishes, the version appears in the catalog and is
   consumable as `@1.0.0`. `@~latest` resolves to the newest published release.

## Versioning note

The GitHub side already tags releases (`v0.3.0`, and the moving `v1` Action tag).
The catalog convention is bare **semver without the `v` prefix** in the
`@<version>` selector. Keep the two in step: a GitLab release tagged `1.0.0`
corresponds to the same tree GitHub tags `v1.0.0`. The remote-include examples
keep pinning the `v`-prefixed GitHub tag; the catalog examples use the bare
semver. Both fetch the same `sixta_review.py` at that ref.

## What is NOT one-click

Listing in the catalog improves discovery and gives versioned, documented
includes, but it is still **copy-paste YAML**: a GitLab user adds the
`include: component:` block to `.gitlab-ci.yml`. There is no "install" button that
wires the job for them. The one-click install story is the GitHub App
(`docs/distribution-plan.md` in the sixta-connect repo), a separate integration
model.
