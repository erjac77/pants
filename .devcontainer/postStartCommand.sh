#!/usr/bin/env bash

set -ex

# Export resolves used by VS Code extensions.
resolves=(flake8 mypy)
for r in ${resolves[@]}; do
    sha256sum_file=dist/sha256sum-$r.txt
    if [[ ! $(sha256sum -c $sha256sum_file) ]]; then
        ./pants export "--resolve=$r"
        sha256sum 3rdparty/python/$r.lock >> $sha256sum_file
    fi
done
