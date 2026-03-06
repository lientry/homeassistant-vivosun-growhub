# Public Release Checklist

## Target

Public HACS-ready release under the new maintainer account:

- GitHub profile: https://github.com/lientry
- Support link: https://buymeacoffee.com/lientry

## Repository setup

1. Create the new repository under `lientry`
2. Copy the integration source, tests, and public docs
3. Set repository description, topics, and issue tracking
4. Enable GitHub issues and Actions

## Metadata updates

1. Update [manifest.json](../custom_components/vivosun_growhub/manifest.json)
   - `documentation`
   - `issue_tracker`
   - `codeowners` if needed
2. Verify root [hacs.json](../hacs.json)
3. Update README links to the public repository
4. Update any badges that still reference the old repository

## Validation

1. Pass the normal CI workflow
2. Pass the HACS validation workflow
3. Pass the hassfest workflow
4. Run the local test suite
5. Perform one manual Home Assistant install smoke test
6. Perform one HACS custom repository install smoke test

## HACS requirements

Current HACS documentation requires:

- integration files under `custom_components/<domain>/`
- a root `README.md`
- a root `hacs.json`
- a valid `manifest.json`
- a public repository for HACS usage

## Release process

1. Make the repository public
2. Create an initial GitHub release, not just a tag
3. Add the repository to HACS as a custom repository and test install from that release
4. After battle testing, decide whether to keep it as custom-repo-only or submit it to the default HACS list

## Branding

Use neutral README branding.
Vendor brand assets can stay inside the integration for identification, but the project should not read like an official vendor release.

## References

- HACS publish docs: https://www.hacs.xyz/docs/publish/integration/
- Home Assistant integration file structure: https://developers.home-assistant.io/docs/creating_integration_file_structure/
- Home Assistant custom integration brands: https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/
