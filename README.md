# Kapowarr tagging companion — preview stage

## GitHub and Docker Hub

The included GitHub Actions workflow runs tests, then publishes amd64 and arm64
images on main-branch pushes, version tags, or manual dispatch. Configure the
repository variable `DOCKERHUB_USERNAME` and Actions secret `DOCKERHUB_TOKEN`
with a Docker Hub token authorized to push to your `kapowarr-tagger` repository.
Enter the token directly in GitHub's secrets UI, not in source code or chat.
Main publishes `YOUR_USER/kapowarr-tagger:preview` plus a commit tag; there is no
`latest` tag while the application is preview-only. After a successful build,
replace `build:` in the Compose addition with
`image: YOUR_USER/kapowarr-tagger:preview` to avoid building on the NAS.
GitHub upload alone does not publish an image: registry credentials and a
successful workflow run are required. Never publish the `state` or `secrets` folders.

This version is deliberately read-only. It polls silasfelinus/Kapowarr every
15 minutes and writes `/state/preview.json`. It does **not** tag comics, trigger
Kavita scans, or promise that the proposed grouping has been verified in Kavita.
No web UI or exposed port. No third-party Python dependencies.

## Installation in your existing stack

1. Copy `Dockerfile` and `companion.py` into `/volume1/docker/kapowarr-tagger`.
2. Create `state` and `secrets` subdirectories there. UID 1029 / GID 100 must
   be able to write `state` and read the secret file.
3. Put your existing Kapowarr API key into `secrets/kapowarr_api_key` on the NAS.
   Do not paste it into chat or commit it. Restrict its file permissions.
4. Merge the `kapowarr-tagger` service from `compose.addition.yaml` into the
   existing stack's `services` section. Keep the existing Kapowarr service.
5. Build and start the companion using your stack manager's build/deploy feature
   or `docker compose up -d --build kapowarr-tagger` from the stack directory.
6. Inspect `state/preview.json` and container logs. A failed API request is retried
   on the next interval. The comics mount remains read-only.

The service uses the internal Docker hostname `kapowarr:5656`, so both services
must belong to the same Compose network. If Kapowarr has a URL base configured,
append it to KAPOWARR_URL. No database mount is needed.

## Identity proposal requiring review

Use `Series = title [CV-volume-ID]`, `Volume = run starting year`, and
`Number = Kapowarr issue number`. The visible CV suffix is intentional: year alone
cannot separate same-name/same-year publishing runs or some collected editions.
This changes the displayed series identity and may require adapting the CBL.
It is a proposal, not a verified Kavita convention for this installation.

Existing descriptions and credits are not included in the proposed changes.
Multiple issue mappings, grouping overrides, non-CBZ files, missing identities,
unusual XML placement, and recently changing files are held for review.
All volume mappings are fetched before filtering, to detect cross-volume clashes.
This preview rechecks the selected files each poll; incremental caching is future work.

## Before enabling any writer

Test proposed tags on copies of the affected GL and Deadpool files. Verify distinct
series, correct book counts, CBL matching, and repeat scans in Kavita. Then implement
archive backups, streaming replacement, CRC verification, race detection, persistent
change tracking, and coordinated Kavita scans. Do not merely change `:ro` to `:rw`:
this version has no write mode. The companion must run on the NAS for continuous use.

## Local tests

`python3 -m unittest -v test_companion.py`
