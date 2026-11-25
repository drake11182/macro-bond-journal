#!/usr/bin/env bash
# 변경분 커밋 후 push (사용자가 동시에 push해도 rebase로 합침)
set -e
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then echo "변경 없음"; exit 0; fi
git commit -m "$1"
for i in 1 2 3; do git pull --rebase && git push && exit 0; sleep 5; done
exit 1
