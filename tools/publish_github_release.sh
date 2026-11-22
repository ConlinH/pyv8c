#!/usr/bin/env bash
set -euo pipefail

tag="${RELEASE_TAG:?}"
ref="refs/tags/$tag"
git check-ref-format "$ref"
[[ "$(git rev-parse HEAD)" == "${GITHUB_SHA:?}" ]]

existing_tag="$(git ls-remote --refs origin "$ref")"
if [[ -n "$existing_tag" ]]; then
    git fetch --no-tags origin "$ref"
    actual="$(git rev-parse 'FETCH_HEAD^{commit}')"
    if [[ "$actual" != "$GITHUB_SHA" ]]; then
        echo "$tag points to $actual instead of $GITHUB_SHA" >&2
        exit 1
    fi
fi
if [[ "${1:-}" == "--check-tag" ]]; then
    exit 0
fi

shopt -s nullglob
wheels=(dist/*.whl)
if [[ "${#wheels[@]}" != 3 ]]; then
    echo "Expected exactly three verified wheels" >&2
    exit 1
fi

if [[ -z "$existing_tag" ]]; then
    git -c user.name='github-actions[bot]' \
        -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
        tag -a "$tag" "$GITHUB_SHA" -m "pyv8c $tag"
    git push origin "$ref"
fi

read_release() {
    gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/releases" |
        jq -c --arg tag "$tag" '
          [.[][] | select(.tag_name == $tag)] |
          if length > 1 then error("Multiple releases for tag") else .[0] end
        '
}

release="$(read_release)"
if [[ "$release" == null ]]; then
    gh release create "$tag" --repo "$GITHUB_REPOSITORY" --verify-tag --draft \
        --title "pyv8c $tag" --generate-notes \
        --notes "Wheels verified by [Build wheels](${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${BUILD_RUN_ID})."
    release="$(read_release)"
fi
release_id="$(jq -er '.id' <<<"$release")"
draft="$(jq -r '.draft' <<<"$release")"

check_assets() {
    local release_json="$1" wheel name digest
    missing=()
    for wheel in "${wheels[@]}"; do
        name="${wheel##*/}"
        digest="$(sha256sum "$wheel")"
        jq -e --arg name "$name" --arg digest "sha256:${digest%% *}" '
          [.assets[] | select(.name == $name)] |
          if length == 0 then false
          elif length == 1 and .[0].state == "uploaded" and .[0].digest == $digest then true
          else error("Existing asset does not match the verified wheel: " + $name)
          end
        ' <<<"$release_json" >/dev/null || {
            status=$?
            if [[ "$status" != 1 ]]; then return "$status"; fi
            missing+=("$wheel")
        }
    done
    jq -e --argjson expected "$((${#wheels[@]} - ${#missing[@]}))" '
      [.assets[] | select(.name | endswith(".whl"))] | length == $expected
    ' <<<"$release_json" >/dev/null
}

check_assets "$release"
if [[ "$draft" == false ]]; then
    if [[ "${#missing[@]}" != 0 ]]; then
        echo "$tag is already public and is missing verified wheels" >&2
        exit 1
    fi
    echo "$tag is already published with the verified wheels"
    exit 0
fi

for wheel in "${missing[@]}"; do
    gh api --method POST --header 'Content-Type: application/octet-stream' --input "$wheel" \
        "https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets?name=${wheel##*/}" \
        --jq '.name'
done

check_assets "$(read_release)"
if [[ "${#missing[@]}" != 0 ]]; then
    echo "The draft is still missing verified wheels" >&2
    exit 1
fi
gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$release_id" -F draft=false --jq '.html_url'
