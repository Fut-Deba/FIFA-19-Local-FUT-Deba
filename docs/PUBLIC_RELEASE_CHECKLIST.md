# Public release gate

Keep the repository private until every required item below has passed.

## Legal and provenance

- [ ] Add an OSI-approved `LICENSE` covering only project-authored source and
  documentation.
- [ ] Confirm that every published source file is project-authored or has a
  compatible third-party licence and attribution.
- [ ] Obtain qualified legal review before publishing a playable binary or
  redistributing any third-party data or assets.
- [ ] Keep the legitimate-installation, localhost-only and Electronic Arts
  non-affiliation statements prominent.

## Clean public history

- [ ] Preserve a private backup of the current repository and tags.
- [ ] Publish a sanitized squash/orphan history; do not expose private
  development logs, captures, machine paths or removed research files.
- [ ] Remove obsolete public-facing tags and releases before changing
  visibility.
- [ ] Scan the final tree and complete public history for credentials, tokens,
  e-mail addresses, personal paths and diagnostic payloads.

## Distribution boundary

- [ ] Replace or remove the current private playable ZIP before the repository
  becomes public. Changing repository visibility also exposes release assets.
- [ ] Public downloads must exclude FIFA executables and DLLs, extracted
  databases, CAS/TOC/SB/BIG/DDS files, mirrored CDN content, card art, logs,
  saves, certificates, keys and caches.
- [ ] If runtime game data is required, reconstruct it on the user's PC only
  from a legitimately obtained supported installation.
- [ ] Keep package integrity and game-build checks automatic; users should not
  need to compare checksums manually.
- [ ] Do not include DRM bypasses, activation material or instructions for
  unauthorized copies.

## Release validation

- [ ] Pass the GitHub source checks and the complete local automated suite.
- [ ] Validate install, launch, FUT entry, STOP and crash recovery on clean
  supported Windows 10 and Windows 11 systems with Defender enabled.
- [ ] Validate EA App build `19.0.4052077.0` independently from the limited v1
  build `19.0.3865658.0`; reject every mixed or unknown EXE/DLL pair.
- [ ] Verify that START and STOP restore `hosts` byte for byte and leave no
  helper process or listener behind.
- [ ] Test Normal and RTG account isolation and backup/restore behavior.
- [ ] Review the public README, install steps, known issues and release notes
  against the final downloadable package.

## GitHub launch

- [ ] Set the repository description and topics.
- [ ] Add a security-reporting route before accepting public reports.
- [ ] Enable branch protection after the repository becomes public.
- [ ] Change visibility to public only after the source tree, history and every
  release asset have passed this checklist.
