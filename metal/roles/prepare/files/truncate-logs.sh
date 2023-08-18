#!/bin/bash
set -eo pipefail

LINES_TO_KEEP=1000
LOGS_DIR=/var/log/

cd $LOGS_DIR
find . -type f -print | grep -E -v "(\.gz|\.xz|\.[0-9])" | while IFS= read -r file
do
    echo "Truncating ${LOGS_DIR}${file}"
    # shellcheck disable=SC2005,SC2086,SC2086
	echo "$(tail -n ${LINES_TO_KEEP} ${file})" > ${file}
done
