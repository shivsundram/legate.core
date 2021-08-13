#!/bin/bash

set -x

main() {
  REMOTES="$@";
  if [ -z "$REMOTES" ]; then
    REMOTES=$(git remote);
  fi
  REMOTES=$(echo "$REMOTES" | xargs -n1 echo)
  CLB=$(git rev-parse --abbrev-ref HEAD);
  echo "$REMOTES" | while read REMOTE; do
    git remote update $REMOTE
    git branch -r \
    | git branch -r | awk 'BEGIN { FS = "/" };/'"$REMOTE"'/{print $2}'  \
    | while read BRANCH; do
      ARB="refs/remotes/$REMOTE/$BRANCH";
      ALB="refs/heads/$BRANCH";
      NBEHIND=$(( $(git rev-list --count $ALB..$ARB 2>/dev/null || echo "-1") ));
      NAHEAD=$(( $(git rev-list --count $ARB..$ALB 2>/dev/null) ));
      if [ "$NBEHIND" -gt 0 ]; then
        if [ "$NAHEAD" -gt 0 ]; then
          echo " branch $BRANCH is $NBEHIND commit(s) behind and $NAHEAD commit(s) ahead of $REMOTE/$BRANCH. Attempting a merge.";
          git merge --no-edit $REMOTE/$BRANCH
        else
          echo " branch $BRANCH was $NBEHIND commit(s) behind of $REMOTE/$BRANCH. resetting local branch to remote";
          git branch -f $BRANCH -t $ARB >/dev/null;
        fi
      elif [ "$NBEHIND" -eq -1 ]; then
          echo " branch $BRANCH does not exist yet. Creating a new branch to track remote";
          git branch -f $BRANCH -t $ARB >/dev/null;
      fi
    done
  done
}

main $@
